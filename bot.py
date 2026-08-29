"""
Bot Telegram utama -- Asisten Plastik (Anugerah Sejahtera Sentosa).

Alur order: admin forward/paste chat customer (atau kirim foto chat/nota) ke
bot ini, bot parse pakai AI, tunjukin hasilnya buat dikonfirmasi, begitu OK
langsung disimpen ke Sheets + invoice & surat jalan otomatis kegenerate.
Order masuk KAPAN AJA langsung diproses (gak ada siklus mingguan).

Command lengkap ada di /start.
"""

import functools
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

import config
import documents
from ai_parser import (
    parse_order_text, parse_order_image, parse_po_text, classify_intent,
    parse_order_correction, parse_price_update, parse_price_update_image,
)
from sheets_client import get_sheets_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("asisten-plastik")


# ---------------- HELPERS ----------------

def owner_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *a, **kw):
        user = update.effective_user
        if config.OWNER_TELEGRAM_IDS and (not user or user.id not in config.OWNER_TELEGRAM_IDS):
            await update.effective_message.reply_text(
                "Maaf, bot ini cuma buat admin. Chat @userinfobot buat tau ID Telegram kamu, "
                "terus minta admin tambahin ke OWNER_TELEGRAM_IDS."
            )
            return
        return await func(update, context, *a, **kw)
    return wrapper


def rupiah(n):
    return documents.rupiah(n)


def _sheets():
    return get_sheets_client()


_PENDING_KEYS = ("pending_order", "pending_po", "pending_edit", "pending_price_update", "pending_cancel")


def _has_pending(context):
    return any(context.user_data.get(k) for k in _PENDING_KEYS)


async def _send_text(update, text, reply_markup=None):
    await update.effective_message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
    )


# ---------------- PREVIEW TEXT ----------------

def _order_preview_text(parsed):
    lines = ["*Hasil baca order:*", ""]
    lines.append(f"Customer: *{parsed.get('nama_customer', '-')}*")
    if parsed.get("no_hp"):
        lines.append(f"HP: {parsed['no_hp']}")
    if parsed.get("alamat"):
        lines.append(f"Alamat: {parsed['alamat']}")
    lines.append(f"Metode: {parsed.get('metode', 'Kirim')}")
    lines.append("")
    total = 0
    for it in parsed.get("items", []):
        harga = it.get("harga_satuan", 0)
        subtotal = float(it.get("qty", 0)) * float(harga or 0)
        total += subtotal
        flag = "" if it.get("item_code") else " ⚠️ *(produk gak ketemu di katalog, cek lagi)*"
        lines.append(f"• {it.get('nama_item')} x{it.get('qty')} {it.get('satuan', '')} = {rupiah(subtotal)}{flag}")
    ongkir = parsed.get("ongkir", 0) or 0
    lines.append("")
    lines.append(f"Subtotal: {rupiah(total)}")
    lines.append(f"Ongkir: {rupiah(ongkir)}")
    lines.append(f"*Total: {rupiah(total + float(ongkir))}*")
    if parsed.get("catatan"):
        lines.append("")
        lines.append(f"📝 Catatan AI: {parsed['catatan']}")
    return "\n".join(lines)


def _po_preview_text(parsed):
    lines = ["*Hasil baca PO ke supplier:*", ""]
    lines.append(f"Supplier: *{parsed.get('nama_supplier', '-')}*")
    lines.append("")
    total = 0
    for it in parsed.get("items", []):
        harga = it.get("harga_satuan", 0) or 0
        subtotal = float(it.get("qty", 0)) * float(harga)
        total += subtotal
        lines.append(f"• {it.get('nama_item')} x{it.get('qty')} {it.get('satuan', '')} @ {rupiah(harga)} = {rupiah(subtotal)}")
    lines.append("")
    lines.append(f"*Total belanja: {rupiah(total)}*")
    if parsed.get("catatan"):
        lines.append("")
        lines.append(f"📝 Catatan AI: {parsed['catatan']}")
    return "\n".join(lines)


def _resolve_items_with_price(parsed_items, sheets):
    """Isi ulang item yang item_code-nya kosong dengan hasil find_product,
    dan pastikan harga_satuan selalu ambil dari PriceList (bukan tebakan
    AI) supaya harga selalu akurat."""
    resolved = []
    for it in parsed_items:
        code = (it.get("item_code") or "").strip()
        row = None
        if code:
            row = sheets.get_price_map().get(code.upper())
        if row is None:
            row = sheets.find_product(it.get("nama_item", ""))
        if row is not None:
            resolved.append({
                "item_code": row["Item_Code"],
                "nama_item": row["Nama"],
                "kategori": row.get("Kategori", ""),
                "satuan": row.get("Satuan", it.get("satuan", "")),
                "harga_satuan": row.get("Harga_Jual", 0),
                "qty": it.get("qty", 0),
            })
        else:
            resolved.append({
                "item_code": "",
                "nama_item": it.get("nama_item", "?"),
                "kategori": "",
                "satuan": it.get("satuan", ""),
                "harga_satuan": 0,
                "qty": it.get("qty", 0),
            })
    return resolved


# ---------------- COMMANDS ----------------

