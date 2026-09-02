"""
Bot utama Miss Piggy PO Assistant.
Jalankan: python bot.py
"""

import asyncio
import json
import logging
import datetime
import re
import uuid

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

import config
import date_helpers
import documents
import receipt
import invoice_image
import monthly_report_pdf
import production_recap_pdf
from sheets_client import get_sheets_client
from ai_parser import parse_customer_chat, parse_customer_chat_image, parse_order_edit, classify_intent
from scheduler_jobs import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in config.OWNER_TELEGRAM_IDS:
            await update.message.reply_text("Bot ini khusus admin Miss Piggy.")
            return
        return await func(update, context)
    return wrapper


def _is_delivery_metode(metode):
    """True kalau metode-nya berarti 'dikirim' -- ada 2 istilah yang beredar
    di sistem ini: halaman order web pakai 'Diantar', tapi order yang di-AI
    parse dari chat manual (ai_parser.py) pakai 'Kirim'. Dicek berbasis
    substring biar dua-duanya (dan variasinya kayak 'Dikirim') kena."""
    m = (metode or "").strip().lower()
    return "antar" in m or "kirim" in m


_PEMISAH_NAMA = re.compile(r"\s*(?:,|&|\bdan\b|\bserta\b)\s*", re.IGNORECASE)


def _split_nama_customer(nama_raw: str) -> list:
    """Pecah teks nama customer yang mungkin berisi BEBERAPA nama sekaligus,
    dipisah koma/'dan'/'&'/'serta' -- misal 'Franky, Kelvin' atau 'Franky
    dan Kelvin' jadi ['Franky', 'Kelvin']. Kalau cuma 1 nama (nggak ada
    pemisah), balikin list isi 1 elemen -- jadi pemanggilnya selalu bisa
    treat hasilnya sebagai list, nggak perlu cek kondisi terpisah buat kasus
    1-nama vs banyak-nama."""
    parts = [p.strip() for p in _PEMISAH_NAMA.split(nama_raw) if p.strip()]
    return parts or [nama_raw.strip()]


def _tujuan_invoice(update):
    """(chat_id, message_thread_id) TUJUAN buat foto invoice customer.
    Ada 2 cara misahin, admin bebas pilih salah satu lewat env var Railway
    (kalau nggak di-setting dua-duanya, fallback balas di chat yang lagi
    dipake SEKARANG -- perilaku lama, aman):

      1) TOPIC di grup yang SAMA (Telegram Forum topics) -- LEBIH SIMPEL,
         bot cuma perlu di-invite/jadi admin di 1 grup. Diisi lewat
         config.TOPIC_ID_INVOICE (angka topic, dapetnya dari /groupid yang
         diketik DI DALAM topic itu sendiri) -- grupnya tetep pake
         config.GROUP_CHAT_ID yang udah ada.
      2) GRUP TERPISAH -- config.GROUP_CHAT_ID_INVOICE diisi ID grup lain.

    Kalau dua-duanya kebetulan diisi, TOPIC yang menang (lebih spesifik).
    getattr dipake di semua tempat biar nggak error walau config.py belum
    sempet ditambahin variabel-variabel baru ini."""
    topic_id = getattr(config, "TOPIC_ID_INVOICE", None)
    if topic_id:
        return (getattr(config, "GROUP_CHAT_ID", None) or update.effective_chat.id), topic_id
    chat_id = getattr(config, "GROUP_CHAT_ID_INVOICE", None)
    if chat_id:
        return chat_id, None
    return update.effective_chat.id, None


def _tujuan_suratjalan(update):
    """Sama kayak _tujuan_invoice, tapi buat 'admin surat jalan' (foto
    surat jalan, rekap pengiriman kurir/ambil sendiri) --
    config.TOPIC_ID_SURATJALAN (topic) atau config.GROUP_CHAT_ID_SURATJALAN
    (grup terpisah)."""
    topic_id = getattr(config, "TOPIC_ID_SURATJALAN", None)
    if topic_id:
        return (getattr(config, "GROUP_CHAT_ID", None) or update.effective_chat.id), topic_id
    chat_id = getattr(config, "GROUP_CHAT_ID_SURATJALAN", None)
    if chat_id:
        return chat_id, None
    return update.effective_chat.id, None


def _tujuan_order(update):
    """(chat_id, message_thread_id) TUJUAN buat preview order BARU (belum
    disimpan) -- baik dari paste/screenshot manual maupun dari web, dan
    hasil gabungan (/gabung). Sama pola-nya kayak _tujuan_invoice/
    _tujuan_suratjalan: TOPIC_ID_ORDER (topic 'ORDER' di grup yang sama)
    atau GROUP_CHAT_ID_ORDER (grup terpisah), fallback ke chat sekarang
    kalau nggak di-setting.

    SENGAJA dipake CUMA di titik order BARU dibikin (bukan di setiap balesan
    koreksi/ongkir/dst) -- begitu preview-nya nongol di topic ORDER, admin
    ngerjain sisanya (klik tombol, ketik koreksi/ongkir) dari SITU juga,
    jadi otomatis nyangkut di topic yang sama tanpa perlu kode tambahan di
    tiap langkah."""
    topic_id = getattr(config, "TOPIC_ID_ORDER", None)
    if topic_id:
        return (getattr(config, "GROUP_CHAT_ID", None) or update.effective_chat.id), topic_id
    chat_id = getattr(config, "GROUP_CHAT_ID_ORDER", None)
    if chat_id:
        return chat_id, None
    return update.effective_chat.id, None


def _tujuan_rekapproduksi(update):
    """(chat_id, message_thread_id) TUJUAN khusus buat rekap produksi
    mingguan (bagian resep/bahan, dari /rekap) -- DIPISAH dari
    _tujuan_invoice supaya rekap produksi nggak numplek sama invoice/
    laporan bulanan. Pola sama persis: config.TOPIC_ID_REKAPPRODUKSI (topic)
    atau config.GROUP_CHAT_ID_REKAPPRODUKSI (grup terpisah), fallback ke
    chat sekarang kalau nggak di-setting."""
    topic_id = getattr(config, "TOPIC_ID_REKAPPRODUKSI", None)
    if topic_id:
        return (getattr(config, "GROUP_CHAT_ID", None) or update.effective_chat.id), topic_id
    chat_id = getattr(config, "GROUP_CHAT_ID_REKAPPRODUKSI", None)
    if chat_id:
        return chat_id, None
    return update.effective_chat.id, None


def _tujuan_pengiriman(update):
    """(chat_id, message_thread_id) TUJUAN khusus buat daftar "DIKIRIM",
    "DIKIRIM (KURIR)", dan "DIAMBIL SENDIRI" (dari /rekap) -- DIPISAH dari _tujuan_suratjalan
    (yang isinya foto surat jalan per-customer) atas permintaan admin, biar
    daftar kirim/ambil nggak numplek sama foto-foto surat jalan. Pola sama
    persis: config.TOPIC_ID_PENGIRIMAN (topic) atau
    config.GROUP_CHAT_ID_PENGIRIMAN (grup terpisah), fallback ke chat
    sekarang kalau nggak di-setting."""
    topic_id = getattr(config, "TOPIC_ID_PENGIRIMAN", None)
    if topic_id:
        return (getattr(config, "GROUP_CHAT_ID", None) or update.effective_chat.id), topic_id
    chat_id = getattr(config, "GROUP_CHAT_ID_PENGIRIMAN", None)
    if chat_id:
        return chat_id, None
    return update.effective_chat.id, None


def _tujuan_laporanbulanan(update):
    """(chat_id, message_thread_id) TUJUAN khusus buat laporan bulanan
    supplier (/laporanbulanan, teks + PDF) -- DIPISAH dari _tujuan_invoice
    supaya laporan bulanan nggak numplek sama invoice/rekap produksi. Pola
    sama persis: config.TOPIC_ID_LAPORANBULANAN (topic) atau
    config.GROUP_CHAT_ID_LAPORANBULANAN (grup terpisah), fallback ke chat
    sekarang kalau nggak di-setting."""
    topic_id = getattr(config, "TOPIC_ID_LAPORANBULANAN", None)
    if topic_id:
        return (getattr(config, "GROUP_CHAT_ID", None) or update.effective_chat.id), topic_id
    chat_id = getattr(config, "GROUP_CHAT_ID_LAPORANBULANAN", None)
    if chat_id:
        return chat_id, None
    return update.effective_chat.id, None


async def _kirim_teks_ke(context, update, tujuan, text, parse_mode="Markdown", reply_markup=None):
    """Kirim TEKS ke tujuan = (chat_id, message_thread_id). Kalau gagal
    (misal bot belum di-invite, ATAU teksnya ngandung karakter Markdown yang
    nggak seimbang kayak * _ ` [ ), dicoba dulu kirim ULANG tanpa format
    sebelum bener-bener nyerah -- biar admin tetep kebagian isinya walau
    jadi polos. Kalau tetap gagal, SELALU kasih tau di chat asal (nggak lagi
    cuma kalau beda topic) -- biar nggak diem-diem ilang kayak sebelumnya.

    Return objek Message yang BENERAN kekirim (biar caller bisa nyimpen
    chat_id+message_id-nya, misal buat nge-EDIT preview order ini lagi
    belakangan alih-alih kirim pesan baru -- liat _send_or_update_preview),
    atau None kalau semua percobaan kirim gagal total."""
    chat_id, thread_id = tujuan
    try:
        return await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode,
            message_thread_id=thread_id, reply_markup=reply_markup,
        )
    except Exception as e:
        logger.error(f"Gagal kirim teks ke chat {chat_id} (topic {thread_id}): {e}")

    try:
        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=None,
            message_thread_id=thread_id, reply_markup=reply_markup,
        )
        if chat_id != update.effective_chat.id:
            await update.effective_chat.send_message(
                "⚠️ Berhasil kirim ke grup/topic tujuan, tapi formatnya jadi polos "
                "(ada karakter yang bikin format tebal/miring gagal)."
            )
        return sent
    except Exception as e2:
        logger.error(f"Tetap gagal kirim teks (plain) ke chat {chat_id} (topic {thread_id}): {e2}")

    await update.effective_chat.send_message(
        f"⚠️ Gagal kirim ke grup/topic tujuan. Cek bot udah di-invite & jadi admin di situ belum.\n\n"
        f"Ini isinya (kirim manual dulu ya):\n\n{text}"
    )
    return None


async def _kirim_foto_ke(context, update, tujuan, photo, caption):
    """Sama kayak _kirim_teks_ke, versi FOTO (invoice/surat jalan)."""
    chat_id, thread_id = tujuan
    try:
        await context.bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, message_thread_id=thread_id)
    except Exception as e:
        logger.error(f"Gagal kirim foto ke chat {chat_id} (topic {thread_id}): {e}")
        if chat_id != update.effective_chat.id:
            await update.effective_chat.send_message(
                f"⚠️ Gagal kirim '{caption}' ke grup/topic tujuan: {e}\n"
                "Cek bot udah di-invite & jadi admin di situ belum."
            )


def build_confirm_keyboard(parsed, order_id):
    """Tombol Simpan/Batal standar, ditambah tombol 'Isi/Ubah Ongkir' KHUSUS
    kalau metode-nya DIKIRIM (bukan ambil sendiri) -- biar admin bisa isi
    ongkir dulu SEBELUM invoice & surat jalan ke-generate (jadi nggak perlu
    /edit belakangan).

    order_id SENGAJA ditempel di callback_data (bukan cuma 'confirm_order'
    polos) -- biar tombol di pesan MANA PUN tetep nunjuk ke order-nya
    sendiri-sendiri, walau ada beberapa order numpuk nunggu diproses
    bareng. Tanpa ini, klik tombol di order lama bisa nyasar
    ngonfirm/nyimpen order LAIN yang kebetulan lagi 'aktif' pas itu."""
    rows = [[
        InlineKeyboardButton("✅ Simpan & Generate", callback_data=f"confirm_order:{order_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"cancel_order:{order_id}"),
    ]]
    if _is_delivery_metode(parsed.get("metode")):
        rows.append([InlineKeyboardButton("✏️ Isi/Ubah Ongkir", callback_data=f"set_ongkir:{order_id}")])
        # Tombol "Isi Kurir" KHUSUS metode kirim/antar juga -- dipake admin
        # nandain order ini lewat EKSPEDISI pihak ketiga (JNE/Paxel/J&T dst),
        # BEDA sama dikirim pake armada/kurir toko sendiri (kalau tombol ini
        # nggak dipencet/dikosongin, defaultnya dianggap armada sendiri).
        # Labelnya nunjukin nama kurir yang lagi keisi (kalau ada) biar
        # admin gampang liat status-nya tanpa buka koreksi.
        kurir_sekarang = parsed.get("kurir")
        kurir_label = f"🚚 Ubah Kurir ({kurir_sekarang})" if kurir_sekarang else "🚚 Isi Kurir (kalau via ekspedisi)"
        rows.append([InlineKeyboardButton(kurir_label, callback_data=f"set_kurir:{order_id}")])
    return InlineKeyboardMarkup(rows)


async def _send_or_update_preview(context, update, parsed, order_id, preview_text, keyboard):
    """Kirim pesan preview order (order BARU), ATAU kalau order ini UDAH
    punya pesan preview sebelumnya (chat_id+message_id-nya kesimpen di
    parsed['_preview_chat_id']/['_preview_msg_id']), EDIT pesan LAMA itu
    di tempat -- BUKAN kirim pesan baru.

    Ini fix buat laporan admin: abis koreksi/edit "berhasil disimpan",
    scroll ke atas masih ada kartu LAMA yang keliatan minta
    konfirmasi/batal padahal udah nggak relevan lagi. Akar masalahnya:
    tiap koreksi teks bebas selalu balas pakai reply_text (pesan BARU),
    jadi kartu lama nggak pernah ke-update/ke-clear sama sekali walau
    datanya di baliknya udah berubah.

    Kalau belum ada pesan lama (order baru) ATAU edit ke pesan lama gagal
    (misal keburu dihapus admin manual / lebih dari 48 jam), fallback
    kirim pesan BARU dan simpen id-nya buat koreksi berikutnya."""
    chat_id = parsed.get("_preview_chat_id")
    msg_id = parsed.get("_preview_msg_id")
    if chat_id and msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=msg_id, text=preview_text,
                parse_mode="Markdown", reply_markup=keyboard,
            )
            _store_pending_order(context, parsed, order_id)
            return
        except Exception as e:
            logger.error(f"Gagal edit preview lama order {order_id}, kirim pesan baru: {e}")

    sent = await update.message.reply_text(preview_text, parse_mode="Markdown", reply_markup=keyboard)
    if sent:
        parsed["_preview_chat_id"] = sent.chat_id
        parsed["_preview_msg_id"] = sent.message_id
    _store_pending_order(context, parsed, order_id)


