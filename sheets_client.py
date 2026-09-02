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

    @staticmethod
    def _safe_text(val):
        """Paksa value disimpen sebagai TEKS murni di Sheets, BUKAN kena
        interpretasi jadi rumus -- ini yang bikin No HP Kelvin muncul jadi
        '#ERROR!' di Sheets (kalau nomornya kebetulan diawali karakter kayak
        '+' atau '=', Google Sheets ngira itu rumus, bukan teks biasa).
        Nempelin apostrof di depan kalau perlu -- pola yang sama kayak yang
        udah dipakai buat Minggu_PO/Tanggal_Kirim, sekarang diperluas buat
        semua field teks bebas (No HP, Nama, Alamat) yang diisi customer/AI
        dan berpotensi kebetulan diawali karakter pemicu rumus."""
        s = str(val) if val is not None else ""
        if s[:1] in ("=", "+", "-", "@"):
            return f"'{s}"
        return s

    @staticmethod
    def _clean_phone(no_hp):
        """Rapiin nomor HP -- buang semua spasi/strip/tanda baca, TAPI
        pertahanin tanda '+' di depan kalau ada (buat nomor luar negeri kayak
        '+31 6 85118794' -> '+31685118794'). Nomor kosong/'-' dibiarin apa
        adanya. Dipanggil sekali di sini biar berlaku SAMA buat semua jalur
        order masuk (web ATAU chat Telegram yang di-AI-parse), nggak perlu
        dibersihin manual satu-satu di /edit."""
        s = str(no_hp or "").strip()
        if not s or s == "-":
            return s
        prefix = "+" if s.startswith("+") else ""
        digits = re.sub(r"[^\d]", "", s)
        return prefix + digits if digits else s

    def add_order_rows(self, order: dict, minggu_po: str, box_groups: list = None):
        """
        order = {
            "nama": str, "no_hp": str, "alamat": str, "metode": "Kirim"/"Ambil",
            "items": [{"kategori": str, "rasa": str, "qty": int}, ...],
            "ongkir": int, "catatan": str (opsional)
        }
        box_groups = rincian pembagian per box (opsional) dari hasil parse AI,
        misal [{"jumlah_box": 22, "items": [{"kategori":.., "rasa":.., "qty_per_box":..}]}].
        Disimpen sebagai JSON di kolom Box_Info (SAMA di semua baris item order
        ini, kayak pola Ongkir/Tanggal_Kirim yang juga diulang di tiap baris) --
        biar kalau surat jalan/invoice di-generate ULANG belakangan (/suratjalan,
        /invoice), rincian box-nya masih bisa dibaca lagi, nggak ilang kayak
        sebelumnya (yang cuma numpang lewat sekali doang pas konfirmasi).

        order["catatan"] (opsional, misal "Donat & Gula dipisah pas packing")
        disimpen ke kolom Catatan (SAMA polanya kayak Ongkir/Box_Info -- diulang
        di semua baris item order ini) -- SEBELUM ini, catatan cuma numpang
        lewat di preview Telegram doang, ilang abis di-Simpan & Generate,
        nggak pernah nyampe ke surat jalan/tukang packing. WAJIB nambahin
        kolom header "Catatan" PALING BELAKANG (setelah Box_Info) di Google
        Sheet Orders-nya dulu, manual, sebelum fitur ini dipakai -- append_rows
        di bawah nulis berdasarkan URUTAN kolom, bukan nama header.

        order["kurir"] (opsional, misal "JNE"/"Paxel"/"J&T", diisi admin
        lewat tombol "Isi Kurir" pas konfirmasi order -- kosong berarti
        dikirim pake armada/kurir toko sendiri) disimpen ke kolom Kurir,
        SAMA polanya kayak Catatan. WAJIB nambahin kolom header "Kurir"
        PALING BELAKANG (setelah Catatan) di Google Sheet Orders-nya dulu,
        manual, sebelum fitur ini dipakai -- kalau belum ada, bot tetep
        jalan normal, cuma info kurirnya nggak kesimpen/ke-pakai di rekap.

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
        box_info_json = json.dumps(box_groups, ensure_ascii=False) if box_groups else ""
        catatan = order.get("catatan") or ""
        # order["kurir"] (opsional, misal "JNE"/"Paxel"/"J&T", diisi admin
        # lewat tombol "Isi Kurir" pas konfirmasi order) -- kosong berarti
        # dikirim pake armada/kurir toko sendiri. Dipakai /rekap buat
        # misahin daftar "DIKIRIM (KURIR)" dari "DIKIRIM" (armada) di topic
        # Pengiriman, dan ikut ditampilin di surat jalan/invoice.
        kurir = order.get("kurir") or ""
        rows = []
        order_records = []
        for item in order["items"]:
            harga, rasa_cocok = self._find_price(price_map, item["kategori"], item["rasa"])
            subtotal = harga * item["qty"]
            rows.append([
                timestamp,
                f"'{minggu_po}",  # apostrof di depan = paksa Sheets simpen sebagai teks,
                                   # biar nggak otomatis diubah jadi format Date sendiri
                self._safe_text(order["nama"]),
                self._safe_text(self._clean_phone(order["no_hp"])),
                self._safe_text(order["alamat"]),
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
                box_info_json,  # kolom BARU lagi, paling belakang setelah Tanggal_Kirim
                self._safe_text(catatan),  # kolom BARU lagi, paling belakang setelah Box_Info
                self._safe_text(kurir),  # kolom BARU lagi, paling belakang setelah Catatan
            ])
            order_records.append({
                "Kategori": item["kategori"],
                "Rasa": rasa_cocok,
                "Qty": item["qty"],
                "Harga_Satuan": harga,
                "Metode": order["metode"],
                "No_HP": self._clean_phone(order["no_hp"]),
                "Alamat": order["alamat"],
                "Ongkir": order.get("ongkir", 0),
                "Tanggal_Kirim": tanggal_kirim,
                "Box_Info": box_info_json,
                "Catatan": catatan,
                "Kurir": kurir,
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
        Order yang Tanggal_Kirim-nya udah nyampe cutoff jam 10:00 WIB PADA
        HARI ITU SENDIRI otomatis dianggap udah kelar diproduksi & dikirim --
        Status-nya diubah dari 'Pending' jadi 'Terkirim'. Jadi order buat
        Kamis tgl 20 bakal keflip Kamis tgl 20 jam 10:00 pagi juga -- nggak
        perlu nunggu lewat ganti hari.

        Cutoff-nya sengaja jam 10:00 (bukan lebih pagi) biar ada waktu buat
        order yang masuk tengah malam (misal jam 00:00) tetap kejaring di
        rekap produksi paginya sebelum ke-flip -- kalau cutoff-nya lebih
        mepet ke jam 00:00, order dini hari kayak gitu bisa keburu ke-flip
        duluan sebelum admin sempet liat & produksi.

        Dipanggil otomatis dari get_pending_orders_by_week() tiap kali ada
        yang minta rekap produksi (jadi keupdate begitu direquest kapan aja
        abis jam 10 pagi hari H). Idealnya dipanggil JUGA dari job terjadwal
        harian jam 10:00 WIB (di scheduler_jobs.py) biar Sheets-nya sendiri
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
        # Minggu_PO itu OPSIONAL buat fallback doang -- kalau nggak ketemu
        # ya udah, order lama yang Tanggal_Kirim-nya kosong tetep diskip aja
        # (nggak fatal, cuma nggak ke-rollover otomatis).
        idx_minggu = header.index("Minggu_PO") if "Minggu_PO" in header else None

        tz = date_helpers.get_timezone()
        now = now or datetime.datetime.now(tz)
        cutoff_hari_ini = now.replace(hour=10, minute=0, second=0, microsecond=0)
        # Sebelum jam 10 pagi, "batas hari" efektifnya masih KEMARIN -- order
        # yang tanggal kirimnya HARI INI belum boleh keflip sampe jam 10 pagi
        # bener-bener lewat.
        boundary = now.date() if now >= cutoff_hari_ini else (now - datetime.timedelta(days=1)).date()

        rows_to_update = []
        for i, row in enumerate(all_values[1:], start=2):  # baris 1 = header
            if len(row) <= idx_status:
                continue
            status = row[idx_status].strip()
            if status.lower() != "pending":
                continue

            # Tanggal_Kirim itu kolom yang ditambahin BELAKANGAN -- order
            # lama (sebelum kolom ini ada) bakal kosong di sini. Kalau
            # kosong, balik ke Minggu_PO (Kamis PO minggu itu) sebagai
            # tanggal kirim implisit -- SAMA PERSIS kayak fallback yang
            # dipakai pas order itu pertama kali disimpen (add_order_rows).
            # Tanpa ini, order lama bakal Pending selama-lamanya dan HARUS
            # diubah manual satu-satu di Sheets.
            tanggal_kirim = row[idx_tanggal_kirim].strip() if idx_tanggal_kirim < len(row) else ""
            if not tanggal_kirim and idx_minggu is not None and idx_minggu < len(row):
                tanggal_kirim = row[idx_minggu].strip()
            if not tanggal_kirim:
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
        ITU JUGA (nggak nunggu cutoff otomatis jam 10:00 WIB di
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

    def get_pending_orders_by_tanggal_range(self, start_date: str, end_date: str):
        """Rekap produksi berdasarkan TANGGAL_KIRIM (BUKAN Minggu_PO) dalam
        rentang tanggal tertentu -- gabungan SEMUA customer yang tanggal
        kirimnya jatuh di rentang itu, nggak peduli Minggu_PO-nya beda-beda.
        Dipakai buat '/rekap 2026-08-29' atau '/rekap 2026-08-28:2026-08-29'.

        Berguna banget buat kasus tanggal kirim custom (besok/lusa) yang
        bikin Minggu_PO-nya beda dari minggu aktif -- customer kayak gitu
        nggak nongol di rekap mingguan biasa, tapi tetep kejaring di sini
        selama Tanggal_Kirim-nya masuk rentang yang diminta.

        start_date, end_date: string 'YYYY-MM-DD' (inklusif dua-duanya).
        Sama kayak get_pending_orders_by_week: jalanin rollover dulu, baru
        buang yang Status-nya udah 'Terkirim'."""
        try:
            self.rollover_delivered_orders()
        except Exception:
            pass

        try:
            d_start = datetime.datetime.strptime(start_date, "%Y-%m-%d").date()
            d_end = datetime.datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            return []

        result = []
        for o in self.get_all_orders():
            if str(o.get("Status", "")).strip().lower() == "terkirim":
                continue
            d = self._parse_tanggal_fleksibel(o.get("Tanggal_Kirim"))
            if d is not None and d_start <= d <= d_end:
                result.append(o)
        return result

    def get_pending_orders_by_customer_week(self, nama_customer: str, minggu_po: str):
        """Sama kayak get_orders_by_customer_week, TAPI (kalau ada campuran)
        buang baris yang Status-nya udah 'Terkirim'. Dipakai KHUSUS oleh
        /invoice & /suratjalan biar order BARU yang lagi mau di-invoice-in
        nggak ke-mix diam-diam sama order LAIN (nggak berhubungan) dari
        customer yang sama, yang KEBETULAN punya Minggu_PO yang sama juga
        tapi udah kelar/Terkirim duluan.

        TAPI kalau baris customer ini buat minggu itu TERNYATA semuanya
        udah 'Terkirim' (nggak ada campuran, murni 1 order yang udah kelar
        dikirim) -- balikin SEMUA baris apa adanya, biar kapabilitas REPRINT
        invoice/surat jalan yang udah kelar (fitur lama) tetep jalan kayak
        biasa. Cuma kasus CAMPURAN (sebagian Terkirim + sebagian Pending)
        yang di-filter, soalnya di situ risiko ke-mix-nya."""
        try:
            self.rollover_delivered_orders()
        except Exception:
            pass
        semua = self.get_orders_by_customer_week(nama_customer, minggu_po)
        belum_terkirim = [o for o in semua if str(o.get("Status", "")).strip().lower() != "terkirim"]
        return belum_terkirim if belum_terkirim else semua

    def get_orders_by_customer_week(self, nama_customer: str, minggu_po: str):
        return [
            o for o in self.get_orders_by_week(minggu_po)
            if o.get("Nama_Customer", "").strip().lower() == nama_customer.strip().lower()
        ]

    def get_orders_by_customer_any_week(self, nama_customer: str):
        """Cari order customer ini di SEMUA Minggu_PO (bukan cuma minggu yang
        lagi aktif) -- dipakai sebagai FALLBACK oleh /edit, /invoice,
        /suratjalan kalau pencarian di minggu aktif nggak ketemu apa-apa.

        Kejadian nyata yang bikin ini perlu: customer yang minta tanggal
        kirim CUSTOM (misal 'besok') bisa aja Minggu_PO-nya udah kepindah ke
        minggu berikutnya sama sistem, sementara admin masih mikirnya itu
        "punya minggu ini" -- alhasil /edit dia nggak ketemu apa-apa padahal
        datanya ada, cuma nyangkut di Minggu_PO yang beda.

        Kalau customer ini ternyata punya order di BEBERAPA Minggu_PO
        berbeda (kasus jarang tapi mungkin), ambil yang Minggu_PO-nya PALING
        BARU aja -- across historical resiko ke-mix minggu lama yang udah
        nggak relevan.

        Return: (list_order, minggu_po_string) atau ([], None) kalau nggak
        ketemu sama sekali."""
        nama_target = nama_customer.strip().lower()
        semua = [
            o for o in self.get_all_orders()
            if o.get("Nama_Customer", "").strip().lower() == nama_target
        ]
        if not semua:
            return [], None

        by_week = {}
        for o in semua:
            d = self._parse_tanggal_fleksibel(o.get("Minggu_PO"))
            key = d or datetime.date.min
            by_week.setdefault(key, []).append(o)

        minggu_terbaru_key = max(by_week.keys())
        orders_terbaru = by_week[minggu_terbaru_key]
        minggu_po_str = str(orders_terbaru[0].get("Minggu_PO", "")).strip()
        return orders_terbaru, minggu_po_str

    def get_pending_orders_by_customer_any_week(self, nama_customer: str):
        """Sama kayak get_orders_by_customer_any_week, TAPI (kalau ada
        campuran) buang baris yang Status-nya udah 'Terkirim' -- pola
        filter-nya SAMA PERSIS kayak get_pending_orders_by_customer_week.

        BUG NYATA yang ini benerin: customer yang order beberapa kali di
        Minggu_PO yang SAMA (order lama udah Terkirim, order baru masih
        Pending) bisa bikin /edit, /invoice, /suratjalan, & /rekap NamaX
        nyasar narik SEMUA baris itu (lama+baru numplek jadi 1) begitu
        pencarian minggu-aktifnya kosong dan jatuh ke fallback 'any week'
        ini -- padahal cuma yang MASIH PENDING yang harusnya kepake/keedit.
        Laporan admin: '/edit franky' nampilin belasan item numpuk (17 baso,
        10 mocha meises, dst) padahal yang masih pending cuma order baru
        yang barusan diketik.

        Kalau baris di minggu yang ketemu itu TERNYATA semuanya udah
        'Terkirim' (murni order lama yang mau di-reprint/edit ulang, bukan
        campuran), balikin semua apa adanya -- kapabilitas reprint order
        lama tetep jalan kayak biasa.

        Return: (list_order, minggu_po_string) atau ([], None)."""
        try:
            self.rollover_delivered_orders()
        except Exception:
            pass
        semua, minggu_po = self.get_orders_by_customer_any_week(nama_customer)
        if not semua:
            return [], None
        belum_terkirim = [o for o in semua if str(o.get("Status", "")).strip().lower() != "terkirim"]
        return (belum_terkirim, minggu_po) if belum_terkirim else (semua, minggu_po)

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
