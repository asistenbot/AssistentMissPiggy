"""
Bot utama Miss Piggy PO Assistant.
Jalankan: python bot.py
"""

import asyncio
import logging
import datetime

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
from sheets_client import get_sheets_client
from ai_parser import parse_customer_chat
from scheduler_jobs import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != config.OWNER_TELEGRAM_ID:
            await update.message.reply_text("Bot ini khusus admin Miss Piggy.")
            return
        return await func(update, context)
    return wrapper


# ---------------- COMMANDS ----------------

@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Halo! Ini asisten admin PO {config.BUSINESS_NAME}.\n\n"
        "Cara pakai:\n"
        "- Paste/forward chat customer ke sini, akan diparse otomatis jadi order.\n"
        "- /pricelist — lihat daftar harga\n"
        "- /rekap — rekap produksi minggu berjalan\n"
        "- /invoice Nama Customer — bikin ulang invoice\n"
        "- /suratjalan Nama Customer — bikin ulang surat jalan\n"
        "- /laporanbulanan — laporan bayar supplier bulan ini\n"
        "- /laporanbulanan 2026-07 — laporan bulan tertentu\n\n"
        "Auto-recap produksi akan dikirim tiap Rabu jam 15:00, 16:00, dan 19:00 WIB."
    )


@owner_only
async def pricelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = get_sheets_client()
    text = sheets.get_pricelist_text()
    await update.message.reply_text(f"*PRICE LIST — {config.BUSINESS_NAME}*\n{text}",
                                     parse_mode="Markdown")


@owner_only
async def rekap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_orders_by_week(minggu_po)
    text = documents.build_production_recap(minggu_po, orders)
    await update.message.reply_text(text, parse_mode="Markdown")


@owner_only
async def invoice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nama = " ".join(context.args)
    if not nama:
        await update.message.reply_text("Format: /invoice Nama Customer")
        return
    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_orders_by_customer_week(nama, minggu_po)
    text = documents.build_invoice(nama, minggu_po, orders)
    await update.message.reply_text(text, parse_mode="Markdown")

    if orders:
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
    text = documents.build_surat_jalan(nama, minggu_po, orders)
    await update.message.reply_text(text, parse_mode="Markdown")

    if orders:
        img = receipt.generate_surat_jalan_image(nama, minggu_po, orders)
        await update.message.reply_photo(
            photo=img, caption="Surat jalan (siap print) — tap gambar → Share → app printer"
        )


@owner_only
async def laporanbulanan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sheets = get_sheets_client()
    if context.args:
        try:
            year, month = map(int, context.args[0].split("-"))
        except ValueError:
            await update.message.reply_text("Format: /laporanbulanan 2026-07")
            return
    else:
        now = datetime.datetime.now(date_helpers.get_timezone())
        year, month = now.year, now.month

    orders = sheets.get_orders_by_month(year, month)
    dough_price_map = sheets.get_dough_price_map()
    text = documents.build_monthly_supplier_report(year, month, orders, dough_price_map)
    await update.message.reply_text(text, parse_mode="Markdown")


# ---------------- FREE-TEXT ORDER PARSING ----------------

@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text
    await update.message.reply_text("Sedang diproses...")

    try:
        # Jalanin pemanggilan AI di thread terpisah (bukan blocking event loop bot),
        # dan kasih batas waktu maksimal 40 detik biar nggak nge-gantung selamanya.
        parsed = await asyncio.wait_for(
            asyncio.to_thread(parse_customer_chat, raw_text), timeout=40
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
        f"Items:\n{items_text}\n"
        f"Catatan: {parsed.get('catatan') or '-'}\n\n"
    )

    if parsed.get("kelengkapan") == "kurang_lengkap":
        preview += "⚠️ Data masih kurang lengkap, cek/edit dulu sebelum disimpan.\n\n"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan & Generate", callback_data="confirm_order"),
        InlineKeyboardButton("❌ Batal", callback_data="cancel_order"),
    ]])
    await update.message.reply_text(preview, parse_mode="Markdown", reply_markup=keyboard)


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel_order":
        context.user_data.pop("pending_order", None)
        await query.edit_message_text("Dibatalin ya.")
        return

    # Cegah klik dobel: begitu diproses, langsung matiin tombol & kasih flag,
    # biar klik kedua (atau nyasar) nggak nyimpen data yang sama 2x.
    if context.user_data.get("saving_in_progress"):
        return
    context.user_data["saving_in_progress"] = True

    parsed = context.user_data.get("pending_order")
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
        "ongkir": 0,
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

    invoice_text = documents.build_invoice(order["nama"], minggu_po, orders)
    surat_jalan_text = documents.build_surat_jalan(order["nama"], minggu_po, orders)

    await query.message.reply_text("Tersimpan! Ini invoice & surat jalannya:")
    await query.message.reply_text(invoice_text, parse_mode="Markdown")

    invoice_img = invoice_image.generate_invoice_image(order["nama"], minggu_po, orders)
    await query.message.reply_photo(photo=invoice_img, caption="Invoice (siap kirim ke customer)")

    await query.message.reply_text(surat_jalan_text, parse_mode="Markdown")

    # Surat jalan versi gambar struk, siap di-share ke app printer Bluetooth
    img = receipt.generate_surat_jalan_image(order["nama"], minggu_po, orders)
    await query.message.reply_photo(
        photo=img, caption="Surat jalan (siap print) — tap gambar → Share → app printer"
    )

    context.user_data.pop("pending_order", None)
    context.user_data["saving_in_progress"] = False


async def on_startup(app: Application):
    # Start scheduler di dalam event loop yang sudah jalan (lebih stabil
    # daripada start sebelum run_polling dipanggil).
    setup_scheduler(app.bot)
    logger.info("Scheduler auto-recap & laporan bulanan aktif.")


def main():
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pricelist", pricelist))
    app.add_handler(CommandHandler("rekap", rekap))
    app.add_handler(CommandHandler("invoice", invoice_cmd))
    app.add_handler(CommandHandler("suratjalan", suratjalan_cmd))
    app.add_handler(CommandHandler("laporanbulanan", laporanbulanan_cmd))
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern="^(confirm_order|cancel_order)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
