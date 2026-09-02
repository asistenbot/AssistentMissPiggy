"""
Generate teks Invoice, Surat Jalan, Rekap Produksi, dan Laporan Bulanan.
Semua dalam format teks rapi (Markdown Telegram) biar gampang di-forward/copas.
"""

import datetime

import config


def rupiah(n):
    return "Rp" + f"{int(n):,}".replace(",", ".")


def _is_delivery_metode(metode):
    """True kalau metode-nya berarti 'dikirim' -- ada 2 istilah yang beredar
    di sistem ini: order dari halaman web pakai 'Diantar', tapi order yang
    di-AI parse dari chat manual (ai_parser.py) pakai 'Kirim'. Disamain sama
    helper versi bot.py/web_order_server.py/receipt.py (dicek berbasis
    substring biar dua-duanya, dan variasi kayak 'Dikirim', kena) -- SEBELUM
    ini, fungsi-fungsi di bawah nyocokin metode pake '== \"Kirim\"' persis,
    jadi order dari web ('Diantar') ke-skip diam-diam dari daftar kurir
    (kejadian di order Ratna: kehitung di rekap produksi tapi ilang dari
    'DIKIRIM KURIR')."""
    m = (metode or "").strip().lower()
    return "antar" in m or "kirim" in m


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
    # DULU: `if orders[0].get("Metode") == "Kirim"` -- exact match doang,
    # jadi order dari web ("Diantar") kena cabang ELSE dan alamatnya nggak
    # ketulis (malah nongol alamat toko buat ambil sendiri, padahal harusnya
    # dikirim ke alamat customer). Sekarang disamain pake _is_delivery_metode.
    if _is_delivery_metode(orders[0].get("Metode")):
        lines.append(f"Alamat: {orders[0].get('Alamat', '-')}")
    else:
        lines.append(f"Ambil di: {config.PICKUP_ADDRESS}")
    lines.append("")
    lines.append("Barang:")
    for o in orders:
        lines.append(f"- {o['Rasa']} ({o['Kategori']}) x{int(o['Qty'])}")

    catatan = orders[0].get("Catatan")
    if catatan:
        lines.append("")
        lines.append(f"Catatan: {catatan}")

    return "\n".join(lines)


def build_production_recap(minggu_po: str, orders: list) -> str:
    """Total produksi per rasa aja (buat baking), tanpa pembagian kirim/ambil."""
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


def build_production_recap_customer(nama_customer: str, minggu_po: str, orders: list) -> str:
    """Rekap produksi TAPI cuma buat 1 customer tertentu -- dipanggil manual
    kalau admin EKSPLISIT minta (misal '/rekap Ci Meyvany' atau bahasa
    natural 'minta rekap produksi Ci Meyvany'), BUKAN bagian dari rekap
    mingguan otomatis (yang tetap gabungan semua customer seperti biasa,
    lihat build_production_recap -- TIDAK diubah/disentuh sama sekali).

    Berguna buat ngecek kebutuhan produksi 1 pesanan gede/khusus secara
    terpisah, apalagi kalau tanggal kirimnya beda dari minggu PO biasa
    (Tanggal_Kirim custom, misal order borongan yang mesti dikirim lebih
    cepat/lambat dari Kamis PO biasa) -- makanya tanggal kirim customer ini
    ditampilin jelas di baris kedua."""
    if not orders:
        return f"Nggak ada order atas nama *{nama_customer}* untuk minggu PO {minggu_po}."

    tanggal_kirim = orders[0].get("Tanggal_Kirim") or minggu_po

    recap = {}  # {(kategori, rasa): total_qty}
    for o in orders:
        key = (o["Kategori"], o["Rasa"])
        recap[key] = recap.get(key, 0) + int(o["Qty"])

    by_category = {}
    for (kategori, rasa), qty in recap.items():
        by_category.setdefault(kategori, []).append((rasa, qty))

    lines = [f"*REKAP PRODUKSI — {nama_customer}*"]
    lines.append(f"(Tanggal Kirim: {tanggal_kirim})")
    grand_total = 0
    for kategori, items in by_category.items():
        lines.append(f"\n*{kategori}*")
        subtotal_kategori = 0
        for rasa, qty in sorted(items, key=lambda x: -x[1]):
            lines.append(f"  {rasa}: {qty} pcs")
            subtotal_kategori += qty
        lines.append(f"  → Subtotal {kategori}: {subtotal_kategori} pcs")
        grand_total += subtotal_kategori

    lines.append(f"\n*Total: {grand_total} pcs*")
    return "\n".join(lines)


