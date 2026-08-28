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
LINE_HEIGHT = 34

FONT_PATH_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_PATH_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# Font digedein dari versi sebelumnya biar gampang dibaca abis keprint kecil
# di kertas 58mm.
FONT_NORMAL = ImageFont.truetype(FONT_PATH_REGULAR, 23)
FONT_BOLD = ImageFont.truetype(FONT_PATH_BOLD, 26)
FONT_SMALL = ImageFont.truetype(FONT_PATH_REGULAR, 17)
FONT_HEADER = ImageFont.truetype(FONT_PATH_BOLD, 21)   # header tiap kategori
FONT_HUGE = ImageFont.truetype(FONT_PATH_BOLD, 29)      # CARA: AMBIL/DIANTAR, paling nonjol

# Urutan kategori yang mau ditampilin duluan di surat jalan (ngikutin urutan
# menu di halaman order). Kategori lain yang nggak ada di list ini tetep
# ditampilin, cuma nyusul di belakang urut abjad -- jadi produk baru nggak
# bakal ilang walau lupa ditambahin ke sini.
CATEGORY_ORDER = ["Roti", "Roti Gandum", "Donat", "Roti Tawar"]


INDENT_ITEM = 22  # geser ke kanan dikit buat rasa di bawah header kategori


def _is_delivery_metode(metode):
    """True kalau metode-nya berarti 'dikirim' -- ada 2 istilah yang beredar
    di sistem ini: halaman order web pakai 'Diantar', tapi order yang di-AI
    parse dari chat manual (ai_parser.py) pakai 'Kirim'. Dicek berbasis
    substring biar dua-duanya (dan variasinya kayak 'Dikirim') kena -- surat
    jalan buat order manual sempet salah nampilin 'Ambil di: ...' padahal
    order-nya harusnya dikirim ke alamat customer, gara-gara di sini cuma
    ngecek == 'Diantar' persis."""
    m = (metode or "").strip().lower()
    return "antar" in m or "kirim" in m


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


def _group_by_category(orders):
    """Kelompokin baris order per Kategori, urut sesuai CATEGORY_ORDER dulu,
    sisanya (kategori yang nggak ada di list) nyusul urut abjad."""
    by_kategori = {}
    for o in orders:
        by_kategori.setdefault(o["Kategori"], []).append(o)

    urutan = [k for k in CATEGORY_ORDER if k in by_kategori]
    sisanya = sorted(k for k in by_kategori if k not in CATEGORY_ORDER)
    urutan += sisanya

    return [(k, by_kategori[k]) for k in urutan]