HELP_TEXT = f"""Halo! Ini *Asisten {config.BUSINESS_NAME}* 👋

*Cara pakai (order customer):*
Tinggal paste/forward chat order customer ke sini, atau kirim fotonya
(screenshot chat / nota tulisan tangan). Bot bakal baca & bikinin invoice +
surat jalan otomatis, tinggal dikonfirmasi dulu.

*Gak usah apal command!* Boleh langsung nanya santai kayak "utang ke
siapa aja masih ada", "piutang berapa sih", "kas bulan ini gimana",
"si Budi udah bayar", "bayar utang ke CV Sumber 500rb" -- bot bakal ngerti
sendiri.

*Salah ketik pas bikin order?* Tinggal chat lagi abis invoice muncul,
misal "edit Grandia Hotel" (kalau nama customernya salah), "alamatnya
salah, harusnya Jl. Melati No. 5", atau "qty-nya jadi 25 pack" -- bot
bakal nanya konfirmasi dulu, begitu di-OK invoice & surat jalannya
otomatis dicetak ulang. Default-nya ngedit order yang PALING BARU; kalau
mau invoice lain, sebutin nomor invoice-nya, misal "invoice
INV-20260828-001 no HP-nya ganti 08123456789".

*Order-nya gak jadi / mau dihapus total?* Bilang aja "hapus order Grandia
Hotel" atau "batalin invoice INV-20260828-001" -- bot nanya konfirmasi
dulu, begitu di-OK order ditandai Batal (datanya tetep ada buat histori,
tapi otomatis keluar dari piutang).

*Mau update harga jual/beli produk?* Tinggal bilang aja, misal "harga
tulip naik jadi 17000" atau "update harga TUL-01 jadi 16500" -- bot
nunjukin dulu harga lama → baru buat dikonfirmasi, begitu di-OK langsung
keupdate di PriceList Google Sheets. Kalau harga barunya dari daftar harga
supplier yang difoto, kirim fotonya dengan CAPTION yang jelas (misal
"update harga beli dari foto ini" atau "daftar harga supplier baru"), bot
bakal baca semua barang di foto sekaligus (dianggap harga BELI/modal) --
tanpa caption yang jelas, foto dianggap ORDER customer (biar aman).

Command di bawah ini cadangan aja kalau mau lebih pasti/cepat:
/pricelist — lihat daftar harga produk
/invoice <no invoice atau nama customer> — cetak ulang invoice
/suratjalan <no invoice atau nama customer> — cetak ulang surat jalan
/lunas <no invoice atau nama customer> — tandai order sudah dibayar
/piutang — rekap tagihan customer yang belum lunas

/po — bikin Purchase Order (belanja) ke supplier
/bayarutang <nama supplier> <jumlah> — catat pembayaran ke supplier
/utang — rekap utang ke semua supplier

/kas masuk <jumlah> <keterangan> — catat uang masuk di luar penjualan
/kas keluar <jumlah> <keterangan> — catat uang keluar (operasional dll)
/laporanbulanan [YYYY-MM] — laporan kas & laba rugi bulanan

/batal — batalin order/PO yang lagi nunggu konfirmasi
"""


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_text(update, HELP_TEXT)


@owner_only
async def groupid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Chat ID: `{update.effective_chat.id}`", parse_mode=ParseMode.MARKDOWN)


async def _do_pricelist(update):
    text = _sheets().get_pricelist_text()
    await _send_text(update, text or "Belum ada produk di PriceList.")


@owner_only
async def pricelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_pricelist(update)


@owner_only
async def batal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("pending_order", None)
    context.user_data.pop("pending_po", None)
    context.user_data.pop("pending_edit", None)
    context.user_data.pop("pending_price_update", None)
    context.user_data.pop("pending_cancel", None)
    context.user_data.pop("awaiting", None)
    await update.effective_message.reply_text("Oke, dibatalin.")


async def _do_piutang(update):
    summary = _sheets().get_piutang_summary()
    if not summary:
        await update.effective_message.reply_text("Gak ada piutang, semua customer udah lunas 🎉")
        return
    lines = ["*Piutang customer (belum lunas):*", ""]
    total = 0
    for nama, jumlah in sorted(summary.items(), key=lambda x: -x[1]):
        lines.append(f"• {nama}: {rupiah(jumlah)}")
        total += jumlah
    lines.append("")
    lines.append(f"*Total piutang: {rupiah(total)}*")
    await _send_text(update, "\n".join(lines))


@owner_only
async def piutang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_piutang(update)


async def _do_utang(update):
    summary = _sheets().get_utang_summary()
    if not summary:
        await update.effective_message.reply_text("Gak ada utang ke supplier saat ini 🎉")
        return
    lines = ["*Utang ke supplier (belum lunas):*", ""]
    total = 0
    for nama, jumlah in sorted(summary.items(), key=lambda x: -x[1]):
        lines.append(f"• {nama}: {rupiah(jumlah)}")
        total += jumlah
    lines.append("")
    lines.append(f"*Total utang: {rupiah(total)}*")
    await _send_text(update, "\n".join(lines))


@owner_only
async def utang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_utang(update)


