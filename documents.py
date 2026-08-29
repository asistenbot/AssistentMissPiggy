"""
Generate Invoice, Surat Jalan, dan Purchase Order sebagai GAMBAR (PNG, buat
preview cepet & gampang di-forward lewat chat) SEKALIGUS PDF (buat di-print
rapi di kertas A4). Tiap fungsi generate_*_image balikin tuple
(png_buffer, pdf_buffer).
"""

import io
import os
import datetime

from PIL import Image, ImageDraw, ImageFont

import config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(BASE_DIR, "fonts")
LOGO_PATH = os.path.join(BASE_DIR, "logo.jpg")

WIDTH = 1000
MARGIN = 50
INK = (30, 35, 33)
MUTED = (110, 122, 114)
ACCENT = (15, 123, 108)
LINE = (220, 225, 219)
BG = (255, 255, 255)
SOFT_BG = (240, 244, 241)


def _font(name, size):
    path = os.path.join(FONT_DIR, name)
    return ImageFont.truetype(path, size)


def F_TITLE(size=34):
    return _font("DejaVuSans-Bold.ttf", size)


def F_BOLD(size=18):
    return _font("DejaVuSans-Bold.ttf", size)


def F_REG(size=17):
    return _font("DejaVuSans.ttf", size)


def F_SMALL(size=14):
    return _font("DejaVuSans.ttf", size)


def _pdf_from_image(img, page_width_in=7.48):
    """Ubah 1 gambar dokumen (invoice/surat jalan/PO) jadi PDF 1 halaman,
    diskalain biar lebar dokumennya ~190mm (7.48in) -- pas dicetak di
    kertas A4 (210mm) masih nyisa margin kiri-kanan yang wajar. Tingginya
    ngikutin proporsi gambar aslinya (dokumen kita ketinggiannya emang
    variabel tergantung jumlah item, bukan dipaksa pas A4 -- kalau order-nya
    pendek ya PDF-nya pendek, printer/PDF reader yang urus \"fit to page\"
    kalau perlu)."""
    resolution = img.width / page_width_in
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PDF", resolution=resolution)
    buf.seek(0)
    return buf


def rupiah(n):
    try:
        n = int(round(float(n)))
    except (TypeError, ValueError):
        n = 0
    return "Rp" + f"{n:,}".replace(",", ".")


def _wrap_text(draw, text, font, max_width):
    text = str(text)
    words = text.split()
    lines = []
    current = ""
    for w in words:
        trial = f"{current} {w}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines or [""]


def _load_logo(max_size=90):
    if not os.path.exists(LOGO_PATH):
        return None
    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo.thumbnail((max_size, max_size))
    return logo


def _header(draw, img, doc_title, doc_number, tanggal):
    logo = _load_logo()
    x = MARGIN
    if logo is not None:
        img.paste(logo, (x, MARGIN), logo)
        x += logo.width + 20
    draw.text((x, MARGIN), config.BUSINESS_NAME, font=F_BOLD(22), fill=INK)
    addr_lines = _wrap_text(draw, config.BUSINESS_ADDRESS, F_SMALL(14), 380)
    y = MARGIN + 30
    for line in addr_lines[:2]:
        draw.text((x, y), line, font=F_SMALL(14), fill=MUTED)
        y += 19

    # kanan atas: judul dokumen + nomor + tanggal
    title_w = draw.textlength(doc_title, font=F_TITLE(30))
    draw.text((WIDTH - MARGIN - title_w, MARGIN), doc_title, font=F_TITLE(30), fill=ACCENT)
    num_text = f"No. {doc_number}"
    num_w = draw.textlength(num_text, font=F_BOLD(16))
    draw.text((WIDTH - MARGIN - num_w, MARGIN + 42), num_text, font=F_BOLD(16), fill=INK)
    tgl_text = tanggal
    tgl_w = draw.textlength(tgl_text, font=F_SMALL(14))
    draw.text((WIDTH - MARGIN - tgl_w, MARGIN + 64), tgl_text, font=F_SMALL(14), fill=MUTED)

    line_y = MARGIN + 110
    draw.line([(MARGIN, line_y), (WIDTH - MARGIN, line_y)], fill=LINE, width=2)
    return line_y + 24


def _footer_note(draw, y, lines):
    for line in lines:
        draw.text((MARGIN, y), line, font=F_SMALL(13), fill=MUTED)
        y += 18
    return y


def _table_header(draw, y, columns):
    """columns: list of (label, x, width, align)"""
    draw.rectangle([(MARGIN, y), (WIDTH - MARGIN, y + 34)], fill=SOFT_BG)
    for label, x, w, align in columns:
        tw = draw.textlength(label, font=F_BOLD(14))
        tx = x + (w - tw if align == "right" else 0)
        draw.text((tx, y + 9), label, font=F_BOLD(14), fill=INK)
    return y + 34


def _now_tanggal():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"][now.weekday()]
    return f"{hari}, {now.day} {['','Januari','Februari','Maret','April','Mei','Juni','Juli','Agustus','September','Oktober','November','Desember'][now.month]} {now.year}"


