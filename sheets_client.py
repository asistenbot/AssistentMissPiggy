"""
Semua fungsi baca/tulis ke Google Sheets ada di sini.
Sheet ini "database" utama sistem -- 7 tab:
  PriceList, Customers, Orders, Suppliers, PurchaseOrders, UtangSupplier, Kas

Bot ini SELF-PROVISIONING: begitu pertama kali jalan, kalau tab-tab di atas
belum ada di spreadsheet, bot otomatis bikinin (termasuk header kolom &
ngisi PriceList/Customers dari SEED_PRODUCTS/SEED_CUSTOMERS di config.py).
Jadi Riky cuma perlu bikin 1 spreadsheet KOSONG & share ke service account
-- sisanya otomatis.
"""

import datetime
import json
import threading

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# ---- definisi skema: nama tab -> header kolom ----
SCHEMA = {
    config.SHEET_PRICELIST: [
        "Item_Code", "Nama", "Deskripsi", "Kategori", "Satuan",
        "Harga_Jual", "Harga_Beli",
    ],
    config.SHEET_CUSTOMERS: ["Nama", "No_HP", "Alamat"],
    config.SHEET_ORDERS: [
        "Timestamp", "No_Invoice", "Nama_Customer", "No_HP", "Alamat",
        "Metode", "Item_Code", "Nama_Item", "Kategori", "Qty", "Satuan",
        "Harga_Satuan", "Subtotal", "Ongkir", "Status", "Tanggal_Bayar",
    ],
    config.SHEET_SUPPLIERS: ["Nama_Supplier", "No_HP", "Barang", "Alamat"],
    config.SHEET_PURCHASE_ORDERS: [
        "Timestamp", "No_PO", "Nama_Supplier", "Item", "Qty", "Satuan",
        "Harga_Satuan", "Subtotal", "Status", "Tanggal_Lunas",
    ],
    config.SHEET_UTANG_SUPPLIER: [
        "Tanggal", "Nama_Supplier", "No_PO", "Jenis", "Jumlah",
        "Saldo_Berjalan", "Catatan",
    ],
    config.SHEET_KAS: [
        "Tanggal", "Jenis", "Kategori", "Jumlah", "Keterangan", "Ref",
    ],
}


def _today_str():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)  # WIB
    return now.strftime("%Y-%m-%d")


def _now_str():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=7)
    return now.strftime("%Y-%m-%d %H:%M:%S")


