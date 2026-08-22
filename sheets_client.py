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
            })
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        return order_records

    def get_all_orders(self):
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        return self._normalize_records(ws)

    def get_orders_by_week(self, minggu_po: str):
        return [o for o in self.get_all_orders() if self._minggu_po_cocok(o.get("Minggu_PO"), minggu_po)]

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

    def _parse_tanggal_fleksibel(self, tanggal_teks):
        """Parse teks tanggal (format bebas, apa adanya dari Sheets) jadi
        objek date, atau None kalau formatnya nggak dikenalin."""
        teks = str(tanggal_teks).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.datetime.strptime(teks, fmt).date()
            except ValueError:
                continue
        return None

    def rollover_delivered_orders(self, now: datetime.datetime = None) -> int:
        """
        Order yang Tanggal_Kirim-nya udah nyampe cutoff jam 06:00 WIB PADA
        HARI ITU SENDIRI otomatis dianggap udah kelar diproduksi & dikirim --
        Status-nya diubah dari 'Pending' jadi 'Terkirim'. Jadi order buat
        Kamis tgl 20 bakal keflip Kamis tgl 20 jam 06:00 pagi juga -- nggak
        perlu nunggu lewat ganti hari.

        Dipanggil otomatis dari get_pending_orders_by_week() tiap kali ada
        yang minta rekap produksi (jadi keupdate begitu direquest kapan aja
        abis jam 6 pagi hari H). Idealnya dipanggil JUGA dari job terjadwal
        harian jam 06:00 WIB (di scheduler_jobs.py) biar Sheets-nya sendiri
        keupdate walau nggak ada satupun yang minta rekap hari itu -- itu
        belum kepasang di sini karena scheduler_jobs.py belum ada.

        now: datetime.datetime timezone-aware, default waktu sekarang
        (timezone bot).
        Return: jumlah baris yang Status-nya barusan diubah.
        """
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return 0

        header = [h.strip().replace(" ", "_") for h in all_values[0]]
        try:
            idx_status = header.index("Status")
            idx_tanggal_kirim = header.index("Tanggal_Kirim")
        except ValueError:
            # Kolom Status/Tanggal_Kirim nggak ketemu (mis. header di Sheets
            # belum lengkap) -- jangan ubah apa-apa, lebih aman diem drpd
            # salah update kolom yang lain.
            return 0

        tz = date_helpers.get_timezone()
        now = now or datetime.datetime.now(tz)
        cutoff_hari_ini = now.replace(hour=6, minute=0, second=0, microsecond=0)
        # Sebelum jam 6 pagi, "batas hari" efektifnya masih KEMARIN -- order
        # yang tanggal kirimnya HARI INI belum boleh keflip sampe jam 6 pagi
        # bener-bener lewat.
        boundary = now.date() if now >= cutoff_hari_ini else (now - datetime.timedelta(days=1)).date()

        rows_to_update = []
        for i, row in enumerate(all_values[1:], start=2):  # baris 1 = header
            if len(row) <= max(idx_status, idx_tanggal_kirim):
                continue
            status = row[idx_status].strip()
            tanggal_kirim = row[idx_tanggal_kirim].strip()
            if status.lower() != "pending" or not tanggal_kirim:
                continue
            d = self._parse_tanggal_fleksibel(tanggal_kirim)
            if d is not None and d <= boundary:
                rows_to_update.append(i)

        for row_idx in rows_to_update:
            ws.update_cell(row_idx, idx_status + 1, "Terkirim")

        return len(rows_to_update)

    def mark_customer_delivered(self, nama_customer: str, minggu_po: str) -> int:
        """
        Tandain SEMUA baris order milik nama_customer untuk minggu_po
        tertentu yang masih Status 'Pending' jadi 'Terkirim' -- dipakai buat
        /kirim, jaring pengaman MANUAL kalau admin mau langsung nandain SAAT
        ITU JUGA (nggak nunggu cutoff otomatis jam 06:00 WIB di
        rollover_delivered_orders).

        Return: jumlah baris yang barusan diubah.
        """
        ws = self.sheet.worksheet(config.SHEET_ORDERS)
        all_values = ws.get_all_values()
        if len(all_values) < 2:
            return 0

        header = [h.strip().replace(" ", "_") for h in all_values[0]]
        try:
            idx_status = header.index("Status")
            idx_minggu = header.index("Minggu_PO")
            idx_nama = header.index("Nama_Customer")
        except ValueError:
            return 0

        nama_target = nama_customer.strip().lower()
        rows_to_update = []
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) <= max(idx_status, idx_minggu, idx_nama):
                continue
            row_nama = row[idx_nama].strip().lower()
            row_minggu = row[idx_minggu]
            row_status = row[idx_status].strip()
            if row_nama == nama_target and row_status.lower() == "pending" \
                    and self._minggu_po_cocok(row_minggu, minggu_po):
                rows_to_update.append(i)

        for row_idx in rows_to_update:
            ws.update_cell(row_idx, idx_status + 1, "Terkirim")

        return len(rows_to_update)

    def get_pending_orders_by_week(self, minggu_po: str):
        """Sama kayak get_orders_by_week, TAPI (1) jalanin
        rollover_delivered_orders dulu (order yang tanggal kirimnya udah
        lewat otomatis kepindah Status-nya), lalu (2) buang order yang
        Status-nya udah 'Terkirim' dari hasilnya. KHUSUS dipakai buat REKAP
        PRODUKSI, biar nggak keitung ulang order yang sebenernya udah kelar
        diproduksi & dikirim.

        SENGAJA dibikin method BARU, bukan ubah get_orders_by_week langsung
        -- soalnya get_orders_by_week masih dipakai /invoice, /suratjalan,
        /edit yang justru HARUS tetep bisa nemuin order biar admin bisa
        cetak ulang / edit order yang udah kelar dikirim kalau perlu."""
        try:
            self.rollover_delivered_orders()
        except Exception:
            # Kalau rollover gagal (mis. kolom belum lengkap di Sheets),
            # tetep lanjut nampilin rekap apa adanya drpd bikin /rekap
            # ikutan error gara-gara ini.
            pass
        return [
            o for o in self.get_orders_by_week(minggu_po)
            if str(o.get("Status", "")).strip().lower() != "terkirim"
        ]

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
            kategori = self._get_field(r, "Kategori")
            rasa = self._get_field(r, "Rasa")
            harga = self._get_field(r, "Harga")
            if kategori is None or rasa is None or harga in (None, ""):
                continue
            try:
                result[(str(kategori).strip(), str(rasa).strip())] = int(harga)
            except (ValueError, TypeError):
                continue
        return result

    def get_catalog_list(self):
        """Return list of (kategori, rasa) yang BENERAN ada di PriceList.
        Dipakai buat dikasih ke AI parsing biar dia cocokin item pesanan
        ke produk asli, bukan asal nebak kategori."""
        return sorted(self.get_price_map().keys())

    def get_pricelist_text(self):
        ws = self.sheet.worksheet(config.SHEET_PRICELIST)
        records = self._normalize_records(ws)
        by_category = {}
        for r in records:
            kategori = self._get_field(r, "Kategori")
            rasa = self._get_field(r, "Rasa")
            harga = self._get_field(r, "Harga")
            if kategori is None or rasa is None or harga in (None, ""):
                continue
            by_category.setdefault(kategori, []).append((rasa, harga))
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
        result = {}
        for r in records:
            kategori = self._get_field(r, "Kategori")
            harga = self._get_field(r, "Harga_Dough_Per_Unit")
            if kategori is None or harga in (None, ""):
                continue
            try:
                result[str(kategori).strip()] = int(harga)
            except (ValueError, TypeError):
                continue
        return result


# Cache koneksi biar nggak "kenalan ulang" ke Google tiap kali dipanggil
# (proses autentikasi itu yang bikin lambat kalau diulang terus).
_cached_client = None


def get_sheets_client() -> "SheetsClient":
    global _cached_client
    if _cached_client is None:
        _cached_client = SheetsClient()
    return _cached_client