def generate_invoice_image(no_invoice, nama_customer, no_hp, alamat, metode, items, ongkir=0):
    """items: list of dict {nama_item, qty, satuan, harga_satuan, subtotal}"""
    row_h = 30
    header_extra = 90  # info customer
    height = 300 + header_extra + row_h * (len(items) + 1) + 160
    img = Image.new("RGB", (WIDTH, int(height)), BG)
    draw = ImageDraw.Draw(img)

    y = _header(draw, img, "INVOICE", no_invoice, _now_tanggal())

    draw.text((MARGIN, y), "Ditagihkan kepada:", font=F_SMALL(13), fill=MUTED)
    y += 20
    draw.text((MARGIN, y), nama_customer, font=F_BOLD(19), fill=INK)
    y += 26
    if no_hp:
        draw.text((MARGIN, y), f"HP: {no_hp}", font=F_REG(14), fill=MUTED)
        y += 19
    if alamat:
        for line in _wrap_text(draw, alamat, F_REG(14), 500):
            draw.text((MARGIN, y), line, font=F_REG(14), fill=MUTED)
            y += 19
    draw.text((MARGIN, y), f"Metode: {metode}", font=F_REG(14), fill=MUTED)
    y += 30

    columns = [
        ("ITEM", MARGIN + 10, 380, "left"),
        ("QTY", MARGIN + 400, 90, "right"),
        ("HARGA", MARGIN + 520, 150, "right"),
        ("SUBTOTAL", MARGIN + 690, WIDTH - MARGIN - 10 - (MARGIN + 690), "right"),
    ]
    y = _table_header(draw, y, columns)

    total = 0
    for it in items:
        subtotal = float(it["qty"]) * float(it["harga_satuan"])
        total += subtotal
        row_top = y
        draw.text((MARGIN + 10, row_top + 6), str(it["nama_item"]), font=F_REG(15), fill=INK)
        qty_text = f"{it['qty']:g} {it.get('satuan', '')}".strip()
        qw = draw.textlength(qty_text, font=F_REG(15))
        draw.text((MARGIN + 400 + 90 - qw, row_top + 6), qty_text, font=F_REG(15), fill=INK)
        harga_text = rupiah(it["harga_satuan"])
        hw = draw.textlength(harga_text, font=F_REG(15))
        draw.text((MARGIN + 520 + 150 - hw, row_top + 6), harga_text, font=F_REG(15), fill=INK)
        sub_text = rupiah(subtotal)
        sw = draw.textlength(sub_text, font=F_REG(15))
        col4_x, col4_w = MARGIN + 690, WIDTH - MARGIN - 10 - (MARGIN + 690)
        draw.text((col4_x + col4_w - sw, row_top + 6), sub_text, font=F_REG(15), fill=INK)
        y += row_h
        draw.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=LINE, width=1)

    y += 16
    grand_total = total + float(ongkir or 0)
    for label, val in [("Subtotal", total), ("Ongkir", ongkir), ("TOTAL", grand_total)]:
        bold = label == "TOTAL"
        font = F_BOLD(18) if bold else F_REG(15)
        color = ACCENT if bold else INK
        label_w = draw.textlength(label, font=font)
        draw.text((WIDTH - MARGIN - 260, y), label, font=font, fill=color)
        val_text = rupiah(val)
        vw = draw.textlength(val_text, font=font)
        draw.text((WIDTH - MARGIN - vw, y), val_text, font=font, fill=color)
        y += 28 if bold else 22

    y += 20
    draw.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=LINE, width=1)
    y += 16
    draw.text((MARGIN, y), "Pembayaran transfer ke:", font=F_SMALL(13), fill=MUTED)
    y += 20
    draw.text(
        (MARGIN, y),
        f"{config.BANK_NAME} {config.BANK_ACCOUNT_NUMBER} a.n. {config.BANK_ACCOUNT_NAME}",
        font=F_BOLD(16), fill=INK,
    )
    y += 32
    draw.text((MARGIN, y), "Terima Kasih", font=F_SMALL(13), fill=MUTED)

    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG")
    png_buf.seek(0)
    pdf_buf = _pdf_from_image(img)
    return png_buf, pdf_buf


