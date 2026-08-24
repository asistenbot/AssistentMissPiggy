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
LOGO_ICON_PATH = "logo_icon.png"
LOGO_WORDMARK_PATH = "logo_wordmark_light.png"

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


def _wrap_text(text, font, max_width, draw):
    """Pecah teks jadi beberapa baris biar nggak kepotong keluar canvas --
    dipakai buat baris 'Rincian Box' yang panjangnya nggak nentu (nggak
    kayak nama produk yang udah dipotong manual jadi 18 karakter)."""
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


def _format_box_group_lines(box_groups, font, max_width, draw):
    """Ubah box_groups (list of {"jumlah_box", "items":[{"rasa","qty_per_box"}]})
    jadi list baris teks yang siap digambar, sudah di-wrap biar muat lebar
    invoice. Return list kosong kalau box_groups kosong/None."""
    if not box_groups:
        return []
    wrapped = []
    for grp in box_groups:
        jumlah = grp.get("jumlah_box", "?")
        desc = ", ".join(
            f"{i.get('rasa', '?')} x{i.get('qty_per_box', '?')}" for i in grp.get("items", [])
        )
        text = f"• {jumlah} box: {desc}"
        wrapped.extend(_wrap_text(text, font, max_width, draw))
    return wrapped


def generate_invoice_image(nama_customer: str, minggu_po: str, orders: list, box_groups: list = None) -> BytesIO:
    x_left = PADDING
    x_right = WIDTH - PADDING
    col_qty = x_left + 230
    col_harga = x_right - 100
    CATEGORY_ROW_HEIGHT = 28
    BOX_LINE_HEIGHT = 19

    # Tanggal_Kirim itu kolom OPSIONAL di Sheets -- kalau admin nggak nentuin
    # tanggal custom (misal 'besok'), balik ke default lama: minggu_po
    # (Kamis PO minggu berjalan), jadi invoice yang udah ada nggak berubah
    # kalau fitur ini nggak dipakai sama sekali.
    tanggal_kirim = (orders[0].get("Tanggal_Kirim") if orders else None) or minggu_po

    # Kelompokin item per kategori, Donat selalu di paling atas, sisanya
    # ikutin urutan config.CATEGORIES.
    kategori_order = ["Donat"] + [c for c in config.CATEGORIES if c != "Donat"]

    def kategori_rank(k):
        try:
            return kategori_order.index(k)
        except ValueError:
            return len(kategori_order)

    grouped = {}
    for o in orders:
        grouped.setdefault(o["Kategori"], []).append(o)
    kategori_list = sorted(grouped.keys(), key=kategori_rank)

    # Ukur dulu baris "Rincian Box" (kalau ada) pakai canvas dummy, biar bisa
    # dipakai buat hitung total tinggi gambar DAN dipakai lagi pas gambar
    # beneran -- dihitung sekali aja biar konsisten antara pass pertama
    # (hitung tinggi) dan pass kedua (gambar).
    dummy_img = Image.new("RGB", (WIDTH, 10), COLOR_BG)
    dummy_draw = ImageDraw.Draw(dummy_img)
    box_lines = _format_box_group_lines(box_groups, F_SMALL, x_right - x_left, dummy_draw)

    # ---- Hitung total tinggi gambar dulu ----
    y = HEADER_HEIGHT + PADDING
    y += 24 + 22 + 22 + 20
    y += 6
    y += ROW_HEIGHT
    for kategori in kategori_list:
        y += CATEGORY_ROW_HEIGHT
        y += len(grouped[kategori]) * ROW_HEIGHT
    if box_lines:
        y += 24 + len(box_lines) * BOX_LINE_HEIGHT + 8
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
    draw.text((x_left, y), f"Tanggal Kirim/Ambil: {tanggal_kirim}", font=F_BODY, fill=COLOR_MUTED)
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
    row_i = 0
    for kategori in kategori_list:
        # Label kategori (background gelap, teks emas)
        draw.rectangle([x_left - 8, y - 4, x_right + 8, y + CATEGORY_ROW_HEIGHT - 8], fill=COLOR_HEADER)
        draw.text((x_left, y + (CATEGORY_ROW_HEIGHT - 8) / 2), kategori.upper(),
                  font=F_BODY_BOLD, fill=COLOR_GOLD, anchor="lm")
        y += CATEGORY_ROW_HEIGHT

        for o in grouped[kategori]:
            qty = int(o["Qty"])
            harga = int(o["Harga_Satuan"])
            subtotal = qty * harga
            total += subtotal

            if row_i % 2 == 1:
                draw.rectangle([x_left - 8, y - 6, x_right + 8, y + ROW_HEIGHT - 8], fill=COLOR_ROW_ALT)
            row_i += 1

            draw.text((x_left, y), str(o["Rasa"])[:18], font=F_BODY, fill=COLOR_TEXT)
            draw.text((col_qty, y), f"x{qty}", font=F_BODY, fill=COLOR_TEXT)
            draw.text((col_harga, y), rupiah(harga), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
            draw.text((x_right, y), rupiah(subtotal), font=F_BODY, fill=COLOR_TEXT, anchor="ra")
            y += ROW_HEIGHT

    # ---- Rincian Box (opsional, cuma muncul kalau order-nya pakai satuan box) ----
    if box_lines:
        draw.text((x_left, y), "Rincian Box:", font=F_BODY_BOLD, fill=COLOR_TEXT)
        y += 22
        for line in box_lines:
            draw.text((x_left, y), line, font=F_SMALL, fill=COLOR_MUTED)
            y += BOX_LINE_HEIGHT
        y += 10

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
