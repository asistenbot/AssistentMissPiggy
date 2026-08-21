"""
Bot utama Miss Piggy PO Assistant.
Jalankan: python bot.py
"""

import asyncio
import logging
import datetime
import re

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
from sheets_client import get_sheets_client
from ai_parser import parse_customer_chat, parse_order_edit, classify_intent
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


def build_confirm_keyboard(parsed):
    """Tombol Simpan/Batal standar, ditambah tombol 'Isi/Ubah Ongkir' KHUSUS
    kalau metode-nya DIKIRIM (bukan ambil sendiri) -- biar admin bisa isi
    ongkir dulu SEBELUM invoice & surat jalan ke-generate (jadi nggak perlu
    /edit belakangan)."""
    rows = [[
        InlineKeyboardButton("✅ Simpan & Generate", callback_data="confirm_order"),
        InlineKeyboardButton("❌ Batal", callback_data="cancel_order"),
    ]]
    if _is_delivery_metode(parsed.get("metode")):
        rows.append([InlineKeyboardButton("✏️ Isi/Ubah Ongkir", callback_data="set_ongkir")])
    return InlineKeyboardMarkup(rows)


def _get_pending_order(context):
    """Ambil pending_order dari 2 kemungkinan tempat:
    - user_data: order dari alur paste-chat manual (cuma keliatan sama admin
      yang lagi ngobrol itu -- perilaku ASLI, nggak diubah).
    - bot_data: order dari WEB (Netlify). Ini sengaja disimpen di bot_data
      (bukan user_data per-admin) karena bot_data itu SATU tempat yang sama
      buat SEMUA admin/HP -- jadi siapa pun yang lebih dulu buka Telegram-nya
      bisa langsung proses, nggak peduli akun/HP mana (sesuai permintaan
      'tetep di grup, bisa dihandle pake 2 hp').
    Return (parsed_atau_None, dari_bot_data_bool)."""
    parsed = context.user_data.get("pending_order")
    if parsed:
        return parsed, False
    parsed = context.bot_data.get("pending_order")
    if parsed:
        return parsed, True
    return None, False


def _save_pending_order(context, parsed, from_bot_data):
    if from_bot_data:
        context.bot_data["pending_order"] = parsed
    else:
        context.user_data["pending_order"] = parsed


def _clear_pending_order(context):
    context.user_data.pop("pending_order", None)
    context.bot_data.pop("pending_order", None)


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
        "- /invoice Nama Customer — bikin ulang invoice\n"
        "- /suratjalan Nama Customer — bikin ulang surat jalan\n"
        "- /edit Nama Customer — edit order yang sudah ada (tambah/kurangi/hapus item)\n"
        "- /laporanbulanan — laporan bayar supplier bulan ini\n"
        "- /laporanbulanan 2026-07 — laporan bulan tertentu\n"
        "- /laporanbulanan 2026-07:2026-08 — laporan rentang beberapa bulan\n"
        "- /groupid — lihat ID chat ini (buat setup grup admin)\n\n"
        "Auto-recap produksi akan dikirim tiap Rabu jam 15:00, 16:00, dan 19:00 WIB."
    )


@owner_only
async def groupid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"ID chat ini: `{chat.id}`\n"
        f"Tipe: {chat.type}\n\n"
        "Kalau ini grup dan mau dipakai buat terima auto-recap, "
        "copy ID di atas (termasuk tanda minus kalau ada) ke GROUP_CHAT_ID di Railway.",
        parse_mode="Markdown",
    )


@owner_only
async def pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = get_sheets_client()
    text = sheets.get_pricelist_text()
    await update.message.reply_text(f"*PRICE LIST — {config.BUSINESS_NAME}*\n{text}",
                                     parse_mode="Markdown")