def generate_surat_jalan_image(no_surat_jalan, nama_customer, no_hp, alamat, metode, items, no_invoice_ref=None):
    """items: list of dict {nama_item, qty, satuan} -- TANPA harga, buat kurir."""
    row_h = 32
    height = 300 + row_h * (len(items) + 1) + 130
    img = Image.new("RGB", (WIDTH, int(height)), BG)
    draw = ImageDraw.Draw(img)

    y = _header(draw, img, "SURAT JALAN", no_surat_jalan, _now_tanggal())

    draw.text((MARGIN, y), "Kepada:", font=F_SMALL(13), fill=MUTED)
    y += 20
    draw.text((MARGIN, y), nama_customer, font=F_BOLD(19), fill=INK)
    y += 26
    if no_hp:
        draw.text((MARGIN, y), f"HP: {no_hp}", font=F_REG(14), fill=MUTED)
        y += 19
    if alamat:
        for line in _wrap_text(draw, alamat, F_REG(14), 500):
            draw.text((MARGIN, y), line, font=F_REG(14), fill=MUTED)
            y += 19
    draw.text((MARGIN, y), f"Metode: {metode}", font=F_REG(14), fill=MUTED)
    y += 20
    if no_invoice_ref:
        draw.text((MARGIN, y), f"Ref. Invoice: {no_invoice_ref}", font=F_REG(14), fill=MUTED)
        y += 19
    y += 10

    columns = [
        ("ITEM", MARGIN + 10, 700, "left"),
        ("QTY", MARGIN + 720, WIDTH - MARGIN - 10 - (MARGIN + 720), "right"),
    ]
    y = _table_header(draw, y, columns)

    for it in items:
        row_top = y
        draw.text((MARGIN + 10, row_top + 7), str(it["nama_item"]), font=F_REG(16), fill=INK)
        qty_text = f"{it['qty']:g} {it.get('satuan', '')}".strip()
        qw = draw.textlength(qty_text, font=F_REG(16))
        col2_x, col2_w = MARGIN + 720, WIDTH - MARGIN - 10 - (MARGIN + 720)
        draw.text((col2_x + col2_w - qw, row_top + 7), qty_text, font=F_REG(16), fill=INK)
        y += row_h
        draw.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=LINE, width=1)

    y += 50
    col_w = (WIDTH - 2 * MARGIN) // 2
    draw.line([(MARGIN, y), (MARGIN + col_w - 30, y)], fill=INK, width=1)
    draw.line([(MARGIN + col_w + 30, y), (WIDTH - MARGIN, y)], fill=INK, width=1)
    draw.text((MARGIN, y + 8), "Dikirim oleh", font=F_SMALL(13), fill=MUTED)
    draw.text((MARGIN + col_w + 30, y + 8), "Diterima oleh", font=F_SMALL(13), fill=MUTED)

    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG")
    png_buf.seek(0)
    pdf_buf = _pdf_from_image(img)
    return png_buf, pdf_buf


def generate_po_image(no_po, nama_supplier, items):
    """items: list of dict {nama_item, qty, satuan, harga_satuan}"""
    row_h = 30
    height = 300 + row_h * (len(items) + 1) + 110
    img = Image.new("RGB", (WIDTH, int(height)), BG)
    draw = ImageDraw.Draw(img)

    y = _header(draw, img, "PURCHASE ORDER", no_po, _now_tanggal())

    draw.text((MARGIN, y), "Kepada Supplier:", font=F_SMALL(13), fill=MUTED)
    y += 20
    draw.text((MARGIN, y), nama_supplier, font=F_BOLD(19), fill=INK)
    y += 40

    columns = [
        ("ITEM", MARGIN + 10, 380, "left"),
        ("QTY", MARGIN + 400, 90, "right"),
        ("HARGA", MARGIN + 520, 150, "right"),
        ("SUBTOTAL", MARGIN + 690, WIDTH - MARGIN - 10 - (MARGIN + 690), "right"),
    ]
    y = _table_header(draw, y, columns)

    total = 0
    for it in items:
        subtotal = float(it["qty"]) * float(it["harga_satuan"])
        total += subtotal
        row_top = y
        draw.text((MARGIN + 10, row_top + 6), str(it["nama_item"]), font=F_REG(15), fill=INK)
        qty_text = f"{it['qty']:g} {it.get('satuan', '')}".strip()
        qw = draw.textlength(qty_text, font=F_REG(15))
        draw.text((MARGIN + 400 + 90 - qw, row_top + 6), qty_text, font=F_REG(15), fill=INK)
        harga_text = rupiah(it["harga_satuan"])
        hw = draw.textlength(harga_text, font=F_REG(15))
        draw.text((MARGIN + 520 + 150 - hw, row_top + 6), harga_text, font=F_REG(15), fill=INK)
        sub_text = rupiah(subtotal)
        sw = draw.textlength(sub_text, font=F_REG(15))
        col4_x, col4_w = MARGIN + 690, WIDTH - MARGIN - 10 - (MARGIN + 690)
        draw.text((col4_x + col4_w - sw, row_top + 6), sub_text, font=F_REG(15), fill=INK)
        y += row_h
        draw.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=LINE, width=1)

    y += 16
    label = "TOTAL"
    val_text = rupiah(total)
    draw.text((WIDTH - MARGIN - 260, y), label, font=F_BOLD(18), fill=ACCENT)
    vw = draw.textlength(val_text, font=F_BOLD(18))
    draw.text((WIDTH - MARGIN - vw, y), val_text, font=F_BOLD(18), fill=ACCENT)
    y += 40

    draw.text((MARGIN, y), "Mohon konfirmasi ketersediaan barang. Terima kasih.", font=F_SMALL(13), fill=MUTED)

    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG")
    png_buf.seek(0)
    pdf_buf = _pdf_from_image(img)
    return png_buf, pdf_buf