class SheetsClient:
    def __init__(self):
        if config.GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(
                config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=SCOPES
            )
        self.gc = gspread.authorize(creds)
        self.sh = self.gc.open_by_key(config.GOOGLE_SHEET_ID)
        self._lock = threading.Lock()
        self._ensure_schema()

    # ---------------- SETUP OTOMATIS ----------------

    def _ensure_schema(self):
        existing = {ws.title: ws for ws in self.sh.worksheets()}
        for name, headers in SCHEMA.items():
            if name not in existing:
                ws = self.sh.add_worksheet(title=name, rows=200, cols=max(len(headers), 8))
                ws.append_row(headers, value_input_option="RAW")
            else:
                ws = existing[name]
                first_row = ws.row_values(1)
                if not first_row:
                    ws.append_row(headers, value_input_option="RAW")

        # kalau spreadsheet baru dibuat gspread nyisain tab default "Sheet1"
        # kosong -- hapus biar rapi (aman diabaikan kalau gagal/gak ada).
        try:
            default_ws = self.sh.worksheet("Sheet1")
            if not default_ws.get_all_values():
                self.sh.del_worksheet(default_ws)
        except gspread.exceptions.WorksheetNotFound:
            pass

        self._seed_pricelist_if_empty()
        self._seed_customers_if_empty()

    def _seed_pricelist_if_empty(self):
        ws = self.sh.worksheet(config.SHEET_PRICELIST)
        rows = ws.get_all_values()
        if len(rows) <= 1:  # cuma header / kosong total
            data = [
                [
                    p["item_code"], p["nama"], p["deskripsi"], p["kategori"],
                    p["satuan"], p["harga_jual"], p["harga_beli"],
                ]
                for p in config.SEED_PRODUCTS
            ]
            if data:
                ws.append_rows(data, value_input_option="RAW")

    def _seed_customers_if_empty(self):
        ws = self.sh.worksheet(config.SHEET_CUSTOMERS)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            data = [[nama, "", ""] for nama in config.SEED_CUSTOMERS]
            if data:
                ws.append_rows(data, value_input_option="RAW")

    def _ws(self, name):
        return self.sh.worksheet(name)

    # kolom yang isinya BISA kelihatan angka murni (no HP, kode item) tapi
    # harus tetap dibaca sebagai teks -- kalau enggak, gspread otomatis
    # nyoba convert ke number dan ngilangin angka 0 di depan (mis. No_HP
    # "0812xxx" jadi 812).
    _TEXT_ONLY_COLUMNS = {"No_HP", "Item_Code"}

    @classmethod
    def _numericise_ignore_for(cls, headers):
        ignore = []
        for i, h in enumerate(headers, start=1):
            if h in cls._TEXT_ONLY_COLUMNS:
                ignore.append(i)
        return ignore

    def _records(self, ws):
        """get_all_records, tapi kolom No_HP/Item_Code selalu dipaksa jadi
        teks (lihat _TEXT_ONLY_COLUMNS) biar angka 0 di depan gak ilang."""
        headers = SCHEMA.get(ws.title) or ws.row_values(1)
        ignore = self._numericise_ignore_for(headers)
        return ws.get_all_records(default_blank="", numericise_ignore=ignore)

    # ---------------- PRICELIST ----------------

    def get_price_list(self):
        return self._records(self._ws(config.SHEET_PRICELIST))

    def get_price_map(self):
        """dict item_code (upper) -> row produk"""
        out = {}
        for row in self.get_price_list():
            code = str(row.get("Item_Code", "")).strip().upper()
            if code:
                out[code] = row
        return out

    def find_product(self, query):
        """Cari produk berdasarkan Item_Code (persis) atau nama/kategori
        (loose contains, case-insensitive). Balikin row pertama yang cocok,
        atau None."""
        query = (query or "").strip().lower()
        if not query:
            return None
        price_list = self.get_price_list()
        for row in price_list:
            if str(row.get("Item_Code", "")).strip().lower() == query:
                return row
        candidates = []
        for row in price_list:
            haystack = " ".join([
                str(row.get("Nama", "")), str(row.get("Kategori", "")),
                str(row.get("Deskripsi", "")),
            ]).lower()
            if query in haystack:
                candidates.append(row)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            # ambil yang nama-nya paling mirip (paling pendek = paling spesifik)
            candidates.sort(key=lambda r: len(str(r.get("Nama", ""))))
            return candidates[0]
        return None

    def update_price(self, item_code, harga_jual=None, harga_beli=None):
        """Update Harga_Jual dan/atau Harga_Beli buat 1 produk di PriceList
        (dicari by Item_Code, persis). Cuma field yang dikasih (bukan None)
        yang diupdate. Balikin True kalau item_code ketemu & keupdate,
        False kalau item_code gak ada di katalog."""
        ws = self._ws(config.SHEET_PRICELIST)
        headers = ws.row_values(1)
        col_code = headers.index("Item_Code") + 1
        col_jual = headers.index("Harga_Jual") + 1
        col_beli = headers.index("Harga_Beli") + 1
        target = (item_code or "").strip().upper()
        if not target:
            return False
        all_values = ws.get_all_values()
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) < col_code:
                continue
            if row[col_code - 1].strip().upper() != target:
                continue
            if harga_jual is not None:
                ws.update_cell(idx, col_jual, harga_jual)
            if harga_beli is not None:
                ws.update_cell(idx, col_beli, harga_beli)
            return True
        return False

    def get_pricelist_text(self):
        rows = self.get_price_list()
        by_kategori = {}
        for r in rows:
            by_kategori.setdefault(r.get("Kategori", "Lainnya"), []).append(r)
        lines = []
        for kategori, items in by_kategori.items():
            lines.append(f"*{kategori}*")
            for it in items:
                harga = f"Rp{int(it['Harga_Jual']):,}".replace(",", ".")
                ket = f" ({it['Deskripsi']})" if it.get("Deskripsi") else ""
                lines.append(f"  {it['Item_Code']} - {it['Nama']}{ket}: {harga}/{it['Satuan']}")
            lines.append("")
        return "\n".join(lines).strip()

    # ---------------- CUSTOMERS ----------------

    def get_customers(self):
        return self._records(self._ws(config.SHEET_CUSTOMERS))

    def get_customer_names(self):
        return [str(r.get("Nama", "")).strip() for r in self.get_customers() if r.get("Nama")]

    def add_customer_if_new(self, nama, no_hp="", alamat=""):
        nama = (nama or "").strip()
        if not nama:
            return
        existing_lower = [n.lower() for n in self.get_customer_names()]
        if nama.lower() not in existing_lower:
            self._ws(config.SHEET_CUSTOMERS).append_row(
                [nama, no_hp, alamat], value_input_option="RAW"
            )

    # ---------------- NOMOR DOKUMEN ----------------

    def _next_number(self, prefix, existing_numbers):
        today = _today_str().replace("-", "")
        today_prefix = f"{prefix}-{today}-"
        seq = 1
        used = [n for n in existing_numbers if n.startswith(today_prefix)]
        if used:
            nums = []
            for n in used:
                try:
                    nums.append(int(n.split("-")[-1]))
                except ValueError:
                    pass
            if nums:
                seq = max(nums) + 1
        return f"{today_prefix}{seq:03d}"

    def next_invoice_number(self):
        existing = [str(r.get("No_Invoice", "")) for r in self.get_all_orders()]
        return self._next_number(config.INVOICE_PREFIX, existing)

    def next_po_number(self):
        existing = [str(r.get("No_PO", "")) for r in self.get_all_pos()]
        return self._next_number(config.PO_PREFIX, existing)

    # ---------------- ORDERS ----------------

    def get_all_orders(self):
        return self._records(self._ws(config.SHEET_ORDERS))

    def add_order(self, nama_customer, no_hp, alamat, metode, items, ongkir=0):
        """items: list of dict {item_code, nama_item, kategori, qty, satuan,
        harga_satuan}. Nulis 1 baris per item, semuanya share No_Invoice yang
        sama. Balikin no_invoice yang dipakai."""
        with self._lock:
            no_invoice = self.next_invoice_number()
            ts = _now_str()
            ws = self._ws(config.SHEET_ORDERS)
            rows = []
            for idx, it in enumerate(items):
                subtotal = float(it["qty"]) * float(it["harga_satuan"])
                rows.append([
                    ts, no_invoice, nama_customer, no_hp, alamat, metode,
                    it.get("item_code", ""), it["nama_item"], it.get("kategori", ""),
                    it["qty"], it.get("satuan", ""), it["harga_satuan"], subtotal,
                    ongkir if idx == 0 else 0,  # ongkir cuma dicatat 1x di baris pertama
                    "Pending", "",
                ])
            ws.append_rows(rows, value_input_option="RAW")
            self.add_customer_if_new(nama_customer, no_hp, alamat)
        return no_invoice

    def get_order_items(self, no_invoice):
        return [r for r in self.get_all_orders() if str(r.get("No_Invoice", "")) == no_invoice]

    def get_orders_by_customer(self, nama_customer, status=None):
        nama_lower = (nama_customer or "").strip().lower()
        out = []
        for r in self.get_all_orders():
            if str(r.get("Nama_Customer", "")).strip().lower() != nama_lower:
                continue
            if status and str(r.get("Status", "")) != status:
                continue
            out.append(r)
        return out

    def get_latest_invoice_for_customer(self, nama_customer):
        orders = self.get_orders_by_customer(nama_customer)
        if not orders:
            return None
        return sorted(orders, key=lambda r: str(r.get("Timestamp", "")))[-1].get("No_Invoice")

    def mark_order_lunas(self, no_invoice):
        """Tandai semua baris dengan No_Invoice ini jadi Lunas. Balikin total
        (subtotal + ongkir) buat dicatat ke Kas."""
        ws = self._ws(config.SHEET_ORDERS)
        headers = ws.row_values(1)
        col_invoice = headers.index("No_Invoice") + 1
        col_status = headers.index("Status") + 1
        col_tgl = headers.index("Tanggal_Bayar") + 1
        col_subtotal = headers.index("Subtotal") + 1
        col_ongkir = headers.index("Ongkir") + 1

        all_values = ws.get_all_values()
        total = 0.0
        found = False
        updates = []
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) < col_invoice:
                continue
            if row[col_invoice - 1] != no_invoice:
                continue
            found = True
            try:
                total += float(row[col_subtotal - 1] or 0)
            except ValueError:
                pass
            try:
                total += float(row[col_ongkir - 1] or 0)
            except ValueError:
                pass
            updates.append((idx, col_status, "Lunas"))
            updates.append((idx, col_tgl, _today_str()))
        if not found:
            return None
        for r, c, v in updates:
            ws.update_cell(r, c, v)
        return total

    def edit_order_header(self, no_invoice, updates):
        """Betulin data header order yang SUDAH kesimpen (nama customer, no
        HP, alamat, dan/atau metode) -- BUKAN item/qty/harga. `updates` dict
        subset dari {nama_customer, no_hp, alamat, metode} -> nilai baru,
        cuma field yang ada isinya yang diupdate. Update semua baris yang
        share No_Invoice ini (1 order = beberapa baris, 1 per item).
        Balikin True kalau invoice ketemu & keupdate, False kalau enggak
        ketemu."""
        field_to_col = {
            "nama_customer": "Nama_Customer",
            "no_hp": "No_HP",
            "alamat": "Alamat",
            "metode": "Metode",
        }
        ws = self._ws(config.SHEET_ORDERS)
        headers = ws.row_values(1)
        col_invoice = headers.index("No_Invoice") + 1
        col_map = {}
        for key, col_name in field_to_col.items():
            val = (updates.get(key) or "").strip()
            if val:
                col_map[key] = (headers.index(col_name) + 1, val)

        if not col_map:
            return False

        all_values = ws.get_all_values()
        found = False
        cell_updates = []
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) < col_invoice or row[col_invoice - 1] != no_invoice:
                continue
            found = True
            for col, val in col_map.values():
                cell_updates.append((idx, col, val))
        if not found:
            return False
        for r, c, v in cell_updates:
            ws.update_cell(r, c, v)
        if "nama_customer" in updates and (updates.get("nama_customer") or "").strip():
            self.add_customer_if_new(updates["nama_customer"].strip())
        return True

    def edit_order_item_qty(self, no_invoice, item_code, qty_baru=None, harga_baru=None):
        """Ganti Qty dan/atau Harga_Satuan (Subtotal dihitung ulang otomatis
        dari nilai final keduanya) buat 1 item di dalam order tertentu.
        Field yang dikasih None dibiarin sama kayak sebelumnya. Balikin
        True kalau baris item ketemu & keupdate, False kalau enggak (atau
        kalau qty_baru & harga_baru dua-duanya None -- gak ada yang mau
        diubah)."""
        if qty_baru is None and harga_baru is None:
            return False
        ws = self._ws(config.SHEET_ORDERS)
        headers = ws.row_values(1)
        col_invoice = headers.index("No_Invoice") + 1
        col_item_code = headers.index("Item_Code") + 1
        col_qty = headers.index("Qty") + 1
        col_harga = headers.index("Harga_Satuan") + 1
        col_subtotal = headers.index("Subtotal") + 1
        target_code = (item_code or "").strip().upper()
        if not target_code:
            return False
        all_values = ws.get_all_values()
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) < col_item_code:
                continue
            if row[col_invoice - 1] != no_invoice:
                continue
            if row[col_item_code - 1].strip().upper() != target_code:
                continue
            try:
                qty_val = float(qty_baru) if qty_baru is not None else float(row[col_qty - 1] or 0)
                harga_val = float(harga_baru) if harga_baru is not None else float(row[col_harga - 1] or 0)
            except (TypeError, ValueError):
                return False
            if qty_baru is not None:
                ws.update_cell(idx, col_qty, qty_val)
            if harga_baru is not None:
                ws.update_cell(idx, col_harga, harga_val)
            ws.update_cell(idx, col_subtotal, qty_val * harga_val)
            return True
        return False

    def cancel_order(self, no_invoice):
        """Tandai semua baris dengan No_Invoice ini Status='Batal' -- order
        DIHAPUS/DIBATALIN secara logis (datanya tetep ada buat histori,
        tapi otomatis keluar dari piutang karena get_pending_orders cuma
        ngitung yang Status-nya Pending). Balikin True kalau ketemu, False
        kalau enggak."""
        ws = self._ws(config.SHEET_ORDERS)
        headers = ws.row_values(1)
        col_invoice = headers.index("No_Invoice") + 1
        col_status = headers.index("Status") + 1
        all_values = ws.get_all_values()
        found = False
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) < col_invoice or row[col_invoice - 1] != no_invoice:
                continue
            found = True
            ws.update_cell(idx, col_status, "Batal")
        return found

    def get_pending_orders(self):
        return [r for r in self.get_all_orders() if str(r.get("Status", "")) == "Pending"]

    def get_piutang_summary(self):
        """dict nama_customer -> total belum lunas"""
        out = {}
        for r in self.get_pending_orders():
            nama = r.get("Nama_Customer", "-")
            try:
                subtotal = float(r.get("Subtotal", 0) or 0)
            except ValueError:
                subtotal = 0
            try:
                ongkir = float(r.get("Ongkir", 0) or 0)
            except ValueError:
                ongkir = 0
            out[nama] = out.get(nama, 0) + subtotal + ongkir
        return out

    # ---------------- SUPPLIERS ----------------

    def get_suppliers(self):
        return self._records(self._ws(config.SHEET_SUPPLIERS))

    def get_supplier_names(self):
        return [str(r.get("Nama_Supplier", "")).strip() for r in self.get_suppliers() if r.get("Nama_Supplier")]

    def add_supplier_if_new(self, nama, no_hp="", barang="", alamat=""):
        nama = (nama or "").strip()
        if not nama:
            return
        existing_lower = [n.lower() for n in self.get_supplier_names()]
        if nama.lower() not in existing_lower:
            self._ws(config.SHEET_SUPPLIERS).append_row(
                [nama, no_hp, barang, alamat], value_input_option="RAW"
            )

    # ---------------- PURCHASE ORDERS ----------------

    def get_all_pos(self):
        return self._records(self._ws(config.SHEET_PURCHASE_ORDERS))

    def add_purchase_order(self, nama_supplier, items):
        """items: list of dict {nama_item, qty, satuan, harga_satuan}.
        Nulis 1 baris per item dgn No_PO yang sama, status Belum Lunas,
        dan otomatis nambah entry 'Utang Baru' ke UtangSupplier.
        Balikin (no_po, total)."""
        with self._lock:
            no_po = self.next_po_number()
            ts = _now_str()
            ws = self._ws(config.SHEET_PURCHASE_ORDERS)
            rows = []
            total = 0.0
            for it in items:
                subtotal = float(it["qty"]) * float(it["harga_satuan"])
                total += subtotal
                rows.append([
                    ts, no_po, nama_supplier, it["nama_item"], it["qty"],
                    it.get("satuan", ""), it["harga_satuan"], subtotal,
                    "Belum Lunas", "",
                ])
            ws.append_rows(rows, value_input_option="RAW")
            self.add_supplier_if_new(nama_supplier)
            self._add_utang_entry(nama_supplier, no_po, "Utang Baru", total, "PO baru")
        return no_po, total

    def get_po_items(self, no_po):
        return [r for r in self.get_all_pos() if str(r.get("No_PO", "")) == no_po]

    def mark_po_lunas(self, no_po):
        ws = self._ws(config.SHEET_PURCHASE_ORDERS)
        headers = ws.row_values(1)
        col_po = headers.index("No_PO") + 1
        col_status = headers.index("Status") + 1
        col_tgl = headers.index("Tanggal_Lunas") + 1
        all_values = ws.get_all_values()
        found = False
        for idx, row in enumerate(all_values[1:], start=2):
            if len(row) >= col_po and row[col_po - 1] == no_po:
                found = True
                ws.update_cell(idx, col_status, "Lunas")
                ws.update_cell(idx, col_tgl, _today_str())
        return found

    # ---------------- UTANG SUPPLIER ----------------

    def _get_saldo_utang(self, nama_supplier):
        total = 0.0
        for r in self._records(self._ws(config.SHEET_UTANG_SUPPLIER)):
            if str(r.get("Nama_Supplier", "")).strip().lower() != nama_supplier.strip().lower():
                continue
            try:
                jumlah = float(r.get("Jumlah", 0) or 0)
            except ValueError:
                jumlah = 0
            if r.get("Jenis") == "Utang Baru":
                total += jumlah
            elif r.get("Jenis") == "Pembayaran":
                total -= jumlah
        return total

    def _add_utang_entry(self, nama_supplier, no_po, jenis, jumlah, catatan=""):
        saldo = self._get_saldo_utang(nama_supplier)
        saldo_baru = saldo + jumlah if jenis == "Utang Baru" else saldo - jumlah
        self._ws(config.SHEET_UTANG_SUPPLIER).append_row(
            [_today_str(), nama_supplier, no_po, jenis, jumlah, saldo_baru, catatan],
            value_input_option="RAW",
        )
        return saldo_baru

    def bayar_utang(self, nama_supplier, jumlah, catatan=""):
        """Catat pembayaran ke supplier. Balikin sisa utang setelah dibayar."""
        with self._lock:
            saldo_baru = self._add_utang_entry(nama_supplier, "", "Pembayaran", jumlah, catatan)
        return saldo_baru

    def get_utang_summary(self):
        """dict nama_supplier -> sisa utang (cuma yang > 0)"""
        names = set()
        for r in self._records(self._ws(config.SHEET_UTANG_SUPPLIER)):
            nama = str(r.get("Nama_Supplier", "")).strip()
            if nama:
                names.add(nama)
        out = {}
        for nama in names:
            saldo = self._get_saldo_utang(nama)
            if round(saldo) != 0:
                out[nama] = saldo
        return out

    # ---------------- KAS ----------------

    def add_kas_entry(self, jenis, kategori, jumlah, keterangan="", ref="", tanggal=None):
        """jenis: 'Masuk' atau 'Keluar'"""
        self._ws(config.SHEET_KAS).append_row(
            [tanggal or _today_str(), jenis, kategori, jumlah, keterangan, ref],
            value_input_option="RAW",
        )

    def get_kas_entries(self, year=None, month=None):
        rows = self._records(self._ws(config.SHEET_KAS))
        if not year or not month:
            return rows
        prefix = f"{year:04d}-{month:02d}"
        return [r for r in rows if str(r.get("Tanggal", "")).startswith(prefix)]

    def get_laporan_bulanan(self, year, month):
        entries = self.get_kas_entries(year, month)
        masuk_by_kategori = {}
        keluar_by_kategori = {}
        total_masuk = 0.0
        total_keluar = 0.0
        for r in entries:
            try:
                jumlah = float(r.get("Jumlah", 0) or 0)
            except ValueError:
                jumlah = 0
            kategori = r.get("Kategori", "Lainnya") or "Lainnya"
            if r.get("Jenis") == "Masuk":
                masuk_by_kategori[kategori] = masuk_by_kategori.get(kategori, 0) + jumlah
                total_masuk += jumlah
            elif r.get("Jenis") == "Keluar":
                keluar_by_kategori[kategori] = keluar_by_kategori.get(kategori, 0) + jumlah
                total_keluar += jumlah
        return {
            "masuk_by_kategori": masuk_by_kategori,
            "keluar_by_kategori": keluar_by_kategori,
            "total_masuk": total_masuk,
            "total_keluar": total_keluar,
            "laba": total_masuk - total_keluar,
        }


_client = None
_client_lock = threading.Lock()


def get_sheets_client() -> "SheetsClient":
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = SheetsClient()
    return _client
