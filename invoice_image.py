"""
Generate Invoice sebagai gambar yang rapi & bagus (buat dikirim ke customer
lewat Telegram/WhatsApp) -- beda sama receipt.py yang formatnya struk polos
buat printer thermal.
"""

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import config

WIDTH = 520
PADDING = 28
HEADER_HEIGHT = 100
ROW_HEIGHT = 28

COLOR_BG = (253, 247, 238)        # krem lembut
COLOR_HEADER = (120, 74, 46)      # coklat tua
COLOR_HEADER_SUB = (232, 210, 190)
COLOR_TEXT = (45, 32, 24)
COLOR_MUTED = (130, 110, 95)
COLOR_LINE = (216, 196, 175)
COLOR_ACCENT = (196, 106, 41)     # oranye
COLOR_TOTAL_BG = (243, 224, 200)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
F_TITLE = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 28)
F_SUB = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 15)
F_BODY = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 16)
F_BODY_BOLD = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 16)
F_SMALL = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 13)
F_TOTAL_LABEL = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 18)
F_TOTAL_VALUE = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 22)


def rupiah(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")


def generate_invoice_image(nama_customer: str, minggu_po: str, orders: list) -> BytesIO:
    x_left = PADDING
    x_right = WIDTH - PADDING
    col_qty = x_left + 230
    col_harga = x_right - 100

    # ---- Hitung total tinggi gambar dulu (nggak usah gambar beneran) ----
    y = HEADER_HEIGHT + PADDING
    y += 24 + 22 + 22 + 20  # kepada, tanggal, metode, spasi
    y += 6  # garis
    y += ROW_HEIGHT  # header tabel
    y += len(orders) * ROW_HEIGHT
    y += 10  # garis
    y += 22 + 22 + 16  # subtotal, ongkir, spasi
    total_box_top = y
    y += 46  # kotak total
    y += 16  # garis + spasi
    y += 20 + 22 + 22  # label pembayaran, bank, atas nama
    y += 20  # garis
    y += 30  # footer
    total_height = y + PADDING

    img = Image.new("RGB", (WIDTH, total_height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ---- Header banner ----
    draw.rectangle([0, 0, WIDTH, HEADER_HEIGHT], fill=COLOR_HEADER)
    draw.text((WIDTH / 2, HEADER_HEIGHT / 2 - 14), config.BUSINESS_NAME,
              font=F_TITLE, fill="white", anchor="mm")
    draw.text((WIDTH / 2, HEADER_HEIGHT / 2 + 18), "I N V O I C E",
              font=F_SUB, fill=COLOR_HEADER_SUB, anchor="mm")

    y = HEADER_HEIGHT + PADDING
    draw.text((x_left, y), f"Kepada: {nama_customer}", font=F_BODY_BOLD, fill=COLOR_TEXT)
    y += 24
    draw.text((x_left, y), f"Tanggal Kirim/Ambil: {minggu_po}", font=F_BODY, fill=COLOR_MUTED)
    y += 22
    metode = orders[0].get("Metode", "-") if orders else "-"
    draw.text((x_left, y), f"Metode: {metode}", font=F_BODY, fill=COLOR_MUTED)
    y += 20

    draw.line([x_left, y, x_right, y], fill=COLOR_LINE, width=1)
    y += 6

    # ---- Header tabel ----
    draw.text((x_left, y), "Item", font=F_BODY_BOLD, fill=COLOR_TEXT)
    draw.text((col_qty, y), "Qty", font=F_BODY_BOLD, fill=COLOR_TEXT)
    draw.text((col_harga, y), "Harga", font=F_BODY_BOLD, fill=COLOR_TEXT, anchor="ra")
    draw.text((x_right, y), "Subtotal", font=F_BODY_BOLD, fill=COLOR_TEXT, anchor="ra")
    y += ROW_HEIGHT
    draw.line([x_left, y - 6, x_right, y - 6], fill=COLOR_LINE, width=1)

    total = 0
    for o in orders:
        qty = int(o["Qty"])
        harga = int(o["Harga_Satuan"])
        subtotal = qty * harga
        total += subtotal
        draw.text((x_left, y), str(o["Rasa"])[:18], font=F_BODY, fill=COLOR_TEXT)
        draw.text((col_qty, y), f"x{qty}", font=F_BODY, fill=COLOR_TEXT)
        draw.text((col_harga, y), rupiah(harga), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
        draw.text((x_right, y), rupiah(subtotal), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
        y += ROW_HEIGHT

    draw.line([x_left, y, x_right, y], fill=COLOR_LINE, width=1)
    y += 14

    ongkir = int(orders[0].get("Ongkir", 0) or 0) if orders else 0
    grand_total = total + ongkir

    draw.text((x_left, y), "Subtotal", font=F_BODY, fill=COLOR_MUTED)
    draw.text((x_right, y), rupiah(total), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
    y += 22
    draw.text((x_left, y), "Ongkir", font=F_BODY, fill=COLOR_MUTED)
    draw.text((x_right, y), rupiah(ongkir), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
    y += 24

    # ---- Kotak Total ----
    draw.rectangle([x_left - 6, y, x_right + 6, y + 40], fill=COLOR_TOTAL_BG)
    draw.text((x_left, y + 20), "TOTAL", font=F_TOTAL_LABEL, fill=COLOR_ACCENT, anchor="lm")
    draw.text((x_right, y + 20), rupiah(grand_total), font=F_TOTAL_VALUE, fill=COLOR_ACCENT, anchor="rm")
    y += 56

    # ---- Info Pembayaran ----
    draw.line([x_left, y, x_right, y], fill=COLOR_LINE, width=1)
    y += 14
    draw.text((x_left, y), "Pembayaran transfer ke:", font=F_SMALL, fill=COLOR_MUTED)
    y += 20
    draw.text((x_left, y), f"{config.BANK_NAME} — {config.BANK_ACCOUNT_NUMBER}",
              font=F_BODY_BOLD, fill=COLOR_TEXT)
    y += 22
    draw.text((x_left, y), f"a.n. {config.BANK_ACCOUNT_NAME}", font=F_BODY, fill=COLOR_MUTED)
    y += 20

    draw.line([x_left, y, x_right, y], fill=COLOR_LINE, width=1)
    y += 18
    draw.text((WIDTH / 2, y), f"Terima kasih sudah pesan di {config.BUSINESS_NAME}",
              font=F_SMALL, fill=COLOR_MUTED, anchor="mm")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