def _new_order_id():
    return uuid.uuid4().hex[:8]


def _store_pending_order(context, parsed, order_id=None):
    """Simpen/update 1 order ke bot_data['pending_orders'][order_id].
    Kalau order_id nggak dikasih, generate ID baru (order BARU) dan
    dijadiin 'order yang lagi aktif' (dipakai buat nebak target koreksi
    bebas kayak 'yang apple salah hapus', liat handle_text).

    SEKARANG SEMUA order (web ATAU paste-chat manual) disimpen di bot_data,
    BUKAN user_data lagi -- bot_data itu SATU tempat yang sama buat SEMUA
    admin/HP (sesuai 'tetep bisa dihandle pake 2 hp'). Dan yang PALING
    PENTING: tiap order punya ID sendiri-sendiri di dalam SATU dict, jadi
    kalau ada beberapa order numpuk (misal beberapa customer submit lewat
    web hampir bareng), mereka nggak saling timpa kayak desain lama yang
    cuma nampung 1 order dalam 1 slot -- itu yang bikin tombol di order
    lama nyasar/ilang pas order baru dateng nyusul.

    Return order_id yang dipakai (baru atau yang di-passing)."""
    orders = context.bot_data.setdefault("pending_orders", {})
    if order_id is None:
        order_id = _new_order_id()
        context.bot_data["active_pending_order_id"] = order_id
    orders[order_id] = parsed
    return order_id


def _get_pending_order_by_id(context, order_id):
    if not order_id:
        return None
    return context.bot_data.get("pending_orders", {}).get(order_id)


def _clear_pending_order_by_id(context, order_id):
    context.bot_data.get("pending_orders", {}).pop(order_id, None)
    # Kalau yang dihapus ini kebetulan yang lagi 'aktif' (target koreksi
    # bebas), lepas juga penunjuknya -- biar koreksi bebas berikutnya
    # nggak nunjuk ke order yang udah nggak ada.
    if context.bot_data.get("active_pending_order_id") == order_id:
        context.bot_data.pop("active_pending_order_id", None)


def _get_active_pending_order(context):
    """Order yang PALING TERAKHIR dibuat/disentuh -- ini yang dianggap
    target kalau admin ngetik koreksi bebas TANPA nge-klik tombol dulu
    (misal 'yang apple salah, tolong hapus'). Kalau ada beberapa order
    numpuk, koreksi bebas cuma bisa nunjuk ke SATU (yang paling baru) --
    sama kayak keterbatasan desain lama, bukan regresi baru. Buat order
    LAIN yang ikut numpuk, admin tetep bisa proses lewat tombol
    Simpan/Batal/Isi Ongkir di pesannya masing-masing (itu udah nggak
    kena batasan ini sama sekali, soalnya order_id-nya nempel di tombol).
    Return (order_id, parsed) atau (None, None)."""
    order_id = context.bot_data.get("active_pending_order_id")
    parsed = _get_pending_order_by_id(context, order_id)
    if parsed is None:
        return None, None
    return order_id, parsed


def _format_kurir_line(parsed):
    """Baris 'Kurir: ...' yang dipakai di SEMUA preview order -- cuma
    dimunculin kalau metode-nya kirim/antar (nggak relevan buat Ambil
    Sendiri). Kosong/(armada sendiri) berarti dianter pake armada/kurir
    toko sendiri (bukan ekspedisi pihak ketiga kayak JNE/Paxel/J&T) --
    liat tombol 'Isi Kurir' di build_confirm_keyboard buat cara isinya."""
    if not _is_delivery_metode(parsed.get("metode")):
        return ""
    return f"Kurir: {parsed.get('kurir') or '(armada sendiri)'}\n"


def _format_catatan_block(parsed):
    """Baris 'Catatan: ...' (+ 'Peringatan AI: ...' kalau ada) yang dipakai
    di SEMUA preview order (baru, abis isi ongkir, abis dikoreksi) -- ditaruh
    di 1 tempat biar konsisten & gampang diubah sekali aja.

    "catatan" = instruksi packing yang ADMIN sendiri tulis (misal "donat &
    gula dipisah") -- ini yang kesimpen ke Sheets & ikut nongol di surat
    jalan. "peringatan_ai" = catetan OTOMATIS dari AI pas baca chat customer
    (no HP kosong, nama rasa ambigu, alamat kurang lengkap, dst) -- berguna
    buat admin CEK dulu sebelum Simpan, tapi SENGAJA nggak ikut
    kesimpen/keprint ke surat jalan (surat jalan itu buat kurir/packing,
    bukan tempat catetan kualitas parsing AI)."""
    text = f"Catatan: {parsed.get('catatan') or '-'}\n"
    if parsed.get("peringatan_ai"):
        text += f"⚠️ Peringatan AI (cek dulu -- TIDAK ikut ke surat jalan/invoice): {parsed['peringatan_ai']}\n"
    return text


def _build_new_order_preview_text(parsed, title="Hasil Parse:"):
    """Susun teks preview order BARU (belum disimpan) dari hasil parse AI --
    dipake bareng sama alur teks (handle_text) DAN alur gambar (handle_photo),
    biar tampilan preview-nya konsisten nggak peduli order-nya dateng dari
    chat diketik/paste atau dari screenshot yang difoto/di-forward."""
    items_text = "\n".join(
        f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
        for i in parsed.get("items", [])
    ) or "  (belum ada item terdeteksi)"

    preview = (
        f"*{title}*\n"
        f"Nama: {parsed.get('nama') or '-'}\n"
        f"No HP: {parsed.get('no_hp') or '-'}\n"
        f"Alamat: {parsed.get('alamat') or '-'}\n"
        f"Metode: {parsed.get('metode') or '-'}\n"
        f"{_format_kurir_line(parsed)}"
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini, ketik tanggal kirim jadi ... buat ubah)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {('Rp' + format(int(parsed.get('ongkir')), ',').replace(',', '.')) if parsed.get('ongkir') else 'belum diisi (Rp0)'}\n"
        f"{_format_catatan_block(parsed)}\n"
    )

    if parsed.get("kelengkapan") == "kurang_lengkap":
        preview += "⚠️ ADA YANG PERLU DICEK (lihat Peringatan AI di atas) sebelum disimpan!\n\n"

    return preview


# ---------------- COMMANDS ----------------

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Halo! Ini asisten admin PO {config.BUSINESS_NAME}.\n\n"
        "Bisa pake bahasa natural, nggak wajib '/'. Contoh:\n"
        "- 'tolong minta rekap produksi'\n"
        "- 'mau liat laporan bulanan dong'\n"
        "- 'yang atas nama Audry, tambah roti bakso 5'\n"
        "- atau paste langsung chat customer buat order baru\n\n"
        "Command '/' juga tetap bisa dipakai:\n"
        "- Paste/forward chat customer ke sini, akan diparse otomatis jadi order.\n"
        "- /pricelist — lihat daftar harga\n"
        "- /rekap — rekap produksi minggu berjalan\n"
        "- /rekap Nama Customer — rekap produksi khusus 1 customer aja\n"
        "- /invoice Nama Customer — bikin ulang invoice\n"
        "- /suratjalan Nama Customer — bikin ulang surat jalan\n"
        "- /edit Nama Customer — edit order yang sudah ada (tambah/kurangi/hapus item)\n"
        "- /kirim Nama Customer — tandain order udah Terkirim SAAT ITU JUGA (biar nggak numplek di rekap)\n"
        "- /gabung Nama Customer — gabungin beberapa order yang numpuk (belum di-Simpan) jadi 1\n"
        "- /laporanbulanan — laporan bayar supplier bulan ini\n"
        "- /laporanbulanan 2026-07 — laporan bulan tertentu\n"
        "- /laporanbulanan 2026-07:2026-08 — laporan rentang beberapa bulan\n"
        "- /groupid — lihat ID chat ini (buat setup grup admin)\n\n"
        "Rekap produksi & laporan bulanan sekarang manual semua (via /rekap "
        "dan /laporanbulanan) — auto-kirim terjadwal udah dimatiin."
    )


@owner_only
async def groupid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    thread_id = update.message.message_thread_id
    teks = (
        f"ID chat ini: `{chat.id}`\n"
        f"Tipe: {chat.type}\n\n"
        "Kalau ini grup dan mau dipakai buat terima auto-recap, "
        "copy ID di atas (termasuk tanda minus kalau ada) ke GROUP_CHAT_ID di Railway."
    )
    if thread_id:
        teks += (
            f"\n\nID TOPIC ini: `{thread_id}`\n"
            "Copy ID topic di atas ke salah satu env var Railway ini (sesuai topic ini "
            "buat apa): TOPIC_ID_ORDER (order masuk & konfirmasi), TOPIC_ID_INVOICE "
            "(invoice foto ke customer), TOPIC_ID_REKAPPRODUKSI (rekap produksi "
            "mingguan/bahan baku), TOPIC_ID_LAPORANBULANAN (laporan bulanan supplier), "
            "TOPIC_ID_SURATJALAN (foto surat jalan per-customer), atau "
            "TOPIC_ID_PENGIRIMAN (daftar DIKIRIM / DIKIRIM (KURIR) / DIAMBIL SENDIRI "
            "dari /rekap) — chat/grup-nya tetep pake GROUP_CHAT_ID yang biasa."
        )
    await update.message.reply_text(teks, parse_mode="Markdown")


@owner_only
async def pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = get_sheets_client()
    text = sheets.get_pricelist_text()
    await update.message.reply_text(f"*PRICE LIST — {config.BUSINESS_NAME}*\n{text}",
                                     parse_mode="Markdown")