async def _do_lunas(update, key):
    if not key:
        await update.effective_message.reply_text("Invoice/customer mana yang mau ditandai lunas?")
        return
    sheets = _sheets()
    no_invoice = key if key.upper().startswith(config.INVOICE_PREFIX) else sheets.get_latest_invoice_for_customer(key)
    if not no_invoice:
        await update.effective_message.reply_text(f"Gak ketemu order buat '{key}'.")
        return
    total = sheets.mark_order_lunas(no_invoice)
    if total is None:
        await update.effective_message.reply_text(f"Invoice {no_invoice} gak ketemu.")
        return
    sheets.add_kas_entry("Masuk", "Penjualan", total, keterangan=f"Pelunasan {no_invoice}", ref=no_invoice)
    await update.effective_message.reply_text(
        f"✅ {no_invoice} ditandai *Lunas* ({rupiah(total)}), udah dicatat ke Kas Masuk.",
        parse_mode=ParseMode.MARKDOWN,
    )


@owner_only
async def lunas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_lunas(update, " ".join(context.args) if context.args else "")


async def _do_invoice_lookup(update, context, key):
    if not key:
        await update.effective_message.reply_text("Invoice/customer mana yang mau dicetak ulang?")
        return
    sheets = _sheets()
    no_invoice = key if key.upper().startswith(config.INVOICE_PREFIX) else sheets.get_latest_invoice_for_customer(key)
    if not no_invoice:
        await update.effective_message.reply_text(f"Gak ketemu order buat '{key}'.")
        return
    await _kirim_invoice(update, context, no_invoice)


@owner_only
async def invoice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_invoice_lookup(update, context, " ".join(context.args) if context.args else "")


async def _do_suratjalan_lookup(update, context, key):
    if not key:
        await update.effective_message.reply_text("Invoice/customer mana yang mau dicetak ulang surat jalannya?")
        return
    sheets = _sheets()
    no_invoice = key if key.upper().startswith(config.INVOICE_PREFIX) else sheets.get_latest_invoice_for_customer(key)
    if not no_invoice:
        await update.effective_message.reply_text(f"Gak ketemu order buat '{key}'.")
        return
    await _kirim_surat_jalan(update, context, no_invoice)


@owner_only
async def suratjalan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _do_suratjalan_lookup(update, context, " ".join(context.args) if context.args else "")


async def _kirim_invoice(update, context, no_invoice):
    sheets = _sheets()
    rows = sheets.get_order_items(no_invoice)
    if not rows:
        await update.effective_message.reply_text(f"Invoice {no_invoice} gak ketemu.")
        return
    first = rows[0]
    items = [{"nama_item": r["Nama_Item"], "qty": r["Qty"], "satuan": r["Satuan"], "harga_satuan": r["Harga_Satuan"]} for r in rows]
    ongkir = 0
    for r in rows:
        try:
            ongkir += float(r.get("Ongkir", 0) or 0)
        except ValueError:
            pass
    img, pdf = documents.generate_invoice_image(
        no_invoice, first["Nama_Customer"], first.get("No_HP", ""), first.get("Alamat", ""),
        first.get("Metode", "Kirim"), items, ongkir,
    )
    await update.effective_message.reply_photo(photo=img, caption=f"Invoice {no_invoice}")
    await update.effective_message.reply_document(
        document=pdf, filename=f"{no_invoice}.pdf", caption="Versi PDF (siap print A4)"
    )


async def _kirim_surat_jalan(update, context, no_invoice):
    sheets = _sheets()
    rows = sheets.get_order_items(no_invoice)
    if not rows:
        await update.effective_message.reply_text(f"Invoice {no_invoice} gak ketemu.")
        return
    first = rows[0]
    items = [{"nama_item": r["Nama_Item"], "qty": r["Qty"], "satuan": r["Satuan"]} for r in rows]
    no_sj = no_invoice.replace(config.INVOICE_PREFIX, config.SURAT_JALAN_PREFIX, 1)
    img, pdf = documents.generate_surat_jalan_image(
        no_sj, first["Nama_Customer"], first.get("No_HP", ""), first.get("Alamat", ""),
        first.get("Metode", "Kirim"), items, no_invoice_ref=no_invoice,
    )
    await update.effective_message.reply_photo(photo=img, caption=f"Surat Jalan {no_sj}")
    await update.effective_message.reply_document(
        document=pdf, filename=f"{no_sj}.pdf", caption="Versi PDF (siap print A4)"
    )


# ---------------- KAS & LAPORAN ----------------

@owner_only
async def kas_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Format: /kas masuk <jumlah> <keterangan>\natau: /kas keluar <jumlah> <keterangan>"
        )
        return
    jenis_raw = context.args[0].lower()
    jenis = "Masuk" if jenis_raw in ("masuk", "in") else "Keluar" if jenis_raw in ("keluar", "out") else None
    if jenis is None:
        await update.effective_message.reply_text("Jenis harus 'masuk' atau 'keluar'.")
        return
    try:
        jumlah = float(context.args[1].replace(".", "").replace(",", ""))
    except ValueError:
        await update.effective_message.reply_text("Jumlah harus angka, contoh: /kas keluar 50000 bayar listrik")
        return
    keterangan = " ".join(context.args[2:]) or "-"
    kategori = "Operasional" if jenis == "Keluar" else "Lainnya"
    _sheets().add_kas_entry(jenis, kategori, jumlah, keterangan=keterangan)
    await update.effective_message.reply_text(f"✅ Kas {jenis.lower()} {rupiah(jumlah)} dicatat ({keterangan}).")


