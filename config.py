"""
Konfigurasi bisnis Miss Piggy.
SEMUA nilai di bawah ini WAJIB dicek/diganti sesuai data asli lo sebelum deploy.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ==== TELEGRAM ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Bisa lebih dari 1 admin -- isi OWNER_TELEGRAM_IDS di .env dipisah koma,
# misal: OWNER_TELEGRAM_IDS=123456789,987654321
# (OWNER_TELEGRAM_ID lama masih didukung buat yang cuma 1 admin)
_owner_ids_raw = os.getenv("OWNER_TELEGRAM_IDS") or os.getenv("OWNER_TELEGRAM_ID", "0")
OWNER_TELEGRAM_IDS = [int(x.strip()) for x in _owner_ids_raw.split(",") if x.strip()]

# Opsional: kalau diisi, auto-recap Rabu & laporan bulanan dikirim ke GRUP ini
# (1x doang, semua admin di grup itu liat bareng), bukan ke tiap admin
# terpisah. ID grup Telegram biasanya angka NEGATIF, contoh: -1001234567890
# Cara ambil ID grup: tambahin bot ke grup, ketik /groupid di grup itu.
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID") or None

# ==== TOPIC ID PER KATEGORI (opsional, isi lewat Railway env var) ====
# Kalau diisi, kategori itu dikirim ke TOPIC tertentu di dalam GROUP_CHAT_ID
# (grup forum Telegram yang sama). Kalau mau grup TERPISAH sama sekali buat
# 1 kategori, isi GROUP_CHAT_ID_xxx di bawah (bukan TOPIC_ID_xxx).
def _topic_id(env_name):
    val = os.getenv(env_name)
    return int(val) if val else None
TOPIC_ID_ORDER = _topic_id("TOPIC_ID_ORDER")
TOPIC_ID_INVOICE = _topic_id("TOPIC_ID_INVOICE")
TOPIC_ID_SURATJALAN = _topic_id("TOPIC_ID_SURATJALAN")
TOPIC_ID_REKAPPRODUKSI = _topic_id("TOPIC_ID_REKAPPRODUKSI")
TOPIC_ID_LAPORANBULANAN = _topic_id("TOPIC_ID_LAPORANBULANAN")

GROUP_CHAT_ID_ORDER = os.getenv("GROUP_CHAT_ID_ORDER") or None
GROUP_CHAT_ID_INVOICE = os.getenv("GROUP_CHAT_ID_INVOICE") or None
GROUP_CHAT_ID_SURATJALAN = os.getenv("GROUP_CHAT_ID_SURATJALAN") or None
GROUP_CHAT_ID_REKAPPRODUKSI = os.getenv("GROUP_CHAT_ID_REKAPPRODUKSI") or None
GROUP_CHAT_ID_LAPORANBULANAN = os.getenv("GROUP_CHAT_ID_LAPORANBULANAN") or None