def generate_surat_jalan_image(nama_customer: str, minggu_po: str, orders: list, box_groups: list = None) -> BytesIO:
    usable_width = RECEIPT_WIDTH - 2 * MARGIN

    # Tanggal_Kirim itu kolom BARU (opsional) di Sheets -- kalau admin nggak
    # nentuin tanggal custom pas order, kolom ini kosong dan kita balik ke
    # default lama: minggu_po (Kamis PO minggu berjalan).
    tanggal_kirim = (orders[0].get("Tanggal_Kirim") if orders else None) or minggu_po
    metode = orders[0].get("Metode", "-") if orders else "-"
    no_hp = orders[0].get("No_HP", "-") if orders else "-"
    alamat = orders[0].get("Alamat", "-") if orders else "-"

    # Susun dulu daftar baris konten (belum digambar), biar tinggi gambar
    # bisa dihitung pas -- nggak kepotong, nggak kelebihan kosong.
    # Tiap entri: (text, font, align, indent) -- align: "center"/"left"/
    # "left_wrap"; indent cuma kepake buat align "left"/"left_wrap".
    lines = []

    lines.append((config.BUSINESS_NAME, FONT_BOLD, "center", 0))
    lines.append(("SURAT JALAN", FONT_BOLD, "center", 0))
    lines.append(("=" * 22, FONT_SMALL, "center", 0))
    lines.append((f"Kirim: {tanggal_kirim}", FONT_NORMAL, "left_wrap", 0))
    lines.append((f"({config.DELIVERY_WINDOW})", FONT_SMALL, "left_wrap", 0))
    lines.append((f"Nama : {nama_customer}", FONT_NORMAL, "left_wrap", 0))
    lines.append((f"HP   : {no_hp}", FONT_NORMAL, "left", 0))

    lines.append(("-" * 22, FONT_SMALL, "center", 0))
    lines.append(("PESANAN:", FONT_BOLD, "left", 0))

    total_qty = sum(int(o["Qty"]) for o in orders)

    if box_groups:
        # Order ini pakai satuan box -- langsung tampilin rincian per box aja
        # (BUKAN daftar per-kategori/rasa biasa), soalnya buat packing lebih
        # jelas ngikutin pembagian box aslinya drpd total per rasa yang udah
        # digabung. Total per rasa tetep bisa dicek di invoice kalau perlu.
        for grp in box_groups:
            jumlah = grp.get("jumlah_box", "?")
            desc = ", ".join(
                f"{i.get('rasa', '?')} x{i.get('qty_per_box', '?')}" for i in grp.get("items", [])
            )
            lines.append((f"{jumlah} box: {desc}", FONT_NORMAL, "left_wrap", INDENT_ITEM))
    else:
        for kategori, items in _group_by_category(orders):
            lines.append((f"» {kategori}", FONT_HEADER, "left", 0))
            for o in items:
                qty = int(o["Qty"])
                lines.append((f"{o['Rasa']}  x{qty}", FONT_NORMAL, "left_wrap", INDENT_ITEM))

    lines.append(("-" * 22, FONT_SMALL, "center", 0))
    lines.append((f"Total item: {total_qty} pcs", FONT_BOLD, "left", 0))
    lines.append(("=" * 22, FONT_SMALL, "center", 0))

    # Info Ambil/Diantar sengaja ditaruh PALING BAWAH (nempel sama total),
    # biar paling gampang kebaca sama bagian packing pas terakhir liat
    # surat jalan ini sebelum barangnya dibungkus/diberangkatin.
    is_delivery = _is_delivery_metode(metode)
    metode_label = "DIANTAR" if is_delivery else "AMBIL SENDIRI"
    lines.append(("", FONT_SMALL, "left", 0))
    lines.append((f"CARA: {metode_label}", FONT_HUGE, "center", 0))
    if is_delivery:
        lines.append(("Alamat:", FONT_BOLD, "left", 0))
        lines.append((alamat, FONT_NORMAL, "left_wrap", 0))
    else:
        lines.append((f"Ambil di: {config.PICKUP_ADDRESS}", FONT_NORMAL, "left_wrap", 0))

    lines.append(("", FONT_SMALL, "left", 0))  # spasi kosong buat sobek kertas

    # Canvas dummy dulu buat ngukur tinggi yang dibutuhkan
    dummy = Image.new("RGB", (RECEIPT_WIDTH, 10), "white")
    draw = ImageDraw.Draw(dummy)

    rendered = []  # (text, font, align, indent, y)
    y = MARGIN
    for text, font, align, indent in lines:
        if align == "left_wrap":
            for wline in _wrap_text(text, font, usable_width - indent, draw):
                rendered.append((wline, font, "left", indent, y))
                y += LINE_HEIGHT
        else:
            rendered.append((text, font, align, indent, y))
            y += LINE_HEIGHT

    total_height = y + MARGIN

    # Gambar beneran di canvas ukuran pas
    img = Image.new("RGB", (RECEIPT_WIDTH, total_height), "white")
    draw = ImageDraw.Draw(img)

    for text, font, align, indent, y in rendered:
        if align == "center":
            w = draw.textlength(text, font=font)
            x = (RECEIPT_WIDTH - w) / 2
        else:
            x = MARGIN + indent
        draw.text((x, y), text, font=font, fill="black")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf
