"""
Generate teks Invoice, Surat Jalan, Rekap Produksi, dan Laporan Bulanan.
Semua dalam format teks rapi (Markdown Telegram) biar gampang di-forward/copas.
"""

import calendar
import datetime

import config


def rupiah(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")


def build_invoice(nama_customer: str, minggu_po: str, orders: list) -> str:
    if not orders:
        return f"Nggak ada order atas nama *{nama_customer}* untuk minggu PO {minggu_po}."

    lines = []
    lines.append(f"*INVOICE — {config.BUSINESS_NAME}*")
    lines.append(f"Kepada: {nama_customer}")
    lines.append(f"Tanggal Kirim/Ambil: {minggu_po}")
    lines.append(f"Metode: {orders[0].get('Metode', '-')}")
    lines.append("")
    lines.append("Rincian Pesanan:")

    total = 0
    for o in orders:
        qty = int(o["Qty"])
        harga = int(o["Harga_Satuan"])
        subtotal = qty * harga
        total += subtotal
        lines.append(f"- {o['Rasa']} ({o['Kategori']}) x{qty} @ {rupiah(harga)} = {rupiah(subtotal)}")

    ongkir = int(orders[0].get("Ongkir", 0) or 0)
    grand_total = total + ongkir

    lines.append("")
    lines.append(f"Subtotal: {rupiah(total)}")
    lines.append(f"Ongkir: {rupiah(ongkir)}")
    lines.append(f"*Total: {rupiah(grand_total)}*")
    lines.append("")
    lines.append("Pembayaran transfer ke:")
    lines.append(f"{config.BANK_NAME} — {config.BANK_ACCOUNT_NUMBER}")
    lines.append(f"a.n. {config.BANK_ACCOUNT_NAME}")
    lines.append("")
    lines.append("Terima kasih sudah pesan di Miss Piggy 🐷🍞")

    return "\n".join(lines)


def build_surat_jalan(nama_customer: str, minggu_po: str, orders: list) -> str:
    if not orders:
        return f"Nggak ada order atas nama *{nama_customer}* untuk minggu PO {minggu_po}."

    lines = []
    lines.append(f"*SURAT JALAN — {config.BUSINESS_NAME}*")
    lines.append(f"Tanggal Kirim: {minggu_po} ({config.DELIVERY_WINDOW})")
    lines.append(f"Nama: {nama_customer}")
    lines.append(f"No HP: {orders[0].get('No_HP', '-')}")
    lines.append(f"Metode: {orders[0].get('Metode', '-')}")
    if orders[0].get("Metode") == "Kirim":
        lines.append(f"Alamat: {orders[0].get('Alamat', '-')}")
    else:
        lines.append(f"Ambil di: {config.PICKUP_ADDRESS}")
    lines.append("")
    lines.append("Barang:")
    for o in orders:
        lines.append(f"- {o['Rasa']} ({o['Kategori']}) x{int(o['Qty'])}")

    return "\n".join(lines)


def build_production_recap(minggu_po: str, orders: list) -> str:
    if not orders:
        return f"Belum ada order masuk untuk minggu PO {minggu_po}."

    recap = {}  # {(kategori, rasa): total_qty}
    for o in orders:
        key = (o["Kategori"], o["Rasa"])
        recap[key] = recap.get(key, 0) + int(o["Qty"])

    by_category = {}
    for (kategori, rasa), qty in recap.items():
        by_category.setdefault(kategori, []).append((rasa, qty))

    lines = [f"*REKAP PRODUKSI — Minggu PO {minggu_po}*"]
    lines.append(f"(Kirim: {config.DELIVERY_DAY.upper()} {config.DELIVERY_WINDOW})")
    grand_total = 0
    for kategori, items in by_category.items():
        lines.append(f"\n*{kategori}*")
        subtotal_kategori = 0
        for rasa, qty in sorted(items, key=lambda x: -x[1]):
            lines.append(f"  {rasa}: {qty} pcs")
            subtotal_kategori += qty
        lines.append(f"  → Subtotal {kategori}: {subtotal_kategori} pcs")
        grand_total += subtotal_kategori

    lines.append(f"\n*Total semua produk: {grand_total} pcs*")
    return "\n".join(lines)


def build_monthly_supplier_report(year: int, month: int, orders: list, dough_price_map: dict) -> str:
    month_name = calendar.month_name[month]
    if not orders:
        return f"Nggak ada data order untuk bulan {month_name} {year}."

    qty_per_category = {}
    for o in orders:
        kategori = o["Kategori"]
        qty_per_category[kategori] = qty_per_category.get(kategori, 0) + int(o["Qty"])

    lines = [f"*LAPORAN BULANAN SUPPLIER — {month_name} {year}*"]
    lines.append(f"({config.BUSINESS_NAME})\n")

    grand_total_qty = 0
    grand_total_bayar = 0
    for kategori in config.CATEGORIES:
        qty = qty_per_category.get(kategori, 0)
        if qty == 0:
            continue
        harga_dough = dough_price_map.get(kategori, 0)
        bayar = qty * harga_dough
        grand_total_qty += qty
        grand_total_bayar += bayar
        lines.append(
            f"{kategori}: {qty} pcs x {rupiah(harga_dough)} = {rupiah(bayar)}"
        )

    lines.append(f"\nTotal Qty: {grand_total_qty} pcs")
    lines.append(f"*Total Bayar ke Supplier: {rupiah(grand_total_bayar)}*")

    return "\n".join(lines)
