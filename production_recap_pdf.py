"""
Generate Rekap Produksi sebagai PDF ukuran A4, siap print -- dikelompokin
per kategori (Roti, Roti Gandum, Donat, dst), tiap kategori ada tabel
rasa+qty sendiri plus subtotal, dan di paling bawah ada TOTAL SEMUA PRODUK.

Gaya visualnya SENGAJA disamain persis kayak monthly_report_pdf.py (warna,
font, tata letak) biar konsisten kalau kedua PDF ini dibuka berdampingan.
"""

from io import BytesIO
from datetime import datetime

import date_helpers

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

import config
import date_helpers

COLOR_HEADER = colors.HexColor("#442818")
COLOR_LINE = colors.HexColor("#d8caa8")
COLOR_ACCENT = colors.HexColor("#bf8f3f")
COLOR_ACCENT_BG = colors.HexColor("#f3e8d3")


def _build_category_table(items, col_widths=None):
    """items = list of (rasa, qty), sudah diurutin dari qty terbesar.
    col_widths = [lebar_rasa, lebar_qty], default lebar penuh 1 kolom.
    Return (table, subtotal_qty)."""
    if col_widths is None:
        col_widths = [10.5 * cm, 6 * cm]
    data = [["Rasa", "Qty (pcs)"]]
    subtotal = 0
    for rasa, qty in items:
        data.append([rasa, f"{qty:,}".replace(",", ".")])
        subtotal += qty
    data.append(["Subtotal", f"{subtotal:,}".replace(",", ".")])

    n_data_rows = len(items)
    table = Table(data, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_HEADER),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 8.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("GRID", (0, 0), (-1, n_data_rows), 0.5, COLOR_LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, COLOR_HEADER),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 8.5),
        ("FONTSIZE", (0, 1), (-1, n_data_rows), 8.5),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("ROWBACKGROUNDS", (0, 1), (-1, n_data_rows), [colors.white, colors.HexColor("#f7f0e2")]),
    ]))
    return table, subtotal


def generate_production_recap_pdf(judul: str, subtitle: str, orders: list) -> BytesIO:
    """
    judul = judul utama, misal "REKAP PRODUKSI -- Minggu PO 2026-08-27"
            atau "REKAP PRODUKSI -- Ci Meyvany" (buat versi per-customer).
    subtitle = baris kecil di bawah judul, misal "(Kirim: THU 14:00 - 15:00)"
               atau "(Tanggal Kirim: 2026-08-25)".
    orders = list of dict record Sheets (harus punya key Kategori, Rasa, Qty).

    Return: BytesIO isi PDF, siap dikirim/di-print.
    """
    recap = {}  # {(kategori, rasa): total_qty}
    for o in orders:
        key = (o["Kategori"], o["Rasa"])
        recap[key] = recap.get(key, 0) + int(o["Qty"])

    by_category = {}
    urutan_kategori = []
    for (kategori, rasa), qty in recap.items():
        if kategori not in by_category:
            by_category[kategori] = []
            urutan_kategori.append(kategori)
        by_category[kategori].append((rasa, qty))

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=1.4 * cm, bottomMargin=1.2 * cm,
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom", parent=styles["Title"], fontSize=15,
        textColor=COLOR_HEADER, alignment=TA_CENTER, spaceAfter=2,
    )
    sub_style = ParagraphStyle(
        "SubCustom", parent=styles["Normal"], fontSize=9.5,
        textColor=colors.grey, alignment=TA_CENTER, spaceAfter=3,
    )
    generated_style = ParagraphStyle(
        "GeneratedCustom", parent=styles["Normal"], fontSize=7.5,
        textColor=colors.grey, alignment=TA_CENTER, spaceAfter=10,
    )
    kategori_header_style = ParagraphStyle(
        "KategoriHeader", parent=styles["Heading2"], fontSize=10.5,
        textColor=COLOR_HEADER, spaceBefore=2, spaceAfter=3,
    )

    elements = []
    elements.append(Paragraph(config.BUSINESS_NAME, title_style))
    elements.append(Paragraph(judul, sub_style))
    if subtitle:
        elements.append(Paragraph(subtitle, sub_style))
    tz_now = datetime.now(date_helpers.get_timezone()).strftime("%d-%m-%Y %H:%M")
    elements.append(Paragraph(f"Digenerate: {tz_now} WIB", generated_style))

    grand_total = 0
    if not by_category:
        elements.append(Paragraph("Belum ada order.", styles["Normal"]))

    # ---- Layout 3 KOLOM SEJAJAR dengan urutan TETAP: Donat - Roti Gandum -
    # Roti (gak tergantung urutan random data, biar konsisten tiap minggu).
    # Kategori LAIN di luar 3 itu (misal Bun Polos, Roti Tawar, Roti Tawar
    # Loaf) otomatis ditumpuk DI BAWAH kolom Donat (kolom paling kiri),
    # bukan bikin baris baru -- soalnya kategori-kategori itu biasanya kecil
    # jumlahnya, jadi lebih hemat tempat ditumpuk drpd makan 1 baris sendiri. ----
    col_w = [3.7 * cm, 1.7 * cm]  # lebar Rasa & Qty per kategori (versi sempit, 3 kolom sejajar)
    PRIMARY_ORDER = ["Donat", "Roti Gandum", "Roti"]
    col_contents = [[], [], []]

    for idx, kategori in enumerate(PRIMARY_ORDER):
        if kategori in by_category:
            items_sorted = sorted(by_category[kategori], key=lambda x: -x[1])
            table, subtotal = _build_category_table(items_sorted, col_widths=col_w)
            grand_total += subtotal
            col_contents[idx].append(Paragraph(kategori, kategori_header_style))
            col_contents[idx].append(table)
            col_contents[idx].append(Spacer(1, 8))

    kategori_ekstra = [k for k in urutan_kategori if k not in PRIMARY_ORDER]
    for kategori in kategori_ekstra:
        items_sorted = sorted(by_category[kategori], key=lambda x: -x[1])
        table, subtotal = _build_category_table(items_sorted, col_widths=col_w)
        grand_total += subtotal
        col_contents[0].append(Paragraph(kategori, kategori_header_style))
        col_contents[0].append(table)
        col_contents[0].append(Spacer(1, 8))

    if by_category:
        row = [content if content else [Spacer(1, 1)] for content in col_contents]
        wrapper = Table([row], colWidths=[5.6 * cm] * 3)
        wrapper.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ]))
        elements.append(wrapper)

    # ---- Kotak TOTAL SEMUA PRODUK ----
    if by_category:
        elements.append(Spacer(1, 2))
        grand_data = [["TOTAL SEMUA PRODUK", f"{grand_total:,} pcs".replace(",", ".")]]
        grand_table = Table(grand_data, colWidths=[10 * cm, 6.5 * cm])
        grand_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), COLOR_ACCENT_BG),
            ("TEXTCOLOR", (0, 0), (-1, -1), COLOR_HEADER),
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (0, -1), 11),
            ("FONTSIZE", (1, 0), (1, -1), 12.5),
            ("TEXTCOLOR", (1, 0), (1, -1), COLOR_ACCENT),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ]))
        elements.append(grand_table)

    doc.build(elements)
    buf.seek(0)
    return buf
