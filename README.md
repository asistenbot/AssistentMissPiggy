# Miss Piggy PO Assistant Bot

Bot Telegram buat bantu admin (lo) ngerapihin chat order customer jadi:
- Data tersimpan rapi di Google Sheets
- Invoice otomatis (harga x qty + no rek)
- Surat Jalan otomatis (tanpa harga, buat kurir)
- Rekap produksi per rasa (on-demand + auto tiap Rabu jam 15:00, 16:00, 19:00 WIB)
- Laporan bulanan buat bayar supplier (total qty per kategori x harga dough)

Bot ini **untuk lo sendiri (admin)**, bukan buat customer. Alurnya: lo paste/forward
chat customer yang berantakan ke bot ini di Telegram, bot yang parse jadi data rapi.

---

## 1. Yang Lo Butuhin Sebelum Jalanin Bot Ini

| Kebutuhan | Buat Apa | Cara Dapetin |
|---|---|---|
| Telegram Bot Token | Bot-nya sendiri | Chat `@BotFather` di Telegram → `/newbot` → ikutin instruksi → dapet token |
| Telegram User ID lo | Biar cuma lo yang bisa pake bot & terima auto-recap | Chat `@userinfobot` di Telegram, dia kasih tau ID lo |
| Anthropic API Key | Buat parsing chat customer yang berantakan jadi data terstruktur | https://console.anthropic.com → API Keys |
| Google Service Account JSON | Biar bot bisa baca/tulis ke Google Sheets lo | Google Cloud Console → aktifkan Sheets API + Drive API → buat Service Account → download JSON key |
| Google Sheet | Tempat semua data disimpen | Bikin 1 spreadsheet baru, share ke email service account (ada di file JSON, `client_email`), kasih akses **Editor** |
| Tempat hosting (buat 24/7) | Biar auto-recap jam 15:00/16:00/19:00 tetep jalan meski HP/laptop lo mati | Railway.app, Render.com, atau VPS kecil (semua ada free/murah tier) |

**Penting:** Semua ini gua siapin kodenya, tapi bagian bikin akun/token di atas
harus lo lakuin sendiri karena butuh akun pribadi lo. Kalau mau, gua bisa
pandu step-by-step pas lo udah mulai.

---

## 2. Isi Project

```
miss_piggy_bot/
├── README.md              <- ini
├── requirements.txt        <- daftar library python
├── .env.example             <- template isian token/config (copy jadi .env)
├── config.py                <- setting harga, kategori, no rek, jadwal, dll
├── sheets_client.py          <- fungsi baca/tulis Google Sheets
├── ai_parser.py               <- fungsi parsing chat customer pakai Claude
├── documents.py                <- fungsi generate Invoice, Surat Jalan, Rekap
├── scheduler_jobs.py            <- jadwal auto-kirim rekap Rabu & laporan bulanan
└── bot.py                        <- file utama, jalanin ini
```

---

## 3. Setup Google Sheets

Bikin 1 Google Sheet baru, kasih nama bebas (misal "Miss Piggy - Data PO"),
lalu bikin 3 tab (sheet) di dalemnya dengan nama & kolom PERSIS seperti ini:

### Tab `Orders`
| Timestamp | Minggu_PO | Nama_Customer | No_HP | Alamat | Metode | Kategori | Rasa | Qty | Harga_Satuan | Subtotal | Ongkir | Status | Tanggal_Kirim | Box_Info | Catatan | Kurir | Addon_Jenis | Addon_Qty | Addon_Total |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

- `Minggu_PO` = tanggal pengiriman Kamis untuk PO minggu itu (format `YYYY-MM-DD`), otomatis diisi bot
- `Metode` = `Kirim` atau `Ambil`
- `Status` = `Pending` / `Terkirim` (otomatis berubah jadi `Terkirim` kalau ada automation
  terpisah yang lo pasang di Google Apps Script, atau update manual di sheet kalau mau)
- `Tanggal_Kirim` = tanggal kirim custom (kalau beda dari `Minggu_PO`), otomatis diisi bot
- `Box_Info` = rincian pembagian per box (JSON), otomatis diisi bot kalau order-nya pakai satuan box
- `Catatan` = catatan bebas per order (misal "Donat & Gula dipisah pas packing"), otomatis
  diisi bot dari chat customer/instruksi admin, dan ikut muncul di Surat Jalan. **Kolom ini
  HARUS ditambahin manual sebagai header PALING BELAKANG** (setelah `Box_Info`) di sheet Orders
  lo sebelum fitur ini kepake -- kalau belum ada, bot tetep jalan normal, cuma catatan-nya
  nggak kesimpen/ke-print aja
- `Kurir` = nama ekspedisi pihak ketiga (misal "JNE", "Paxel", "J&T") kalau order-nya
  dikirim lewat itu, bukan armada/kurir toko sendiri -- kosong berarti armada sendiri.
  Diisi admin lewat tombol "🚚 Isi/Ubah Kurir" pas konfirmasi order (khusus metode
  Kirim/Diantar), dan bikin `/rekap` misahin daftar "DIKIRIM (KURIR)" dari "DIKIRIM"
  (armada) di topic Pengiriman, plus ikut muncul di Surat Jalan/Invoice. **Kolom ini
  HARUS ditambahin manual sebagai header PALING BELAKANG** (setelah `Catatan`) di sheet
  Orders lo sebelum fitur ini kepake -- kalau belum ada, bot tetep jalan normal, cuma
  info kurirnya nggak kesimpen/ke-pakai aja