def build_production_recap_multi(daftar_nama: list, orders: list) -> str:
    """Rekap produksi buat BEBERAPA customer sekaligus dalam 1 pesan -- dipanggil
    kalau admin nyebut lebih dari 1 nama dalam satu instruksi (misal 'rekap
    produksi Franky sama Kelvin', yang di-pecah _split_nama_customer() jadi
    ['Franky', 'Kelvin']). Orders yang dikirim ke sini udah digabung dari
    SEMUA nama itu oleh pemanggilnya (lihat bot.py), jadi di sini tinggal
    agregasi qty per kategori+rasa kayak build_production_recap_customer,
    cuma judulnya nyebutin semua nama."""
    nama_text = ", ".join(daftar_nama)

    if not orders:
        return f"Nggak ada order atas nama *{nama_text}* untuk minggu ini."

    recap = {}  # {(kategori, rasa): total_qty}
    for o in orders:
        key = (o["Kategori"], o["Rasa"])
        recap[key] = recap.get(key, 0) + int(o["Qty"])

    by_category = {}
    for (kategori, rasa), qty in recap.items():
        by_category.setdefault(kategori, []).append((rasa, qty))

    lines = [f"*REKAP PRODUKSI — {nama_text}*"]
    grand_total = 0
    for kategori, items in by_category.items():
        lines.append(f"\n*{kategori}*")
        subtotal_kategori = 0
        for rasa, qty in sorted(items, key=lambda x: -x[1]):
            lines.append(f"  {rasa}: {qty} pcs")
            subtotal_kategori += qty
        lines.append(f"  → Subtotal {kategori}: {subtotal_kategori} pcs")
        grand_total += subtotal_kategori

    lines.append(f"\n*Total: {grand_total} pcs*")
    return "\n".join(lines)


def build_production_recap_tanggal(label: str, orders: list) -> str:
    """Rekap produksi berdasarkan RENTANG TANGGAL KIRIM (BUKAN Minggu_PO) --
    gabungan SEMUA customer yang tanggal kirimnya jatuh di rentang itu,
    nggak peduli Minggu_PO-nya beda-beda. Dipanggil manual kalau admin minta
    tanggal spesifik (misal '/rekap 2026-08-29' atau bahasa natural 'rekap
    produksi besok'/'rekap produksi sampe besok').

    Berguna khusus buat kasus tanggal kirim custom (besok/lusa) yang bikin
    Minggu_PO-nya beda dari minggu aktif -- customer kayak gitu nggak nongol
    di rekap mingguan biasa (build_production_recap) ataupun rekap per-nama
    (build_production_recap_customer) kalau nama-nya nggak disebut satu-satu,
    tapi tetep kejaring di sini asal Tanggal_Kirim-nya masuk rentang.

    label: teks buat judul, misal '2026-08-29' atau '2026-08-28 s/d 2026-08-29'."""
    if not orders:
        return f"Belum ada order untuk tanggal {label}."

    recap = {}  # {(kategori, rasa): total_qty}
    for o in orders:
        key = (o["Kategori"], o["Rasa"])
        recap[key] = recap.get(key, 0) + int(o["Qty"])

    by_category = {}
    for (kategori, rasa), qty in recap.items():
        by_category.setdefault(kategori, []).append((rasa, qty))

    lines = [f"*REKAP PRODUKSI — {label}*"]
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


