"""
Generate Surat Jalan sebagai gambar struk (siap print via app printer
Bluetooth kayak RawBT) — bukan cuma teks biasa.

Cara pakai di HP: tap gambar surat jalan di Telegram -> Share -> pilih
app printer Bluetooth (misal RawBT) -> print.
"""

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import config

# Lebar kertas thermal umum:
#   58mm printer  -> 384px (paling umum buat printer Bluetooth portable kecil)
#   80mm printer  -> 576px
# Ganti angka ini kalau printer lo beda ukuran.
RECEIPT_WIDTH = 384
MARGIN = 16
LINE_HEIGHT = 26

FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

FONT_NORMAL = ImageFont.truetype(FONT_PATH_REGULAR, 20)
FONT_BOLD = ImageFont.truetype(FONT_PATH_BOLD, 24)
FONT_SMALL = ImageFont.truetype(FONT_PATH_REGULAR, 16)


def _wrap_text(text, font, max_width, draw):
    words = text.split(" ")
    lines = []
    current = ""
    for w in words:
        test = (current + " " + w).strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines or [""]


def generate_surat_jalan_image(nama_customer: str, minggu_po: str, orders: list) -> BytesIO:
    usable_width = RECEIPT_WIDTH - 2 * MARGIN

    # Susun dulu daftar baris konten (belum digambar), biar tinggi gambar
    # bisa dihitung pas -- nggak kepotong, nggak kelebihan kosong.
    lines = []  # (text, font, align) -- align: "center" / "left" / "left_wrap"

    lines.append((config.BUSINESS_NAME, FONT_BOLD, "center"))
    lines.append(("SURAT JALAN", FONT_BOLD, "center"))
    lines.append(("-" * 32, FONT_SMALL, "center"))
    lines.append((f"Kirim: {minggu_po} ({config.DELIVERY_WINDOW})", FONT_NORMAL, "left_wrap"))
    lines.append((f"Nama : {nama_customer}", FONT_NORMAL, "left"))

    no_hp = orders[0].get("No_HP", "-") if orders else "-"
    metode = orders[0].get("Metode", "-") if orders else "-"
    lines.append((f"HP   : {no_hp}", FONT_NORMAL, "left"))
    lines.append((f"Cara : {metode}", FONT_NORMAL, "left"))

    if orders and metode == "Kirim":
        alamat = orders[0].get("Alamat", "-")
        lines.append(("Alamat:", FONT_NORMAL, "left"))
        lines.append((alamat, FONT_NORMAL, "left_wrap"))
    else:
        lines.append((f"Ambil di: {config.PICKUP_ADDRESS}", FONT_NORMAL, "left_wrap"))

    lines.append(("-" * 32, FONT_SMALL, "center"))
    lines.append(("BARANG:", FONT_BOLD, "left"))

    total_qty = 0
    for o in orders:
        qty = int(o["Qty"])
        total_qty += qty
        lines.append((f"- {o['Rasa']} ({o['Kategori']}) x{qty}", FONT_NORMAL, "left_wrap"))

    lines.append(("-" * 32, FONT_SMALL, "center"))
    lines.append((f"Total item: {total_qty} pcs", FONT_BOLD, "left"))
    lines.append(("", FONT_SMALL, "left"))  # spasi kosong buat sobek kertas

    # Canvas dummy dulu buat ngukur tinggi yang dibutuhkan
    dummy = Image.new("RGB", (RECEIPT_WIDTH, 10), "white")
    draw = ImageDraw.Draw(dummy)

    rendered = []  # (text, font, align, y)
    y = MARGIN
    for text, font, align in lines:
        if align == "left_wrap":
            for wline in _wrap_text(text, font, usable_width, draw):
                rendered.append((wline, font, "left", y))
                y += LINE_HEIGHT
        else:
            rendered.append((text, font, align, y))
            y += LINE_HEIGHT

    total_height = y + MARGIN

    # Gambar beneran di canvas ukuran pas
    img = Image.new("RGB", (RECEIPT_WIDTH, total_height), "white")
    draw = ImageDraw.Draw(img)

    for text, font, align, y in rendered:
        if align == "center":
            w = draw.textlength(text, font=font)
            x = (RECEIPT_WIDTH - w) / 2
        else:
            x = MARGIN
        draw.text((x, y), text, font=font, fill="black")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