- `Addon_Jenis` / `Addon_Qty` / `Addon_Total` = add-on packing (misal "Tali Pita",
  "Kartu Ucapan", atau "Tali Pita + Kartu Ucapan"), harganya udah ditentuin tetap
  di `config.py` (`ADDON_PRICES`, default @Rp5.000/Rp5.000/Rp10.000) dan dikaliin
  otomatis sama `Addon_Qty` yang lo isi. Diisi lewat tombol "🎀 Isi/Ubah Add-on" pas
  konfirmasi order (SELALU muncul, nggak peduli metode Kirim/Ambil), atau dikirim
  dari form web (field `addon_jenis`/`addon_qty` di JSON order). `Addon_Total`
  ikut nambah ke Total di Invoice, dan `Addon_Jenis`+`Addon_Qty` ikut ditampilin
  di Surat Jalan buat yang packing. **OTOMATIS KEDETEKSI JUGA** kalau customer
  udah nyebut "pita"/"kartu ucapan" dari chat/caption ASLINYA (misal customer
  nulis "catatan: pakai pita + kartu ucapan") -- admin nggak perlu pencet
  tombol lagi, langsung ke-isi & ke-total pas hasil parse pertama muncul
  (qty default 1 kecuali ada angka yang literally nempel di kata pita/kartu
  ucapan, misal "pita 2"). Tombol "🎀 Isi/Ubah Add-on" tetep ada buat
  nambah/ubah/hapus manual kapan aja. **3 kolom ini HARUS ditambahin manual
  sebagai header PALING BELAKANG** (setelah `Kurir`, urutannya: `Addon_Jenis`,
  `Addon_Qty`, `Addon_Total`) di sheet Orders lo sebelum fitur ini kepake --
  kalau belum ada, bot tetep jalan normal, cuma add-on-nya nggak
  kesimpen/ke-pakai aja

### Tab `PriceList`
| Kategori | Rasa | Harga |
|---|---|---|

Isi ini manual dulu sesuai harga jual lo per rasa. Bot bakal baca harga dari
sini tiap kali bikin invoice.

### Tab `SupplierDough`
| Kategori | Harga_Dough_Per_Unit |
|---|---|

Contoh isi:
```
Roti                 | 3500
Roti Gandum          | 4000
Donat                | 3000
Roti Tawar           | 5000
Bun Polos            | 2500
Roti Tawar Loaf      | 6000
```
Ganti angka-angka di atas sesuai harga dough asli dari supplier lo.

Setelah itu, share spreadsheet-nya ke `client_email` yang ada di file JSON
service account, kasih akses **Editor**.

---

## 4. Install & Jalanin (Lokal, buat testing)

```bash
cd miss_piggy_bot
pip install -r requirements.txt
cp .env.example .env
# isi .env dengan token-token lo
python bot.py
```

Kalau berhasil, chat bot lo di Telegram, ketik `/start`.

---

## 5. Deploy 24/7 (biar auto-recap jalan terus)

Paling gampang pake **Railway.app** atau **Render.com** (background worker):
1. Push folder ini ke GitHub repo
2. Connect repo ke Railway/Render
3. Set environment variables (isi dari `.env`) di dashboard mereka
4. Deploy — mereka otomatis `pip install` dan jalanin `python bot.py`

---

## 6. Command Bot

| Command | Fungsi |
|---|---|
| `/start` | Salam pembuka + bantuan |
| `/pricelist` | Tampilin price list dari Sheets |
| (kirim/forward chat customer sebagai teks biasa) | Bot otomatis parse jadi order, minta konfirmasi, lalu simpan + generate Invoice & Surat Jalan |
| `/rekap` | Rekap produksi per rasa untuk minggu PO berjalan (on-demand, kapan aja lo mau) |
| `/invoice Nama Customer` | Generate ulang invoice customer tsb (minggu berjalan) |
| `/suratjalan Nama Customer` | Generate ulang surat jalan customer tsb |
| `/laporanbulanan` | Generate laporan bulanan buat bayar supplier (bulan berjalan) |
| `/laporanbulanan 2026-07` | Generate laporan bulanan untuk bulan tertentu |

Auto-terjadwal: **NGGAK ADA** -- rekap produksi mingguan (dulu tiap Rabu
15:00/16:00/19:00) dan laporan bulanan (dulu tanggal 1 jam 09:00) dua-duanya
udah dimatiin atas permintaan admin. Semua manual lewat `/rekap` dan
`/laporanbulanan` kapan pun perlu. Kalau nanti mau dinyalain lagi, tinggal
un-comment bagian yang sesuai di `scheduler_jobs.py`.

---

## 7. Yang Masih Perlu Lo Isi/Sesuaikan di `config.py`

- Nomor rekening & nama bank
- Alamat pickup Miss Piggy
- Jam & hari operasional (default udah sesuai: PO Sabtu-Rabu 18:00, kirim Kamis 14:00-15:00)
- ID Telegram lo (biar cuma lo yang bisa akses bot & yang nerima auto-recap)

---

## 8. Batasan Versi Ini (biar lo nggak kaget)

- Invoice & Surat Jalan dikirim sebagai **teks rapi** di Telegram (gampang di-forward/copas ke customer/kurir). Kalau nanti mau versi PDF/gambar cantik, itu tinggal ditambahin — bilang aja.
- Parsing chat customer pakai AI itu cukup pinter buat kalimat natural
  ("mau pesen roti coklat 5, sourdough 2, dikirim ya, nama Budi..."), tapi
  tetep bakal nunjukin hasil parse-nya dulu buat lo konfirmasi sebelum
  disimpen — supaya nggak ada salah baca.
- Ongkir masih manual (lo input pas konfirmasi), sesuai alur lo sekarang.