async def _do_bayarutang(update, nama_supplier, jumlah, catatan=""):
    if not nama_supplier or not jumlah:
        await update.effective_message.reply_text("Bayar utang ke supplier siapa, berapa jumlahnya?")
        return
    sisa = _sheets().bayar_utang(nama_supplier, jumlah, catatan)
    _sheets().add_kas_entry("Keluar", "Bayar Supplier", jumlah, keterangan=f"Bayar utang {nama_supplier} {catatan}".strip())
    sisa_text = rupiah(sisa) if sisa > 0 else "Rp0 (lunas)"
    await update.effective_message.reply_text(f"✅ Pembayaran {rupiah(jumlah)} ke *{nama_supplier}* dicatat. Sisa utang: {sisa_text}", parse_mode=ParseMode.MARKDOWN)


@owner_only
async def bayarutang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.effective_message.reply_text("Format: /bayarutang <nama supplier> <jumlah> [catatan]")
        return
    # cara paling gampang: jumlah = angka terakhir yang valid, sisanya nama supplier
    jumlah = None
    jumlah_idx = None
    for i in range(len(context.args) - 1, -1, -1):
        raw = context.args[i].replace(".", "").replace(",", "")
        if raw.isdigit():
            jumlah = float(raw)
            jumlah_idx = i
            break
    if jumlah is None:
        await update.effective_message.reply_text("Gak nemu jumlah uangnya. Format: /bayarutang <nama supplier> <jumlah> [catatan]")
        return
    nama_supplier = " ".join(context.args[:jumlah_idx])
    catatan = " ".join(context.args[jumlah_idx + 1:])
    await _do_bayarutang(update, nama_supplier, jumlah, catatan)


BULAN_NAMA = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
              "Agustus", "September", "Oktober", "November", "Desember"]


async def _do_laporanbulanan(update, year, month):
    lap = _sheets().get_laporan_bulanan(year, month)
    lines = [f"*Laporan Kas — {BULAN_NAMA[month]} {year}*", ""]
    lines.append("Kas Masuk:")
    for kat, val in lap["masuk_by_kategori"].items():
        lines.append(f"  • {kat}: {rupiah(val)}")
    lines.append(f"Total Masuk: {rupiah(lap['total_masuk'])}")
    lines.append("")
    lines.append("Kas Keluar:")
    for kat, val in lap["keluar_by_kategori"].items():
        lines.append(f"  • {kat}: {rupiah(val)}")
    lines.append(f"Total Keluar: {rupiah(lap['total_keluar'])}")
    lines.append("")
    lines.append(f"*Laba/Rugi bersih: {rupiah(lap['laba'])}*")
    await _send_text(update, "\n".join(lines))


@owner_only
async def laporanbulanan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import datetime
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    year, month = now.year, now.month
    if context.args:
        try:
            year, month = [int(x) for x in context.args[0].split("-")]
        except ValueError:
            await update.effective_message.reply_text("Format: /laporanbulanan atau /laporanbulanan 2026-07")
            return
    await _do_laporanbulanan(update, year, month)


# ---------------- ORDER FLOW (teks & foto) ----------------

@owner_only
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    awaiting = context.user_data.get("awaiting")

    if awaiting == "po_text":
        context.user_data["awaiting"] = None
        await _process_po_text(update, context, text)
        return

    if _has_pending(context):
        await update.effective_message.reply_text(
            "Masih ada order/PO/perubahan yang nunggu konfirmasi di atas. Klik tombolnya dulu, atau /batal buat batalin."
        )
        return

    await _route_text(update, context, text)


async def _route_text(update, context, text):
    """Semua chat bebas (bukan command, bukan lagi nunggu konfirmasi) lewat
    sini dulu -- ditebak dulu maksudnya (order? nanya laporan? bayar utang?)
    pakai AI, biar gak perlu apal command. Kalau gagal nebak / gak yakin,
    default-nya tetep dianggap ORDER customer (paling aman)."""
    try:
        route = classify_intent(text)
    except Exception:
        logger.exception("gagal classify_intent, fallback ke order")
        route = {"intent": "order"}

    intent = route.get("intent", "order")
    target = (route.get("target") or "").strip()
    bulan = (route.get("bulan") or "").strip()
    jumlah = route.get("jumlah") or 0

    if intent == "report_pricelist":
        await _do_pricelist(update)
    elif intent == "report_piutang":
        await _do_piutang(update)
    elif intent == "report_utang":
        await _do_utang(update)
    elif intent == "report_kas_bulanan":
        import datetime
        now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
        year, month = now.year, now.month
        if bulan:
            try:
                year, month = [int(x) for x in bulan.split("-")]
            except ValueError:
                pass
        await _do_laporanbulanan(update, year, month)
    elif intent == "mark_lunas":
        await _do_lunas(update, target)
    elif intent == "bayar_utang":
        await _do_bayarutang(update, target, float(jumlah) if jumlah else 0)
    elif intent == "lihat_invoice":
        await _do_invoice_lookup(update, context, target)
    elif intent == "lihat_suratjalan":
        await _do_suratjalan_lookup(update, context, target)
    elif intent == "edit_order":
        await _do_edit_order(update, context, target, text)
    elif intent == "batal_order":
        await _do_cancel_order(update, context, target)
    elif intent == "update_harga":
        await _do_update_harga(update, context, text)
    elif intent == "po":
        await _process_po_text(update, context, text)
    elif intent == "lainnya":
        await update.effective_message.reply_text(
            "Hmm, kurang paham maksudnya. Kirim order customer, atau tanya soal "
            "utang/piutang/kas/harga -- ketik /start buat liat semua yang bisa gua bantu."
        )
    else:  # "order" atau intent gak dikenal -> default paling aman
        await _process_order_text(update, context, text)


