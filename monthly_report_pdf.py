"""
Generate Laporan Bulanan Supplier sebagai PDF ukuran A4, siap print.
Kalau periodenya lebih dari 1 bulan, breakdown-nya dipisah per bulan
(masing-masing tabel sendiri), baru di paling bawah ada TOTAL SEMUA BULAN.
"""

from io import BytesIO
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER

import config

COLOR_HEADER = colors.HexColor("#442818")
COLOR_LINE = colors.HexColor("#d8caa8")
COLOR_ACCENT = colors.HexColor("#bf8f3f")
COLOR_ACCENT_BG = colors.HexColor("#f3e8d3")


def rupiah(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")


def _build_month_table(rows, bulan_qty, bulan_bayar):
    data = [["Kategori", "Qty (pcs)", "Harga / pcs", "Subtotal"]]
    for kategori, qty, harga_dough, subtotal in rows:
        data.append([
            kategori,
            f"{qty:,}".replace(",", "."),
            rupiah(harga_dough),
            rupiah(subtotal),
        ])
    data.append(["", "", "Subtotal bulan ini", rupiah(bulan_bayar)])

    n_data_rows = len(rows)
    table = Table(data, colWidths=[6 * cm, 3 * cm, 3.5 * cm, 4 * cm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), COLOR_HEADER),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("GRID", (0, 0), (-1, n_data_rows), 0.5, COLOR_LINE),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, COLOR_HEADER),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("ROWBACKGROUNDS", (0, 1), (-1, n_data_rows), [colors.white, colors.HexColor("#f7f0e2")]),
    ]))
    return table


def _grand_total_by_category(month_results):
    """Gabungin semua bulan jadi total per KATEGORI aja (Roti semua bulan,
    Donat semua bulan, dst). Return: list of (kategori, total_qty, total_bayar),
    urut sesuai kemunculan pertama kali di data."""
    totals = {}
    urutan = []
    for bulan_label, rows, bulan_qty, bulan_bayar in month_results:
        for kategori, qty, harga_dough, subtotal in rows:
            if kategori not in totals:
                totals[kategori] = [0, 0]
                urutan.append(kategori)
            totals[kategori][0] += qty
            totals[kategori][1] += subtotal
    return [(k, totals[k][0], totals[k][1]) for k in urutan]


def generate_monthly_report_pdf(periode_label: str, month_results: list, grand_total_qty: int, grand_total_bayar: int) -> BytesIO:
    """
    month_results = list of (bulan_label, rows, bulan_qty, bulan_bayar)
    rows = list of (kategori, qty, harga_dough, subtotal)
    Return: BytesIO isi PDF, siap dikirim/di-print.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        topMargin=2.2 * cm, bottomMargin=2 * cm,
        leftMargin=2 * cm, rightMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleCustom", parent=styles["Title"], fontSize=20,
        textColor=COLOR_HEADER, alignment=TA_CENTER, spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "SubCustom", parent=styles["Normal"], fontSize=12,
        textColor=colors.grey, alignment=TA_CENTER, spaceAfter=6,
    )
    generated_style = ParagraphStyle(
        "GeneratedCustom", parent=styles["Normal"], fontSize=9,
        textColor=colors.grey, alignment=TA_CENTER, spaceAfter=24,
    )
    month_header_style = ParagraphStyle(
        "MonthHeader", parent=styles["Heading2"], fontSize=13,
        textColor=COLOR_HEADER, spaceBefore=6, spaceAfter=8,
    )

    elements = []
    elements.append(Paragraph(config.BUSINESS_NAME, title_style))
    elements.append(Paragraph(f"Periode: {periode_label}", sub_style))
    tz_now = datetime.now(date_helpers.get_timezone()).strftime("%d-%m-%Y %H:%M")
    elements.append(Paragraph(f"Digenerate: {tz_now} WIB", generated_style))

    for bulan_label, rows, bulan_qty, bulan_bayar in month_results:
        elements.append(Paragraph(bulan_label, month_header_style))
        elements.append(_build_month_table(rows, bulan_qty, bulan_bayar))
        elements.append(Spacer(1, 16))

    # ---- Kalau lebih dari 1 bulan, tabel breakdown per kategori (gabungan semua bulan) ----
    if len(month_results) > 1:
        kategori_totals = _grand_total_by_category(month_results)
        if kategori_totals:
            elements.append(Paragraph("Total per kategori (semua bulan)", month_header_style))
            data = [["Kategori", "Qty (pcs)", "Subtotal"]]
            for kategori, qty, bayar in kategori_totals:
                data.append([kategori, f"{qty:,}".replace(",", "."), rupiah(bayar)])
            kategori_table = Table(data, colWidths=[7 * cm, 3.5 * cm, 6 * cm])
            kategori_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), COLOR_ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), COLOR_HEADER),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 10),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("GRID", (0, 0), (-1, -1), 0.5, COLOR_LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f7f0e2")]),
            ]))
            elements.append(kategori_table)
            elements.append(Spacer(1, 16))

    # ---- Kotak TOTAL SEMUA BULAN ----
    elements.append(Spacer(1, 6))
    grand_data = [["TOTAL SEMUA BULAN", rupiah(grand_total_bayar)]]
    grand_table = Table(grand_data, colWidths=[10 * cm, 6.5 * cm])
    grand_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), COLOR_ACCENT_BG),
        ("TEXTCOLOR", (0, 0), (-1, -1), COLOR_HEADER),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (0, -1), 13),
        ("FONTSIZE", (1, 0), (1, -1), 15),
        ("TEXTCOLOR", (1, 0), (1, -1), COLOR_ACCENT),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
    ]))
    elements.append(grand_table)

    elements.append(Spacer(1, 14))
    footer_style = ParagraphStyle(
        "FooterCustom", parent=styles["Normal"], fontSize=10, textColor=colors.grey,
    )
    elements.append(Paragraph(f"Total Qty semua bulan: {grand_total_qty:,} pcs".replace(",", "."), footer_style))

    doc.build(elements)
    buf.seek(0)
    return buf