@owner_only
async def rekap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sheets = get_sheets_client()
        minggu_po = date_helpers.current_po_week_thursday()
        orders = await asyncio.wait_for(
            asyncio.to_thread(sheets.get_orders_by_week, minggu_po), timeout=20
        )
    except asyncio.TimeoutError:
        await update.message.reply_text("Timeout ambil data dari Sheets. Coba lagi.")
        return
    except Exception as e:
        await update.message.reply_text(f"Gagal ambil data rekap: {e}")
        return

    # Kirim sebagai pesan TERPISAH biar gampang di-forward tanpa crop:
    # 1) total produksi per rasa (buat baking)
    # 2) daftar yang dikirim kurir
    # 3) daftar yang diambil sendiri
    text_produksi = documents.build_production_recap(minggu_po, orders)
    await update.message.reply_text(text_produksi, parse_mode="Markdown")

    text_kirim = documents.build_delivery_kirim(minggu_po, orders)
    if text_kirim:
        await update.message.reply_text(text_kirim, parse_mode="Markdown")

    text_ambil = documents.build_delivery_ambil(minggu_po, orders)
    if text_ambil:
        await update.message.reply_text(text_ambil, parse_mode="Markdown")


@owner_only
async def invoice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /invoice Nama Customer")
        return
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_orders_by_customer_week(nama, minggu_po)
    if not orders:
        text = documents.build_invoice(nama, minggu_po, orders)
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    img = invoice_image.generate_invoice_image(nama, minggu_po, orders)
    await update.message.reply_photo(photo=img, caption="Invoice (siap kirim ke customer)")


@owner_only
async def suratjalan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /suratjalan Nama Customer")
        return
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_orders_by_customer_week(nama, minggu_po)
    if not orders:
        text = documents.build_surat_jalan(nama, minggu_po, orders)
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    img = receipt.generate_surat_jalan_image(nama, minggu_po, orders)
    await update.message.reply_photo(
        photo=img, caption="Surat jalan (siap print) — tap gambar → Share → app printer"
    )


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
    await update.message.reply_text(text, parse_mode="Markdown")

    month_results, grand_total_qty, grand_total_bayar = documents.aggregate_dough_by_month(orders, dough_price_map)
    if month_results:
        pdf_buf = monthly_report_pdf.generate_monthly_report_pdf(
            periode_label, month_results, grand_total_qty, grand_total_bayar
        )
        filename = f"Laporan_Bulanan_{year_start}{month_start:02d}-{year_end}{month_end:02d}.pdf"
        await update.message.reply_document(
            document=pdf_buf, filename=filename, caption="Versi PDF (siap print A4)"
        )