async def _process_order_text(update, context, text):
    await update.effective_message.reply_chat_action("typing")
    sheets = _sheets()
    try:
        parsed = parse_order_text(text, sheets.get_price_list(), sheets.get_customer_names())
    except Exception as e:
        logger.exception("gagal parse order teks")
        await update.effective_message.reply_text(f"Waduh, gagal baca order ini: {e}")
        return
    parsed["items"] = _resolve_items_with_price(parsed.get("items", []), sheets)

    # Safety net: kalau AI gak nemu barang order sama sekali, jangan tunjukin
    # preview order kosong (Rp0) dengan tombol Simpan -- bikin bingung dan
    # kalau kepencet malah nyimpen order kosong ke Sheets.
    if not parsed.get("items"):
        msg = "Gak nemu barang order di pesan ini."
        if parsed.get("catatan"):
            msg += f"\n\n📝 {parsed['catatan']}"
        await update.effective_message.reply_text(msg)
        return

    context.user_data["pending_order"] = parsed
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="order_confirm"),
        InlineKeyboardButton("❌ Batal", callback_data="order_cancel"),
    ]])
    await _send_text(update, _order_preview_text(parsed), reply_markup=kb)


_EDIT_FIELD_LABEL = {
    "nama_customer": "Nama Customer", "no_hp": "No HP",
    "alamat": "Alamat", "metode": "Metode",
}
_EDIT_FIELD_COLUMN = {
    "nama_customer": "Nama_Customer", "no_hp": "No_HP",
    "alamat": "Alamat", "metode": "Metode",
}


async def _do_edit_order(update, context, target, text):
    sheets = _sheets()

    no_invoice = None
    if target and target.upper().startswith(config.INVOICE_PREFIX):
        no_invoice = target
    if not no_invoice:
        no_invoice = context.user_data.get("last_invoice")
    if not no_invoice and target:
        no_invoice = sheets.get_latest_invoice_for_customer(target)
    if not no_invoice:
        await update.effective_message.reply_text(
            "Order/invoice yang mana yang mau diedit? Sebutin nomor invoice-nya "
            "atau nama customernya, contoh: \"invoice INV-20260828-001 nama "
            "customernya salah, harusnya Grandia Hotel\"."
        )
        return

    rows = sheets.get_order_items(no_invoice)
    if not rows:
        await update.effective_message.reply_text(f"Invoice {no_invoice} gak ketemu.")
        return
    first = rows[0]
    items_desc = "\n".join(
        f"  - {r.get('Item_Code','')} | {r.get('Nama_Item','')} | qty {r.get('Qty','')} {r.get('Satuan','')} "
        f"| harga satuan Rp{r.get('Harga_Satuan','')}"
        for r in rows
    )
    current_desc = (
        f"No Invoice: {no_invoice}\n"
        f"Nama Customer: {first.get('Nama_Customer', '')}\n"
        f"No HP: {first.get('No_HP', '')}\n"
        f"Alamat: {first.get('Alamat', '')}\n"
        f"Metode: {first.get('Metode', '')}\n"
        f"ITEM DI ORDER INI:\n{items_desc}"
    )
    try:
        corr = parse_order_correction(text, current_desc)
    except Exception as e:
        logger.exception("gagal parse koreksi order")
        await update.effective_message.reply_text(f"Waduh, gagal baca koreksinya: {e}")
        return

    header_fields = {"nama_customer", "no_hp", "alamat", "metode"}
    updates = {
        k: (v or "").strip() for k, v in corr.items()
        if k in header_fields and (v or "").strip()
    }

    item_updates = []
    rows_by_code = {str(r.get("Item_Code", "")).strip().upper(): r for r in rows}
    for it in corr.get("items", []) or []:
        code = (it.get("item_code") or "").strip()
        qty_baru = it.get("qty_baru") or None
        harga_baru = it.get("harga_satuan_baru") or None
        if not code or (qty_baru is None and harga_baru is None):
            continue
        cur_row = rows_by_code.get(code.upper())
        if not cur_row:
            continue
        item_updates.append({
            "item_code": code,
            "nama": cur_row.get("Nama_Item", code),
            "satuan": cur_row.get("Satuan", ""),
            "qty_lama": cur_row.get("Qty", ""),
            "qty_baru": qty_baru,
            "harga_lama": cur_row.get("Harga_Satuan", ""),
            "harga_baru": harga_baru,
        })

    if not updates and not item_updates:
        await update.effective_message.reply_text(
            "Gak nangkep bagian mana yang mau diganti (atau nilainya udah sama "
            "kayak yang kesimpen sekarang). Coba lebih jelas, misal: \"ganti "
            "nama customer jadi Grandia Hotel\" atau \"qty tulip jadi 25 pack\"."
        )
        return

    context.user_data["pending_edit"] = {
        "no_invoice": no_invoice, "updates": updates, "item_updates": item_updates,
    }
    lines = [f"*Mau diganti di {no_invoice}:*", ""]
    for k, v in updates.items():
        lama = first.get(_EDIT_FIELD_COLUMN[k], "") or "-"
        lines.append(f"• {_EDIT_FIELD_LABEL[k]}: {lama} → *{v}*")
    for iu in item_updates:
        if iu["qty_baru"] is not None:
            lines.append(f"• Qty {iu['nama']}: {iu['qty_lama']} {iu['satuan']} → *{iu['qty_baru']} {iu['satuan']}*")
        if iu["harga_baru"] is not None:
            lines.append(
                f"• Harga satuan {iu['nama']} (order ini aja): "
                f"{rupiah(iu['harga_lama'])} → *{rupiah(iu['harga_baru'])}*"
            )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan Perubahan", callback_data="editorder_confirm"),
        InlineKeyboardButton("❌ Batal", callback_data="editorder_cancel"),
    ]])
    await _send_text(update, "\n".join(lines), reply_markup=kb)


