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
from ai_parser import parse_customer_chat, parse_order_edit, classify_intent
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
    }

    await update.message.reply_text(
        f"*Order {nama} saat ini:*\n{item_list_text}\n\n"
        f"Ketik perubahannya (bebas, misal: 'tambah donat gula 5, ham cheese jadi 20 pcs').",
        parse_mode="Markdown",
    )


# ---------------- FREE-TEXT ORDER PARSING ----------------

@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Kalau lagi dalam mode edit order (habis /edit Nama Customer atau baru
    # ke-detect mau edit), teks ini instruksi perubahan, BUKAN order baru.
    if context.user_data.get("editing_order"):
        await handle_edit_instruction(update, context)
        return

    raw_text = update.message.text

    # Coba tebak dulu maksud admin (bahasa natural, nggak wajib pakai '/')
    try:
        intent_result = await asyncio.wait_for(
            asyncio.to_thread(classify_intent, raw_text), timeout=20
        )
    except Exception:
        intent_result = {"intent": "order_baru", "nama_customer": None, "bulan": None, "instruksi_edit": None}

    intent = intent_result.get("intent", "order_baru")

    if intent == "rekap_produksi":
        await rekap(update, context)
        return

    if intent == "laporan_bulanan":
        bulan = intent_result.get("bulan")
        context.args = [bulan] if bulan else []
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
        }
        # Langsung proses instruksinya, nggak perlu tanya ulang ke admin
        await handle_edit_instruction(update, context, instruction_override=instruksi)
        return

    # Default: anggap order baru (perilaku sama seperti sebelumnya)
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
        f"Ongkir: {('Rp' + format(int(parsed.get('ongkir')), ',').replace(',', '.')) if parsed.get('ongkir') else 'belum diisi (Rp0)'}\n"
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
        "ongkir": int(parsed.get("ongkir") or 0),
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

    invoice_img = invoice_image.generate_invoice_image(order["nama"], minggu_po, orders)
    await query.message.reply_photo(photo=invoice_img, caption="Invoice (siap kirim ke customer)")

    img = receipt.generate_surat_jalan_image(order["nama"], minggu_po, orders)
    await query.message.reply_photo(
        photo=img, caption="Surat jalan (siap print) — tap gambar → Share → app printer"
    )

    context.user_data.pop("pending_order", None)
    context.user_data["saving_in_progress"] = False


# ---------------- EDIT ORDER ----------------

async def handle_edit_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    instruction_override: str = None):
    editing = context.user_data["editing_order"]
    instruction = instruction_override or update.message.text
    await update.message.reply_text("Menghitung ulang order...")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(parse_order_edit, editing["existing_items"], instruction),
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

    preview = (
        f"*Order Lama:*\n{old_text}\n\n"
        f"*Order Baru (setelah diedit):*\n{new_text}\n\n"
        f"Ongkir: {ongkir_rupiah}\n"
        f"Catatan: {catatan}\n\n"
        f"⚠️ Kalau dikonfirmasi, order lama bakal DIHAPUS total dan diganti yang baru ini."
    )

    context.user_data["pending_edit"] = {
        "nama": editing["nama"],
        "no_hp": editing["no_hp"],
        "alamat": editing["alamat"],
        "metode": editing["metode"],
        "items": new_items,
        "ongkir": ongkir_final,
        "minggu_po": editing["minggu_po"],
    }

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Konfirmasi Edit", callback_data="confirm_edit"),
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

    def _hapus_lalu_tulis_ulang():
        sheets.delete_customer_week_rows(pending["nama"], pending["minggu_po"])
        order = {
            "nama": pending["nama"],
            "no_hp": pending["no_hp"],
            "alamat": pending["alamat"],
            "metode": pending["metode"],
            "items": pending["items"],
            "ongkir": pending.get("ongkir", 0),
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

    minggu_po = pending["minggu_po"]
    nama = pending["nama"]

    await query.message.reply_text("Order berhasil diupdate!")

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
    app.add_handler(CommandHandler("edit", edit_cmd))
    app.add_handler(CallbackQueryHandler(handle_confirm, pattern="^(confirm_order|cancel_order)$"))
    app.add_handler(CallbackQueryHandler(handle_edit_confirm, pattern="^(confirm_edit|cancel_edit)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot jalan...")
    app.run_polling()


if __name__ == "__main__":
    main()
