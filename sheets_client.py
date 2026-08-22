"""
Semua interaksi dengan Google Sheets ada di sini.
Pakai gspread + service account.
"""

import json
import datetime
import difflib
import re
import gspread
from google.oauth2.service_account import Credentials

import config
import date_helpers

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsClient:
    def __init__(self):
        if config.GOOGLE_SERVICE_ACCOUNT_JSON:
            # Dipakai pas deploy di Railway/Render: kredensial disimpen sebagai
            # environment variable (isi JSON dalam 1 baris), bukan file.
            info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            # Dipakai pas jalan di komputer lokal: baca dari file JSON.
            creds = Credentials.from_service_account_file(
                config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
        self.gc = gspread.authorize(creds)
        self.sheet = self.gc.open_by_key(config.GOOGLE_SHEET_ID)

    # ---------- ORDERS ----------

    def _normalize_records(self, ws):
        """
        Ambil semua baris dari sebuah worksheet, tapi "normalisasi" nama
        kolomnya dulu (spasi jadi underscore, dll) -- biar nggak masalah
        walau header di Sheets ditulis 'Minggu PO' atau 'Minggu_PO', dua-duanya
        tetap kebaca sebagai field yang sama oleh kode ini.

        Untuk worksheet ORDERS, kolom 'Status' juga dihitung ULANG di sini
        (lihat _effective_status) -- jadi order yang Tanggal_Kirim-nya udah
        lewat/hari ini otomatis kebaca statusnya 'Sudah Dikirim', walau di
        Sheets aslinya masih tertulis 'Pending'.
        """
        records = ws.get_all_records()
        normalized = []
        for r in records:
            new_r = {}
            for k, v in r.items():
                key_norm = k.strip().replace(" ", "_")
                new_r[key_norm] = v
            if "Tanggal_Kirim" in new_r or "Status" in new_r:
                new_r["Status"] = self._effective_status(new_r)
            normalized.append(new_r)
        return normalized

    def _effective_status(self, order_record):
        """Status 'asli' order (biasanya 'Pending' pas baru disimpen), TAPI
        kalau Tanggal_Kirim order itu udah lewat atau PERSIS hari ini,
        otomatis dianggap SUDAH PASTI DIKIRIM -- nggak perlu admin ubah
        status manual di Sheets. Kalau kolom Status di Sheets udah keisi
        'Sudah Dikirim'/'Selesai'/'Delivered' secara manual, itu tetap
        dihormati (nggak ketimpa)."""
        status_asli = str(order_record.get("Status") or "").strip()
        if status_asli.lower() in ("sudah dikirim", "selesai", "delivered"):
            return status_asli

        tanggal_kirim = order_record.get("Tanggal_Kirim")
        if not tanggal_kirim:
            return status_asli or "Pending"

        teks = str(tanggal_kirim).strip()
        tz = date_helpers.get_timezone()
        today = datetime.datetime.now(tz).date()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                d = datetime.datetime.strptime(teks, fmt).date()
                if d <= today:
                    return "Sudah Dikirim"
                break
            except ValueError:
                continue
        return status_asli or "Pending"

    @staticmethod
    def _loose_key(s):
        """Ubah nama kolom jadi bentuk paling polos buat dibandingin
        (buang semua spasi/underscore, huruf kecil semua). Biar 'Harga Dough
        per Unit', 'Harga_Dough_Per_Unit', 'harga dough perunit' dianggap sama."""
        return re.sub(r"[^a-z0-9]", "", str(s).lower())

    def _get_field(self, record, target_name, default=None):
        """Cari value dari dict record berdasarkan nama kolom yang PALING MIRIP
        sama target_name (toleran beda kapitalisasi/spasi/underscore)."""
        target_loose = self._loose_key(target_name)
        for k, v in record.items():
            if self._loose_key(k) == target_loose:
                return v
        return default

    def _find_price(self, price_map, kategori, rasa):
        """
        Cari harga untuk (kategori, rasa). Kalau nama rasa-nya nggak persis sama
        (misal chat customer bilang 'Meses' tapi di PriceList tertulis 'Meises'),
        cari yang paling MIRIP dalam kategori yang sama, bukan langsung anggap Rp0.
        Return: (harga, nama_rasa_yang_dipakai_buat_disimpan)
        """
        kategori = kategori.strip()
        rasa = rasa.strip()
        key = (kategori, rasa)
        if key in price_map:
            return price_map[key], rasa

        kandidat = [r for (k, r) in price_map.keys() if k == kategori]
        mirip = difflib.get_close_matches(rasa, kandidat, n=1, cutoff=0.6)
        if mirip:
            rasa_cocok = mirip[0]
            return price_map[(kategori, rasa_cocok)], rasa_cocok

        return 0, rasa

    def add_order_rows(self, order: dict, minggu_po: str):
        """
        order = {
            "nama": str, "no_hp": str, "alamat": str, "metode": "Kirim"/"Ambil",
            "items": [{"kategori": str, "rasa": str, "qty": int}, ...],
            "ongkir": int
        }
        Satu order bisa berisi banyak item -> tiap item jadi 1 baris,
        biar gampang di-rekap per rasa.

        Return: list of dict record item yang baru disimpan (dipakai langsung
        buat generate invoice/surat jalan tanpa perlu baca ulang ke Sheets,
        karena baca-langsung-setelah-tulis kadang belum "settle" di Google Sheets).
        """
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        price_map = self.get_price_map()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Tanggal_Kirim itu OPSIONAL -- kalau admin nggak nentuin tanggal
        # custom (misal 'besok'), defaultnya sama kayak minggu_po (Kamis PO
        # minggu berjalan), jadi perilaku lama nggak berubah kalau fitur ini
        # nggak dipakai sama sekali.
        tanggal_kirim = order.get("tanggal_kirim") or minggu_po
        rows = []
        order_records = []
        for item in order["items"]:
            harga, rasa_cocok = self._find_price(price_map, item["kategori"], item["rasa"])
            subtotal = harga * item["qty"]
            rows.append([
                timestamp,
                f"'{minggu_po}",  # apostrof di depan = paksa Sheets simpen sebagai teks,
                                   # biar nggak otomatis diubah jadi format Date sendiri
                order["nama"],
                order["no_hp"],
                order["alamat"],
                order["metode"],
                item["kategori"],
                rasa_cocok,
                item["qty"],
                harga,
                subtotal,
                order.get("ongkir", 0),
                "Pending",
                f"'{tanggal_kirim}",  # kolom BARU, sengaja PALING BELAKANG biar
                                       # nggak nggeser kolom lama yang udah ada
            ])
            order_records.append({
                "Kategori": item["kategori"],
                "Rasa": rasa_cocok,
                "Qty": item["qty"],
                "Harga_Satuan": harga,
                "Metode": order["metode"],
                "No_HP": order["no_hp"],
                "Alamat": order["alamat"],
                "Ongkir": order.get("ongkir", 0),
                "Tanggal_Kirim": tanggal_kirim,
                "Status": "Pending",
            })
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        return order_records

    def get_all_orders(self):
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        return self._normalize_records(ws)

    def get_orders_by_week(self, minggu_po: str):
        """PENTING: filter berdasarkan Tanggal_Kirim (tanggal kirim ASLI),
        BUKAN Minggu_PO (yang cuma tag batch, selalu Kamis PO terdekat).
        Ini yang bikin order yang tanggal kirimnya custom (misal 'hari ini')
        nggak ikut kesedot ke rekap minggu PO lain walau Minggu_PO-nya
        kebetulan sama."""
        return [o for o in self.get_all_orders() if self._tanggal_kirim_cocok(o, minggu_po)]

    def _tanggal_kirim_cocok(self, order_record, target_date):
        tanggal_kirim = order_record.get("Tanggal_Kirim")
        if tanggal_kirim:
            return self._minggu_po_cocok(tanggal_kirim, target_date)
        # Fallback buat data lama sebelum kolom Tanggal_Kirim ada
        return self._minggu_po_cocok(order_record.get("Minggu_PO"), target_date)

    def _minggu_po_cocok(self, nilai_di_sheet, minggu_po):
        teks = str(nilai_di_sheet).strip()
        if teks == minggu_po:
            return True
        # Coba beberapa format lain, jaga-jaga Google Sheets ubah formatnya sendiri
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                d = datetime.datetime.strptime(teks, fmt)
                if d.strftime("%Y-%m-%d") == minggu_po:
                    return True
            except ValueError:
                continue
        return False

    def delete_customer_week_rows(self, nama_customer: str, minggu_po: str) -> int:
        """
        Hapus SEMUA baris order milik nama_customer untuk minggu_po tertentu.
        Dipakai buat fitur edit order: hapus yang lama dulu, baru ditulis ulang
        yang baru -- biar nggak dobel keitung di rekap produksi.

        Return: jumlah baris yang dihapus.
        """
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return 0

        header = [h.strip().replace(" ", "_") for h in all_values[0]]
        try:
            idx_minggu = header.index("Minggu_PO")
            idx_nama = header.index("Nama_Customer")
        except ValueError:
            # Kolom nggak ketemu sama sekali -- jangan hapus apa-apa, lebih aman diem
            return 0

        nama_target = nama_customer.strip().lower()
        rows_to_delete = []
        for i, row in enumerate(all_values[1:], start=2):  # baris 1 = header, gspread 1-indexed
            if len(row) <= max(idx_minggu, idx_nama):
                continue
            row_nama = row[idx_nama].strip().lower()
            row_minggu = row[idx_minggu]
            if row_nama == nama_target and self._minggu_po_cocok(row_minggu, minggu_po):
                rows_to_delete.append(i)

        # Hapus dari baris PALING BAWAH dulu, biar nomor baris di atasnya nggak geser
        for row_idx in sorted(rows_to_delete, reverse=True):
            ws.delete_rows(row_idx)

        return len(rows_to_delete)

    def get_orders_by_customer_week(self, nama_customer: str, minggu_po: str):
        return [
            o for o in self.get_orders_by_week(minggu_po)
            if o.get("Nama_Customer", "").strip().lower() == nama_customer.strip().lower()
        ]

    def get_orders_by_month(self, year: int, month: int):
        """Filter berdasarkan Minggu_PO (tanggal Kamis pengiriman) yang jatuh di bulan tsb."""
        return self.get_orders_by_month_range(year, month, year, month)

    def get_orders_by_month_range(self, year_start: int, month_start: int, year_end: int, month_end: int):
        """Filter berdasarkan Minggu_PO yang jatuh di rentang bulan year_start-month_start
        sampai year_end-month_end (inklusif). Buat laporan bulanan yang minta
        beberapa bulan sekaligus, misal '2 bulan ke belakang' atau 'Juli-Agustus'."""
        start_key = year_start * 100 + month_start
        end_key = year_end * 100 + month_end
        result = []
        for o in self.get_all_orders():
            teks = str(o.get("Minggu_PO")).strip()
            d = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                try:
                    d = datetime.datetime.strptime(teks, fmt)
                    break
                except ValueError:
                    continue
            if d:
                key = d.year * 100 + d.month
                if start_key <= key <= end_key:
                    result.append(o)
        return result

    # ---------- PRICE LIST ----------

    def get_price_map(self):
        """Return dict {(kategori, rasa): harga}"""
        ws = self.sheet.worksheet(config.SHEET_PRICELIST)
        records = self._normalize_records(ws)
        result = {}
        for r in records:
            kategori = self._get_field(r,