async def _do_cancel_order(update, context, target):
    sheets = _sheets()

    no_invoice = None
    if target and target.upper().startswith(config.INVOICE_PREFIX):
        no_invoice = target
    if not no_invoice:
        no_invoice = context.user_data.get("last_invoice")
    if not no_invoice and target:
        no_invoice = sheets.get_latest_invoice_for_customer(target)
    if not no_invoice:
        await update.effective_message.reply_text(
            "Order/invoice yang mana yang mau dibatalin? Sebutin nomor invoice-nya "
            "atau nama customernya."
        )
        return

    rows = sheets.get_order_items(no_invoice)
    if not rows:
        await update.effective_message.reply_text(f"Invoice {no_invoice} gak ketemu.")
        return
    if str(rows[0].get("Status", "")) == "Batal":
        await update.effective_message.reply_text(f"{no_invoice} udah dibatalin sebelumnya.")
        return

    total = 0.0
    for r in rows:
        try:
            total += float(r.get("Subtotal", 0) or 0)
        except ValueError:
            pass
    try:
        total += float(rows[0].get("Ongkir", 0) or 0)
    except ValueError:
        pass

    context.user_data["pending_cancel"] = no_invoice
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Ya, Batalin", callback_data="cancelorder_confirm"),
        InlineKeyboardButton("❌ Jangan Dulu", callback_data="cancelorder_cancel"),
    ]])
    await _send_text(
        update,
        f"Yakin mau batalin *{no_invoice}* ({rows[0].get('Nama_Customer', '-')}, "
        f"total {rupiah(total)})? Order ditandai *Batal* (datanya tetep ada buat "
        "histori, tapi otomatis keluar dari piutang).",
        reply_markup=kb,
    )


async def _present_price_update(update, context, parsed):
    """Dipake bareng sama chat teks & foto -- ubah hasil parse (list items
    harga_jual/harga_beli baru) jadi preview + tombol konfirmasi."""
    sheets = _sheets()
    price_map = sheets.get_price_map()
    resolved = []
    not_found = []
    for it in parsed.get("items", []):
        code = (it.get("item_code") or "").strip().upper()
        row = price_map.get(code) if code else None
        if row is None:
            row = sheets.find_product(it.get("nama_disebut", ""))
        if row is None:
            not_found.append(it.get("nama_disebut") or "?")
            continue
        harga_jual_baru = it.get("harga_jual") or 0
        harga_beli_baru = it.get("harga_beli") or 0
        if not harga_jual_baru and not harga_beli_baru:
            continue
        resolved.append({
            "item_code": row["Item_Code"],
            "nama": row["Nama"],
            "harga_jual_lama": row.get("Harga_Jual", 0),
            "harga_beli_lama": row.get("Harga_Beli", 0),
            "harga_jual_baru": harga_jual_baru or None,
            "harga_beli_baru": harga_beli_baru or None,
        })

    if not resolved:
        msg = "Gak nemu perubahan harga yang jelas dari pesan/foto ini."
        if not_found:
            msg += " Barang yang gak ketemu di katalog: " + ", ".join(not_found) + "."
        await update.effective_message.reply_text(msg)
        return

    nama_supplier = (parsed.get("nama_supplier") or "").strip()
    context.user_data["pending_price_update"] = {"items": resolved, "nama_supplier": nama_supplier}
    lines = ["*Mau diubah harganya:*"]
    if nama_supplier:
        lines.append(f"(dari daftar harga {nama_supplier})")
    lines.append("")
    for r in resolved:
        if r["harga_jual_baru"]:
            lines.append(f"• {r['nama']} ({r['item_code']}) — harga jual: {rupiah(r['harga_jual_lama'])} → *{rupiah(r['harga_jual_baru'])}*")
        if r["harga_beli_baru"]:
            lines.append(f"• {r['nama']} ({r['item_code']}) — harga beli: {rupiah(r['harga_beli_lama'])} → *{rupiah(r['harga_beli_baru'])}*")
    if not_found:
        lines.append("")
        lines.append("⚠️ Gak ketemu di katalog, dilewatin: " + ", ".join(not_found))
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="priceupdate_confirm"),
        InlineKeyboardButton("❌ Batal", callback_data="priceupdate_cancel"),
    ]])
    await _send_text(update, "\n".join(lines), reply_markup=kb)


async def _do_update_harga(update, context, text):
    sheets = _sheets()
    await update.effective_message.reply_chat_action("typing")
    try:
        parsed = parse_price_update(text, sheets.get_price_list())
    except Exception as e:
        logger.exception("gagal parse update harga")
        await update.effective_message.reply_text(f"Waduh, gagal baca perubahan harganya: {e}")
        return
    await _present_price_update(update, context, parsed)