def _group_per_customer(orders: list) -> dict:
    """Key-nya SENGAJA dinormalisir -- nama di-strip+lower, dan metode
    diringkas jadi boolean 'ini pengiriman apa bukan' lewat
    _is_delivery_metode -- BUKAN metode/nama literal apa adanya.

    DULU key-nya `(metode, nama)` mentah, jadi 1 customer yang SAMA tapi
    kebetulan punya order dari 2 SUMBER beda (web selalu nulis 'Diantar',
    chat manual yang di-AI-parse kadang nulis 'Kirim') kepisah jadi 2 grup
    beda -- muncul 2x di daftar kurir padahal orangnya sama (ini yang
    kejadian ke Pupu: order Roti dari web 'Diantar' + order Donat manual
    'Kirim' jadi 2 baris terpisah, padahal harusnya 1)."""
    per_customer = {}
    for o in orders:
        nama = str(o.get("Nama_Customer", "-")).strip()
        metode = o.get("Metode", "-")
        key = (_is_delivery_metode(metode), nama.lower())
        per_customer.setdefault(key, []).append(o)
    return per_customer


def build_delivery_kirim(minggu_po: str, orders: list) -> str | None:
    """Daftar customer yang DIKIRIM KURIR aja -- pesan terpisah, siap forward
    ke bagian gudang/kurir tanpa perlu crop screenshot."""
    per_customer = _group_per_customer(orders)
    kirim_entries = {k: v for k, v in per_customer.items() if k[0]}
    if not kirim_entries:
        return None

    lines = [f"🛵 *DIKIRIM KURIR — Minggu PO {minggu_po}*\n"]
    for (is_delivery, nama_key), items in kirim_entries.items():
        nama = items[0].get("Nama_Customer", "-")
        no_hp = items[0].get("No_HP", "-")
        alamat = items[0].get("Alamat", "-")
        lines.append(f"• {nama} — {alamat} — {no_hp}")
    return "\n".join(lines)


def build_delivery_ambil(minggu_po: str, orders: list) -> str | None:
    """Daftar customer yang AMBIL SENDIRI aja -- pesan terpisah."""
    per_customer = _group_per_customer(orders)
    ambil_entries = {k: v for k, v in per_customer.items() if not k[0]}
    if not ambil_entries:
        return None

    lines = [f"🏠 *DIAMBIL SENDIRI — Minggu PO {minggu_po}*\n"]
    for (is_delivery, nama_key), items in ambil_entries.items():
        nama = items[0].get("Nama_Customer", "-")
        no_hp = items[0].get("No_HP", "-")
        lines.append(f"• {nama} — {no_hp}")
    return "\n".join(lines)


_NAMA_BULAN_ID = {
    1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni",
    7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember",
}


def format_periode_label(year_start: int, month_start: int, year_end: int, month_end: int) -> str:
    """Bikin label periode yang enak dibaca, entah cuma 1 bulan atau rentang
    beberapa bulan. Contoh: 'Juli 2026' atau 'Juli - Agustus 2026' atau
    'Desember 2026 - Januari 2027'."""
    nama_mulai = _NAMA_BULAN_ID[month_start]
    nama_akhir = _NAMA_BULAN_ID[month_end]

    if year_start == year_end and month_start == month_end:
        return f"{nama_mulai} {year_start}"
    if year_start == year_end:
        return f"{nama_mulai} - {nama_akhir} {year_start}"
    return f"{nama_mulai} {year_start} - {nama_akhir} {year_end}"


def aggregate_dough(orders: list, dough_price_map: dict):
    """Hitung total qty & total bayar per kategori dari daftar order.
    Return: (rows, grand_total_qty, grand_total_bayar)
    rows = list of (kategori, qty, harga_dough, subtotal), cuma kategori yang qty>0.
    Dipakai bareng buat versi teks (Telegram) dan versi PDF (print A4)."""
    qty_per_category = {}
    for o in orders:
        kategori = o["Kategori"]
        qty_per_category[kategori] = qty_per_category.get(kategori, 0) + int(o["Qty"])

    rows = []
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
        rows.append((kategori, qty, harga_dough, bayar))

    return rows, grand_total_qty, grand_total_bayar


