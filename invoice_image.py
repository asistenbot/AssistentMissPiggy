"""
Generate Invoice sebagai gambar yang rapi & bagus (buat dikirim ke customer
lewat Telegram/WhatsApp) -- beda sama receipt.py yang formatnya struk polos
buat printer thermal.
"""

from io import BytesIO
from PIL import Image, ImageDraw, ImageFont

import config

WIDTH = 520
PADDING = 30
HEADER_HEIGHT = 130
ROW_HEIGHT = 30
LOGO_ICON_PATH = "assets/logo_icon.png"
LOGO_WORDMARK_PATH = "assets/logo_wordmark_light.png"

# Palet warna bakery yang lebih hangat & "niat"
COLOR_BG = (250, 244, 233)          # krem lembut
COLOR_HEADER = (68, 40, 24)         # coklat tua elegan
COLOR_HEADER_TEXT_SUB = (216, 186, 140)
COLOR_GOLD = (191, 143, 63)         # aksen emas hangat
COLOR_TEXT = (52, 36, 26)
COLOR_MUTED = (140, 118, 98)
COLOR_LINE = (222, 202, 176)
COLOR_ROW_ALT = (243, 232, 211)     # selang-seling baris tabel
COLOR_TOTAL_BG = (191, 143, 63)     # kotak total emas
COLOR_TOTAL_TEXT = (54, 34, 12)

FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
F_TITLE = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 27)
F_SUB = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 14)
F_BODY = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 16)
F_BODY_BOLD = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 16)
F_SMALL = ImageFont.truetype(FONT_DIR + "DejaVuSans.ttf", 13)
F_TOTAL_LABEL = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 18)
F_TOTAL_VALUE = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 23)
F_MONOGRAM = ImageFont.truetype(FONT_DIR + "DejaVuSans-Bold.ttf", 20)


