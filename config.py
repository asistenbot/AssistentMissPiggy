"""
Konfigurasi bisnis Miss Piggy.
SEMUA nilai di bawah ini WAJIB dicek/diganti sesuai data asli lo sebelum deploy.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ==== TELEGRAM ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "0"))

# ==== ANTHROPIC ====
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"

# ==== GOOGLE SHEETS ====
# Untuk deploy di Railway/Render: isi GOOGLE_SERVICE_ACCOUNT_JSON (isi lengkap
# file service_account.json, di-paste sebagai 1 baris) di Environment Variables.
# Untuk jalan di komputer lokal: taruh file service_account.json di folder ini,
# biarin GOOGLE_SERVICE_ACCOUNT_JSON kosong.
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

SHEET_ORDERS = "Orders"
SHEET_PRICELIST = "PriceList"
SHEET_SUPPLIER_DOUGH = "SupplierDough"

# ==== TIMEZONE ====
TIMEZONE = "Asia/Jakarta"

# ==== JADWAL BISNIS ====
# Hari buka PO: Sabtu s.d Rabu. Cutoff input ke produksi: Rabu jam 18:00.
PO_CUTOFF_DAY = "wed"      # Rabu
PO_CUTOFF_HOUR = 18
PO_CUTOFF_MINUTE = 0

# Pengiriman: Kamis jam 14:00 - 15:00 dari lokasi Miss Piggy
DELIVERY_DAY = "thu"
DELIVERY_WINDOW = "14:00 - 15:00"

# ==== JADWAL AUTO REKAP PRODUKSI (tiap Rabu) ====
# Format 24 jam, WIB
AUTO_RECAP_TIMES = [
    (15, 0),
    (16, 0),
    (19, 0),
]

# ==== JADWAL AUTO LAPORAN BULANAN ====
# Tanggal berapa tiap bulan, jam berapa. Default: tanggal 1, jam 09:00,
# ngerekap bulan SEBELUMNYA.
MONTHLY_REPORT_DAY = 1
MONTHLY_REPORT_HOUR = 9
MONTHLY_REPORT_MINUTE = 0

# ==== INFO PEMBAYARAN (buat invoice customer) ====
BANK_NAME = "GANTI_NAMA_BANK"          # contoh: "BCA"
BANK_ACCOUNT_NUMBER = "GANTI_NOMOR_REKENING"
BANK_ACCOUNT_NAME = "GANTI_ATAS_NAMA"

# ==== LOKASI PICKUP ====
PICKUP_ADDRESS = "GANTI_ALAMAT_MISS_PIGGY"

# ==== KATEGORI PRODUK ====
# Dipakai buat validasi & buat laporan bulanan ke supplier.
# Harga dough per kategori sebenarnya diambil dari tab SupplierDough di Sheets,
# list ini cuma buat referensi/validasi kategori yang valid.
CATEGORIES = [
    "Roti",
    "Roti Gandum",
    "Donat",
    "Roti Tawar",
    "Bun Polos",
    "Roti Tawar Loaf",
]

BUSINESS_NAME = "Miss Piggy"
