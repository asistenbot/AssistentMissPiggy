"""
Semua interaksi dengan Google Sheets ada di sini.
Pakai gspread + service account.
"""

import json
import datetime
import difflib
import gspread
from google.oauth2.service_account import Credentials

import config

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
        """
        records = ws.get_all_records()
        normalized = []
        for r in records:
            new_r = {}
            for k, v in r.items():
                key_norm = k.strip().replace(" ", "_")
                new_r[key_norm] = v
            normalized.append(new_r)
        return normalized

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
            })
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        return order_records

    def get_all_orders(self):
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        return self._normalize_records(ws)

    def get_orders_by_week(self, minggu_po: str):
        def cocok(nilai_di_sheet):
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

        return [o for o in self.get_all_orders() if cocok(o.get("Minggu_PO"))]

    def get_orders_by_customer_week(self, nama_customer: str, minggu_po: str):
        return [
            o for o in self.get_orders_by_week(minggu_po)
            if o.get("Nama_Customer", "").strip().lower() == nama_customer.strip().lower()
        ]

    def get_orders_by_month(self, year: int, month: int):
        """Filter berdasarkan Minggu_PO (tanggal Kamis pengiriman) yang jatuh di bulan tsb."""
        result = []
        for o in self.get_all_orders():
            try:
                d = datetime.datetime.strptime(str(o.get("Minggu_PO")), "%Y-%m-%d")
                if d.year == year and d.month == month:
                    result.append(o)
            except (ValueError, TypeError):
                continue
        return result

    # ---------- PRICE LIST ----------

    def get_price_map(self):
        """Return dict {(kategori, rasa): harga}"""
        ws = self.sheet.worksheet(config.SHEET_PRICELIST)
        records = self._normalize_records(ws)
        return {
            (r["Kategori"].strip(), r["Rasa"].strip()): int(r["Harga"])
            for r in records
        }

    def get_pricelist_text(self):
        ws = self.sheet.worksheet(config.SHEET_PRICELIST)
        records = self._normalize_records(ws)
        by_category = {}
        for r in records:
            by_category.setdefault(r["Kategori"], []).append((r["Rasa"], r["Harga"]))
        lines = []
        for kategori, items in by_category.items():
            lines.append(f"\n*{kategori}*")
            for rasa, harga in items:
                lines.append(f"  {rasa} — Rp{int(harga):,}".replace(",", "."))
        return "\n".join(lines)

    # ---------- SUPPLIER DOUGH PRICE ----------

    def get_dough_price_map(self):
        """Return dict {kategori: harga_dough_per_unit}"""
        ws = self.sheet.worksheet(config.SHEET_SUPPLIER_DOUGH)
        records = self._normalize_records(ws)
        return {r["Kategori"].strip(): int(r["Harga_Dough_Per_Unit"]) for r in records}


# Cache koneksi biar nggak "kenalan ulang" ke Google tiap kali dipanggil
# (proses autentikasi itu yang bikin lambat kalau diulang terus).
_cached_client = None


def get_sheets_client() -> "SheetsClient":
    global _cached_client
    if _cached_client is None:
        _cached_client = SheetsClient()
    return _cached_client
