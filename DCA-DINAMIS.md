# Bot DCA Dinamis: Teknikal + Sentimen Grok

Rancangan bot DCA (Dollar Cost Averaging) yang ukuran ordernya membesar saat pasar panik atau RSI oversold, dan mengecil saat pasar euforia. Dokumen ini memakai skor sentimen dari [desain mesin sentimen](README.md) dan jalur order dari [Eksekusi Aman](EKSEKUSI-AMAN.md).

> **Status:** dokumen desain. Semua parameter adalah titik awal yang harus divalidasi lewat backtest. Bukan nasihat keuangan.

---

## Daftar Isi

1. [Logika Strategi](#1-logika-strategi)
2. [Rumus Manajemen Risiko](#2-rumus-manajemen-risiko)
3. [Prompt untuk Grok 4](#3-prompt-untuk-grok-4)
4. [Alur Backtesting](#4-alur-backtesting)
5. [Tolok Ukur Evaluasi](#5-tolok-ukur-evaluasi)
6. [Implementasi Referensi](#6-implementasi-referensi)

---

## 1. Logika Strategi

DCA biasa membeli dengan nominal tetap secara berkala. Versi dinamis ini tetap membeli secara berkala, tapi nominalnya dikalikan **multiplier** yang tergantung kondisi pasar:

| Kondisi | Multiplier | Alasan |
|---------|-----------|--------|
| Normal | 1.0× | Pembelian standar |
| RSI oversold dan/atau sentimen panik | 1.5× – 3.0× | Akumulasi lebih banyak saat harga tertekan |
| RSI overbought dan sentimen euforia | 0.5× | Kurangi pembelian di harga mahal |
| Data sentimen `NO_SIGNAL` | Pakai komponen RSI saja | Data buruk tidak boleh memperbesar order |
| Breaker drawdown aktif | Maksimal 1.0×, atau berhenti | Lindungi modal (§2.4) |

**Dua risiko yang harus selalu diingat:**
1. **Falling knife.** "Panik ekstrem" bisa berlanjut berminggu-minggu (contoh: LUNA Mei 2022, FTX November 2022). Kalau multiplier besar dipakai tanpa batas, anggaran habis di awal penurunan, dan saat harga benar-benar di dasar sudah tidak ada dana tersisa. Karena itu ada pacing anggaran dan cooldown (§2.2–2.3).
2. **Aset yang tidak pulih.** DCA hanya masuk akal untuk aset yang kamu yakini punya nilai jangka panjang. Bot ini sebaiknya dibatasi ke aset besar (misalnya BTC, ETH), bukan altcoin kecil.

---

## 2. Rumus Manajemen Risiko

### 2.1 Alokasi Modal Bertahap

| Simbol | Arti | Contoh |
|--------|------|--------|
| C | Total modal di sub-account bot | $10.000 |
| f_dca | Porsi modal untuk program DCA | 0.8 (sisa 20% cadangan, tidak pernah disentuh bot) |
| B = C · f_dca | Anggaran DCA | $8.000 |
| N | Jumlah slot pembelian yang direncanakan | 52 (mingguan selama 1 tahun) |
| b₀ = B / N | Order dasar | $153,85 |

### 2.2 Multiplier Ukuran Order

**Komponen RSI** (RSI 14, timeframe harian):

```
m_rsi = 1 + a · clip( (RSI_low − RSI) / (RSI_low − RSI_floor) , 0 , 1 )
```
Contoh parameter: `RSI_low = 35`, `RSI_floor = 15`, `a = 1.0`. RSI 35 → 1.0×, RSI 25 → 1.5×, RSI ≤ 15 → 2.0×.

**Komponen sentimen** (memakai Z_t, yaitu skor relatif terhadap baseline 30 hari, bukan skor mentah):

```
m_sent = 1 + b · clip( (−Z_t − z_on) / (z_max − z_on) , 0 , 1 )      jika sinyal valid
m_sent = 1                                                           jika NO_SIGNAL
```
Contoh parameter: `z_on = 1.0`, `z_max = 3.0`, `b = 1.0`. Z = −1 → 1.0×, Z = −2 → 1.5×, Z ≤ −3 → 2.0×.

**Komponen euforia (pengurang):**

```
m_euph = 0.5   jika RSI > 70 DAN Z_t > +2
m_euph = 1     selain itu
```

**Penyesuaian volatilitas:** saat volatilitas sangat tinggi, nominal sama berarti risiko lebih besar.

```
m_vol = clip( σ_target / σ_30d , 0.5 , 1.0 )
```
`σ_30d` adalah volatilitas harian realisasi 30 hari. `σ_target` diambil dari median historis aset itu. Batas atas 1.0 artinya volatilitas rendah **tidak** memperbesar order.

**Multiplier akhir:**

```
m = clip( m_rsi · m_sent · m_euph · m_vol , m_min , m_max )        m_min = 0.5, m_max = 3.0
```

Dikalikan, bukan dijumlahkan: saat RSI oversold **dan** sentimen panik terjadi bersamaan, sinyalnya saling menguatkan (maksimal 2.0 × 2.0 = 4.0, lalu dibatasi `m_max` 3.0).

### 2.3 Pacing Anggaran (Mencegah Dana Habis Terlalu Cepat)

```
R            = sisa anggaran DCA
S            = sisa slot pembelian
order_kasar  = b₀ · m
order_pacing = R / max(1, S · γ)                 # γ = 0.5 → order maks 2× rata-rata sisa
order        = min( order_kasar , order_pacing , cap_30d − belanja_30d )
```

| Aturan | Nilai awal | Fungsi |
|--------|-----------|--------|
| `cap_30d` | 2 × (b₀ × slot per 30 hari) | Belanja 30 hari tidak boleh lebih dari 2× jadwal normal |
| Cooldown boost | 3 hari | Setelah order dengan m > 1.5, order berikutnya maksimal 1.0× selama 3 hari |
| Minimum order | Notional minimum bursa | Order di bawah minimum dilewati, bukan dibulatkan ke atas |
| Konsentrasi aset | Nilai aset ≤ 70% total C | Kalau terlampaui, pembelian berhenti |

### 2.4 Batas Penurunan Modal (Drawdown)

Pada DCA, aset yang sudah dibeli wajar kalau sedang merugi. Karena itu drawdown diukur pada **ekuitas total** (kas + nilai aset), bukan per pembelian.

```
E_t    = kas_t + q_t · P_t
DD_t   = 1 − E_t / max(E_0..E_t)
```

| Level | Kondisi | Aksi |
|-------|---------|------|
| Waspada | DD ≥ 15% | Multiplier dibatasi maksimal 1.0×, tidak ada boost |
| Halt | DD ≥ 25% | Pembelian baru berhenti, kirim alert, reset harus manual |
| Hard stop | DD ≥ 35% atau rugi bulanan > 15% C | Hentikan bot sepenuhnya, evaluasi strategi |

Ambang di atas **harus disesuaikan** dengan hasil backtest. BTC pernah turun lebih dari 70% dari puncak, jadi akumulator jangka panjang yang murni membeli akan mengalami drawdown besar. Tentukan dulu berapa drawdown yang sanggup kamu tanggung, lalu setel f_dca dan ambang sesuai angka itu.

### 2.5 Harga Rata-Rata

```
P_avg = Σ (q_i · p_i + fee_i) / Σ q_i
```
Biaya transaksi dimasukkan ke harga rata-rata, supaya keputusan take profit memakai titik impas yang sebenarnya.

### 2.6 Trailing Take Profit

Take profit dijual bertahap, bukan sekaligus, supaya bot tetap memegang sebagian posisi kalau tren naik berlanjut.

```
1. Aktivasi: P_t ≥ P_avg · (1 + TP_act)              TP_act = 25%
2. Lacak puncak sejak aktivasi: H = max(P sejak aktivasi)
3. Jarak trailing: trail = max( trail_min , k · ATR_14 / P_t )      trail_min = 8%, k = 3
4. Picu jual: P_t ≤ H · (1 − trail)
5. Jual porsi φ = 30% dari posisi
6. Setelah jual: reset H, aktivasi berikutnya butuh kenaikan TP_act lagi dari harga jual terakhir
```

| Penyesuaian | Kondisi | Efek |
|------------|---------|------|
| Euforia | Z_t > +2.5 dan RSI > 75 | `trail` diperketat menjadi 0.6× |
| Tranche maksimal | Maksimal 3 penjualan per siklus | Sisa posisi inti dipegang jangka panjang |
| Hasil penjualan | Kembali ke anggaran DCA (R) | Dipakai untuk akumulasi saat panik berikutnya |

**Kenapa ATR?** Jarak trailing tetap (misalnya 8%) terlalu ketat saat volatilitas tinggi (posisi terjual karena noise) dan terlalu longgar saat volatilitas rendah. ATR menyesuaikan jarak dengan volatilitas aktual.

---

## 3. Prompt untuk Grok 4

Prompt ini dirancang supaya kode yang dihasilkan **bisa diuji** dan **tidak membawa risiko keamanan**. Tempel ke Grok 4 apa adanya, lalu sesuaikan parameter di bagian `PARAMETER`.

> ⚠️ Kode hasil LLM wajib di-review baris per baris dan diuji di backtest + testnet. Jangan pernah menjalankannya langsung dengan akun riil.

````text
Kamu adalah quantitative developer senior. Tulis bot DCA dinamis dalam Python 3.11 untuk satu aset
(BTCUSDT, spot). Ikuti spesifikasi di bawah dengan tepat. Jika ada bagian yang ambigu, tulis asumsimu
sebagai komentar singkat dan pilih opsi yang paling konservatif. Jangan menambah fitur di luar spesifikasi.

## BATASAN KEAMANAN (WAJIB)
1. Bot TIDAK BOLEH memanggil API bursa secara langsung dan TIDAK BOLEH membaca API key.
   Semua order dikirim sebagai TradeIntent ke gateway lewat fungsi
   `send_intent(intent: dict) -> dict` yang hanya kamu buat sebagai interface (stub).
2. Tidak ada fungsi withdraw/transfer.
3. Hanya order LIMIT IOC. Harga limit = harga acuan × (1 + max_slippage_bps/10000) untuk BUY.
4. Semua angka uang dan kuantitas memakai decimal.Decimal, bukan float.
5. Tidak ada library yang tidak saya sebutkan, kecuali standard library.
   Library yang boleh: pandas, numpy, pytest.

## STRUKTUR FILE
- config.py      : dataclass Config berisi semua parameter (lihat PARAMETER), divalidasi di __post_init__
- indicators.py  : rsi(close, 14) metode Wilder, atr(high, low, close, 14) metode Wilder,
                   realized_vol(close, 30). Semua hanya memakai data sampai bar t (tanpa look-ahead).
- sizing.py      : fungsi murni (tanpa I/O) untuk multiplier dan ukuran order
- risk.py        : drawdown, state breaker (NORMAL, WASPADA, HALT, HARD_STOP), trailing take profit
- engine.py      : class DCAEngine dengan method on_bar(bar, sentiment) -> list[Action].
                   Engine yang SAMA dipakai untuk backtest dan live.
- backtest.py    : simulator yang memanggil DCAEngine bar per bar, eksekusi di OPEN bar berikutnya,
                   dengan fee dan slippage
- live.py        : loop yang memanggil DCAEngine dan mengirim Action lewat send_intent()
- tests/         : pytest untuk sizing.py, risk.py, dan indicators.py

## INPUT
- bar: dict {ts, open, high, low, close, volume} timeframe harian, UTC
- sentiment: dict {z: float | None, status: "OK" | "NO_SIGNAL"}
  z adalah skor sentimen relatif terhadap baseline 30 hari (negatif = panik).

## RUMUS SIZING (implementasikan persis)
b0 = C * f_dca / N
m_rsi  = 1 + a * clip((RSI_low - RSI) / (RSI_low - RSI_floor), 0, 1)
m_sent = 1 + b * clip((-z - z_on) / (z_max - z_on), 0, 1) jika status OK, selain itu 1
m_euph = 0.5 jika RSI > 70 dan status OK dan z > 2, selain itu 1
m_vol  = clip(sigma_target / sigma_30d, 0.5, 1.0)
m      = clip(m_rsi * m_sent * m_euph * m_vol, m_min, m_max)
Jika state breaker WASPADA: m = min(m, 1).
Cooldown: jika ada order dengan m > 1.5 dalam cooldown_days terakhir, m = min(m, 1).
order = min(b0 * m, R / max(1, S * gamma), cap_30d - spent_30d)
Lewati order jika order < min_notional. Lewati jika nilai aset / C > max_asset_share.

## RISIKO
E = cash + qty * close ; DD = 1 - E / peak(E)
DD >= dd_warn -> WASPADA ; DD >= dd_halt -> HALT (tidak ada beli, butuh reset manual)
DD >= dd_hard -> HARD_STOP (tidak ada aksi apa pun).
P_avg memasukkan fee.

## TRAILING TAKE PROFIT
Aktif saat close >= P_avg * (1 + tp_act). H = max close sejak aktif.
trail = max(trail_min, k_atr * ATR / close); jika status OK dan z > 2.5 dan RSI > 75, trail *= 0.6.
Jual sell_frac dari posisi saat close <= H * (1 - trail). Maksimal max_tranches per siklus.
Hasil jual masuk ke R. Setelah jual, aktivasi berikutnya dihitung dari harga jual terakhir.

## PARAMETER (default)
C=10000, f_dca=0.8, N=52, buy_every_days=7, a=1.0, b=1.0, RSI_low=35, RSI_floor=15,
z_on=1.0, z_max=3.0, m_min=0.5, m_max=3.0, sigma_target=dihitung dari median vol 365 hari pertama,
gamma=0.5, cap_30d_mult=2.0, cooldown_days=3, min_notional=10, max_asset_share=0.7,
dd_warn=0.15, dd_halt=0.25, dd_hard=0.35, tp_act=0.25, trail_min=0.08, k_atr=3.0,
sell_frac=0.3, max_tranches=3, fee_rate=0.001, slippage_bps=10, max_slippage_bps=25.

## TES (WAJIB, pytest)
1. RSI pada data sintetis yang terus naik mendekati 100; terus turun mendekati 0.
2. m tidak pernah keluar dari [m_min, m_max] untuk 10.000 input acak.
3. NO_SIGNAL tidak pernah membuat m_sent > 1.
4. Cooldown: dua bar panik berturut-turut, bar kedua m <= 1.
5. Total belanja tidak pernah melebihi B selama backtest pada data acak.
6. HALT tidak pernah kembali ke NORMAL tanpa reset manual.
7. Trailing TP tidak terpicu sebelum aktivasi.
8. Tidak ada look-ahead: hasil on_bar pada bar t tidak berubah jika data setelah t diganti acak.

## OUTPUT
Tulis setiap file lengkap dalam blok kode terpisah dengan nama file sebagai judul.
Di akhir, tulis daftar asumsi yang kamu buat. Jangan menulis penjelasan panjang di luar itu.
````

**Kenapa prompt-nya disusun seperti ini:**
- **Rumus ditulis eksplisit**, sehingga Grok tidak mengarang logika sizing sendiri, dan hasilnya bisa dicocokkan dengan implementasi referensi di §6.
- **Engine yang sama untuk backtest dan live.** Kalau keduanya dibuat terpisah, sering ada perbedaan halus yang membuat hasil backtest tidak mencerminkan perilaku live.
- **Tes no look-ahead (tes 8)** menangkap bug paling umum di kode backtest buatan LLM: indikator yang tidak sengaja memakai data masa depan.
- **Stub `send_intent`** menjaga agar kode hasil LLM tidak pernah menyentuh API key (sesuai [Eksekusi Aman](EKSEKUSI-AMAN.md)).

---

## 4. Alur Backtesting

### Langkah 1 — Siapkan Data Harga

- OHLCV harian dari bursa yang sama dengan yang akan dipakai live, sepanjang mungkin (BTC sejak 2017–2018), supaya mencakup beberapa siklus: crash Maret 2020, bull 2021, bear 2022, dan pemulihannya.
- Cek data: tidak ada bar hilang, tidak ada harga 0, timestamp UTC konsisten.

### Langkah 2 — Masalah Data Sentimen Historis

Ini bagian yang paling sering salah:

1. **Skor Grok historis tidak tersedia.** Mesin sentimen baru mulai mengumpulkan data sejak dijalankan.
2. **Jangan menyuruh Grok menilai cuitan lama secara retroaktif.** Model sudah dilatih dengan data sesudah kejadian itu. Ia "tahu" apa yang terjadi kemudian, dan pengetahuan ini bisa bocor ke skor tanpa disadari (look-ahead tersembunyi). Selain itu, akses arsip penuh X API mahal.

Solusinya, backtest dibagi dua tahap:

| Tahap | Data sentimen | Tujuan |
|-------|--------------|--------|
| **A. Backtest panjang** | Proxy historis, misalnya Crypto Fear & Greed Index (tersedia sejak 2018), dikonversi ke z-score 30 hari | Validasi logika sizing dan risiko di banyak siklus pasar |
| **B. Validasi forward** | Skor Grok asli yang disimpan mesin sentimen sejak mulai berjalan | Cek apakah sinyal Grok berperilaku mirip dengan proxy |

Hasil Tahap A harus dilaporkan sebagai **"dengan proxy sentimen"**, bukan sebagai hasil strategi dengan Grok.

### Langkah 3 — Point-in-Time & Eksekusi Realistis

- Keputusan di bar t hanya memakai data sampai close bar t. Order dieksekusi di **open bar t+1**.
- Skor sentimen di bar t harus yang sudah tersedia saat close bar t, termasuk keterlambatan publikasi (untuk Fear & Greed, nilai harian dirilis sekitar pukul 00:00 UTC).
- Fee 0.1% per transaksi, slippage 10 bps, aturan notional minimum dan lot size bursa.

### Langkah 4 — Baseline & Ablasi

Strategi hanya berguna kalau mengalahkan alternatif yang lebih sederhana. Jalankan semuanya dengan anggaran dan jadwal yang sama:

| Varian | Isi |
|--------|-----|
| B0 | Lump sum: seluruh B dibeli di hari pertama |
| B1 | DCA tetap: b₀ setiap periode |
| B2 | DCA + RSI saja (m_sent = 1) |
| B3 | DCA + sentimen saja (m_rsi = 1) |
| **S** | Strategi lengkap (RSI + sentimen + risiko + trailing TP) |
| S−TP | Strategi lengkap tanpa trailing TP |

Kalau S tidak lebih baik dari B1 secara meyakinkan, kompleksitas tambahannya tidak sebanding.

### Langkah 5 — Walk-Forward

```
|── Tuning 2018–2019 ──|── Uji 2020 ──|
          |── Tuning 2019–2020 ──|── Uji 2021 ──|
                    |── Tuning 2020–2021 ──|── Uji 2022 ──| ...
```

- Parameter dicari hanya di jendela tuning, lalu dikunci dan dinilai di jendela uji berikutnya.
- Batasi grid pencarian (maksimal sekitar 50 kombinasi) dan **catat jumlah kombinasi yang dicoba**. Semakin banyak yang dicoba, semakin besar peluang menemukan hasil bagus hanya karena kebetulan.

### Langkah 6 — Uji Ketahanan

| Uji | Cara | Lolos jika |
|-----|------|-----------|
| Sensitivitas parameter | Ubah setiap parameter ±20% | Hasil tidak runtuh. Parameter yang "hanya bagus di satu titik" adalah tanda overfit. |
| Tanggal mulai | Jalankan dengan tanggal mulai berbeda setiap bulan | Keunggulan atas B1 konsisten di sebagian besar tanggal mulai |
| Monte Carlo | Block bootstrap return harian (blok 20 hari), 1.000 simulasi | Persentil 5% dari max drawdown masih dalam batas toleransi |
| Stress test | Fokus ke Maret 2020, Mei 2022, November 2022 | Breaker bekerja, anggaran tidak habis sebelum dasar |
| Aset lain | ETH dengan parameter yang sama | Hasil searah (tidak harus sama persis) |

### Langkah 7 — Paper Trading & Go-Live Bertahap

| Fase | Durasi | Modal | Syarat lanjut |
|------|--------|------|---------------|
| Paper trading (sinyal Grok asli, order simulasi) | 4–8 minggu | $0 | Keputusan live sama dengan replay backtest di periode yang sama |
| Live kecil (lewat gateway, testnet dulu lalu mainnet) | 2–3 bulan | 10% dari C | Tidak ada insiden, slippage aktual ≤ asumsi backtest |
| Skala penuh | — | 100% C | Review manual atas seluruh log |

**Kriteria lolos sebelum live (contoh):**
- Harga rata-rata S lebih rendah dari B1 di ≥ 70% tanggal mulai
- Max drawdown S ≤ max drawdown B1 + 5 poin persen
- Calmar S ≥ Calmar B1
- Semua tes pytest lolos

---

## 5. Tolok Ukur Evaluasi

### 5.1 Metrik Khusus DCA

Return biasa menyesatkan untuk DCA karena modal masuk sedikit demi sedikit. Gunakan metrik berikut:

| Metrik | Rumus | Interpretasi |
|--------|------|--------------|
| **Perbaikan harga rata-rata** | `1 − P_avg(S) / P_avg(B1)` | Metrik utama: berapa persen lebih murah strategi mengakumulasi dibanding DCA biasa |
| **Money-weighted return (XIRR)** | IRR dari arus kas (setiap pembelian = keluar, nilai akhir = masuk) | Return yang memperhitungkan waktu masuknya modal |
| **Utilisasi modal** | Rata-rata (dana terpakai / B) | Utilisasi rendah berarti banyak kas menganggur. Bandingkan return per dolar yang benar-benar dipakai. |
| **Rasio boost di bawah median** | Proporsi belanja ber-multiplier > 1 yang terjadi di bawah harga median 90 hari | Mengukur apakah boost benar-benar terjadi di harga murah |

### 5.2 Metrik Risiko-Imbal Hasil

| Metrik | Rumus | Catatan |
|--------|------|---------|
| Sharpe | `mean(r_harian) / std(r_harian) · √365` | Crypto diperdagangkan 365 hari, jadi gunakan √365, bukan √252 |
| Sortino | `mean(r) / std(r ∣ r < 0) · √365` | Hanya menghukum volatilitas turun |
| Calmar | `CAGR / |MaxDD|` | Paling relevan untuk strategi akumulasi |
| Max drawdown | `max_t (1 − E_t / max(E_0..E_t))` | Dihitung pada ekuitas total (kas + aset) |
| Durasi drawdown | Hari terlama dari puncak sampai kembali ke puncak | Seberapa lama kamu harus menahan rugi |

### 5.3 Persentase Keberhasilan

Win rate untuk DCA perlu didefinisikan dengan hati-hati, karena tidak ada "trade" dengan entry dan exit yang jelas:

| Definisi | Rumus | Catatan |
|----------|------|---------|
| Win rate per pembelian | % pembelian yang harganya lebih rendah dari harga 90 hari kemudian | Ukur terpisah untuk order boost vs order normal |
| Win rate siklus TP | % siklus take profit yang menjual di atas P_avg | |
| Profit factor | `Σ untung / |Σ rugi|` dari semua penjualan | > 1.5 sudah baik |
| Ekspektansi | `win_rate · rata_untung − (1 − win_rate) · rata_rugi` | Win rate tinggi tidak berarti apa-apa kalau rugi saat kalah jauh lebih besar |

### 5.4 Signifikansi Statistik

Selisih kecil antara S dan B1 bisa terjadi karena kebetulan:
- Hitung **interval kepercayaan bootstrap** untuk selisih metrik utama (perbaikan harga rata-rata, Calmar) antara S dan B1. Kalau intervalnya mencakup 0, keunggulan belum terbukti.
- Kalau banyak kombinasi parameter dicoba, gunakan **Deflated Sharpe Ratio** (Bailey & López de Prado) untuk mengoreksi efek pencarian berulang.

---

## 6. Implementasi Referensi

Potongan ini dipakai untuk **mencocokkan** hasil kode Grok: jalankan keduanya pada input yang sama, hasilnya harus identik.

```python
from decimal import Decimal as D

def clip(x, lo, hi):
    return max(lo, min(hi, x))

def multiplier(rsi: float, z: float | None, status: str, sigma_30d: float, cfg) -> float:
    m_rsi = 1 + cfg.a * clip((cfg.rsi_low - rsi) / (cfg.rsi_low - cfg.rsi_floor), 0, 1)
    ok = status == "OK" and z is not None
    m_sent = 1 + cfg.b * clip((-z - cfg.z_on) / (cfg.z_max - cfg.z_on), 0, 1) if ok else 1.0
    m_euph = 0.5 if (ok and rsi > 70 and z > 2) else 1.0
    m_vol = clip(cfg.sigma_target / sigma_30d, 0.5, 1.0) if sigma_30d > 0 else 1.0
    return clip(m_rsi * m_sent * m_euph * m_vol, cfg.m_min, cfg.m_max)

def order_size(m: float, remaining: D, slots_left: int, spent_30d: D, cfg) -> D:
    b0 = cfg.C * cfg.f_dca / cfg.N
    raw = b0 * D(str(m))
    paced = remaining / max(D(1), D(slots_left) * D(str(cfg.gamma)))
    cap_left = cfg.cap_30d - spent_30d
    size = min(raw, paced, cap_left)
    return size if size >= cfg.min_notional else D(0)
```

```python
import numpy as np
import pandas as pd

def max_drawdown(equity: pd.Series) -> float:
    return float((1 - equity / equity.cummax()).max())

def sharpe(equity: pd.Series) -> float:
    r = equity.pct_change().dropna()
    return float(r.mean() / r.std() * np.sqrt(365)) if r.std() > 0 else 0.0

def sortino(equity: pd.Series) -> float:
    r = equity.pct_change().dropna()
    down = r[r < 0].std()
    return float(r.mean() / down * np.sqrt(365)) if down > 0 else 0.0

def calmar(equity: pd.Series) -> float:
    years = (equity.index[-1] - equity.index[0]).days / 365
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1
    mdd = max_drawdown(equity)
    return cagr / mdd if mdd > 0 else float("inf")

def xirr(cashflows: list[tuple[pd.Timestamp, float]]) -> float:
    """cashflows: pembelian bernilai negatif, nilai akhir portofolio bernilai positif."""
    t0 = cashflows[0][0]
    def npv(rate):
        return sum(cf / (1 + rate) ** ((t - t0).days / 365) for t, cf in cashflows)
    lo, hi = -0.99, 10.0
    for _ in range(200):                     # bisection; asumsi ada satu akar di rentang ini
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if npv(mid) > 0 else (lo, mid)
    return (lo + hi) / 2

def avg_price_improvement(p_avg_strategy: float, p_avg_plain_dca: float) -> float:
    return 1 - p_avg_strategy / p_avg_plain_dca
```

Catatan: `calmar` di atas memakai kurva ekuitas total. Karena modal pada DCA masuk bertahap, ekuitas awal bisa didominasi kas, sehingga CAGR terlihat rendah di awal. Selalu tampilkan Calmar berdampingan dengan XIRR dan perbaikan harga rata-rata, jangan sendirian.