@owner_only
async def edit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /edit Nama Customer")
        return

    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_orders_by_customer_week(nama, minggu_po)

    if not orders:
        await update.message.reply_text(f"Nggak ada order atas nama {nama} untuk minggu ini.")
        return

    item_list_text = "\n".join(f"  - {o['Rasa']} ({o['Kategori']}) x{int(o['Qty'])}" for o in orders)

    context.user_data["editing_order"] = {
        "nama": orders[0].get("Nama_Customer", nama),
        "no_hp": orders[0].get("No_HP", "-"),
        "alamat": orders[0].get("Alamat", "-"),
        "metode": orders[0].get("Metode", "Ambil"),
        "existing_items": [
            {"kategori": o["Kategori"], "rasa": o["Rasa"], "qty": int(o["Qty"])} for o in orders
        ],
        "ongkir": int(orders[0].get("Ongkir", 0) or 0),
        "minggu_po": minggu_po,
        "tanggal_kirim": orders[0].get("Tanggal_Kirim") or minggu_po,
    }

    await update.message.reply_text(
        f"*Order {nama} saat ini:*\n{item_list_text}\n\n"
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
    if context.user_data.get("awaiting_ongkir"):
        await handle_ongkir_input(update, context)
        return

    # Kalau lagi dalam mode edit order (habis /edit Nama Customer atau baru
    # ke-detect mau edit), teks ini instruksi perubahan, BUKAN order baru.
    if context.user_data.get("editing_order"):
        await handle_edit_instruction(update, context)
        return

    # Kalau masih ada preview order yang nunggu Konfirmasi/Batal (belum
    # disimpen ke Sheets sama sekali), anggap chat berikutnya itu KOREKSI ke
    # preview itu -- misal "yang apple salah, tolong hapus" -- bukan order
    # baru. Kalau admin emang mau mulai order lain, klik dulu tombol Batal
    # di preview yang lagi jalan.
    pending_parsed, pending_from_bot_data = _get_pending_order(context)
    if pending_parsed:
        await handle_pending_correction(update, context, pending_parsed, pending_from_bot_data)
        return

    raw_text = update.message.text

    # Coba tebak dulu maksud admin (bahasa natural, nggak wajib pakai '/')
    try:
        intent_result = await asyncio.wait_for(
            asyncio.to_thread(classify_intent, raw_text), timeout=20
        )
    except Exception:
        intent_result = {"intent": "order_baru", "nama_customer": None, "bulan_mulai": None, "bulan_akhir": None, "instruksi_edit": None}

    intent = intent_result.get("intent", "order_baru")

    if intent == "rekap_produksi":
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
        orders = sheets.get_orders_by_customer_week(nama, minggu_po)

        if not orders:
            await update.message.reply_text(f"Nggak ada order atas nama {nama} untuk minggu ini.")
            return

        context.user_data["editing_order"] = {
            "nama": orders[0].get("Nama_Customer", nama),
            "no_hp": orders[0].get("No_HP", "-"),
            "alamat": orders[0].get("Alamat", "-"),
            "metode": orders[0].get("Metode", "Ambil"),
            "existing_items": [
                {"kategori": o["Kategori"], "rasa": o["Rasa"], "qty": int(o["Qty"])} for o in orders
            ],
            "ongkir": int(orders[0].get("Ongkir", 0) or 0),
            "minggu_po": minggu_po,
            "tanggal_kirim": orders[0].get("Tanggal_Kirim") or minggu_po,
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

    context.user_data["pending_order"] = parsed

    items_text = "\n".join(
        f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
        for i in parsed.get("items", [])
    ) or "  (belum ada item terdeteksi)"

    preview = (
        f"*Hasil Parse:*\n"
        f"Nama: {parsed.get('nama') or '-'}\n"
        f"No HP: {parsed.get('no_hp') or '-'}\n"
        f"Alamat: {parsed.get('alamat') or '-'}\n"
        f"Metode: {parsed.get('metode') or '-'}\n"
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini, ketik tanggal kirim jadi ... buat ubah)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {('Rp' + format(int(parsed.get('ongkir')), ',').replace(',', '.')) if parsed.get('ongkir') else 'belum diisi (Rp0)'}\n"
        f"Catatan: {parsed.get('catatan') or '-'}\n\n"
    )

    if parsed.get("kelengkapan") == "kurang_lengkap":
        preview += "⚠️ ADA YANG PERLU DICEK (lihat Catatan di atas) sebelum disimpan!\n\n"

    keyboard = build_confirm_keyboard(parsed)
    await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_order":
        _clear_pending_order(context)
        await query.edit_message_text("Dibatalin ya.")
        return

    # Cegah klik dobel: begitu diproses, langsung matiin tombol & kasih flag,
    # biar klik kedua (atau nyasar) nggak nyimpen data yang sama 2x.
    if context.user_data.get("saving_in_progress"):
        return
    context.user_data["saving_in_progress"] = True

    parsed, _ = _get_pending_order(context)
    if not parsed or not parsed.get("items"):
        await query.edit_message_text("Nggak ada data order yang tersimpan. Kirim ulang chat-nya ya.")
        context.user_data["saving_in_progress"] = False
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
    }

    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()

    try:
        orders = await asyncio.wait_for(
            asyncio.to_thread(sheets.add_order_rows, order, minggu_po), timeout=30
        )
    except asyncio.TimeoutError:
        await query.message.reply_text(
            "Timeout — gagal simpan ke Sheets (lebih dari 30 detik). "
            "Cek koneksi Google Sheets, lalu kirim ulang chat order-nya."
        )
        context.user_data["saving_in_progress"] = False
        return
    except Exception as e:
        await query.message.reply_text(f"Gagal simpan ke Sheets: {e}\nKirim ulang chat-nya ya.")
        context.user_data["saving_in_progress"] = False
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

    invoice_img = invoice_image.generate_invoice_image(order["nama"], minggu_po, orders)
    await query.message.reply_photo(photo=invoice_img, caption="Invoice (siap kirim ke customer)")

    img = receipt.generate_surat_jalan_image(order["nama"], minggu_po, orders)
    await query.message.reply_photo(
        photo=img, caption="Surat jalan (siap print) — tap gambar → Share → app printer"
    )

    _clear_pending_order(context)
    context.user_data["saving_in_progress"] = False