def rupiah(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")


def _dashed_line(draw, x1, y, x2, color, dash=5, gap=4, width=1):
    x = x1
    while x < x2:
        x_end = min(x + dash, x2)
        draw.line([x, y, x_end, y], fill=color, width=width)
        x += dash + gap


def generate_invoice_image(nama_customer: str, minggu_po: str, orders: list) -> BytesIO:
    x_left = PADDING
    x_right = WIDTH - PADDING
    col_qty = x_left + 230
    col_harga = x_right - 100

    # ---- Hitung total tinggi gambar dulu ----
    y = HEADER_HEIGHT + PADDING
    y += 24 + 22 + 22 + 20
    y += 6
    y += ROW_HEIGHT
    y += len(orders) * ROW_HEIGHT
    y += 10
    y += 22 + 22 + 16
    y += 50  # kotak total
    y += 16
    y += 20 + 22 + 22
    y += 20
    y += 34
    total_height = y + PADDING

    img = Image.new("RGB", (WIDTH, total_height), COLOR_BG)
    draw = ImageDraw.Draw(img)

    # ---- Header banner ----
    draw.rectangle([0, 0, WIDTH, HEADER_HEIGHT], fill=COLOR_HEADER)
    draw.rectangle([0, HEADER_HEIGHT, WIDTH, HEADER_HEIGHT + 5], fill=COLOR_GOLD)

    lockup_cy = HEADER_HEIGHT * 0.42  # sedikit di atas tengah, sisain ruang buat subtitle di bawah

    try:
        icon = Image.open(LOGO_ICON_PATH).convert("RGBA")
        icon_h = 54
        icon.thumbnail((icon_h * 3, icon_h), Image.LANCZOS)
        # thumbnail jaga rasio, hitung ulang biar tingginya pas icon_h
        scale = icon_h / icon.height
        icon = icon.resize((int(icon.width * scale), icon_h), Image.LANCZOS)

        wordmark = Image.open(LOGO_WORDMARK_PATH).convert("RGBA")
        wm_h = 46
        scale_wm = wm_h / wordmark.height
        wordmark = wordmark.resize((int(wordmark.width * scale_wm), wm_h), Image.LANCZOS)

        gap = 14
        total_w = icon.width + gap + wordmark.width
        start_x = int((WIDTH - total_w) / 2)

        icon_y = int(lockup_cy - icon.height / 2)
        img.paste(icon, (start_x, icon_y), icon)

        wm_x = start_x + icon.width + gap
        wm_y = int(lockup_cy - wordmark.height / 2)
        img.paste(wordmark, (wm_x, wm_y), wordmark)

    except FileNotFoundError:
        # Fallback kalau file logo nggak ketemu di server: pakai teks biasa
        draw.text((WIDTH / 2, lockup_cy), config.BUSINESS_NAME, font=F_TITLE, fill="white", anchor="mm")

    draw.text((WIDTH / 2, HEADER_HEIGHT * 0.8), "I N V O I C E",
              font=F_SUB, fill=COLOR_HEADER_TEXT_SUB, anchor="mm")

    y = HEADER_HEIGHT + PADDING
    draw.text((x_left, y), f"Kepada: {nama_customer}", font=F_BODY_BOLD, fill=COLOR_TEXT)
    y += 24
    draw.text((x_left, y), f"Tanggal Kirim/Ambil: {minggu_po}", font=F_BODY, fill=COLOR_MUTED)
    y += 22
    metode = orders[0].get("Metode", "-") if orders else "-"
    draw.text((x_left, y), f"Metode: {metode}", font=F_BODY, fill=COLOR_MUTED)
    y += 20

    _dashed_line(draw, x_left, y, x_right, COLOR_GOLD)
    y += 6

    # ---- Header tabel ----
    draw.text((x_left, y), "Item", font=F_BODY_BOLD, fill=COLOR_TEXT)
    draw.text((col_qty, y), "Qty", font=F_BODY_BOLD, fill=COLOR_TEXT)
    draw.text((col_harga, y), "Harga", font=F_BODY_BOLD, fill=COLOR_TEXT, anchor="ra")
    draw.text((x_right, y), "Subtotal", font=F_BODY_BOLD, fill=COLOR_TEXT, anchor="ra")
    y += ROW_HEIGHT
    draw.line([x_left, y - 8, x_right, y - 8], fill=COLOR_HEADER, width=1)

    total = 0
    for i, o in enumerate(orders):
        qty = int(o["Qty"])
        harga = int(o["Harga_Satuan"])
        subtotal = qty * harga
        total += subtotal

        if i % 2 == 1:
            draw.rectangle([x_left - 8, y - 6, x_right + 8, y + ROW_HEIGHT - 8], fill=COLOR_ROW_ALT)

        draw.text((x_left, y), str(o["Rasa"])[:18], font=F_BODY, fill=COLOR_TEXT)
        draw.text((col_qty, y), f"x{qty}", font=F_BODY, fill=COLOR_TEXT)
        draw.text((col_harga, y), rupiah(harga), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
        draw.text((x_right, y), rupiah(subtotal), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
        y += ROW_HEIGHT

    _dashed_line(draw, x_left, y, x_right, COLOR_LINE)
    y += 14

    ongkir = int(orders[0].get("Ongkir", 0) or 0) if orders else 0
    grand_total = total + ongkir

    draw.text((x_left, y), "Subtotal", font=F_BODY, fill=COLOR_MUTED)
    draw.text((x_right, y), rupiah(total), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
    y += 22
    draw.text((x_left, y), "Ongkir", font=F_BODY, fill=COLOR_MUTED)
    draw.text((x_right, y), rupiah(ongkir), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
    y += 26

    # ---- Kotak Total (rounded, emas) ----
    draw.rounded_rectangle([x_left - 8, y, x_right + 8, y + 46], radius=10, fill=COLOR_TOTAL_BG)
    draw.text((x_left + 4, y + 23), "TOTAL", font=F_TOTAL_LABEL, fill=COLOR_TOTAL_TEXT, anchor="lm")
    draw.text((x_right - 4, y + 23), rupiah(grand_total), font=F_TOTAL_VALUE, fill=COLOR_TOTAL_TEXT, anchor="rm")
    y += 62

    # ---- Info Pembayaran ----
    _dashed_line(draw, x_left, y, x_right, COLOR_GOLD)
    y += 16
    draw.text((x_left, y), "Pembayaran transfer ke:", font=F_SMALL, fill=COLOR_MUTED)
    y += 20
    draw.text((x_left, y), f"{config.BANK_NAME} — {config.BANK_ACCOUNT_NUMBER}",
              font=F_BODY_BOLD, fill=COLOR_TEXT)
    y += 22
    draw.text((x_left, y), f"a.n. {config.BANK_ACCOUNT_NAME}", font=F_BODY, fill=COLOR_MUTED)
    y += 20

    _dashed_line(draw, x_left, y, x_right, COLOR_LINE)
    y += 18
    draw.text((WIDTH / 2, y), f"Terima kasih sudah pesan di {config.BUSINESS_NAME}",
              font=F_SMALL, fill=COLOR_MUTED, anchor="mm")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