@owner_only
async def rekap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Kalau argumennya PERSIS format tanggal 'YYYY-MM-DD' atau rentang
    # 'YYYY-MM-DD:YYYY-MM-DD' (misal '/rekap 2026-08-29' atau
    # '/rekap 2026-08-28:2026-08-29'), tampilin rekap produksi berdasarkan
    # TANGGAL KIRIM (gabungan SEMUA customer di rentang itu, nggak peduli
    # Minggu_PO-nya beda-beda) -- BEDA sama filter per-nama-customer di
    # bawah. Dicek PALING ATAS sebelum dianggap nama customer, soalnya nama
    # customer nggak bakal persis berformat tanggal kayak gini.
    if context.args and len(context.args) == 1:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(?::(\d{4}-\d{2}-\d{2}))?$", context.args[0])
        if m:
            tgl_mulai = m.group(1)
            tgl_akhir = m.group(2) or tgl_mulai
            sheets = get_sheets_client()
            orders = sheets.get_pending_orders_by_tanggal_range(tgl_mulai, tgl_akhir)
            label = tgl_mulai if tgl_mulai == tgl_akhir else f"{tgl_mulai} s/d {tgl_akhir}"
            text_tanggal = documents.build_production_recap_tanggal(label, orders)
            tujuan_produksi = _tujuan_rekapproduksi(update)
            await _kirim_teks_ke(context, update, tujuan_produksi, text_tanggal)

            if orders:
                try:
                    pdf_buf = production_recap_pdf.generate_production_recap_pdf(
                        f"REKAP PRODUKSI — {label}", "", orders,
                    )
                    chat_id_produksi, thread_id_produksi = tujuan_produksi
                    await context.bot.send_document(
                        chat_id=chat_id_produksi, document=pdf_buf,
                        filename=f"Rekap_Produksi_{tgl_mulai}_{tgl_akhir}.pdf",
                        caption="Versi PDF (siap print)", message_thread_id=thread_id_produksi,
                    )
                except Exception as e:
                    logger.error(f"Gagal generate/kirim PDF rekap produksi tanggal: {e}")
            return

    # Kalau admin kasih nama customer (misal '/rekap Ci Meyvany' atau lewat
    # bahasa natural 'minta rekap produksi Ci Meyvany'), tampilin rekap
    # produksi KHUSUS customer itu doang -- BUKAN pengganti rekap mingguan
    # biasa (di bawah ini, kalau context.args kosong, tetep jalan seperti
    # semula, gabungan SEMUA customer). Fitur ini cuma aktif kalau admin
    # EKSPLISIT minta pakai nama, jadi auto-recap terjadwal (scheduler_jobs.py)
    # nggak kesenggol sama sekali -- itu selalu manggil rekap() tanpa args.
    #
    # Boleh SATU nama atau BEBERAPA sekaligus dipisah "dan"/","/"&"/"serta"
    # (misal '/rekap Franky, Kelvin' atau 'rekap produksi Franky dan Kelvin')
    # -- daftar nama-nya dipecah lewat _split_nama_customer, tiap nama dicari
    # order-nya sendiri-sendiri (dengan fallback ke minggu lama juga kalau
    # perlu), baru digabung jadi 1 rekap.
    nama_customer_rekap = " ".join(context.args) if context.args else None
    if nama_customer_rekap:
        daftar_nama = _split_nama_customer(nama_customer_rekap)
        sheets = get_sheets_client()
        minggu_po = date_helpers.current_po_week_thursday()

        if len(daftar_nama) == 1:
            nama_tunggal = daftar_nama[0]
            orders = sheets.get_pending_orders_by_customer_week(nama_tunggal, minggu_po)
            if not orders:
                orders_fallback, minggu_fallback = sheets.get_pending_orders_by_customer_any_week(nama_tunggal)
                if orders_fallback:
                    orders = orders_fallback
                    minggu_po = minggu_fallback
            text_customer = documents.build_production_recap_customer(nama_tunggal, minggu_po, orders)
            judul_pdf = f"REKAP PRODUKSI — {nama_tunggal}"
            subtitle_pdf = f"(Tanggal Kirim: {orders[0].get('Tanggal_Kirim') or minggu_po})" if orders else ""
            filename_pdf = f"Rekap_Produksi_{nama_tunggal}.pdf"
        else:
            orders = []
            for nama_satu in daftar_nama:
                o = sheets.get_pending_orders_by_customer_week(nama_satu, minggu_po)
                if not o:
                    o, _ = sheets.get_pending_orders_by_customer_any_week(nama_satu)
                orders.extend(o)
            text_customer = documents.build_production_recap_multi(daftar_nama, orders)
            judul_pdf = f"REKAP PRODUKSI — {', '.join(daftar_nama)}"
            subtitle_pdf = ""
            filename_pdf = f"Rekap_Produksi_{len(daftar_nama)}_customer.pdf"

        tujuan_produksi = _tujuan_rekapproduksi(update)
        await _kirim_teks_ke(context, update, tujuan_produksi, text_customer)

        if orders:
            try:
                pdf_buf = production_recap_pdf.generate_production_recap_pdf(
                    judul_pdf, subtitle_pdf, orders,
                )
                chat_id_produksi, thread_id_produksi = tujuan_produksi
                await context.bot.send_document(
                    chat_id=chat_id_produksi, document=pdf_buf,
                    filename=filename_pdf,
                    caption="Versi PDF (siap print)", message_thread_id=thread_id_produksi,
                )
            except Exception as e:
                logger.error(f"Gagal generate/kirim PDF rekap produksi customer: {e}")
        return

    try:
        sheets = get_sheets_client()
        minggu_po = date_helpers.current_po_week_thursday()
        # get_pending_orders_by_week (BUKAN get_orders_by_week) -- ini yang
        # otomatis nge-rollover order yang tanggal kirimnya udah lewat jadi
        # 'Terkirim', trus buang order yang statusnya udah 'Terkirim' dari
        # rekap ini. Order yang dikirim SEBELUM tanggal kirimnya sendiri
        # (mis. dikirim lebih cepet di hari H) tetep kehitung sampe besok --
        # baru ilang otomatis keesokan harinya.
        orders = await asyncio.wait_for(
            asyncio.to_thread(sheets.get_pending_orders_by_week, minggu_po), timeout=20
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("Timeout ambil data dari Sheets. Coba lagi.")
        return
    except Exception as e:
        await update.message.reply_text(f"Gagal ambil data rekap: {e}")
        return

    # Kirim sebagai pesan TERPISAH biar gampang di-forward tanpa crop, DAN
    # ke grup TUJUAN yang beda-beda: rekap produksi ke grup admin rekap
    # produksi (buat baking/bahan), daftar kirim & ambil ke grup admin surat
    # jalan (buat kurir) -- kalau grup-grup itu belum di-setting, fallback
    # balas di chat ini kayak biasa.
    text_produksi = documents.build_production_recap(minggu_po, orders)
    tujuan_produksi = _tujuan_rekapproduksi(update)
    await _kirim_teks_ke(context, update, tujuan_produksi, text_produksi)

    try:
        pdf_buf = production_recap_pdf.generate_production_recap_pdf(
            f"REKAP PRODUKSI — Minggu PO {minggu_po}",
            f"(Kirim: {config.DELIVERY_DAY.upper()} {config.DELIVERY_WINDOW})",
            orders,
        )
        chat_id_produksi, thread_id_produksi = tujuan_produksi
        await context.bot.send_document(
            chat_id=chat_id_produksi, document=pdf_buf,
            filename=f"Rekap_Produksi_{minggu_po}.pdf",
            caption="Versi PDF (siap print)", message_thread_id=thread_id_produksi,
        )
    except Exception as e:
        logger.error(f"Gagal generate/kirim PDF rekap produksi: {e}")

    text_kirim = documents.build_delivery_kirim(minggu_po, orders)
    if text_kirim:
        await _kirim_teks_ke(context, update, _tujuan_pengiriman(update), text_kirim)

    text_kurir = documents.build_delivery_kurir(minggu_po, orders)
    if text_kurir:
        await _kirim_teks_ke(context, update, _tujuan_pengiriman(update), text_kurir)

    text_ambil = documents.build_delivery_ambil(minggu_po, orders)
    if text_ambil:
        await _kirim_teks_ke(context, update, _tujuan_pengiriman(update), text_ambil)


@owner_only
async def invoice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /invoice Nama Customer")
        return
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_pending_orders_by_customer_week(nama, minggu_po)
    catatan_minggu_lama = None
    if not orders:
        orders_fallback, minggu_fallback = sheets.get_pending_orders_by_customer_any_week(nama)
        if orders_fallback:
            orders = orders_fallback
            minggu_po = minggu_fallback
            catatan_minggu_lama = minggu_fallback
    if not orders:
        text = documents.build_invoice(nama, minggu_po, orders)
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    img = invoice_image.generate_invoice_image(nama, minggu_po, orders)
    caption = "Invoice (siap kirim ke customer)"
    if catatan_minggu_lama:
        caption += f"\n⚠️ Order dari minggu PO {catatan_minggu_lama} (bukan minggu aktif sekarang)."
    await _kirim_foto_ke(context, update, _tujuan_invoice(update), img, caption)


@owner_only
async def suratjalan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /suratjalan Nama Customer")
        return
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_pending_orders_by_customer_week(nama, minggu_po)
    catatan_minggu_lama = None
    if not orders:
        orders_fallback, minggu_fallback = sheets.get_pending_orders_by_customer_any_week(nama)
        if orders_fallback:
            orders = orders_fallback
            minggu_po = minggu_fallback
            catatan_minggu_lama = minggu_fallback
    if not orders:
        text = documents.build_surat_jalan(nama, minggu_po, orders)
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    box_groups = None
    box_info_raw = orders[0].get("Box_Info")
    if box_info_raw:
        try:
            box_groups = json.loads(box_info_raw)
        except (json.JSONDecodeError, TypeError):
            box_groups = None

    img = receipt.generate_surat_jalan_image(nama, minggu_po, orders, box_groups=box_groups)
    caption = "Surat jalan (siap print) — tap gambar → Share → app printer"
    if catatan_minggu_lama:
        caption += f"\n⚠️ Order dari minggu PO {catatan_minggu_lama} (bukan minggu aktif sekarang)."
    await _kirim_foto_ke(
        context, update, _tujuan_suratjalan(update), img, caption,
    )


async def _tandai_terkirim(update: Update, nama: str):
    """Tandain order 1 customer (minggu berjalan) jadi 'Terkirim' SAAT ITU
    JUGA -- jaring pengaman manual buat pas admin mau langsung nandain abis
    kurir beneran berangkat, nggak perlu nunggu cutoff otomatis jam 06:00
    WIB (lihat rollover_delivered_orders di sheets_client.py). Dipakai bareng
    sama command /kirim dan deteksi bahasa natural ('apple udah dikirim')."""
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    try:
        jumlah = await asyncio.wait_for(
            asyncio.to_thread(sheets.mark_customer_delivered, nama, minggu_po), timeout=20
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("Timeout pas update status. Coba lagi.")
        return
    except Exception as e:
        await update.message.reply_text(f"Gagal update status: {e}")
        return

    if jumlah == 0:
        await update.message.reply_text(
            f"Nggak ada order Pending atas nama {nama} untuk minggu ini "
            f"(mungkin udah Terkirim duluan, atau nama/minggunya beda)."
        )
        return

    await update.message.reply_text(
        f"✅ {jumlah} baris order {nama} ditandain Terkirim -- nggak bakal numplek lagi di rekap produksi."
    )


@owner_only
async def kirim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /kirim Nama Customer")
        return
    await _tandai_terkirim(update, nama)


_FILLER_DEPAN_KIRIM = re.compile(r"^(yg|yang|itu|order)\s+", re.IGNORECASE)
_FILLER_BELAKANG_KIRIM = re.compile(
    r"^(hari ini|tadi|barusan|dong|ya|yah)\b.*$", re.IGNORECASE
)
_KATA_PEMICU_KIRIM = re.compile(r"\b(sudah|udah)\s*(di\s*)?(kirim|terkirim)\b", re.IGNORECASE)
# Kata-kata yang nunjukkin ini KOMENTAR/PERTANYAAN, BUKAN perintah langsung
# -- misal 'loh yg apple kan udah dikirim hari ini' itu admin lagi
# NGELUH/NANYA soal rekap, bukan minta bot nandain status. Kalau salah satu
# ini ada di kalimatnya, mending nggak usah dieksekusi otomatis -- lebih
# aman drpd salah ubah data di Sheets gara-gara nyeletuk doang.
_PENANDA_BUKAN_PERINTAH = re.compile(
    r"\b(loh|lho|kok|kan|napa|kenapa|gimana|harusnya|masa|knp)\b|\?", re.IGNORECASE
)


def _try_parse_delivered_mark(text):
    """Coba deteksi instruksi 'tandain order si X udah dikirim' langsung
    dari kata kunci, TANPA perlu command /kirim -- misal 'apple udah di
    kirim' atau 'yg apple udah dikirim'. Return nama customer kalau ketemu,
    atau None kalau nggak yakin (fallback ke alur normal / AI
    classify_intent yang udah ada, jadi kapabilitas lama nggak keganggu).

    Sengaja mensyaratkan kata 'sudah'/'udah' NEMPEL LANGSUNG sebelum
    'kirim'/'terkirim' (cuma boleh disisipin 'di') -- biar teks order BARU
    yang kebetulan nyebut '... di kirim bun polos ...' (metode pengiriman,
    bukan status) nggak salah kesedot ke sini. Juga SENGAJA nolak kalimat
    yang ada penanda komentar/pertanyaan (liat _PENANDA_BUKAN_PERINTAH) dan
    hasil ekstraksi nama yang kepanjangan (>4 kata) -- dua-duanya nurunin
    resiko salah nandain order jadi Terkirim gara-gara admin cuma
    nyeletuk/nanya, bukan ngasih perintah."""
    text = text.strip()
    lower = text.lower()

    if _PENANDA_BUKAN_PERINTAH.search(lower):
        return None

    m = _KATA_PEMICU_KIRIM.search(lower)
    if not m:
        return None

    before = text[:m.start()].strip(" ,.!")
    after = text[m.end():].strip(" ,.!")

    before_clean = _FILLER_DEPAN_KIRIM.sub("", before).strip()
    after_clean = _FILLER_BELAKANG_KIRIM.sub("", after).strip()

    nama = before_clean or after_clean
    if not nama or len(nama) < 2 or len(nama.split()) > 4:
        return None
    return nama


_KATA_PEMICU_GABUNG = re.compile(r"\b(gabung(?:in|kan)?|satuin|satukan)\b", re.IGNORECASE)
# Kata pengiring umum yang sering nempel di depan/belakang nama customer di
# kalimat gabung -- dibuang kata-per-kata (bukan regex ^...$ sekali jalan)
# soalnya bisa lebih dari 1 filler nempel bareng DAN bisa berdiri sendiri
# (mis. before-text-nya cuma 'tolong' doang, harus abis dibuang jadi "").
_FILLER_WORDS_GABUNG = {
    "yg", "yang", "itu", "order", "orderan", "pesanan", "punya", "punyanya",
    "tolong", "nya", "dong", "donk", "ya", "yah", "aja", "dulu",
    "sih", "deh", "nih", "toh",
}


def _clean_nama_gabung(chunk):
    """Bersihin kata pengiring umum (order/orderan/pesanan/tolong/dong/dst)
    dari DEPAN dan BELAKANG potongan teks, kata per kata, sampai ketemu kata
    yang bukan filler (dianggap bagian dari nama customer)."""
    words = chunk.strip(" ,.!").split()
    while words and words[0].lower() in _FILLER_WORDS_GABUNG:
        words.pop(0)
    while words and words[-1].lower() in _FILLER_WORDS_GABUNG:
        words.pop()
    return " ".join(words)


def _try_parse_merge_pending(text):
    """Coba deteksi instruksi 'gabungin order/pesanan si X' langsung dari
    kata kunci, TANPA perlu command /gabung -- misal 'orderan pupu tolong
    gabungin' atau 'gabungin order pupu'. Return nama customer kalau ketemu,
    atau None kalau nggak yakin. Sama kayak _try_parse_delivered_mark,
    SENGAJA nolak kalimat yang keliatan komentar/pertanyaan
    (_PENANDA_BUKAN_PERINTAH) biar nggak salah gabung gara-gara admin cuma
    nanya 'gimana caranya gabungin order?' misalnya."""
    text = text.strip()
    lower = text.lower()

    if _PENANDA_BUKAN_PERINTAH.search(lower):
        return None

    m = _KATA_PEMICU_GABUNG.search(lower)
    if not m:
        return None

    before = _clean_nama_gabung(text[:m.start()])
    after = _clean_nama_gabung(text[m.end():])

    nama = before or after
    if not nama or len(nama) < 2 or len(nama.split()) > 4:
        return None
    return nama


async def _gabung_pending_orders_by_nama(update: Update, context: ContextTypes.DEFAULT_TYPE, nama: str):
    """Cari SEMUA order yang masih PENDING (preview belum di-'Simpan &
    Generate') atas nama yang sama (case-insensitive) -- misal 2 order dari
    web yang numpuk punya 1 customer -- gabungin item-nya jadi 1 order baru,
    lalu kirim preview + tombol Simpan/Batal/Isi Ongkir yang baru.

    Order-order lama otomatis kehapus dari pending_orders begitu digabung,
    jadi tombol di pesan-pesan LAMA nggak berlaku lagi kalau dipencet
    (bakal muncul 'Nggak ada data order yang tersimpan') -- ini SENGAJA,
    soalnya isinya udah pindah ke order gabungan yang baru. Dipakai bareng
    sama trigger bahasa natural ('orderan pupu tolong gabungin') DAN command
    manual /gabung Nama Customer.

    PENTING: ini cuma gabungin PREVIEW yang belum disimpan ke Sheets sama
    sekali -- abis digabung, masih ada 1 langkah review terakhir (pencet
    Simpan & Generate) sebelum kesave & invoice/surat jalan ke-generate,
    biar aman kalau gabungannya ada yang keliru (misal alamat beda)."""
    target = nama.strip().lower()
    orders = context.bot_data.get("pending_orders", {})
    matched_ids = [
        oid for oid, p in orders.items()
        if (p.get("nama") or "").strip().lower() == target
    ]

    if len(matched_ids) < 2:
        keterangan = (
            "nggak ketemu order pending atas nama itu" if not matched_ids
            else "cuma ketemu 1 order pending atas nama itu, nggak ada yang perlu digabung"
        )
        await update.message.reply_text(f"{nama.title()}: {keterangan}.")
        return

    matched = [orders[oid] for oid in matched_ids]

    # Gabungin item: kalau kategori+rasa-nya SAMA (case-insensitive), qty-nya
    # dijumlah jadi 1 baris; kalau beda, ditambahin sebagai baris baru.
    merged_items = []
    index_by_key = {}
    for p in matched:
        for item in p.get("items", []):
            key = (
                str(item.get("kategori", "")).strip().lower(),
                str(item.get("rasa", "")).strip().lower(),
            )
            if key in index_by_key:
                merged_items[index_by_key[key]]["qty"] += int(item.get("qty") or 0)
            else:
                index_by_key[key] = len(merged_items)
                merged_items.append({
                    "kategori": item.get("kategori"),
                    "rasa": item.get("rasa"),
                    "qty": int(item.get("qty") or 0),
                })

    def _first_nonempty(field):
        for p in matched:
            v = p.get(field)
            if v:
                return v
        return None

    def _first_ongkir():
        for p in matched:
            v = int(p.get("ongkir") or 0)
            if v:
                return v
        return 0

    merged = {
        "nama": matched[0].get("nama") or nama,
        "no_hp": _first_nonempty("no_hp"),
        "alamat": _first_nonempty("alamat"),
        "metode": _first_nonempty("metode"),
        "tanggal_kirim": _first_nonempty("tanggal_kirim"),
        "kurir": _first_nonempty("kurir"),
        "ongkir": _first_ongkir(),
        "items": merged_items,
        "kelengkapan": "lengkap",
    }

    # Catatan PACKING asli (yang admin tulis sendiri di order-order yang
    # digabung) digabung apa adanya -- ini beneran mau kesimpen/keprint.
    catatan_list = [p.get("catatan") for p in matched if p.get("catatan")]
    merged["catatan"] = " | ".join(catatan_list) if catatan_list else None

    # Kalau ternyata field penting (alamat/no_hp/metode) BEDA antar order
    # yang digabung, jangan diem-diem dipilih salah satu -- ini peringatan
    # OTOMATIS dari sistem (bukan catatan admin), jadi masuk 'peringatan_ai'
    # (sama kayak peringatan ambigu dari AI parser) BUKAN 'catatan' -- biar
    # nggak ikut kesimpen/keprint ke surat jalan, cuma buat admin cek
    # manual di Telegram sebelum pencet Simpan & Generate.
    peringatan_list = [p.get("peringatan_ai") for p in matched if p.get("peringatan_ai")]
    for field, label in (("alamat", "Alamat"), ("no_hp", "No HP"), ("metode", "Metode")):
        nilai_beda = {str(p.get(field)).strip() for p in matched if p.get(field)}
        if len(nilai_beda) > 1:
            peringatan_list.append(f"{label} beda antar order yang digabung ({' vs '.join(nilai_beda)}), cek manual!")
            merged["kelengkapan"] = "kurang_lengkap"
    merged["peringatan_ai"] = " | ".join(peringatan_list) if peringatan_list else None

    for oid in matched_ids:
        _clear_pending_order_by_id(context, oid)

    order_id = _store_pending_order(context, merged)
    preview = _build_new_order_preview_text(
        merged, f"Digabung dari {len(matched_ids)} order — Hasil Gabungan:"
    )
    keyboard = build_confirm_keyboard(merged, order_id)
    sent = await _kirim_teks_ke(context, update, _tujuan_order(update), preview, reply_markup=keyboard)
    if sent:
        merged["_preview_chat_id"] = sent.chat_id
        merged["_preview_msg_id"] = sent.message_id


@owner_only
async def gabung_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /gabung Nama Customer")
        return
    await _gabung_pending_orders_by_nama(update, context, nama)


@owner_only
async def laporanbulanan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = get_sheets_client()

    # Format argumen yang didukung:
    #   (kosong)              -> bulan berjalan
    #   2026-07                -> 1 bulan tertentu
    #   2026-07:2026-08         -> rentang beberapa bulan
    if context.args:
        arg = context.args[0]
        try:
            if ":" in arg:
                start_str, end_str = arg.split(":", 1)
                year_start, month_start = map(int, start_str.split("-"))
                year_end, month_end = map(int, end_str.split("-"))
            else:
                year_start, month_start = map(int, arg.split("-"))
                year_end, month_end = year_start, month_start
        except ValueError:
            await update.message.reply_text(
                "Format: /laporanbulanan 2026-07 (1 bulan) atau "
                "/laporanbulanan 2026-07:2026-08 (rentang beberapa bulan)"
            )
            return
    else:
        now = datetime.datetime.now(date_helpers.get_timezone())
        year_start = year_end = now.year
        month_start = month_end = now.month

    periode_label = documents.format_periode_label(year_start, month_start, year_end, month_end)

    try:
        orders = await asyncio.wait_for(
            asyncio.to_thread(sheets.get_orders_by_month_range, year_start, month_start, year_end, month_end),
            timeout=20,
        )
        dough_price_map = await asyncio.wait_for(
            asyncio.to_thread(sheets.get_dough_price_map), timeout=20
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("Timeout ambil data dari Sheets. Coba lagi.")
        return
    except Exception as e:
        await update.message.reply_text(
            f"Gagal bikin laporan bulanan: {e}\n"
            "Kemungkinan tab 'SupplierDough' di Sheets belum keisi lengkap, atau ada "
            "format yang salah di situ. Cek manual ya."
        )
        return

    text = documents.build_monthly_supplier_report(periode_label, orders, dough_price_map)
    tujuan = _tujuan_laporanbulanan(update)
    invoice_chat_id, invoice_thread_id = tujuan
    await _kirim_teks_ke(context, update, tujuan, text)

    month_results, grand_total_qty, grand_total_bayar = documents.aggregate_dough_by_month(orders, dough_price_map)
    if month_results:
        try:
            pdf_buf = monthly_report_pdf.generate_monthly_report_pdf(
                periode_label, month_results, grand_total_qty, grand_total_bayar
            )
            filename = f"Laporan_Bulanan_{year_start}{month_start:02d}-{year_end}{month_end:02d}.pdf"
            await context.bot.send_document(
                chat_id=invoice_chat_id, document=pdf_buf, filename=filename,
                caption="Versi PDF (siap print A4)", message_thread_id=invoice_thread_id,
            )
        except Exception as e:
            logger.error(f"Gagal bikin/kirim PDF laporan bulanan ke chat {invoice_chat_id}: {e}")
            if invoice_chat_id != update.effective_chat.id:
                await update.effective_chat.send_message(
                    f"⚠️ Gagal bikin/kirim PDF laporan bulanan ke grup/topic admin laporan bulanan: {e}"
                )
            else:
                await update.effective_chat.send_message(
                    f"⚠️ Gagal bikin file PDF-nya: {e}\nVersi teks di atas tetap valid kok."
                )


@owner_only
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /edit Nama Customer")
        return

    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_pending_orders_by_customer_week(nama, minggu_po)

    catatan_minggu_lama = None
    if not orders:
        orders_fallback, minggu_fallback = sheets.get_pending_orders_by_customer_any_week(nama)
        if orders_fallback:
            orders = orders_fallback
            minggu_po = minggu_fallback
            catatan_minggu_lama = minggu_fallback

    if not orders:
        await update.message.reply_text(f"Nggak ada order atas nama {nama} sama sekali.")
        return

    item_list_text = "\n".join(f"  - {o['Rasa']} ({o['Kategori']}) x{int(o['Qty'])}" for o in orders)

    context.user_data["editing_order"] = {
        "nama": orders[0].get("Nama_Customer", nama),
        "original_nama": orders[0].get("Nama_Customer", nama),
        "no_hp": orders[0].get("No_HP", "-"),
        "alamat": orders[0].get("Alamat", "-"),
        "metode": orders[0].get("Metode", "Ambil"),
        "existing_items": [
            {"kategori": o["Kategori"], "rasa": o["Rasa"], "qty": int(o["Qty"])} for o in orders
        ],
        "ongkir": int(orders[0].get("Ongkir", 0) or 0),
        "minggu_po": minggu_po,
        "tanggal_kirim": orders[0].get("Tanggal_Kirim") or minggu_po,
        "catatan": orders[0].get("Catatan") or None,
        "kurir": orders[0].get("Kurir") or None,
    }

    peringatan_minggu = (
        f"⚠️ Order ini dari minggu PO {catatan_minggu_lama} (bukan minggu aktif sekarang) -- "
        f"dilanjut edit di minggu itu ya.\n\n" if catatan_minggu_lama else ""
    )
    await update.message.reply_text(
        f"{peringatan_minggu}*Order {nama} saat ini:*\n{item_list_text}\n\n"
        f"Ketik perubahannya (bebas), contoh:\n"
        f"- Item: 'tambah donat gula 5, ham cheese jadi 20 pcs'\n"
        f"- No HP: 'no hp nya salah, benerin jadi 081234567890'\n"
        f"- Alamat/Nama/Metode: 'alamat ganti jadi ...', 'nama ganti jadi ...', 'metode jadi diantar'\n"
        f"- Tanggal kirim: 'tanggal kirim jadi besok', 'tanggal kirim jadi 25 agustus'\n"
        f"- Batal total: 'batalin aja semua'",
        parse_mode="Markdown",
    )


# ---------------- FREE-TEXT ORDER PARSING ----------------

@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Lagi nunggu admin ngetik nominal ongkir (abis klik "Isi/Ubah Ongkir")?
    # Ini dicek PALING atas, sebelum kemungkinan lain.
    if context.user_data.get("awaiting_ongkir_for"):
        await handle_ongkir_input(update, context)
        return

    # Sama kayak ongkir di atas, tapi buat nama kurir/ekspedisi (abis klik
    # "Isi/Ubah Kurir").
    if context.user_data.get("awaiting_kurir_for"):
        await handle_kurir_input(update, context)
        return

    # Kalau lagi dalam mode edit order (habis /edit Nama Customer atau baru
    # ke-detect mau edit), teks ini instruksi perubahan, BUKAN order baru.
    if context.user_data.get("editing_order"):
        await handle_edit_instruction(update, context)
        return

    # Deteksi deterministik buat instruksi 'gabungin order/pesanan si X' --
    # misal 'orderan pupu tolong gabungin' -- DICEK DULU SEBELUM blok
    # koreksi-ke-order-aktif di bawah. Soalnya kalau ada beberapa order
    # numpuk 1 customer yang sama (kasus yang justru mau digabung), salah
    # satunya otomatis jadi 'order aktif' -- kalau nggak dicek di sini
    # duluan, perintah gabung ini malah kesedot jadi 'koreksi' ke SATU
    # order aktif itu doang (lewat handle_pending_correction), bukan
    # digabungin ke semua order pending atas nama itu.
    nama_digabung = _try_parse_merge_pending(update.message.text)
    if nama_digabung:
        await _gabung_pending_orders_by_nama(update, context, nama_digabung)
        return

    # Kalau masih ada preview order yang nunggu Konfirmasi/Batal (belum
    # disimpen ke Sheets sama sekali), anggap chat berikutnya itu KOREKSI ke
    # preview yang PALING TERAKHIR dibuat/disentuh -- misal "yang apple
    # salah, tolong hapus" -- bukan order baru. Kalau ada beberapa order
    # numpuk, koreksi bebas cuma nunjuk ke yang paling baru itu; order lain
    # yang ikut numpuk tetep aman diproses lewat tombol di pesannya
    # masing-masing (order_id-nya nempel di situ, nggak kena batasan ini).
    # Kalau admin emang mau mulai order lain via ketik bebas, klik dulu
    # tombol Batal di preview yang lagi aktif.
    active_order_id, pending_parsed = _get_active_pending_order(context)
    if pending_parsed:
        await handle_pending_correction(update, context, pending_parsed, active_order_id)
        return

    raw_text = update.message.text

    # Deteksi deterministik (BUKAN lewat AI) buat instruksi manual 'tandain
    # udah dikirim' -- misal 'apple udah di kirim' -- biar admin nggak
    # wajib ketik /kirim Nama Customer. Dicek SEBELUM classify_intent (AI)
    # biar reliable & konsisten sama pola _try_parse_field_correction yang
    # udah ada; kalau nggak yakin (misal kalimatnya lebih kayak komentar/
    # pertanyaan), balik None dan lanjut ke alur normal di bawah.
    nama_dikirim = _try_parse_delivered_mark(raw_text)
    if nama_dikirim:
        await _tandai_terkirim(update, nama_dikirim)
        return

    # Coba tebak dulu maksud admin (bahasa natural, nggak wajib pakai '/')
    try:
        intent_result = await asyncio.wait_for(
            asyncio.to_thread(classify_intent, raw_text), timeout=20
        )
    except Exception:
        intent_result = {"intent": "order_baru", "nama_customer": None, "bulan_mulai": None, "bulan_akhir": None, "instruksi_edit": None}

    intent = intent_result.get("intent", "order_baru")

    if intent == "rekap_produksi":
        # Kalau AI nangkep TANGGAL/RENTANG TANGGAL spesifik (misal "rekap
        # produksi besok", "rekap produksi sampe besok", "rekap produksi
        # hari ini dan besok"), prioritasin itu -- pass sebagai context.args
        # format 'YYYY-MM-DD' atau 'YYYY-MM-DD:YYYY-MM-DD' biar rekap()
        # ke-detect sebagai rekap by tanggal (gabungan semua customer),
        # BUKAN dianggap nama customer.
        tanggal_mulai_rekap = intent_result.get("tanggal_mulai_rekap")
        if tanggal_mulai_rekap:
            tanggal_akhir_rekap = intent_result.get("tanggal_akhir_rekap") or tanggal_mulai_rekap
            if tanggal_akhir_rekap != tanggal_mulai_rekap:
                context.args = [f"{tanggal_mulai_rekap}:{tanggal_akhir_rekap}"]
            else:
                context.args = [tanggal_mulai_rekap]
            await rekap(update, context)
            return

        # Kalau AI kebetulan nangkep nama customer TERTENTU disebut bareng
        # permintaan rekap (misal "minta rekap produksi Ci Meyvany"), pass
        # sebagai context.args biar rekap() nunjukkin versi khusus 1
        # customer itu. Kalau nggak ada nama/tanggal, context.args dikosongin
        # eksplisit (bukan dibiarin nyisa dari command sebelumnya) biar
        # rekap() jalan seperti biasa (gabungan semua customer).
        nama_customer_rekap = intent_result.get("nama_customer")
        context.args = nama_customer_rekap.split() if nama_customer_rekap else []
        await rekap(update, context)
        return

    if intent == "laporan_bulanan":
        bulan_mulai = intent_result.get("bulan_mulai")
        bulan_akhir = intent_result.get("bulan_akhir")
        if bulan_mulai and bulan_akhir and bulan_mulai != bulan_akhir:
            context.args = [f"{bulan_mulai}:{bulan_akhir}"]
        elif bulan_mulai:
            context.args = [bulan_mulai]
        else:
            context.args = []
        await laporanbulanan_cmd(update, context)
        return

    if intent == "pricelist":
        await pricelist(update, context)
        return

    if intent == "invoice" and intent_result.get("nama_customer"):
        context.args = intent_result["nama_customer"].split()
        await invoice_cmd(update, context)
        return

    if intent == "surat_jalan" and intent_result.get("nama_customer"):
        context.args = intent_result["nama_customer"].split()
        await suratjalan_cmd(update, context)
        return

    if intent == "edit_order" and intent_result.get("nama_customer"):
        nama = intent_result["nama_customer"]
        instruksi = intent_result.get("instruksi_edit") or raw_text

        sheets = get_sheets_client()
        minggu_po = date_helpers.current_po_week_thursday()
        orders = sheets.get_pending_orders_by_customer_week(nama, minggu_po)
        if not orders:
            orders_fallback, minggu_fallback = sheets.get_pending_orders_by_customer_any_week(nama)
            if orders_fallback:
                orders = orders_fallback
                minggu_po = minggu_fallback

        if not orders:
            await update.message.reply_text(f"Nggak ada order atas nama {nama} sama sekali.")
            return

        context.user_data["editing_order"] = {
            "nama": orders[0].get("Nama_Customer", nama),
            "original_nama": orders[0].get("Nama_Customer", nama),
            "no_hp": orders[0].get("No_HP", "-"),
            "alamat": orders[0].get("Alamat", "-"),
            "metode": orders[0].get("Metode", "Ambil"),
            "existing_items": [
                {"kategori": o["Kategori"], "rasa": o["Rasa"], "qty": int(o["Qty"])} for o in orders
            ],
            "ongkir": int(orders[0].get("Ongkir", 0) or 0),
            "minggu_po": minggu_po,
            "tanggal_kirim": orders[0].get("Tanggal_Kirim") or minggu_po,
            "catatan": orders[0].get("Catatan") or None,
            "kurir": orders[0].get("Kurir") or None,
        }
        # Langsung proses instruksinya, nggak perlu tanya ulang ke admin
        await handle_edit_instruction(update, context, instruction_override=instruksi)
        return

    # Default: anggap order baru (perilaku sama seperti sebelumnya)
    await update.message.reply_text("Sedang diproses...")

    # Ambil daftar produk asli dari PriceList dulu, biar AI cocokin ke situ
    # (bukan asal nebak kategori) -- ini yang bikin "Meses" nggak salah masuk
    # ke kategori yang nggak ada produknya.
    sheets = get_sheets_client()
    try:
        catalog = await asyncio.wait_for(asyncio.to_thread(sheets.get_catalog_list), timeout=15)
    except Exception:
        catalog = None  # kalau gagal ambil, tetep lanjut tanpa catalog (fallback)

    try:
        # Jalanin pemanggilan AI di thread terpisah (bukan blocking event loop bot),
        # dan kasih batas waktu maksimal 40 detik biar nggak nge-gantung selamanya.
        parsed = await asyncio.wait_for(
            asyncio.to_thread(parse_customer_chat, raw_text, catalog), timeout=40
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "Timeout — proses parsing kelamaan (lebih dari 40 detik). "
            "Coba kirim ulang pesannya."
        )
        return
    except Exception as e:
        await update.message.reply_text(f"Ada error pas parsing: {e}\nCoba kirim ulang.")
        return

    # AI parser (ai_parser.py) fokusnya nge-parse ITEM pesanan, bukan selalu
    # nangkep tanggal kirim custom kayak "besok"/"lusa" yang nyempil di
    # kalimat chat customer (mis. "mau buat besok, ..."). Jadi kalau AI-nya
    # nggak ngisi tanggal_kirim sendiri, coba deteksi juga pake parser
    # deterministik yang sama kayak yang dipakai buat koreksi -- biar tanggal
    # kirim custom kedetect dari pesan PERTAMA, nggak perlu dikoreksi manual.
    if not parsed.get("tanggal_kirim"):
        tanggal_dari_teks = _parse_tanggal_kirim(raw_text)
        if tanggal_dari_teks:
            parsed["tanggal_kirim"] = tanggal_dari_teks

    order_id = _store_pending_order(context, parsed)
    preview = _build_new_order_preview_text(parsed, "Hasil Parse:")
    keyboard = build_confirm_keyboard(parsed, order_id)
    sent = await _kirim_teks_ke(context, update, _tujuan_order(update), preview, reply_markup=keyboard)
    if sent:
        # Simpen id pesan preview ini -- biar koreksi teks bebas belakangan
        # (handle_pending_correction) bisa EDIT kartu ini langsung di
        # tempat, bukan numpuk kirim kartu baru tiap kali dikoreksi.
        parsed["_preview_chat_id"] = sent.chat_id
        parsed["_preview_msg_id"] = sent.message_id


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin kirim/forward SCREENSHOT chat order customer (bukan diketik/paste
    teks) -- dibaca pake Claude vision (ai_parser.parse_customer_chat_image)
    trus diperlakukan PERSIS kayak order dari teks: masuk ke pending_orders
    yang sama, keluar preview + tombol Simpan/Batal/Isi Ongkir yang sama.

    Nggak nyentuh alur teks sama sekali -- foto & teks jalan independen,
    jadi admin bebas kirim orderan lewat cara mana aja (ketik/paste ATAU
    screenshot), bahkan bisa gantian nyampur keduanya buat order yang beda."""
    # Kalau lagi di tengah langkah SATU-KALI-JAWAB yang nunggu TEKS spesifik
    # (nominal ongkir, atau instruksi edit), foto yang nyasar ke sini bukan
    # itu yang diharepin -- kasih tau admin biar beresin dulu step teksnya.
    if context.user_data.get("awaiting_ongkir_for"):
        await update.message.reply_text(
            "Lagi nunggu kamu ketik nominal ongkir buat order sebelumnya dulu ya, "
            "baru kirim gambar order lain."
        )
        return
    if context.user_data.get("awaiting_kurir_for"):
        await update.message.reply_text(
            "Lagi nunggu kamu ketik nama kurir/ekspedisi buat order sebelumnya dulu ya, "
            "baru kirim gambar order lain."
        )
        return
    if context.user_data.get("editing_order"):
        await update.message.reply_text(
            "Lagi dalam mode edit order (nunggu instruksi teks) -- selesein dulu "
            "atau /edit lagi, baru kirim gambar order baru."
        )
        return

    await update.message.reply_text("Lagi baca gambar...")

    try:
        photo = update.message.photo[-1]  # resolusi paling tinggi
        photo_file = await photo.get_file()
        image_bytes = bytes(await photo_file.download_as_bytearray())
    except Exception as e:
        await update.message.reply_text(f"Gagal download gambarnya: {e}\nCoba kirim ulang.")
        return

    caption = (update.message.caption or "").strip() or None

    sheets = get_sheets_client()
    try:
        catalog = await asyncio.wait_for(asyncio.to_thread(sheets.get_catalog_list), timeout=15)
    except Exception:
        catalog = None

    try:
        parsed = await asyncio.wait_for(
            asyncio.to_thread(parse_customer_chat_image, image_bytes, "image/jpeg", caption, catalog),
            timeout=40,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "Timeout — proses baca gambar kelamaan (lebih dari 40 detik). "
            "Coba kirim ulang gambarnya."
        )
        return
    except Exception as e:
        await update.message.reply_text(f"Ada error pas baca gambar: {e}\nCoba kirim ulang.")
        return

    # Sama kayak alur teks: AI parser kadang nggak nangkep tanggal kirim
    # custom ('besok'/'lusa') dari CAPTION yang ditulis admin bareng foto --
    # coba deteksi juga pake parser deterministik yang sama.
    if caption and not parsed.get("tanggal_kirim"):
        tanggal_dari_caption = _parse_tanggal_kirim(caption)
        if tanggal_dari_caption:
            parsed["tanggal_kirim"] = tanggal_dari_caption

    order_id = _store_pending_order(context, parsed)
    preview = _build_new_order_preview_text(parsed, "Hasil Baca Gambar:")
    keyboard = build_confirm_keyboard(parsed, order_id)
    sent = await _kirim_teks_ke(context, update, _tujuan_order(update), preview, reply_markup=keyboard)
    if sent:
        parsed["_preview_chat_id"] = sent.chat_id
        parsed["_preview_msg_id"] = sent.message_id


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # callback_data formatnya "confirm_order:<id>" / "cancel_order:<id>" --
    # order_id-nya nempel di tombol itu sendiri, jadi tombol di pesan MANA
    # PUN (walau ada beberapa order numpuk nunggu diproses bareng) selalu
    # nunjuk ke order yang BENER, nggak nyasar ke order lain yang kebetulan
    # lagi "aktif". Fallback split(":") kalau somehow ID-nya kosong/lama.
    action, _, order_id = query.data.partition(":")

    if action == "cancel_order":
        _clear_pending_order_by_id(context, order_id)
        await query.edit_message_text("Dibatalin ya.")
        return

    # Cegah klik dobel -- SEKARANG per-order_id (disimpen di bot_data, bukan
    # user_data per-admin), soalnya order lain yang numpuk bareng harus
    # tetep bisa diproses walau ada 1 order lain lagi disimpen. Kalau masih
    # per-admin (desain lama), 1 admin nyimpen 1 order bakal ke-lock buat
    # SEMUA order lain yang dia pegang juga -- padahal harusnya independen.
    saving_flags = context.bot_data.setdefault("saving_in_progress", set())
    if order_id in saving_flags:
        return
    saving_flags.add(order_id)

    parsed = _get_pending_order_by_id(context, order_id)
    if not parsed or not parsed.get("items"):
        await query.edit_message_text("Nggak ada data order yang tersimpan. Kirim ulang chat-nya ya.")
        saving_flags.discard(order_id)
        return

    await query.edit_message_text("Menyimpan...")

    order = {
        "nama": parsed.get("nama") or "Tanpa Nama",
        "no_hp": parsed.get("no_hp") or "-",
        "alamat": parsed.get("alamat") or "-",
        "metode": parsed.get("metode") or "Ambil",
        "items": [
            {"kategori": i["kategori"], "rasa": i["rasa"], "qty": int(i["qty"])}
            for i in parsed["items"]
        ],
        "ongkir": int(parsed.get("ongkir") or 0),
        "tanggal_kirim": parsed.get("tanggal_kirim"),
        "catatan": parsed.get("catatan"),
        "kurir": parsed.get("kurir"),
    }

    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()

    try:
        orders = await asyncio.wait_for(
            asyncio.to_thread(sheets.add_order_rows, order, minggu_po, parsed.get("box_groups")), timeout=30
        )
    except asyncio.TimeoutError:
        await query.message.reply_text(
            "Timeout — gagal simpan ke Sheets (lebih dari 30 detik). "
            "Cek koneksi Google Sheets, lalu kirim ulang chat order-nya."
        )
        saving_flags.discard(order_id)
        return
    except Exception as e:
        await query.message.reply_text(f"Gagal simpan ke Sheets: {e}\nKirim ulang chat-nya ya.")
        saving_flags.discard(order_id)
        return

    await query.message.reply_text("Tersimpan!")

    harga_kosong = sorted(set(
        o["Rasa"] for o in orders if int(o.get("Harga_Satuan", 0)) == 0
    ))
    if harga_kosong:
        await query.message.reply_text(
            "⚠️ PERHATIAN: harga Rp0 untuk " + ", ".join(harga_kosong) + ". "
            "Kemungkinan nama rasa/kategori itu nggak ketemu persis di PriceList. "
            "Cek & benerin manual di Google Sheets ya."
        )

    # Invoice & surat jalan DIPISAH ke 2 grup TUJUAN beda (admin invoice /
    # admin surat jalan) kalau udah di-setting -- biar nggak numplek di 1
    # tempat sama pesan status ("Menyimpan...", "Tersimpan!") yang tetep di
    # sini (chat/grup tempat order-nya diproses). box_groups (rincian
    # pembagian per box, kalau order-nya pakai satuan box) ikut dioper ke
    # invoice & surat jalan biar ada section tambahan "Rincian Box" -- cuma
    # tersedia SEKALI di titik konfirmasi ini (nggak kesimpen ke Sheets),
    # jadi kalau invoice/surat jalan ini di-generate ULANG lewat /invoice
    # atau /suratjalan nanti, rincian box-nya nggak ikut muncul lagi.
    invoice_img = invoice_image.generate_invoice_image(
        order["nama"], minggu_po, orders, box_groups=parsed.get("box_groups")
    )
    await _kirim_foto_ke(context, update, _tujuan_invoice(update), invoice_img, "Invoice (siap kirim ke customer)")

    img = receipt.generate_surat_jalan_image(
        order["nama"], minggu_po, orders, box_groups=parsed.get("box_groups")
    )
    await _kirim_foto_ke(
        context, update, _tujuan_suratjalan(update), img,
        "Surat jalan (siap print) — tap gambar → Share → app printer",
    )

    _clear_pending_order_by_id(context, order_id)
    saving_flags.discard(order_id)


async def handle_set_ongkir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipicu tombol '✏️ Isi/Ubah Ongkir' di preview order (khusus metode
    Diantar). Copot tombol di pesan lama (biar nggak kepencet Confirm yang
    ongkirnya masih 0), lalu minta admin ketik nominalnya."""
    query = update.callback_query
    await query.answer()

    # callback_data formatnya "set_ongkir:<id>" -- liat catatan di
    # handle_confirm soal kenapa order_id nempel di tombol.
    _, _, order_id = query.data.partition(":")

    parsed = _get_pending_order_by_id(context, order_id)
    if not parsed:
        await query.message.reply_text("Nggak ada order yang lagi diproses.")
        return

    # Disimpen per-admin (user_data) SENGAJA -- ini cuma nunggu 1 langkah
    # balasan angka dari admin yang SAMA yang barusan mijit tombolnya,
    # beda konsepnya sama pending_orders yang emang perlu dibagi ke semua
    # admin/HP. order_id ikut disimpen di sini juga, jadi angka yang diketik
    # abis ini pasti nempel ke order yang BENER (bukan order lain yang
    # kebetulan lagi 'aktif').
    context.user_data["awaiting_ongkir_for"] = order_id
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text("Ketik nominal ongkirnya ya (angka aja, contoh: 15000).")


async def handle_ongkir_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nerima balasan admin abis klik 'Isi/Ubah Ongkir', update pending_order
    yang ada, lalu kirim ulang preview + tombol Simpan/Batal (Confirm/Cancel
    yang sebenernya, handle_confirm() nggak diubah sama sekali)."""
    raw = update.message.text.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())

    if not digits:
        await update.message.reply_text("Formatnya angka aja ya, contoh: 15000. Coba ketik lagi.")
        return

    order_id = context.user_data.pop("awaiting_ongkir_for", None)

    parsed = _get_pending_order_by_id(context, order_id)
    if not parsed:
        await update.message.reply_text(
            "Order-nya udah nggak ada / expired. Minta customer kirim ulang, atau paste ulang chat-nya."
        )
        return

    parsed["ongkir"] = int(digits)
    _store_pending_order(context, parsed, order_id)

    items_text = "\n".join(
        f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
        for i in parsed.get("items", [])
    ) or "  (belum ada item terdeteksi)"
    ongkir_rupiah = "Rp" + format(parsed["ongkir"], ",").replace(",", ".")

    preview = (
        f"*Order (ongkir sudah diisi):*\n"
        f"Nama: {parsed.get('nama') or '-'}\n"
        f"No HP: {parsed.get('no_hp') or '-'}\n"
        f"Alamat: {parsed.get('alamat') or '-'}\n"
        f"Metode: {parsed.get('metode') or '-'}\n"
        f"{_format_kurir_line(parsed)}"
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {ongkir_rupiah}\n"
        f"{_format_catatan_block(parsed)}"
    )

    keyboard = build_confirm_keyboard(parsed, order_id)
    await _send_or_update_preview(context, update, parsed, order_id, preview, keyboard)


_KURIR_CLEAR_KEYWORDS = ("armada", "hapus", "batal", "kosong", "gak jadi", "tidak jadi", "sendiri")


async def handle_set_kurir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipicu tombol '🚚 Isi/Ubah Kurir' di preview order (khusus metode
    Kirim/Diantar). Sama polanya kayak handle_set_ongkir -- copot tombol di
    pesan lama (biar nggak kepencet Confirm sebelum kurir-nya jelas), lalu
    minta admin ketik nama ekspedisinya."""
    query = update.callback_query
    await query.answer()

    _, _, order_id = query.data.partition(":")

    parsed = _get_pending_order_by_id(context, order_id)
    if not parsed:
        await query.message.reply_text("Nggak ada order yang lagi diproses.")
        return

    context.user_data["awaiting_kurir_for"] = order_id
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    kurir_sekarang = parsed.get("kurir")
    hint_sekarang = f" (sekarang: {kurir_sekarang})" if kurir_sekarang else ""
    await query.message.reply_text(
        f"Ketik nama ekspedisinya ya (misal JNE, Paxel, J&T, SiCepat){hint_sekarang}.\n"
        "Kalau ternyata dikirim pake armada/kurir toko sendiri (bukan "
        "ekspedisi), ketik 'armada' atau 'hapus'."
    )


async def handle_kurir_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nerima balasan admin abis klik 'Isi/Ubah Kurir', update pending_order
    yang ada, lalu kirim ulang preview + tombol Simpan/Batal (sama polanya
    kayak handle_ongkir_input)."""
    raw = update.message.text.strip()
    order_id = context.user_data.pop("awaiting_kurir_for", None)

    parsed = _get_pending_order_by_id(context, order_id)
    if not parsed:
        await update.message.reply_text(
            "Order-nya udah nggak ada / expired. Minta customer kirim ulang, atau paste ulang chat-nya."
        )
        return

    if raw.lower().strip() in _KURIR_CLEAR_KEYWORDS:
        parsed["kurir"] = None
    else:
        parsed["kurir"] = raw

    keyboard = build_confirm_keyboard(parsed, order_id)
    preview = _build_new_order_preview_text(
        parsed, "Order (kurir sudah diisi):" if parsed.get("kurir") else "Order (armada sendiri):"
    )
    await _send_or_update_preview(context, update, parsed, order_id, preview, keyboard)


async def handle_pending_correction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     parsed: dict, order_id: str):
    """Order masih di tahap preview (belum diklik Konfirmasi/Batal). Admin
    ngetik koreksi bebas -- misal 'yang apple salah, tolong hapus' atau
    'harusnya charsiu bukan cranberry' -- dan kita pakai ULANG mesin AI edit
    yang sama kayak /edit (parse_order_edit), cuma target-nya items yang ada
    di PREVIEW ini, bukan yang udah kesimpen di Sheets."""
    instruction = update.message.text

    # Cek dulu apa ini koreksi DATA (No HP/Nama/Alamat/Metode/Tanggal Kirim)
    # -- kalau iya, apply langsung tanpa manggil AI item-parser sama sekali.
    field_correction = _try_parse_field_correction(instruction)
    if field_correction:
        field, new_value = field_correction
        parsed[field] = new_value
        _store_pending_order(context, parsed, order_id)

        field_label = {
            "no_hp": "No HP", "nama": "Nama", "alamat": "Alamat",
            "metode": "Metode", "tanggal_kirim": "Tanggal Kirim",
            "catatan": "Catatan", "kurir": "Kurir",
        }[field]
        items_text = "\n".join(
            f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
            for i in parsed.get("items", [])
        ) or "  (belum ada item terdeteksi)"
        ongkir_val = int(parsed.get("ongkir") or 0)
        ongkir_text = ("Rp" + format(ongkir_val, ",").replace(",", ".")) if ongkir_val else "belum diisi (Rp0)"
        tanggal_kirim_text = parsed.get("tanggal_kirim") or "(default: Kamis PO minggu ini)"
        # catatan bisa sengaja dikosongin (new_value None) -- tampilin
        # "(dikosongin)" alih-alih literal "None".
        display_value = new_value if new_value is not None else "(dikosongin)"

        preview = (
            f"*{field_label} diganti jadi:* {display_value}\n\n"
            f"Nama: {parsed.get('nama') or '-'}\n"
            f"No HP: {parsed.get('no_hp') or '-'}\n"
            f"Alamat: {parsed.get('alamat') or '-'}\n"
            f"Metode: {parsed.get('metode') or '-'}\n"
            f"{_format_kurir_line(parsed)}"
            f"Tanggal Kirim: {tanggal_kirim_text}\n"
            f"Items:\n{items_text}\n"
            f"Ongkir: {ongkir_text}\n"
            f"{_format_catatan_block(parsed)}"
        )
        keyboard = build_confirm_keyboard(parsed, order_id)
        await _send_or_update_preview(context, update, parsed, order_id, preview, keyboard)
        return

    await update.message.reply_text("Oke, ngitung ulang preview-nya...")

    sheets = get_sheets_client()
    try:
        catalog = await asyncio.wait_for(asyncio.to_thread(sheets.get_catalog_list), timeout=15)
    except Exception:
        catalog = None

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(parse_order_edit, parsed.get("items", []), instruction, catalog),
            timeout=40,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "Timeout — proses ngitung ulang kelamaan. Coba kirim ulang koreksinya."
        )
        return
    except Exception as e:
        await update.message.reply_text(f"Ada error: {e}\nCoba kirim ulang koreksinya.")
        return

    parsed["items"] = result.get("items", [])
    if result.get("ongkir") is not None:
        parsed["ongkir"] = int(result.get("ongkir"))
    # BUG NYATA yang ini benerin: "box_groups" (rincian "2 box: Baso x4,
    # Cream Cheese x4" dst yang ditampilin di invoice & surat jalan) DIISI
    # SEKALI pas parse AWAL doang, dan parse_order_edit yang dipanggil di
    # atas SAMA SEKALI nggak tau-menau soal box_groups -- dia cuma dikasih
    # "items" (yang udah diratain/di-total) sebagai konteks, dan hasilnya
    # cuma "items" versi baru, bukan box_groups versi baru. Jadi kalau abis
    # ini admin ngoreksi salah satu item yang KEBETULAN bagian dari sebuah
    # box (misal ngoreksi nama rasa "Cream Cheese" jadi "Mocha Meises"),
    # "items" (dipakai buat total & Sheets) udah bener, TAPI "box_groups"
    # (dipakai KHUSUS buat tampilan rincian box) masih nyimpen nama/qty
    # LAMA -- makanya invoice & surat jalan sempet nampilin "Mocha Meises
    # x8" di daftar item tapi "Cream Cheese x4" di rincian box, padahal
    # maksudnya barang yang SAMA. Daripada beresiko nampilin data box yang
    # UDAH USANG (nama/qty salah), box_groups DIBUANG abis ada koreksi item
    # apa pun -- surat jalan/invoice balik nampilin per-kategori biasa
    # (sumbernya "items" yang udah pasti akurat), bukan rincian per-box lagi.
    box_groups_dibuang = bool(parsed.get("box_groups"))
    if box_groups_dibuang:
        parsed["box_groups"] = []
    # PENTING -- BUG lama yang barusan diperbaiki: result["catatan"] di sini
    # itu RINGKASAN PERUBAHAN dari AI edit-item (lihat EDIT_SYSTEM_PROMPT_BASE
    # di ai_parser.py, field "catatan"-nya didefinisikan sebagai "ringkasan
    # perubahan yang dilakukan"), BUKAN catatan packing bebas dari customer
    # (field "catatan" YANG SAMA NAMANYA di dict `parsed`, tapi asalnya dari
    # PARSE_SYSTEM_PROMPT_BASE, artinya beda total -- misal "donat & gula
    # dipisah pas packing"). Kode lama nimpa parsed["catatan"] pakai
    # ringkasan AI ini TIAP KALI ada koreksi item apa pun (bukan cuma
    # koreksi soal catatan) -- itu sebabnya catatan packing yang udah
    # ditulis admin ("catatan untuk donat gula dipisah") ilang ketiban teks
    # kayak "Catatan hapus" begitu admin ngoreksi item lain. FIX: catatan
    # packing (parsed["catatan"]) SAMA SEKALI nggak disentuh di sini lagi --
    # cuma bisa diubah lewat koreksi "catatan ..." (_try_parse_field_correction)
    # atau pas order awal diparse. Ringkasan AI cuma ditampilin SEKALI
    # sebagai info tambahan (misal peringatan nama rasa ambigu), nggak
    # disimpen ke mana-mana.
    ai_edit_summary = result.get("catatan")
    if ai_edit_summary and ai_edit_summary != "-":
        await update.message.reply_text(f"ℹ️ {ai_edit_summary}")
    if box_groups_dibuang:
        await update.message.reply_text(
            "ℹ️ Rincian per-box order ini kereset gara-gara ada koreksi item -- "
            "invoice/surat jalan bakal nampilin per-kategori biasa (total udah "
            "tetap akurat, cuma nggak dikelompokin per box lagi)."
        )

    items_text = "\n".join(
        f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
        for i in parsed.get("items", [])
    ) or "  (kosong -- semua item kehapus)"
    ongkir_val = int(parsed.get("ongkir") or 0)
    ongkir_text = ("Rp" + format(ongkir_val, ",").replace(",", ".")) if ongkir_val else "belum diisi (Rp0)"

    preview = (
        f"*Preview (sudah dikoreksi):*\n"
        f"Nama: {parsed.get('nama') or '-'}\n"
        f"No HP: {parsed.get('no_hp') or '-'}\n"
        f"Alamat: {parsed.get('alamat') or '-'}\n"
        f"Metode: {parsed.get('metode') or '-'}\n"
        f"{_format_kurir_line(parsed)}"
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {ongkir_text}\n"
        f"{_format_catatan_block(parsed)}"
    )
    keyboard = build_confirm_keyboard(parsed, order_id)
    await _send_or_update_preview(context, update, parsed, order_id, preview, keyboard)


# ---------------- EDIT ORDER ----------------

def _extract_after_splitter(lower_text, original_text):
    """Ambil teks SETELAH kata sambung kayak 'jadi'/'ganti ke', dari akhir
    kalimat -- biar 'nama nya salah, ganti jadi Budi Santoso' ngambil
    'Budi Santoso' doang."""
    for splitter in (" jadi ", " ganti ke ", " menjadi ", " ke ", " di ", " adalah "):
        idx = lower_text.rfind(splitter)
        if idx != -1:
            val = original_text[idx + len(splitter):].strip(" .,!")
            if val:
                return val
    return None


_BULAN_ID = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11,
    "desember": 12,
}


def _parse_tanggal_kirim(text):
    """Parse teks tanggal kirim bebas jadi string 'YYYY-MM-DD', atau None
    kalau nggak ketemu pola yang dikenal. Support: 'besok', 'lusa', tanggal
    eksplisit 'DD/MM' atau 'DD/MM/YYYY', atau 'DD <nama bulan>' / 'DD <nama
    bulan> YYYY'. Kalau tahun nggak disebut & tanggalnya udah lewat buat
    tahun ini, dianggap tahun depan (jaga-jaga order lintas tahun baru)."""
    lower = text.lower()
    tz = date_helpers.get_timezone()
    today = datetime.datetime.now(tz).date()

    if "hari ini" in lower or lower.strip() in ("sekarang", "hari ini juga"):
        return today.strftime("%Y-%m-%d")
    if "besok" in lower:
        return (today + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    if "lusa" in lower:
        return (today + datetime.timedelta(days=2)).strftime("%Y-%m-%d")

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?\b", text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = datetime.date(year, month, day)
            if not m.group(3) and d < today:
                d = datetime.date(year + 1, month, day)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})\s+(" + "|".join(_BULAN_ID.keys()) + r")\b(?:\s+(\d{4}))?", lower)
    if m:
        day = int(m.group(1))
        month = _BULAN_ID[m.group(2)]
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            d = datetime.date(year, month, day)
            if not m.group(3) and d < today:
                d = datetime.date(year + 1, month, day)
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


def _try_parse_field_correction(instruction):
    """Coba deteksi instruksi koreksi DATA customer (No HP/Alamat/Nama/
    Metode/Tanggal Kirim/Catatan) langsung dari kata kunci, TANPA lewat AI
    -- biar reliable buat hal krusial kayak nomor HP (nggak digantung
    interpretasi model bahasa), DAN biar catatan packing (field "catatan")
    nggak pernah kesasar ke alur edit-item AI yang bisa nimpa isinya
    (liat komentar di blok "catatan" di bawah).
    Return (field, value_baru) kalau ketemu SATU koreksi yang jelas, atau
    None kalau nggak yakin -- yang berarti fallback ke alur edit ITEM (AI)
    yang udah ada, jadi kapabilitas lama nggak keganggu sama sekali.

    Catatan: 1 pesan = 1 koreksi field. Mau ubah lebih dari 1 field, tinggal
    kirim beberapa pesan berurutan (masih dalam mode /edit yang sama)."""
    text = instruction.strip()
    lower = text.lower()

    # No HP -- paling gampang & paling penting buat dideteksi akurat.
    if any(k in lower for k in ("no hp", "nomor hp", "no telp", "nomor telp", "no wa", "nomor wa")):
        digits = re.findall(r"\d[\d\-\s]{7,14}\d", text)
        if digits:
            new_hp = re.sub(r"[^\d]", "", digits[-1])
            if len(new_hp) >= 8:
                return "no_hp", new_hp

    # Catatan (catatan packing bebas, misal "donat & gula dipisah") --
    # SENGAJA dicek SEBELUM alamat/nama, soalnya isi catatannya sendiri
    # sering nyebut kata "alamat"/"nama" (misal "catatan: alamat sementara
    # pindah dulu"), yang kalau nggak keburu ke-tangkep di sini bakal salah
    # kesedot jadi koreksi alamat/nama padahal maksudnya cuma catatan
    # packing. Dulu instruksi kayak ini ("catatan untuk donat gula
    # dipisah") nggak dikenalin field koreksi apa pun sama sekali, jadi
    # nyasar ke alur edit-item AI (parse_order_edit) yang ujung-ujungnya
    # malah nimpa catatan pakai ringkasan perubahan AI -- liat catatan di
    # handle_pending_correction soal bug itu.
    if "catatan" in lower:
        if any(k in lower for k in ("hapus", "kosong", "kosongin", "hilangin", "buang")):
            return "catatan", None
        val = _extract_after_splitter(lower, text)
        if not val:
            # _extract_after_splitter cuma nangkep splitter umum kayak
            # 'jadi'/'ke'/'di' -- kalimat catatan sering nggak pakai itu
            # ("catatan untuk donat gula dipisah", "catatan: ..."), jadi
            # coba juga ambil semua teks SETELAH kata "catatan" itu sendiri.
            idx = lower.rfind("catatan")
            after = text[idx + len("catatan"):].strip(" :.-")
            for prefix in ("untuk ", "buat ", "nya "):
                if after.lower().startswith(prefix):
                    after = after[len(prefix):].strip()
                    break
            val = after or None
        if val:
            return "catatan", val

    if "alamat" in lower:
        val = _extract_after_splitter(lower, text)
        if val:
            return "alamat", val

    if re.search(r"\bnama\b", lower):
        val = _extract_after_splitter(lower, text)
        if val:
            return "nama", val

    if "metode" in lower:
        if "antar" in lower or "kirim" in lower:
            return "metode", "Diantar"
        if "ambil" in lower:
            return "metode", "Ambil sendiri"

    # Kurir/ekspedisi (misal "kurir jne", "kirim pake paxel", "ekspedisi:
    # j&t") -- pola sama kayak catatan: dicek dari kata kunci, biar admin
    # bisa isi/ubah kurir lewat ketik bebas juga (BUKAN cuma lewat tombol
    # 'Isi Kurir'). Sama _KURIR_CLEAR_KEYWORDS yang dipake handle_kurir_input
    # buat ngenalin "balikin ke armada sendiri".
    if "kurir" in lower or "ekspedisi" in lower:
        if any(k in lower for k in _KURIR_CLEAR_KEYWORDS):
            return "kurir", None
        val = _extract_after_splitter(lower, text)
        if not val:
            for kata in ("ekspedisi", "kurir"):
                idx = lower.rfind(kata)
                if idx == -1:
                    continue
                after = text[idx + len(kata):].strip(" :.-")
                for prefix in ("nya ", "pake ", "pakai ", "jadi "):
                    if after.lower().startswith(prefix):
                        after = after[len(prefix):].strip()
                        break
                if after:
                    val = after
                    break
        if val:
            return "kurir", val

    # Tanggal kirim custom (default-nya tetep Kamis PO minggu berjalan kalau
    # nggak pernah disebut sama sekali). Nggak wajib kata "tanggal" -- admin
    # sering cuma bilang "buat besok" doang -- tapi ini SENGAJA dicek PALING
    # TERAKHIR (semua field lain udah dicoba duluan) biar "besok" yang
    # nyempil di kalimat lain nggak salah kesedot jadi koreksi tanggal.
    if ("tanggal" in lower and "kirim" in lower) or "tgl kirim" in lower \
            or "besok" in lower or "lusa" in lower or "hari ini" in lower:
        tanggal = _parse_tanggal_kirim(text)
        if tanggal:
            return "tanggal_kirim", tanggal

    return None

async def handle_edit_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    instruction_override: str = None):
    editing = context.user_data["editing_order"]
    instruction = instruction_override or update.message.text

    # Cek dulu apa ini koreksi DATA customer (No HP/Nama/Alamat/Metode) --
    # kalau iya, langsung apply & minta konfirmasi, item-nya nggak disentuh
    # sama sekali (nggak perlu manggil AI item-parser buat ini).
    field_correction = _try_parse_field_correction(instruction)
    if field_correction:
        field, new_value = field_correction
        editing[field] = new_value

        field_label = {
            "no_hp": "No HP", "nama": "Nama", "alamat": "Alamat",
            "metode": "Metode", "tanggal_kirim": "Tanggal Kirim",
            "catatan": "Catatan", "kurir": "Kurir",
        }[field]
        item_list_text = "\n".join(
            f"  - {i['rasa']} ({i['kategori']}) x{i['qty']}" for i in editing["existing_items"]
        ) or "  (kosong)"
        tanggal_kirim_now = editing.get("tanggal_kirim") or editing["minggu_po"]
        # catatan bisa sengaja dikosongin (new_value None) -- tampilin
        # "(dikosongin)" alih-alih literal "None".
        display_value = new_value if new_value is not None else "(dikosongin)"

        preview = (
            f"*{field_label} diganti jadi:* {display_value}\n\n"
            f"*Data order {editing['nama']} sekarang:*\n"
            f"Nama: {editing['nama']}\n"
            f"No HP: {editing['no_hp']}\n"
            f"Alamat: {editing['alamat']}\n"
            f"Metode: {editing['metode']}\n"
            f"{_format_kurir_line(editing)}"
            f"Tanggal Kirim: {tanggal_kirim_now}\n"
            f"Catatan: {editing.get('catatan') or '-'}\n"
            f"Items (nggak berubah):\n{item_list_text}\n\n"
            f"Item pesanan nggak ikut berubah. Mau koreksi field lain juga? "
            f"Ketik lagi sebelum konfirmasi. Kalau udah bener, klik Konfirmasi."
        )

        context.user_data["pending_edit"] = {
            "nama": editing["nama"],
            "original_nama": editing.get("original_nama", editing["nama"]),
            "no_hp": editing["no_hp"],
            "alamat": editing["alamat"],
            "metode": editing["metode"],
            "items": editing["existing_items"],
            "ongkir": editing["ongkir"],
            "minggu_po": editing["minggu_po"],
            "tanggal_kirim": tanggal_kirim_now,
            "catatan": editing.get("catatan"),
            "kurir": editing.get("kurir"),
        }

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Konfirmasi", callback_data="confirm_edit"),
            InlineKeyboardButton("❌ Batal", callback_data="cancel_edit"),
        ]])
        await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)
        return

    await update.message.reply_text("Menghitung ulang order...")

    sheets = get_sheets_client()
    try:
        catalog = await asyncio.wait_for(asyncio.to_thread(sheets.get_catalog_list), timeout=15)
    except Exception:
        catalog = None

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(parse_order_edit, editing["existing_items"], instruction, catalog),
            timeout=40,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            "Timeout — proses ngitung ulang kelamaan. Coba kirim ulang instruksinya."
        )
        return
    except Exception as e:
        await update.message.reply_text(f"Ada error: {e}\nCoba kirim ulang instruksinya.")
        return

    new_items = result.get("items", [])
    # PENTING: result["catatan"] itu RINGKASAN PERUBAHAN dari AI (lihat
    # EDIT_SYSTEM_PROMPT_BASE di ai_parser.py) -- BUKAN catatan packing
    # bebas dari customer (field "catatan" yang beda arti di editing/
    # pending_edit, misal "donat & gula dipisah"). Ditampilin di preview
    # cuma sebagai info ringkasan/peringatan ambigu dari AI, dan LABELNYA
    # sengaja dibedain ("Ringkasan AI") biar admin nggak ngira ini catatan
    # packing asli -- catatan packing asli-nya sendiri (editing["catatan"])
    # nggak disentuh sama sekali di sini.
    ai_edit_summary = result.get("catatan") or "-"
    # Kalau admin sebut ongkir baru di instruksinya, pakai itu. Kalau nggak
    # disebut sama sekali, PERTAHANKAN ongkir lama (jangan direset ke 0).
    ongkir_final = result.get("ongkir")
    ongkir_final = int(ongkir_final) if ongkir_final is not None else editing["ongkir"]

    old_text = "\n".join(
        f"  - {i['rasa']} ({i['kategori']}) x{i['qty']}" for i in editing["existing_items"]
    ) or "  (kosong)"
    new_text = "\n".join(
        f"  - {i['rasa']} ({i['kategori']}) x{i['qty']}" for i in new_items
    ) or "  (kosong)"

    ongkir_rupiah = "Rp" + format(ongkir_final, ",").replace(",", ".")

    # Item KOSONG artinya customer batal total (semua item dihapus lewat
    # instruksi, misal "batalin aja semua"), bukan sekadar edit biasa. Kasih
    # peringatan & label tombol yang beda biar jelas ini pembatalan.
    is_cancel = len(new_items) == 0

    preview = (
        f"*Order Lama:*\n{old_text}\n\n"
        f"*Order Baru (setelah diedit):*\n{new_text}\n\n"
        f"Ongkir: {ongkir_rupiah}\n"
        f"{_format_kurir_line(editing)}"
        f"Catatan (packing): {editing.get('catatan') or '-'}\n"
        f"Ringkasan AI: {ai_edit_summary}\n\n"
        + (
            "⚠️ Item KOSONG — kalau dikonfirmasi, order ini bakal DIBATALIN TOTAL "
            "(dihapus dari Sheets, TANPA invoice/surat jalan baru)."
            if is_cancel else
            "⚠️ Kalau dikonfirmasi, order lama bakal DIHAPUS total dan diganti yang baru ini."
        )
    )

    context.user_data["pending_edit"] = {
        "nama": editing["nama"],
        "original_nama": editing.get("original_nama", editing["nama"]),
        "no_hp": editing["no_hp"],
        "alamat": editing["alamat"],
        "metode": editing["metode"],
        "items": new_items,
        "ongkir": ongkir_final,
        "minggu_po": editing["minggu_po"],
        "tanggal_kirim": editing.get("tanggal_kirim"),
        "catatan": editing.get("catatan"),
        "kurir": editing.get("kurir"),
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Ya, batalin order" if is_cancel else "✅ Konfirmasi Edit",
            callback_data="confirm_edit",
        ),
        InlineKeyboardButton("❌ Batal", callback_data="cancel_edit"),
    ]])
    await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)


async def handle_edit_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_edit":
        context.user_data.pop("editing_order", None)
        context.user_data.pop("pending_edit", None)
        await query.edit_message_text("Edit dibatalin, order lama nggak berubah.")
        return

    if context.user_data.get("saving_in_progress"):
        return
    context.user_data["saving_in_progress"] = True

    pending = context.user_data.get("pending_edit")
    if not pending:
        await query.edit_message_text("Data edit nggak ketemu, coba /edit lagi.")
        context.user_data["saving_in_progress"] = False
        return

    await query.edit_message_text("Menyimpan perubahan...")

    sheets = get_sheets_client()
    nama = pending["nama"]
    old_nama = pending.get("original_nama", nama)
    minggu_po = pending["minggu_po"]

    if not pending["items"]:
        # Semua item dihapus lewat instruksi -> ini PEMBATALAN TOTAL, bukan
        # edit biasa. Cukup hapus baris di Sheets, JANGAN ditulis ulang
        # (kosong) dan JANGAN generate invoice/surat jalan (nggak ada apa-apa
        # buat dikirim ke customer yang udah batal).
        try:
            await asyncio.wait_for(
                asyncio.to_thread(sheets.delete_customer_week_rows, old_nama, minggu_po), timeout=30
            )
        except asyncio.TimeoutError:
            await query.message.reply_text(
                "Timeout pas ngehapus order. Cek manual di Google Sheets ya."
            )
            context.user_data["saving_in_progress"] = False
            return
        except Exception as e:
            await query.message.reply_text(f"Gagal batalin order: {e}\nCek manual di Sheets ya.")
            context.user_data["saving_in_progress"] = False
            return

        await query.message.reply_text(
            f"Order {nama} berhasil DIBATALIN & dihapus dari Sheets minggu ini."
        )
        context.user_data.pop("editing_order", None)
        context.user_data.pop("pending_edit", None)
        context.user_data["saving_in_progress"] = False
        return

    try:
        orders = await asyncio.wait_for(
            asyncio.to_thread(_rewrite_customer_order, sheets, pending), timeout=30
        )
    except asyncio.TimeoutError:
        await query.message.reply_text(
            "Timeout pas nyimpen perubahan. PENTING: cek manual di Google Sheets, "
            "soalnya order lama mungkin udah kehapus tapi yang baru belum sempet ditulis. "
            "Kalau perlu, input ulang manual dulu."
        )
        context.user_data["saving_in_progress"] = False
        return
    except Exception as e:
        await query.message.reply_text(f"Gagal simpan perubahan: {e}\nCek manual di Sheets ya.")
        context.user_data["saving_in_progress"] = False
        return

    await query.message.reply_text("Order berhasil diupdate!")

    harga_kosong = sorted(set(
        o["Rasa"] for o in orders if int(o.get("Harga_Satuan", 0)) == 0
    ))
    if harga_kosong:
        await query.message.reply_text(
            "⚠️ PERHATIAN: harga Rp0 untuk " + ", ".join(harga_kosong) + ". "
            "Kemungkinan nama rasa/kategori itu nggak ketemu persis di PriceList. "
            "Cek & benerin manual di Google Sheets ya."
        )

    invoice_img = invoice_image.generate_invoice_image(nama, minggu_po, orders)
    await _kirim_foto_ke(
        context, update, _tujuan_invoice(update), invoice_img, "Invoice terbaru (siap kirim ke customer)"
    )

    img = receipt.generate_surat_jalan_image(nama, minggu_po, orders)
    await _kirim_foto_ke(
        context, update, _tujuan_suratjalan(update), img,
        "Surat jalan terbaru (siap print) — tap gambar → Share → app printer",
    )

    context.user_data.pop("editing_order", None)
    context.user_data.pop("pending_edit", None)
    context.user_data["saving_in_progress"] = False


def _rewrite_customer_order(sheets, pending):
    """Hapus baris LAMA di Sheets (pakai nama ASLI sebelum diedit, bukan
    nama baru kalau kebetulan admin lagi ganti nama juga -- kalau pakai nama
    BARU buat nyari baris lama, baris lama nggak ketemu/nggak kehapus dan
    malah numpuk dobel, itu yang kejadian pas nama diganti lewat /edit),
    lalu tulis ulang order dengan data TERBARU (termasuk nama baru kalau
    ada). Dipanggil di thread terpisah dari handle_edit_confirm biar nggak
    blocking event loop bot."""
    sheets.delete_customer_week_rows(pending.get("original_nama", pending["nama"]), pending["minggu_po"])
    order = {
        "nama": pending["nama"],
        "no_hp": pending["no_hp"],
        "alamat": pending["alamat"],
        "metode": pending["metode"],
        "items": pending["items"],
        "ongkir": pending.get("ongkir", 0),
        "tanggal_kirim": pending.get("tanggal_kirim"),
        "catatan": pending.get("catatan"),
        "kurir": pending.get("kurir"),
    }
    return sheets.add_order_rows(order, pending["minggu_po"])


async def on_startup(app: Application):
    # Start scheduler di dalam event loop yang sudah jalan (lebih stabil
    # daripada start sebelum run_polling dipanggil).
    setup_scheduler(app.bot)
    logger.info("Scheduler auto-recap & laporan bulanan aktif.")

    # Server kecil buat nerima order dari halaman web (Netlify) dan
    # nerusinnya ke alur konfirmasi Telegram yang udah ada (handle_confirm
    # di bawah, nggak diubah sama sekali).
    from web_order_server import start_web_order_server
    app.create_task(start_web_order_server(app))
    logger.info("Web order server dijadwalkan buat start.")


def main():
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("groupid", groupid_cmd))
    app.add_handler(CommandHandler("pricelist", pricelist))
    app.add_handler(CommandHandler("rekap", rekap))
    app.add_handler(CommandHandler("invoice", invoice_cmd))
    app.add_handler(CommandHandler("suratjalan", suratjalan_cmd))
    app.add_handler(CommandHandler("laporanbulanan", laporanbulanan_cmd))
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CommandHandler("kirim", kirim_cmd))
    app.add_handler(CommandHandler("gabung", gabung_cmd))
    # Pattern-nya "^(confirm_order|cancel_order):" (BUKAN "$" persis lagi) --
    # soalnya callback_data sekarang bawa order_id juga, misal
    # "confirm_order:a1b2c3d4", biar tombol tetep bener walau ada beberapa
    # order numpuk nunggu diproses bareng (liat build_confirm_keyboard).
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern="^(confirm_order|cancel_order):"))
    app.add_handler(CallbackQueryHandler(handle_set_ongkir, pattern="^set_ongkir:"))
    app.add_handler(CallbackQueryHandler(handle_set_kurir, pattern="^set_kurir:"))
    app.add_handler(CallbackQueryHandler(handle_edit_confirm, pattern="^(confirm_edit|cancel_edit)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Bot jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