async def handle_set_ongkir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dipicu tombol '✏️ Isi/Ubah Ongkir' di preview order (khusus metode
    Diantar). Copot tombol di pesan lama (biar nggak kepencet Confirm yang
    ongkirnya masih 0), lalu minta admin ketik nominalnya."""
    query = update.callback_query
    await query.answer()

    parsed, _ = _get_pending_order(context)
    if not parsed:
        await query.message.reply_text("Nggak ada order yang lagi diproses.")
        return

    context.user_data["awaiting_ongkir"] = True
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

    context.user_data["awaiting_ongkir"] = False

    parsed, from_bot_data = _get_pending_order(context)
    if not parsed:
        await update.message.reply_text(
            "Order-nya udah nggak ada / expired. Minta customer kirim ulang, atau paste ulang chat-nya."
        )
        return

    parsed["ongkir"] = int(digits)
    _save_pending_order(context, parsed, from_bot_data)

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
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {ongkir_rupiah}\n"
        f"Catatan: {parsed.get('catatan') or '-'}\n"
    )

    keyboard = build_confirm_keyboard(parsed)
    await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)


async def handle_pending_correction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     parsed: dict, from_bot_data: bool):
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
        _save_pending_order(context, parsed, from_bot_data)

        field_label = {
            "no_hp": "No HP", "nama": "Nama", "alamat": "Alamat",
            "metode": "Metode", "tanggal_kirim": "Tanggal Kirim",
        }[field]
        items_text = "\n".join(
            f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
            for i in parsed.get("items", [])
        ) or "  (belum ada item terdeteksi)"
        ongkir_val = int(parsed.get("ongkir") or 0)
        ongkir_text = ("Rp" + format(ongkir_val, ",").replace(",", ".")) if ongkir_val else "belum diisi (Rp0)"
        tanggal_kirim_text = parsed.get("tanggal_kirim") or "(default: Kamis PO minggu ini)"

        preview = (
            f"*{field_label} diganti jadi:* {new_value}\n\n"
            f"Nama: {parsed.get('nama') or '-'}\n"
            f"No HP: {parsed.get('no_hp') or '-'}\n"
            f"Alamat: {parsed.get('alamat') or '-'}\n"
            f"Metode: {parsed.get('metode') or '-'}\n"
            f"Tanggal Kirim: {tanggal_kirim_text}\n"
            f"Items:\n{items_text}\n"
            f"Ongkir: {ongkir_text}\n"
            f"Catatan: {parsed.get('catatan') or '-'}\n"
        )
        keyboard = build_confirm_keyboard(parsed)
        await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)
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
    catatan_baru = result.get("catatan")
    if catatan_baru and catatan_baru != "-":
        parsed["catatan"] = catatan_baru

    _save_pending_order(context, parsed, from_bot_data)

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
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {ongkir_text}\n"
        f"Catatan: {parsed.get('catatan') or '-'}\n"
    )
    keyboard = build_confirm_keyboard(parsed)
    await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)


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
    Metode) langsung dari kata kunci, TANPA lewat AI -- biar reliable buat
    hal krusial kayak nomor HP (nggak digantung interpretasi model bahasa).
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

    # Tanggal kirim custom (default-nya tetep Kamis PO minggu berjalan kalau
    # nggak pernah disebut sama sekali). Nggak wajib kata "tanggal" -- admin
    # sering cuma bilang "buat besok" doang -- tapi ini SENGAJA dicek PALING
    # TERAKHIR (semua field lain udah dicoba duluan) biar "besok" yang
    # nyempil di kalimat lain nggak salah kesedot jadi koreksi tanggal.
    if ("tanggal" in lower and "kirim" in lower) or "tgl kirim" in lower \
            or "besok" in lower or "lusa" in lower:
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
        }[field]
        item_list_text = "\n".join(
            f"  - {i['rasa']} ({i['kategori']}) x{i['qty']}" for i in editing["existing_items"]
        ) or "  (kosong)"
        tanggal_kirim_now = editing.get("tanggal_kirim") or editing["minggu_po"]

        preview = (
            f"*{field_label} diganti jadi:* {new_value}\n\n"
            f"*Data order {editing['nama']} sekarang:*\n"
            f"Nama: {editing['nama']}\n"
            f"No HP: {editing['no_hp']}\n"
            f"Alamat: {editing['alamat']}\n"
            f"Metode: {editing['metode']}\n"
            f"Tanggal Kirim: {tanggal_kirim_now}\n"
            f"Items (nggak berubah):\n{item_list_text}\n\n"
            f"Item pesanan nggak ikut berubah. Mau koreksi field lain juga? "
            f"Ketik lagi sebelum konfirmasi. Kalau udah bener, klik Konfirmasi."
        )

        context.user_data["pending_edit"] = {
            "nama": editing["nama"],
            "no_hp": editing["no_hp"],
            "alamat": editing["alamat"],
            "metode": editing["metode"],
            "items": editing["existing_items"],
            "ongkir": editing["ongkir"],
            "minggu_po": editing["minggu_po"],
            "tanggal_kirim": tanggal_kirim_now,
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
    catatan = result.get("catatan", "-")
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
        f"Catatan: {catatan}\n\n"
        + (
            "⚠️ Item KOSONG — kalau dikonfirmasi, order ini bakal DIBATALIN TOTAL "
            "(dihapus dari Sheets, TANPA invoice/surat jalan baru)."
            if is_cancel else
            "⚠️ Kalau dikonfirmasi, order lama bakal DIHAPUS total dan diganti yang baru ini."
        )
    )

    context.user_data["pending_edit"] = {
        "nama": editing["nama"],
        "no_hp": editing["no_hp"],
        "alamat": editing["alamat"],
        "metode": editing["metode"],
        "items": new_items,
        "ongkir": ongkir_final,
        "minggu_po": editing["minggu_po"],
        "tanggal_kirim": editing.get("tanggal_kirim"),
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
    minggu_po = pending["minggu_po"]

    if not pending["items"]:
        # Semua item dihapus lewat instruksi -> ini PEMBATALAN TOTAL, bukan
        # edit biasa. Cukup hapus baris di Sheets, JANGAN ditulis ulang
        # (kosong) dan JANGAN generate invoice/surat jalan (nggak ada apa-apa
        # buat dikirim ke customer yang udah batal).
        try:
            await asyncio.wait_for(
                asyncio.to_thread(sheets.delete_customer_week_rows, nama, minggu_po), timeout=30
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

    def _hapus_lalu_tulis_ulang():
        sheets.delete_customer_week_rows(pending["nama"], pending["minggu_po"])
        order = {
            "nama": pending["nama"],
            "no_hp": pending["no_hp"],
            "alamat": pending["alamat"],
            "metode": pending["metode"],
            "items": pending["items"],
            "ongkir": pending.get("ongkir", 0),
            "tanggal_kirim": pending.get("tanggal_kirim"),
        }
        return sheets.add_order_rows(order, pending["minggu_po"])

    try:
        orders = await asyncio.wait_for(asyncio.to_thread(_hapus_lalu_tulis_ulang), timeout=30)
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
    await query.message.reply_photo(photo=invoice_img, caption="Invoice terbaru (siap kirim ke customer)")

    img = receipt.generate_surat_jalan_image(nama, minggu_po, orders)
    await query.message.reply_photo(
        photo=img, caption="Surat jalan terbaru (siap print) — tap gambar → Share → app printer"
    )

    context.user_data.pop("editing_order", None)
    context.user_data.pop("pending_edit", None)
    context.user_data["saving_in_progress"] = False


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
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern="^(confirm_order|cancel_order)$"))
    app.add_handler(CallbackQueryHandler(handle_set_ongkir, pattern="^set_ongkir$"))
    app.add_handler(CallbackQueryHandler(handle_edit_confirm, pattern="^(confirm_edit|cancel_edit)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