async def _process_price_photo(update, context):
    sheets = _sheets()
    await update.effective_message.reply_chat_action("typing")
    photo = update.effective_message.photo[-1]
    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    try:
        parsed = parse_price_update_image(image_bytes, "image/jpeg", sheets.get_price_list())
    except Exception as e:
        logger.exception("gagal parse foto price list")
        await update.effective_message.reply_text(f"Waduh, gagal baca foto daftar harga ini: {e}")
        return
    await _present_price_update(update, context, parsed)


async def _process_order_photo(update, context):
    sheets = _sheets()
    await update.effective_message.reply_chat_action("typing")
    photo = update.effective_message.photo[-1]
    file = await photo.get_file()
    image_bytes = bytes(await file.download_as_bytearray())
    try:
        parsed = parse_order_image(image_bytes, "image/jpeg", sheets.get_price_list(), sheets.get_customer_names())
    except Exception as e:
        logger.exception("gagal parse order foto")
        await update.effective_message.reply_text(f"Waduh, gagal baca foto ini: {e}")
        return
    parsed["items"] = _resolve_items_with_price(parsed.get("items", []), sheets)

    # Safety net: kalau AI gak nemu barang order sama sekali (misal fotonya
    # ternyata price list/dokumen lain, bukan order customer), JANGAN
    # tunjukin preview order kosong (Rp0) dengan tombol Simpan -- itu bikin
    # bingung dan kalau kepencet malah nyimpen order kosong ke Sheets. Kasih
    # tau langsung apa yang kebaca AI-nya, tanpa bikin pending_order.
    if not parsed.get("items"):
        msg = "Gak nemu barang order di foto ini."
        if parsed.get("catatan"):
            msg += f"\n\n📝 {parsed['catatan']}"
        msg += (
            "\n\nKalau ini sebenernya daftar harga dari supplier, kirim ulang "
            "fotonya dengan caption yang jelas, misal \"update harga beli dari "
            "foto ini\"."
        )
        await update.effective_message.reply_text(msg)
        return

    context.user_data["pending_order"] = parsed
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="order_confirm"),
        InlineKeyboardButton("❌ Batal", callback_data="order_cancel"),
    ]])
    await _send_text(update, _order_preview_text(parsed), reply_markup=kb)


