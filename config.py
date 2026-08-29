"""
Konfigurasi bisnis Anugerah Sejahtera Sentosa.
SEMUA nilai di bawah ini WAJIB dicek/diganti sesuai data asli sebelum deploy.
Yang keliatan "TODO" belum diisi Riky pas awal setup -- edit langsung di sini
atau tinggal minta tolong Claude Code buat ganti.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ==== TELEGRAM ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
_owner_ids_raw = os.getenv("OWNER_TELEGRAM_IDS") or os.getenv("OWNER_TELEGRAM_ID", "0")
OWNER_TELEGRAM_IDS = [int(x.strip()) for x in _owner_ids_raw.split(",") if x.strip()]

# Opsional: kalau diisi, order/invoice/dll dikirim ke GRUP ini juga (bukan cuma DM admin).
# Cara ambil ID grup: tambahin bot ke grup, ketik /groupid di grup itu.
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID") or None

# ==== ANTHROPIC ====
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = "claude-sonnet-4-6"

# ==== GOOGLE SHEETS ====
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

SHEET_ORDERS = "Orders"
SHEET_PRICELIST = "PriceList"
SHEET_CUSTOMERS = "Customers"
SHEET_SUPPLIERS = "Suppliers"
SHEET_PURCHASE_ORDERS = "PurchaseOrders"
SHEET_UTANG_SUPPLIER = "UtangSupplier"
SHEET_KAS = "Kas"

# ==== TIMEZONE ====
TIMEZONE = "Asia/Jakarta"

# ==== IDENTITAS USAHA (buat kop Invoice / Surat Jalan / PO) ====
BUSINESS_NAME = "Anugerah Sejahtera Sentosa"

# TODO: Riky belum kasih alamat usaha -- isi di sini biar muncul di kop invoice
# & surat jalan. Kalau ada 2 lokasi (misal gudang vs alamat pengiriman/pickup),
# boleh dibedain juga.
BUSINESS_ADDRESS = "TODO: isi alamat usaha di sini"
PICKUP_ADDRESS = "TODO: isi alamat pickup/gudang di sini (kalau beda dari BUSINESS_ADDRESS)"

# ==== INFO PEMBAYARAN (buat invoice customer) ====
BANK_NAME = "BCA"
BANK_ACCOUNT_NUMBER = "5170260998"
BANK_ACCOUNT_NAME = "Riky Kurniawan"

# ==== KATEGORI PRODUK ====
CATEGORIES = [
    "TULIP",
    "SAMPAH",
    "PP",
    "PE",
    "PILLOW CAKE",
]

# ==== SATUAN YANG VALID ====
UNITS = ["KG", "Piece", "Pax"]

# ==== KATALOG AWAL (di-seed otomatis ke tab PriceList kalau tab-nya masih kosong) ====
# Kalau mau ubah harga sehari-hari, GAK PERLU edit di sini -- cukup edit
# langsung di tab PriceList di Google Sheets, bot selalu baca dari situ.
# List ini cuma dipakai SEKALI waktu bot pertama kali jalan (buat isi awal).
SEED_PRODUCTS = [
    {"item_code": "PS91", "nama": "Plastik Tulip Putih 15", "deskripsi": "POLOS", "kategori": "TULIP", "satuan": "Pax", "harga_jual": 17000, "harga_beli": 0},
    {"item_code": "T30", "nama": "Tulip Putih UK 30", "deskripsi": "POLOS", "kategori": "TULIP", "satuan": "KG", "harga_jual": 28000, "harga_beli": 26000},
    {"item_code": "PS60", "nama": "Plastik Sampah 60x100x05", "deskripsi": "POLOS", "kategori": "SAMPAH", "satuan": "KG", "harga_jual": 20000, "harga_beli": 18165},
    {"item_code": "T40", "nama": "Tulip Putih UK 40", "deskripsi": "POLOS", "kategori": "TULIP", "satuan": "KG", "harga_jual": 28000, "harga_beli": 26000},
    {"item_code": "PCB", "nama": "Pillow Cake Plong", "deskripsi": "SABLON 1 SISI", "kategori": "PILLOW CAKE", "satuan": "Piece", "harga_jual": 1000, "harga_beli": 710},
    {"item_code": "PP12", "nama": "PP Bening 12x25", "deskripsi": "POLOS", "kategori": "PP", "satuan": "KG", "harga_jual": 33000, "harga_beli": 28050},
    {"item_code": "PP40", "nama": "PP Bening 40x60", "deskripsi": "POLOS", "kategori": "PP", "satuan": "KG", "harga_jual": 33000, "harga_beli": 28050},
    {"item_code": "PS90", "nama": "Plastik Sampah 90x120x06", "deskripsi": "POLOS", "kategori": "SAMPAH", "satuan": "KG", "harga_jual": 21500, "harga_beli": 18165},
    {"item_code": "PP35", "nama": "PP Bening 35x50", "deskripsi": "POLOS", "kategori": "PP", "satuan": "KG", "harga_jual": 34500, "harga_beli": 28050},
    {"item_code": "PE15", "nama": "PE Bening UK 15", "deskripsi": "POLOS", "kategori": "PE", "satuan": "Piece", "harga_jual": 375, "harga_beli": 125},
    {"item_code": "PE28", "nama": "PE Bening UK 28", "deskripsi": "POLOS", "kategori": "PE", "satuan": "Piece", "harga_jual": 700, "harga_beli": 365},
    {"item_code": "PCT", "nama": "Pillow Cake XL", "deskripsi": "SABLON 1 SISI", "kategori": "PILLOW CAKE", "satuan": "Piece", "harga_jual": 1600, "harga_beli": 1380},
    {"item_code": "BY24", "nama": "Baso Yen UK 24", "deskripsi": "SABLON 1 SISI", "kategori": "TULIP", "satuan": "Piece", "harga_jual": 935, "harga_beli": 400},
    {"item_code": "PS80", "nama": "Plastik Sampah 80x120x06", "deskripsi": "POLOS", "kategori": "SAMPAH", "satuan": "KG", "harga_jual": 20000, "harga_beli": 18165},
    {"item_code": "PP26", "nama": "PP Bening 26x40", "deskripsi": "POLOS", "kategori": "PP", "satuan": "KG", "harga_jual": 34500, "harga_beli": 28050},
    {"item_code": "PCG", "nama": "Pillow Cake L", "deskripsi": "SABLON 1 SISI", "kategori": "PILLOW CAKE", "satuan": "Piece", "harga_jual": 915, "harga_beli": 505},
]

# ==== CUSTOMER AWAL (di-seed otomatis ke tab Customers kalau masih kosong) ====
SEED_CUSTOMERS = [
    "GRANDIA HOTEL",
    "Baso Yen",
    "KURO KOFFEE",
    "SHU GUO YIN XIANG",
    "PILLOW CAKE",
]

# ==== NOMOR DOKUMEN ====
# Format nomor invoice / surat jalan / PO. {seq} diganti nomor urut per hari.
INVOICE_PREFIX = "INV"
SURAT_JALAN_PREFIX = "SJ"
PO_PREFIX = "PO"

# ==== KATEGORI KAS (buat /kas manual & laporan bulanan) ====
KAS_KELUAR_KATEGORI = ["Bayar Supplier", "Operasional", "Gaji", "Lainnya"]