def aggregate_dough_by_month(orders: list, dough_price_map: dict):
    """Kelompokin orders per BULAN dulu (berdasarkan Minggu_PO), baru hitung
    aggregate_dough masing-masing bulan secara terpisah. Dipakai buat laporan
    yang rentangnya lebih dari 1 bulan, biar breakdown-nya jelas per bulan,
    bukan digabung jadi satu angka doang.

    Return: (month_results, grand_total_qty, grand_total_bayar)
    month_results = list of (bulan_label, rows, bulan_qty, bulan_bayar),
    urut dari bulan paling awal ke paling akhir.
    """
    by_month = {}
    for o in orders:
        teks = str(o.get("Minggu_PO")).strip()
        d = None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                d = datetime.datetime.strptime(teks, fmt)
                break
            except ValueError:
                continue
        if not d:
            continue
        by_month.setdefault((d.year, d.month), []).append(o)

    month_results = []
    grand_total_qty = 0
    grand_total_bayar = 0
    for (year, month) in sorted(by_month.keys()):
        rows, qty, bayar = aggregate_dough(by_month[(year, month)], dough_price_map)
        bulan_label = f"{_NAMA_BULAN_ID[month]} {year}"
        month_results.append((bulan_label, rows, qty, bayar))
        grand_total_qty += qty
        grand_total_bayar += bayar

    return month_results, grand_total_qty, grand_total_bayar


def _grand_total_by_category(month_results):
    """Gabungin semua bulan jadi total per KATEGORI aja (Roti semua bulan,
    Donat semua bulan, dst) -- dipakai buat breakdown di bagian akhir laporan
    yang rentangnya lebih dari 1 bulan.
    Return: list of (kategori, total_qty, total_bayar), urut sesuai config.CATEGORIES."""
    totals = {}
    for bulan_label, rows, bulan_qty, bulan_bayar in month_results:
        for kategori, qty, harga_dough, subtotal in rows:
            if kategori not in totals:
                totals[kategori] = [0, 0]
            totals[kategori][0] += qty
            totals[kategori][1] += subtotal

    result = []
    for kategori in config.CATEGORIES:
        if kategori in totals:
            qty, bayar = totals[kategori]
            result.append((kategori, qty, bayar))
    return result


def build_monthly_supplier_report(periode_label: str, orders: list, dough_price_map: dict) -> str:
    if not orders:
        return f"Nggak ada data order untuk periode {periode_label}."

    month_results, grand_total_qty, grand_total_bayar = aggregate_dough_by_month(orders, dough_price_map)

    lines = [f"*LAPORAN BULANAN SUPPLIER — {periode_label}*"]
    lines.append(f"({config.BUSINESS_NAME})")

    for bulan_label, rows, bulan_qty, bulan_bayar in month_results:
        lines.append(f"\n*{bulan_label}*")
        for kategori, qty, harga_dough, bayar in rows:
            lines.append(f"  {kategori}: {qty} pcs x {rupiah(harga_dough)} = {rupiah(bayar)}")
        lines.append(f"  → Subtotal {bulan_label}: {rupiah(bulan_bayar)}")

    # Kalau lebih dari 1 bulan, kasih breakdown total per kategori juga
    # (Roti semua bulan, Donat semua bulan, dst) sebelum total akhir.
    if len(month_results) > 1:
        kategori_totals = _grand_total_by_category(month_results)
        lines.append("\n" + "─" * 24)
        lines.append("*TOTAL PER KATEGORI (SEMUA BULAN)*")
        for kategori, qty, bayar in kategori_totals:
            lines.append(f"  {kategori}: {qty} pcs = {rupiah(bayar)}")

    lines.append("\n" + "─" * 24)
    lines.append(f"Total Qty semua bulan: {grand_total_qty} pcs")
    lines.append(f"*TOTAL BAYAR SEMUA BULAN: {rupiah(grand_total_bayar)}*")

    return "\n".join(lines)