@owner_only
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _has_pending(context):
        await update.effective_message.reply_text(
            "Masih ada order/PO/perubahan yang nunggu konfirmasi. Klik tombolnya dulu, atau /batal buat batalin."
        )
        return

    # Kalau foto ini dikirim dengan CAPTION (keterangan) yang jelas maksudnya
    # bukan order (misal "update harga dari foto ini"), pakai itu buat
    # nebak maksud fotonya. Tanpa caption / caption gak jelas -> default
    # dianggap ORDER (paling aman, order customer gak boleh kelewat).
    caption = (update.effective_message.caption or "").strip()
    intent = "order"
    if caption:
        try:
            route = classify_intent(caption)
            intent = route.get("intent", "order")
        except Exception:
            logger.exception("gagal classify_intent dari caption foto, fallback ke order")
            intent = "order"

    if intent == "update_harga":
        await _process_price_photo(update, context)
        return

    await _process_order_photo(update, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "order_cancel":
        context.user_data.pop("pending_order", None)
        await query.edit_message_text("Order dibatalin.")
        return

    if data == "order_confirm":
        parsed = context.user_data.pop("pending_order", None)
        if not parsed:
            await query.edit_message_text("Order-nya udah gak ada / kadaluarsa, coba kirim ulang.")
            return
        sheets = _sheets()
        no_invoice = sheets.add_order(
            parsed.get("nama_customer", "-"), parsed.get("no_hp", ""), parsed.get("alamat", ""),
            parsed.get("metode", "Kirim"), parsed["items"], ongkir=parsed.get("ongkir", 0) or 0,
        )
        context.user_data["last_invoice"] = no_invoice
        await query.edit_message_text(f"✅ Order disimpan sebagai *{no_invoice}*. Lagi bikin invoice & surat jalan...", parse_mode=ParseMode.MARKDOWN)
        await _kirim_invoice(update, context, no_invoice)
        await _kirim_surat_jalan(update, context, no_invoice)
        return

    if data == "editorder_cancel":
        context.user_data.pop("pending_edit", None)
        await query.edit_message_text("Gak jadi diedit.")
        return

    if data == "editorder_confirm":
        pending = context.user_data.pop("pending_edit", None)
        if not pending:
            await query.edit_message_text("Perubahannya udah gak ada / kadaluarsa, coba ulang.")
            return
        sheets = _sheets()
        no_invoice = pending["no_invoice"]
        found = bool(sheets.get_order_items(no_invoice))
        if not found:
            await query.edit_message_text(f"Invoice {no_invoice} gak ketemu lagi, mungkin udah berubah.")
            return
        if pending.get("updates"):
            sheets.edit_order_header(no_invoice, pending["updates"])
        for iu in pending.get("item_updates", []):
            sheets.edit_order_item_qty(
                no_invoice, iu["item_code"],
                qty_baru=iu.get("qty_baru"), harga_baru=iu.get("harga_baru"),
            )
        context.user_data["last_invoice"] = no_invoice
        await query.edit_message_text(
            f"✅ {no_invoice} udah diupdate. Lagi bikin ulang invoice & surat jalan...",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _kirim_invoice(update, context, no_invoice)
        await _kirim_surat_jalan(update, context, no_invoice)
        return

    if data == "cancelorder_cancel":
        context.user_data.pop("pending_cancel", None)
        await query.edit_message_text("Oke, gak jadi dibatalin.")
        return

    if data == "cancelorder_confirm":
        no_invoice = context.user_data.pop("pending_cancel", None)
        if not no_invoice:
            await query.edit_message_text("Udah gak ada order yang nunggu dibatalin, coba ulang.")
            return
        sheets = _sheets()
        ok = sheets.cancel_order(no_invoice)
        if not ok:
            await query.edit_message_text(f"Invoice {no_invoice} gak ketemu lagi.")
            return
        await query.edit_message_text(f"✅ {no_invoice} udah ditandai *Batal*.", parse_mode=ParseMode.MARKDOWN)
        return

    if data == "priceupdate_cancel":
        context.user_data.pop("pending_price_update", None)
        await query.edit_message_text("Gak jadi diubah.")
        return

    if data == "priceupdate_confirm":
        pending = context.user_data.pop("pending_price_update", None)
        if not pending:
            await query.edit_message_text("Perubahannya udah gak ada / kadaluarsa, coba ulang.")
            return
        sheets = _sheets()
        items = pending.get("items", [])
        for r in items:
            sheets.update_price(r["item_code"], harga_jual=r["harga_jual_baru"], harga_beli=r["harga_beli_baru"])
        nama_supplier = (pending.get("nama_supplier") or "").strip()
        if nama_supplier:
            sheets.add_supplier_if_new(nama_supplier)
        ringkas = ", ".join(r["nama"] for r in items)
        msg = f"✅ Harga *{ringkas}* udah diupdate di PriceList."
        if nama_supplier:
            msg += f" Supplier *{nama_supplier}* juga udah dicatat di tab Suppliers."
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    if data == "po_cancel":
        context.user_data.pop("pending_po", None)
        await query.edit_message_text("PO dibatalin.")
        return

    if data == "po_confirm":
        parsed = context.user_data.pop("pending_po", None)
        if not parsed:
            await query.edit_message_text("PO-nya udah gak ada / kadaluarsa, coba /po lagi.")
            return
        sheets = _sheets()
        no_po, total = sheets.add_purchase_order(parsed.get("nama_supplier", "-"), parsed["items"])
        await query.edit_message_text(
            f"✅ PO *{no_po}* disimpan ({rupiah(total)}), otomatis nambah utang ke supplier ini.",
            parse_mode=ParseMode.MARKDOWN,
        )
        img, pdf = documents.generate_po_image(no_po, parsed.get("nama_supplier", "-"), parsed["items"])
        await update.effective_chat.send_photo(photo=img, caption=f"Purchase Order {no_po}")
        await update.effective_chat.send_document(
            document=pdf, filename=f"{no_po}.pdf", caption="Versi PDF (siap print A4)"
        )
        return


# ---------------- PO FLOW ----------------

@owner_only
async def po_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting"] = "po_text"
    await update.effective_message.reply_text(
        "Oke, ketik detail belanjanya dalam 1 pesan. Contoh:\n\n"
        "\"PO ke CV Sumber Plastik Jaya: PP bening 40x60 100kg harga 28050, "
        "tulip putih 30 50kg harga 26000\"\n\n"
        "Boleh bahasa santai, nanti gua baca. Ketik /batal buat batalin."
    )


async def _process_po_text(update, context, text):
    await update.effective_message.reply_chat_action("typing")
    sheets = _sheets()
    try:
        parsed = parse_po_text(text, sheets.get_supplier_names())
    except Exception as e:
        logger.exception("gagal parse PO")
        await update.effective_message.reply_text(f"Waduh, gagal baca PO ini: {e}")
        return
    context.user_data["pending_po"] = parsed
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data="po_confirm"),
        InlineKeyboardButton("❌ Batal", callback_data="po_cancel"),
    ]])
    await _send_text(update, _po_preview_text(parsed), reply_markup=kb)


# ---------------- MAIN ----------------

async def on_startup(app: Application):
    # panggil sekali biar SheetsClient nyiapin skema tab kalau belum ada
    _sheets()
    logger.info("Asisten Plastik siap jalan.")


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN belum diisi di environment variables.")

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("groupid", groupid_cmd))
    app.add_handler(CommandHandler("pricelist", pricelist_cmd))
    app.add_handler(CommandHandler("lunas", lunas_cmd))
    app.add_handler(CommandHandler("invoice", invoice_cmd))
    app.add_handler(CommandHandler("suratjalan", suratjalan_cmd))
    app.add_handler(CommandHandler("piutang", piutang_cmd))
    app.add_handler(CommandHandler("po", po_cmd))
    app.add_handler(CommandHandler("bayarutang", bayarutang_cmd))
    app.add_handler(CommandHandler("utang", utang_cmd))
    app.add_handler(CommandHandler("kas", kas_cmd))
    app.add_handler(CommandHandler("laporanbulanan", laporanbulanan_cmd))
    app.add_handler(CommandHandler("batal", batal_cmd))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("Starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
