# Fase 1: Pengumpulan Data — Panduan Menjalankan

Implementasi Fase 1 dari [Master Plan](MASTER-PLAN.md#fase-1--pengumpulan-data-mentah-minggu-14): mengumpulkan tweet dari X, menilainya dengan Grok, menyimpan harga dan Fear & Greed Index, serta menyiapkan golden set untuk mengukur akurasi Grok.

Belum ada filter anti-noise, agregasi skor, atau trading di fase ini. Semua itu dikerjakan di fase berikutnya di atas data yang terkumpul sekarang.

---

## Isi

| Perintah | Fungsi |
|---------|--------|
| `gse migrate` | Membuat/memperbarui skema database |
| `gse ingest-x` | Mengambil tweet baru per aset (X API v2 recent search) beserta profil akun dan tweet induk reply/quote |
| `gse snapshot-metrics` | Mengambil metrik tweet (like, view, dst.) saat tweet berumur 60 menit, dan menandai tweet yang dihapus |
| `gse score` | Menilai tweet yang belum punya skor dengan Grok (skor -1..1, confidence, sarkasme, prediksi, promosi, injection) |
| `gse market` | Sinkronisasi OHLCV harian dan per jam dari Binance sejak 2017 |
| `gse fear-greed` | Sinkronisasi seluruh histori Crypto Fear & Greed Index (proxy sentimen untuk backtest panjang) |
| `gse golden-export` / `golden-import` / `golden-eval` | Alur golden set: ekspor ke CSV, labeli di spreadsheet, impor, bandingkan dengan Grok |
| `gse status` | Cakupan data per aset, jeda terpanjang, celah sampel, pemakaian API, dan status Gate 1 |

Struktur kode ada di `src/gse/`, skema database di `src/gse/migrations/001_init.sql`.

---

## 1. Persiapan

Butuh Python 3.11+, Docker, serta akun developer X dan xAI.

```bash
cd grok-sentiment
cp .env.example .env          # lalu isi password, X_BEARER_TOKEN, XAI_API_KEY
docker compose up -d db
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
set -a && source .env && set +a
gse migrate
```

Database **wajib UTF8** (teks tweet berisi emoji). `gse migrate` menolak database dengan encoding lain.

## 2. Atur Anggaran Kuota X Dulu

X API menagih per post yang dibaca, dengan batas bulanan sesuai tier. Hitung konsumsi terburuk sebelum menyalakan cron:

```
post/bulan ≈ jumlah_aset × run_per_hari × X_MAX_PAGES × 100 × 30
```

Contoh: 2 aset, ingest tiap 15 menit (96 run/hari), `X_MAX_PAGES=2` → maksimal sekitar **1,15 juta post/bulan**. Angka ini hanya tercapai kalau setiap run selalu penuh, tapi tetap jauh di atas kuota tier rendah. Sesuaikan:

- **Query lebih sempit** di `config/assets.toml` (default sudah cashtag saja).
- **Interval cron lebih jarang** atau `X_MAX_PAGES` lebih kecil.
- `snapshot-metrics` juga membaca post (satu kali per tweet). Matikan kalau kuota sangat terbatas.

Cek konsumsi aktual setiap hari dengan `gse status` (bagian "pemakaian API"), lalu bandingkan dengan kuota tier di developer.x.com.

## 3. Jadwal Cron

```cron
# m   h  dom mon dow  perintah (jalankan dari folder grok-sentiment dengan .env dimuat)
*/15  *  *   *   *    gse ingest-x
*/15  *  *   *   *    gse snapshot-metrics
*/10  *  *   *   *    gse score
5     *  *   *   *    gse market
30    0  *   *   *    gse fear-greed
```

Contoh baris lengkap dengan logging:

```cron
*/15 * * * * cd /opt/gse/grok-sentiment && set -a && . ./.env && set +a && .venv/bin/gse ingest-x >> logs/ingest.log 2>&1
```

**Exit code** dipakai untuk memantau cron: `0` sukses, `1` error (jaringan, API, input), `2` rate limit X (run berikutnya otomatis melanjutkan).

## 4. Perilaku Penting

- **Tidak ada tweet hilang saat run terputus.** Posisi terakhir (`since_id`) baru dimajukan setelah semua halaman tersimpan. Kalau run berhenti karena rate limit atau jaringan, run berikutnya mengambil ulang dan duplikat diabaikan.
- **Batas halaman tercatat sebagai celah.** Kalau tweet baru lebih banyak dari `X_MAX_PAGES × 100`, tweet di tengah terlewat dan dicatat di tabel `ingest_gaps`, supaya kamu tahu data mana yang hanya sampel.
- **Skor Grok terikat versi.** Setiap skor disimpan dengan nama model dan `prompt_hash`. Kalau model atau prompt diganti, tweet dinilai ulang dan skor lama tetap tersimpan untuk perbandingan.
- **Gangguan API Grok tidak "menghanguskan" tweet.** Percobaan ulang (maks. 3) hanya dihitung kalau Grok merespons tapi output untuk tweet itu tidak valid. Kalau API-nya down atau kredit habis, run berhenti dan tweet dicoba lagi nanti.
- **Teks tweet di-escape** sebelum dikirim ke Grok, sehingga tweet yang berisi `</tweet>` tidak bisa keluar dari tag dan menyusup sebagai instruksi.
- **Bar yang belum tutup tidak disimpan**, supaya data harga tidak berubah setelah tersimpan.

## 5. Golden Set (Akurasi Grok)

```bash
gse golden-export --out golden_ana.csv --n 500 --labeler ana
# buka di Excel/Google Sheets, isi kolom "label" dengan -1..1 sesuai rubrik di README §2.3
gse golden-import golden_ana.csv --labeler ana
gse score --golden            # pastikan semua tweet golden sudah dinilai Grok
gse golden-eval
```

- Skor Grok **sengaja tidak ikut diekspor**, supaya pelabel tidak terpengaruh.
- Label boleh memakai koma desimal (`-0,5`). Baris dengan label kosong dilewati, jadi kamu bisa melabeli bertahap.
- Lebih dari satu pelabel? Pakai `--labeler` berbeda. Evaluasi memakai rata-rata label.
- Isi kolom `note` untuk kasus sulit (sarkasme, slang), berguna saat memperbaiki prompt.

## 6. Gate 1

Dari [Master Plan](MASTER-PLAN.md#fase-1--pengumpulan-data-mentah-minggu-14), Fase 1 dianggap selesai jika:

| Kriteria | Cara cek |
|----------|---------|
| Pengumpulan 14 hari tanpa jeda > 1 jam | `gse status` → "Gate 1 (pengumpulan): LOLOS" untuk setiap aset |
| Golden set ≥ 500 tweet berlabel | `gse golden-eval` |
| Spearman skor Grok vs label manusia ≥ 0.6 | `gse golden-eval` → "Gate 1 ...: LOLOS" |

Jeda > 1 jam di `gse status` dihitung dari tweet yang terkumpul. Untuk aset ramai, jeda selama itu hampir pasti berarti cron atau API sempat berhenti. Celah sampel di `ingest_gaps` ditampilkan terpisah dan tidak menggagalkan gate.

## 7. Menjalankan Test

```bash
pytest                                              # unit test (tanpa database)
GSE_TEST_DATABASE_URL=postgresql://gse:...@localhost:5432/gse_test pytest   # + test integrasi
```

Test integrasi **menghapus seluruh isi skema `public`** di database yang ditunjuk. Selalu pakai database khusus test, jangan database data asli.

## 8. Yang Belum Diuji

Klien X API, xAI, Binance, dan alternative.me diuji dengan respons tiruan. Bentuk respons mengikuti dokumentasi, tapi belum pernah dijalankan terhadap API sungguhan. Sebelum menyalakan cron, jalankan setiap perintah sekali secara manual dan periksa hasilnya di database.
