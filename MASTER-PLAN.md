# Master Plan: Sistem Trading Multi-Agent Berbasis Grok

Rencana R&D dan deployment yang menyatukan tiga dokumen sebelumnya menjadi satu sistem:

- [Mesin Sentimen](README.md): skor -1.0 s/d +1.0 dari data X
- [Eksekusi Aman](EKSEKUSI-AMAN.md): API key, policy gateway, circuit breaker, format intent
- [Bot DCA Dinamis](DCA-DINAMIS.md): strategi, manajemen risiko, backtest, metrik

> **Status:** dokumen perencanaan. Durasi dan ambang adalah estimasi awal. Bukan nasihat keuangan.

---

## Daftar Isi

0. [Keputusan Arsitektur Utama](#0-keputusan-arsitektur-utama)
1. [Pembagian Peran Agen](#1-pembagian-peran-agen)
2. [Pipeline Data: dari Data Mentah ke Live](#2-pipeline-data-dari-data-mentah-ke-live)
3. [Tech Stack](#3-tech-stack)
4. [Rencana Uji Ketahanan](#4-rencana-uji-ketahanan)
5. [Timeline & Gate](#5-timeline--gate)
6. [Runbook Operasional](#6-runbook-operasional)

---

## 0. Keputusan Arsitektur Utama

**LLM hanya dipakai di Sentiment Agent.** Risk Manager dan Execution Agent dibangun sebagai **service deterministik** (kode biasa), bukan agen LLM.

| Kalau Risk Manager memakai LLM | Kalau Risk Manager memakai kode deterministik |
|-------------------------------|---------------------------------------------|
| Keputusan bisa berbeda untuk input yang sama | Input sama selalu menghasilkan keputusan sama |
| Bisa dibujuk lewat teks berbahaya yang lolos dari data X | Tidak bisa dibujuk, hanya menjalankan aturan |
| Sulit diuji: tidak ada jaminan aturan ditegakkan | Setiap aturan punya unit test |
| Latensi detik dan ada biaya per keputusan | Latensi milidetik, tanpa biaya |
| Kalau API Grok mati, validasi risiko ikut mati | Tetap berjalan saat API Grok mati |

Istilah "agen" di dokumen ini berarti **service otonom dengan tanggung jawab tunggal** yang berkomunikasi lewat message broker. Hanya satu agen yang memakai LLM.

**Prinsip lain:**
1. **Setiap agen bisa gagal sendiri tanpa menjatuhkan yang lain.** Sentimen mati → strategi jalan dengan RSI saja. Strategi mati → posisi tetap dilindungi stop di bursa.
2. **Bursa adalah sumber kebenaran** untuk saldo dan posisi. State lokal selalu direkonsiliasi ke bursa.
3. **Kode yang sama dari backtest sampai live.** Yang berganti hanya adapter data dan adapter eksekusi.

---

## 1. Pembagian Peran Agen

### 1.1 Peta Sistem

```
              ┌────────────────────┐
  X API ─────►│  SENTIMENT AGENT   │  (satu-satunya yang memakai Grok)
  Grok API ──►│  ingest·filter·skor│
              └─────────┬──────────┘
                        │ sentiment.signals
  Market data ─┐        ▼
               └─►┌────────────────────┐
                  │  STRATEGY ENGINE   │  DCA dinamis (deterministik)
                  │  RSI·sizing·TP     │
                  └─────────┬──────────┘
                            │ strategy.intents  (TradeIntent v1)
                            ▼
                  ┌────────────────────┐
                  │ RISK MANAGER AGENT │  policy · saldo · limit · breaker
                  └─────────┬──────────┘
                            │ risk.approved
                            ▼
                  ┌────────────────────┐
                  │  EXECUTION AGENT   │  satu-satunya pemegang API key
                  └─────────┬──────────┘
                            │ REST/WS bertanda tangan
                            ▼
                     Binance / Coinbase
                            │ fills, saldo
                            ▼
                     exec.reports ──► Risk Manager, Strategy, DB

  ┌──────────────────────┐        ┌───────────────────────────┐
  │  WATCHDOG (host lain)│──────► │  NOTIFIER                 │
  │  heartbeat · harga   │ alerts │  Telegram + kanal cadangan│
  │  independen · kill   │        └───────────────────────────┘
  └──────────────────────┘
```

Kamu menyebut tiga agen. Dua komponen tambahan, **Strategy Engine** dan **Watchdog**, sengaja dipisahkan:
- **Strategy Engine** berisi logika DCA. Kalau logika ini ditaruh di Risk Manager, satu service akan sekaligus membuat dan memvalidasi usulan order, sehingga fungsi pengecekannya hilang.
- **Watchdog** harus berjalan terpisah. Kalau ia berada di proses atau server yang sama dengan yang diawasinya, ia ikut mati saat server itu mati.

### 1.2 Kontrak Setiap Agen

| | Sentiment Agent | Strategy Engine | Risk Manager Agent | Execution Agent | Watchdog |
|---|---|---|---|---|---|
| **Tugas** | Ambil data X, filter anti-noise, skor dengan Grok, agregasi | Hitung RSI/ATR, multiplier, ukuran order, trailing TP | Validasi intent terhadap saldo, limit, harga, dan status breaker | Tanda tangan dan kirim order, pasang stop, rekonsiliasi | Pantau kesehatan semua komponen dan harga, picu breaker |
| **Pakai LLM?** | Ya (Grok) | Tidak | Tidak | Tidak | Tidak |
| **Input** | X API, Grok API | `sentiment.signals`, data pasar | `strategy.intents`, saldo dari `exec.reports` | `risk.approved` | Heartbeat semua agen, feed harga sendiri |
| **Output** | `sentiment.signals` | `strategy.intents` | `risk.approved` / `risk.rejected` | `exec.reports` | `system.breaker`, `alerts` |
| **Akses secret** | Key X API, key xAI | Tidak ada | Tidak ada | Key bursa (trade-only) | Key bursa cadangan (trade-only, untuk cancel) |
| **Kalau mati** | Strategi pakai RSI saja (`NO_SIGNAL`) | Tidak ada order baru, stop di bursa tetap aktif | Semua intent tertahan (fail-closed) | Breaker `HALT_NEW`, Watchdog ambil alih cancel | Agen lain lanjut, alert "watchdog down" dari heartbeat eksternal |
| **Dokumen detail** | [README](README.md) | [DCA-DINAMIS](DCA-DINAMIS.md) | [EKSEKUSI-AMAN §3](EKSEKUSI-AMAN.md#3-desain-eksekusi-aifi-yang-tahan-manipulasi) | [EKSEKUSI-AMAN §2, §4.2](EKSEKUSI-AMAN.md#2-standar-keamanan-api-key) | [EKSEKUSI-AMAN §4](EKSEKUSI-AMAN.md#4-circuit-breaker) |

**Fail-closed:** kalau Risk Manager tidak bisa memastikan intent aman (misalnya data saldo belum diperbarui atau database tidak bisa dibaca), intent **ditolak**, bukan diloloskan.

### 1.3 Topik Message Broker

| Topik | Produsen | Konsumen | Isi | TTL / umur maks |
|------|----------|----------|-----|-----------------|
| `sentiment.signals` | Sentiment | Strategy, DB | `{asset, ts, S, Z, n_eff, dispersi, bot_share, status}` | 2 jam (lebih tua dari itu dianggap `NO_SIGNAL`) |
| `market.bars` | Market ingestor | Strategy, Watchdog, DB | OHLCV | — |
| `strategy.intents` | Strategy | Risk Manager, DB | TradeIntent v1 | 30 detik |
| `risk.approved` | Risk Manager | Execution | Intent + `risk_check_id` | 30 detik |
| `risk.rejected` | Risk Manager | DB, Notifier | Intent + `reason_code` | — |
| `exec.reports` | Execution | Risk, Strategy, DB | Status order, fill, saldo, posisi | — |
| `system.breaker` | Watchdog, Risk | Semua | `{state, reason, ts}` | — |
| `system.heartbeat` | Semua | Watchdog | `{agent, ts, versi}` setiap 5 detik | — |
| `alerts` | Semua | Notifier | `{severity, pesan}` | — |

**Aturan pesan:**
- Setiap pesan memiliki `msg_id` unik. Konsumen menyimpan ID yang sudah diproses sehingga pesan dobel tidak dieksekusi dua kali.
- Pesan yang melewati TTL **dibuang**, bukan diproses terlambat. Intent beli dari 10 menit lalu tidak relevan lagi.
- Order tidak pernah dikirim hanya karena ada pesan di antrian. Execution menulis order ke tabel `orders` di PostgreSQL (pola *outbox*) sebelum mengirim ke bursa, sehingga setelah crash ia tahu order mana yang sudah atau belum terkirim.

---

## 2. Pipeline Data: dari Data Mentah ke Live

```
Fase 1          Fase 2              Fase 3          Fase 4           Fase 5          Fase 6
Pengumpulan ──► Simulasi lokal ──► Backtest  ──►  Paper trading ──► Live kecil ──► Skala penuh
(data mentah)   (DB + spreadsheet)  (walk-fwd)     (order simulasi)  (10% modal)    (100% modal)
     │                │                 │                │                │
   GATE 1           GATE 2            GATE 3           GATE 4           GATE 5
```

Setiap fase punya **gate**: kriteria tertulis yang harus terpenuhi sebelum lanjut. Kalau gagal, kembali ke fase sebelumnya, bukan memaksa maju.

### Fase 1 — Pengumpulan Data Mentah (minggu 1–4)

| Data | Sumber | Penyimpanan | Catatan |
|------|--------|------------|---------|
| Cuitan + metrik | X API (stream/search) | Postgres (`tweets_raw`), arsip Parquet harian | Snapshot teks saat masuk, metrik pada t+60 menit |
| Skor Grok | Grok API | Postgres (`tweet_scores`) | Simpan versi model, prompt hash, output mentah |
| Profil akun | X API | Postgres (`accounts`, `account_snapshots`) | Snapshot harian, supaya riwayat bisa dihitung point-in-time |
| OHLCV | API bursa | TimescaleDB (`bars_1m`, `bars_1d`) | Mulai unduh histori panjang sekarang |
| Proxy sentimen historis | Fear & Greed Index | Postgres | Untuk backtest panjang (lihat [DCA-DINAMIS §4](DCA-DINAMIS.md#4-alur-backtesting)) |

**Kenapa data mulai dikumpulkan sejak hari pertama:** skor Grok historis tidak bisa dibuat ulang tanpa bocoran informasi masa depan. Setiap hari pengumpulan yang tertunda berarti satu hari data validasi yang hilang permanen.

**Gate 1:** pengumpulan berjalan 14 hari tanpa celah > 1 jam, golden set 500 cuitan sudah dilabeli, Spearman skor Grok vs label manusia ≥ 0.6.

### Fase 2 — Simulasi Lokal: Database vs Spreadsheet (minggu 3–6)

**Database lokal** (PostgreSQL + DuckDB) untuk simulasi. **Spreadsheet** hanya untuk inspeksi manual.

| Pakai spreadsheet untuk | Jangan pakai spreadsheet untuk |
|------------------------|------------------------------|
| Melabeli golden set (lebih nyaman untuk manusia) | Menjalankan simulasi strategi |
| Memeriksa sampel 100 cuitan yang difilter dan alasannya | Menyimpan data utama (rawan diedit tidak sengaja, tanpa riwayat) |
| Mengecek hitungan sizing satu per satu dengan rumus manual | Data > 100 ribu baris |
| Membaca ringkasan hasil backtest | Apa pun yang harus bisa diulang persis |

Alur kerja yang disarankan: query DuckDB atas file Parquet → ekspor sampel kecil ke CSV/Google Sheets untuk ditinjau → hasil review (misalnya label) diimpor kembali ke database. **Database tetap jadi sumber utama.**

Aktivitas di fase ini:
- Menghitung ulang agregasi sentimen dengan berbagai parameter (τ, k, ambang filter) di atas data Fase 1.
- Memeriksa apakah filter anti-noise membuang hal yang benar (precision/recall pada sampel berlabel).
- Mencocokkan output kode strategi dengan perhitungan manual di spreadsheet untuk 20 kasus.

**Gate 2:** precision filter bot ≥ 0.9 pada sampel berlabel, perhitungan sizing kode = perhitungan manual untuk semua kasus uji.

### Fase 3 — Backtest (minggu 5–8)

Mengikuti [DCA-DINAMIS §4](DCA-DINAMIS.md#4-alur-backtesting): proxy sentimen untuk periode panjang, baseline dan ablasi, walk-forward, dan uji ketahanan.

**Gate 3:** kriteria lolos di DCA-DINAMIS §4 Langkah 7 terpenuhi, dan hasil sudah direview oleh orang kedua.

### Fase 4 — Paper Trading (minggu 9–16)

Seluruh sistem berjalan **sungguhan** (data live, Grok live, semua agen, broker, database, notifikasi). Satu-satunya perbedaan: Execution Agent memakai **adapter simulasi** yang mengisi order berdasarkan order book live (harga, kedalaman, fee), bukan mengirim ke bursa.

Yang diukur:
- Keputusan paper trading identik dengan replay backtest di periode yang sama (selisih = bug).
- Latensi end-to-end (cuitan masuk → intent) dan konsumsi kuota API per hari.
- Jumlah breaker terpicu dan apakah setiap kejadian memang pantas.
- Uji ketahanan §4 dijalankan di lingkungan ini.

**Gate 4:** 8 minggu tanpa insiden kritis, semua skenario uji ketahanan lolos, konsumsi kuota API dalam anggaran dengan margin ≥ 30%.

### Fase 5 — Live Kecil (minggu 17–28)

- **Testnet dulu (1–2 minggu)** untuk memastikan adapter eksekusi asli, tanda tangan, dan pemasangan stop bekerja.
- **Mainnet dengan 10% modal** di sub-account khusus, semua setting keamanan dari [EKSEKUSI-AMAN §6](EKSEKUSI-AMAN.md#6-checklist-sebelum-live) aktif.
- Approval manusia untuk setiap order boost (m > 1.5) di bulan pertama.

**Gate 5:** 3 bulan berjalan, slippage aktual ≤ asumsi backtest, tidak ada selisih rekonsiliasi yang tidak terjelaskan, review seluruh log.

### Fase 6 — Skala Penuh

Naikkan modal bertahap (25% → 50% → 100%) dengan jeda minimal 1 bulan per tahap. Batas risiko di Risk Manager dinaikkan bersama modal. Approval manusia hanya untuk order di atas ambang.

---

## 3. Tech Stack

Kriteria pemilihan: **stabil dan banyak dipakai**, mudah dioperasikan oleh tim kecil, dan tidak menambah komponen yang belum dibutuhkan.

### 3.1 Pilihan Utama

| Lapisan | Pilihan | Alasan | Alternatif saat skala membesar |
|--------|---------|--------|-------------------------------|
| Bahasa | **Python 3.12** (asyncio) | Ekosistem quant/data terlengkap, SDK xAI/OpenAI, library bursa | Rust/Go untuk Execution Agent jika latensi < 10 ms dibutuhkan (tidak untuk DCA) |
| Validasi data | **pydantic v2** | Schema strict untuk semua pesan antar-agen | — |
| API gateway | **FastAPI** + uvicorn | Async, validasi otomatis lewat pydantic | — |
| Message broker | **Redis Streams** | Consumer group, ack, replay. Redis juga sekaligus dipakai untuk nonce, rate limit, dan cache. Satu komponen untuk banyak kebutuhan. | **NATS JetStream** (ringan, persistensi lebih kuat), **Kafka** (hanya jika throughput sangat besar) |
| Database utama | **PostgreSQL 16 + TimescaleDB** | Transaksi ACID untuk order dan saldo, plus time series untuk harga dan skor dalam satu database | — |
| Riset & backtest | **DuckDB** + **Parquet** | Query analitik cepat di laptop tanpa server | — |
| Library bursa | **ccxt** untuk data pasar, **SDK resmi bursa** atau klien sendiri untuk eksekusi | ccxt memudahkan data multi-bursa. Untuk eksekusi, klien yang lebih tipis lebih mudah diaudit. | — |
| Secret | **AWS/GCP Secret Manager** atau **Vault** | Lihat [EKSEKUSI-AMAN §2.2](EKSEKUSI-AMAN.md#22-enkripsi-environment-variables--penyimpanan-secret) | — |
| Kontainer | **Docker Compose** di satu VPS dengan IP statis | Cukup untuk fase awal, mudah dipahami | Kubernetes hanya jika jumlah service dan tim membesar |
| Supervisi proses | `restart: unless-stopped` + healthcheck | Kontainer yang crash otomatis bangkit | — |
| Sinkronisasi waktu | **chrony** | Wajib karena bursa menolak request dengan timestamp bergeser | — |
| Monitoring | **Prometheus + Grafana** | Metrik: latensi, lag konsumen, kuota API, state breaker | — |
| Log | **Loki** atau log JSON terstruktur ke file + rotasi | Bisa dicari per `intent_id` | — |
| Error tracking | **Sentry** | Stack trace dan konteks error | — |
| CI | **GitHub Actions**: pytest, ruff, mypy, gitleaks | Setiap perubahan diuji sebelum deploy | — |

### 3.2 Notifikasi Darurat

Satu kanal notifikasi tidak cukup. Kalau Telegram sedang down atau HP tidak terdengar, alert kritis bisa hilang.

| Tingkat | Contoh kejadian | Kanal |
|---------|----------------|-------|
| INFO | Order terisi, laporan harian | Telegram (grup info, notifikasi senyap) |
| WARNING | Breaker `DEGRADED`, kuota API 70%, sentimen `NO_SIGNAL` | Telegram (grup alert) |
| CRITICAL | `HALT_NEW`, `KILL`, rekonsiliasi gagal, error autentikasi bursa, Watchdog down | Telegram **dan** PagerDuty/Opsgenie (panggilan telepon/SMS sampai di-acknowledge) |

**Bot Telegram:**
- Hanya menerima perintah dari **daftar user ID** yang diizinkan, dan hanya di chat pribadi.
- Perintah yang tersedia: `/status`, `/pause` (ke `HALT_NEW`), `/kill` (ke `KILL`, dengan konfirmasi kedua).
- **Tidak ada perintah `/resume` lewat Telegram.** Melanjutkan setelah `HALT_NEW`/`KILL` hanya lewat CLI di server dengan login SSH + 2FA dan alasan tertulis. Kalau akun Telegram diambil alih, penyerang paling jauh hanya bisa menghentikan bot, tidak bisa menjalankannya kembali.

**Dead man's switch untuk monitoring:** Watchdog mengirim ping ke layanan cek eksternal (misalnya Healthchecks.io) setiap menit. Kalau ping berhenti, layanan itu yang mengirim alert. Ini menutup kasus di mana justru sistem notifikasi yang mati.

### 3.3 Topologi Deployment

```
VPS-1 (utama, IP statis, di-allowlist bursa)          VPS-2 (kecil, region/provider berbeda)
┌──────────────────────────────────────────┐         ┌────────────────────────────┐
│ sentiment · strategy · risk · execution  │         │ watchdog                   │
│ redis · postgres/timescale · notifier    │◄───────►│ feed harga sendiri         │
│ prometheus · grafana                     │ VPN/    │ key bursa cadangan (cancel)│
└──────────────────────────────────────────┘ WireGuard└────────────────────────────┘
          │ backup harian terenkripsi                          │ ping
          ▼                                                    ▼
   object storage (object lock)                         Healthchecks.io
```

IP VPS-2 juga dimasukkan ke allowlist key cadangan, supaya Watchdog bisa membatalkan order saat VPS-1 mati.

---

## 4. Rencana Uji Ketahanan

### 4.1 Kuota API Habis

| Skenario | Cara menguji | Perilaku yang diharapkan | Kriteria lolos |
|----------|-------------|--------------------------|----------------|
| **Kredit/kuota Grok habis** | Mock server xAI yang mengembalikan 429 lalu error kredit habis | Sentiment Agent retry dengan exponential backoff + jitter, lalu menerbitkan `NO_SIGNAL`. Strategi lanjut dengan RSI saja. Alert WARNING. | Tidak ada order dengan m_sent > 1 selama `NO_SIGNAL`, tidak ada crash |
| **Kuota bulanan X API habis** | Ganti kredensial ke akun uji dengan kuota habis, atau mock 429 permanen | Sama seperti di atas, ditambah alert CRITICAL karena pemulihannya butuh tindakan manusia (upgrade tier atau tunggu reset) | Sistem tetap stabil selama 24 jam tanpa data sentimen |
| **Rate limit bursa (429)** | Toxiproxy/mock yang mengembalikan 429 | Execution memperlambat request sesuai header, breaker `DEGRADED` | Tidak ada request lanjutan yang memicu blokir IP |
| **IP diblokir bursa (418)** | Mock 418 | `KILL`, alert CRITICAL | Semua agen berhenti mengirim request dalam < 5 detik |
| **Kuota hampir habis** | Simulasi konsumsi 75% di tengah bulan | Alert pada 70% dan 90%. Mode hemat aktif: interval scoring diperpanjang, aset prioritas rendah dihentikan dulu. | Proyeksi kuota akhir bulan kembali di bawah 100% |

**Pencegahan sebelum kuota habis:** Sentiment Agent mencatat konsumsi per jam dan memproyeksikan sisa kuota sampai akhir periode. Mode hemat bertingkat:

| Proyeksi konsumsi | Aksi |
|------------------|------|
| < 80% | Normal |
| 80–100% | Batch scoring lebih besar, interval agregasi diperpanjang |
| > 100% | Hanya aset prioritas (BTC, ETH) yang diproses |
| Kuota habis | `NO_SIGNAL` sampai periode berikutnya |

### 4.2 Volatilitas Melonjak

| Skenario | Cara menguji | Perilaku yang diharapkan | Kriteria lolos |
|----------|-------------|--------------------------|----------------|
| **Replay crash historis** | Putar ulang data menit Maret 2020, Mei 2022, November 2022 ke seluruh pipeline dengan kecepatan 10× | Breaker naik level sesuai aturan, pacing membatasi belanja, sentimen ditangani sesuai mitigasi panik | Belanja 30 hari tidak melebihi cap, tidak ada order ditolak bursa karena harga basi |
| **Gap harga sintetis** | Suntikkan penurunan −15% dalam 1 menit | Watchdog memicu `HALT_NEW`, order entry dibatalkan, stop di bursa tetap ada | Waktu deteksi → cancel < 10 detik |
| **Lonjakan volume cuitan 20×** | Putar ulang stream rekaman dengan kecepatan 20× | Antrian menumpuk tapi tidak hilang. Data melewati TTL dibuang. Grok dipanggil dalam batch lebih besar. | Lag konsumen pulih < 15 menit setelah lonjakan, memori stabil |
| **Divergensi harga antar-bursa** | Feed Binance dan Coinbase dibuat berbeda 4% | `HALT_NEW` (anggap data rusak) | Tidak ada order selama divergensi |
| **Spread melebar & book menipis** | Mock order book dengan kedalaman −90% | Risk Manager menolak intent (`INSUFFICIENT_LIQUIDITY`) | Tidak ada fill dengan slippage di atas batas |

### 4.3 Chaos Engineering (Infrastruktur)

| Skenario | Cara | Perilaku yang diharapkan |
|----------|------|-------------------------|
| Execution crash di tengah pengiriman order | `docker kill` tepat setelah request terkirim | Saat bangkit: baca outbox, cek status order di bursa via `client_order_id`, tidak mengirim ulang order yang sudah ada |
| Koneksi VPS-1 ke bursa putus | Blokir egress dengan iptables | Stop di bursa melindungi posisi. Watchdog di VPS-2 mendeteksi dan memicu breaker. |
| Redis mati | Stop kontainer | Agen fail-closed: tidak ada intent yang diproses. Pulih otomatis tanpa pesan dobel. |
| Postgres mati | Stop kontainer | Risk Manager menolak semua intent (tidak bisa validasi saldo). Alert CRITICAL. |
| Pesan dobel / tidak berurutan | Kirim ulang pesan dengan `msg_id` sama, ubah urutan | Diproses tepat satu kali, pesan basi dibuang |
| Jam bergeser 3 detik | Ubah waktu sistem | Error `-1021` terdeteksi, `HALT_NEW`, alert |
| Watchdog mati | Matikan VPS-2 | Healthchecks.io mengirim alert dalam ≤ 2 menit |
| Notifier mati | Blokir akses ke Telegram | Alert CRITICAL tetap sampai lewat PagerDuty |

### 4.4 Jadwal Pengujian

| Kapan | Apa |
|-------|-----|
| Setiap commit (CI) | Unit test, tes policy, tes no look-ahead, scan secret |
| Setiap rilis | Replay 1 crash historis di staging |
| Fase 4 (paper trading) | Seluruh matriks §4.1–4.3 minimal sekali |
| Bulanan saat live | **Game day**: 2–3 skenario dijalankan di staging, ditambah latihan kill switch manual dari HP |
| Setelah insiden | Post-mortem tertulis, skenario insiden tersebut ditambahkan ke matriks uji |

---

## 5. Timeline & Gate

| Minggu | Fase | Deliverable utama | Gate |
|--------|------|-------------------|------|
| 1–2 | 0. Fondasi | Repo, CI, Docker Compose, Postgres/Redis, secret manager, skema pesan | Semua service kosong bisa jalan dan saling kirim heartbeat |
| 1–4 | 1. Data | Ingestion X + Grok + OHLCV, golden set v1 | Gate 1 |
| 3–6 | 2. Simulasi lokal | Filter anti-noise, agregasi, review sampel di spreadsheet | Gate 2 |
| 5–8 | 3. Backtest | Engine DCA, simulator, laporan walk-forward | Gate 3 |
| 7–10 | Infrastruktur risiko | Risk Manager, Execution (adapter simulasi), Watchdog, Notifier | Semua tes policy lolos |
| 9–16 | 4. Paper trading | Sistem lengkap live dengan order simulasi, uji ketahanan | Gate 4 |
| 17–28 | 5. Live kecil | Testnet → mainnet 10% modal | Gate 5 |
| 29+ | 6. Skala | Modal 25% → 50% → 100% | Review per tahap |

Estimasi ini untuk 1–2 engineer. Fase yang tumpang tindih bisa dikerjakan paralel jika ada lebih dari satu orang.

---

## 6. Runbook Operasional

**Harian (otomatis, dikirim ke Telegram):** PnL, eksposur, jumlah order, state breaker, konsumsi kuota API, latensi rata-rata, jumlah intent yang ditolak beserta alasannya.

**Mingguan (manual, 30 menit):**
- Review log intent yang ditolak dan breaker yang terpicu
- Bandingkan slippage aktual vs asumsi
- Cek drift skor Grok pada sampel golden set

**Saat alert CRITICAL:**
1. Buka dashboard, cek state breaker dan posisi di **dashboard bursa** (bukan hanya di Grafana).
2. Kalau ragu: `/kill` dari Telegram. Posisi tetap dilindungi stop di bursa.
3. Kalau dicurigai key bocor: cabut key di dashboard bursa terlebih dahulu, baru investigasi.
4. Selidiki penyebab lewat log berdasarkan `intent_id` / `msg_id`.
5. Resume hanya lewat CLI, dengan alasan tertulis di audit log.
6. Tulis post-mortem dan tambahkan skenario ke matriks uji §4.

**Rotasi berkala:** API key bursa tiap 90 hari, kunci tanda tangan agen tiap kuartal, kalibrasi ulang skor Grok setiap kali versi model berubah.
