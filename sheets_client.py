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

# ---------- PAKET BUNDLING (multi-paket, didefinisiin admin lewat chat) ----------
# Beda dari versi awal yang HARDCODE 1 paket doang ("8 Roti + 1 Dubai
# Coklat" = flat 150rb) -- sekarang definisi paket (nama, harga, isi/
# komposisi) disimpen di tab Sheets config.SHEET_PAKET_BUNDLING, admin bisa
# nambah/ubah/nonaktifin paket KAPAN AJA lewat chat ke bot (liat
# upsert_bundle/set_bundle_active/set_bundle_harga di bawah), TANPA perlu
# ubah kode/upload ulang web ke Netlify. 1 baris Sheets = 1 "slot" komposisi
# (kategori + qty, rasa kosong berarti bebas pilih dalam kategori itu, rasa
# keisi berarti item TETAP/terkunci kayak dulu Dubai Coklat); beberapa baris
# Nama_Paket yang sama = 1 paket dengan beberapa slot.
#
# Harga paket TETEP nggak dipercaya dari input web/AI -- server yang baca
# harga dari definisi paket di Sheets (get_bundle_by_name), klien cuma
# ngirim/nyaranin nama paket + komposisi item-nya doang.

def _norm_nama(s):
    """Normalisasi nama customer buat DIBANDINGIN (bukan buat disimpen) --
    strip+lower doang KURANG, soalnya spasi ganda/nggak konsisten (misal
    kepencet 2 spasi nggak sengaja, atau nama kecopy dari WA yang
    formatnya beda) bikin exact-match GAGAL padahal nama-nya SAMA PERSIS
    kalau dibaca manusia.

    Kejadian nyata yang bikin ini perlu: order "Bianca ( untuk pak
    Joshua )" nggak ketemu sama sekali lewat /edit, walaupun admin udah
    COPY-PASTE PERSIS dari cell Sheets-nya -- ternyata nama itu kesimpen
    dengan spasi ganda yang nggak keliatan di surat jalan (soalnya
    word-wrap di situ ngerapiin spasi ganda jadi 1 tanpa sengaja pas
    nge-render ke gambar), padahal di data mentah Sheets spasinya masih
    ganda. Collapse semua whitespace beruntun jadi 1 spasi SEBELUM
    strip+lower, biar pencocokan nama nggak kesandung beda spasi kayak
    gini lagi -- dipakai di SEMUA tempat yang nyocokin Nama_Customer."""
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def is_komposisi_bundle_valid(bundle: dict, items: list) -> bool:
    """True kalau 'items' PERSIS nutupin semua slot di definisi 'bundle'
    (dict hasil get_bundle_by_name/get_all_bundles) -- nggak boleh
    kurang, nggak boleh lebih, nggak boleh ada item DI LUAR slot yang
    didefinisiin. Slot rasa=None (bebas pilih) diitung TOTAL qty-nya
    lintas rasa dalam kategori itu (KECUALI rasa yang ada di slot["kecuali"]
    -- rasa yang di-exclude nggak boleh dihitung masuk slot bebas-pilih
    itu sama sekali, jadi kalau ada di order, order dianggap DI LUAR
    definisi slot manapun); slot rasa keisi (item tetap/terkunci, kayak
    dulu Dubai Coklat) harus PERSIS kategori+rasa itu sejumlah qty situ.
    Order tetep bisa disimpen walau komposisinya nggak valid -- cuma
    harganya dihitung normal per item (liat pemanggil fungsi ini di
    add_order_rows), bukan digagalin total."""
    free_targets = {}   # kategori(lower) -> total qty dibutuhin (bebas rasa)
    free_kecuali = {}   # kategori(lower) -> set rasa(lower) yang di-exclude dari slot bebas itu
    fixed_targets = {}  # (kategori(lower), rasa(lower)) -> qty dibutuhin (rasa tetap)
    for slot in bundle.get("slots", []):
        kat = str(slot.get("kategori", "")).strip().lower()
        rasa = slot.get("rasa")
        qty = int(slot.get("qty") or 0)
        if rasa:
            key = (kat, str(rasa).strip().lower())
            fixed_targets[key] = fixed_targets.get(key, 0) + qty
        else:
            free_targets[kat] = free_targets.get(kat, 0) + qty
            kecuali_set = {str(r).strip().lower() for r in (slot.get("kecuali") or []) if str(r).strip()}
            free_kecuali.setdefault(kat, set()).update(kecuali_set)

    free_used = {k: 0 for k in free_targets}
    fixed_used = {k: 0 for k in fixed_targets}

    for it in items:
        kategori = str(it.get("kategori", "")).strip().lower()
        rasa = str(it.get("rasa", "")).strip().lower()
        qty = int(it.get("qty") or 0)
        fixed_key = (kategori, rasa)
        if fixed_key in fixed_targets:
            fixed_used[fixed_key] += qty
        elif kategori in free_targets and rasa not in free_kecuali.get(kategori, set()):
            free_used[kategori] += qty
        else:
            return False  # item di luar definisi slot manapun (atau rasa-nya di-exclude)

    return free_used == free_targets and fixed_used == fixed_targets


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

    def _get_all_records_safe(self, ws):
        """Ganti ws.get_all_records() bawaan gspread -- versi bawaan itu
        RAISE ERROR (nge-buyarin SELURUH /rekap, /invoice, dst sekaligus)
        kalau ternyata ada 2 KOLOM HEADER yang namanya SAMA PERSIS (atau
        sama-sama kosong) di baris pertama sheet. Gampang kejadian kalau
        admin nambah header baru manual terus nggak sadar ke-double /
        typo (misal nambah 'Addon_Jenis' tapi ternyata sheet-nya udah
        punya kolom kosong tanpa nama di situ, atau nge-copy-paste header
        yang sama 2x).

        Di sini kita baca RAW values sendiri (get_all_values, BUKAN
        get_all_records), lalu header yang dobel/kosong dibikin unik
        otomatis (ditambahin '_2', '_3', dst, atau '_kosong' buat yang
        blank) -- jadi bot TETEP JALAN walau ada duplikat header, bukan
        mati total. (Kolom yang ke-rename kayak gini emang jadi nggak
        kebaca bener sampai admin benerin header aslinya di Sheets, tapi
        minimal fitur LAIN yang nggak nyentuh kolom situ tetep normal.)
        """
        values = ws.get_all_values()
        if not values:
            return []
        header_row = values[0]
        seen = {}
        headers = []
        for h in header_row:
            h = (h or "").strip()
            if not h:
                h = "_kosong"
            if h in seen:
                seen[h] += 1
                h = f"{h}_{seen[h]}"
            else:
                seen[h] = 1
            headers.append(h)

        records = []
        for row in values[1:]:
            if not any(str(cell).strip() for cell in row):
                continue  # baris kosong total, skip (sama kayak get_all_records)
            row_padded = list(row) + [""] * (len(headers) - len(row))
            records.append(dict(zip(headers, row_padded)))
        return records

    def _normalize_records(self, ws):
        """
        Ambil semua baris dari sebuah worksheet, tapi "normalisasi" nama
        kolomnya dulu (spasi jadi underscore, dll) -- biar nggak masalah
        walau header di Sheets ditulis 'Minggu PO' atau 'Minggu_PO', dua-duanya
        tetap kebaca sebagai field yang sama oleh kode ini.
        """
        records = self._get_all_records_safe(ws)
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

        order["addon_jenis"]/["addon_qty"]/["addon_total"] (opsional, misal
        jenis "Tali Pita" qty 2 total Rp10.000, diisi admin lewat tombol
        "Isi Add-on" pas konfirmasi order, atau dikirim dari form web)
        disimpen ke kolom Addon_Jenis/Addon_Qty/Addon_Total, SAMA polanya
        kayak Kurir. WAJIB nambahin 3 kolom header "Addon_Jenis",
        "Addon_Qty", "Addon_Total" PALING BELAKANG (setelah Kurir, dalam
        urutan itu) di Google Sheet Orders-nya dulu, manual, sebelum fitur
        ini dipakai -- kalau belum ada, bot tetep jalan normal, cuma
        add-on-nya nggak kesimpen/ke-pakai aja.

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
        # order["addon_jenis"]/["addon_qty"]/["addon_total"] (opsional, misal
        # "Tali Pita" qty 2 total Rp10.000, diisi admin lewat tombol "Isi
        # Add-on" pas konfirmasi order, atau dikirim dari form web) -- kosong
        # semua berarti nggak pakai add-on. Addon_Total ikut ditambahin ke
        # grand total invoice, dan Addon_Jenis+Addon_Qty ikut ditampilin di
        # surat jalan buat yang packing.
        addon_jenis = order.get("addon_jenis") or ""
        addon_qty = int(order.get("addon_qty") or 0)
        addon_total = int(order.get("addon_total") or 0)

        # ---------- Paket Bundling (opsional, multi-paket) ----------
        # order["paket_bundling_nama"] (dari bot.py/web_order_server.py) cuma
        # NAMA paket yang DIKLAIM admin/customer -- harga & validitas
        # komposisi TETEP dihitung ulang di sini berdasarkan definisi paket
        # yang beneran ada di Sheets (get_bundle_by_name) + validator
        # is_komposisi_bundle_valid(), BUKAN dipercaya gitu aja. Kalau nama
        # paketnya nggak ketemu / komposisinya nggak PAS, order tetep
        # kesimpen normal (harga per item dari PriceList biasa) -- cuma flag
        # "bundling_diterapkan" balik False biar caller (bot.py) bisa kasih
        # tau admin buat cek manual.
        #
        # Skema harganya: hitung dulu harga ASLI tiap baris dari PriceList
        # (biar rasa yang lebih mahal/murah tetep kerasa beda), skalakan
        # proporsional biar TOTAL semua baris PAS harga paket, lalu bulatin
        # tiap baris ke kelipatan 500 (biar angkanya rapi kayak harga
        # PriceList biasa). SATU baris "anchor" (item qty=1 yang cocok slot
        # rasa-tetap kalau ada -- kayak dulu Dubai Coklat -- atau baris
        # TERAKHIR kalau paketnya semua slot bebas pilih) nampung SISA
        # pembulatan, biar totalnya PERSIS pas, bukan meleset beberapa
        # rupiah gara-gara pembulatan per baris.
        bundling_diterapkan = False
        harga_override_by_index = {}
        nama_paket_diklaim = order.get("paket_bundling_nama")
        bundle_def = self.get_bundle_by_name(nama_paket_diklaim) if nama_paket_diklaim else None
        if bundle_def and is_komposisi_bundle_valid(bundle_def, order["items"]):
            harga_asli_list = [
                self._find_price(price_map, it["kategori"], it["rasa"])[0] for it in order["items"]
            ]
            total_asli = sum(h * it["qty"] for h, it in zip(harga_asli_list, order["items"]))
            harga_paket = int(bundle_def.get("harga") or 0)
            if total_asli > 0 and harga_paket > 0:
                scale = harga_paket / total_asli

                fixed_rasa_set = {
                    (str(s.get("kategori", "")).strip().lower(), str(s.get("rasa", "")).strip().lower())
                    for s in bundle_def.get("slots", []) if s.get("rasa")
                }
                anchor_idx = None
                for idx, it in enumerate(order["items"]):
                    key = (str(it["kategori"]).strip().lower(), str(it["rasa"]).strip().lower())
                    if key in fixed_rasa_set and int(it.get("qty") or 0) == 1:
                        anchor_idx = idx
                        break
                if anchor_idx is None:
                    anchor_idx = len(order["items"]) - 1

                subtotal_selain_anchor = 0
                for idx, (h, it) in enumerate(zip(harga_asli_list, order["items"])):
                    if idx == anchor_idx:
                        continue
                    harga_bulat = max(500, round(h * scale / 500) * 500)
                    harga_override_by_index[idx] = harga_bulat
                    subtotal_selain_anchor += harga_bulat * it["qty"]
                qty_anchor = int(order["items"][anchor_idx].get("qty") or 1) or 1
                sisa = harga_paket - subtotal_selain_anchor
                # qty_anchor hampir selalu 1 (slot rasa-tetap kayak Dubai
                # Coklat SELALU qty 1 -- kalau paketnya semua bebas pilih dan
                # anchor kebetulan qty > 1, sisa pembulatan dibagi rata,
                # meleset PALING BANYAK beberapa rupiah doang, nggak masalah).
                harga_override_by_index[anchor_idx] = sisa // qty_anchor
                bundling_diterapkan = True

        # Nama paket bundling (kalau beneran KETERAPAN -- validasi lolos,
        # harga dihitung ulang proporsional) DISIMPEN juga ke tiap baris item
        # order ini, kolom Paket_Bundling, PALING BELAKANG (setelah
        # Addon_Total). Dulu nama paketnya nggak kesimpen sama sekali abis
        # dipakai buat hitung harga -- jadi invoice/surat jalan yang
        # digenerate ULANG belakangan (/invoice, /suratjalan) nggak ada
        # petunjuk lagi item mana yang bagian dari paket apa. WAJIB nambahin
        # kolom header "Paket_Bundling" PALING BELAKANG di Google Sheet
        # Orders-nya dulu, manual, sebelum fitur ini kepake -- kalau belum
        # ada, bot tetep jalan normal, cuma keterangan paketnya nggak
        # kesimpen/ke-pakai di invoice/surat jalan.
        paket_bundling_label = bundle_def["nama"] if (bundling_diterapkan and bundle_def) else ""

        rows = []
        order_records = []
        for idx, item in enumerate(order["items"]):
            if idx in harga_override_by_index:
                harga = harga_override_by_index[idx]
                rasa_cocok = item["rasa"]  # udah dijamin match persis lewat validator, ga perlu fuzzy-match lagi
            else:
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
                self._safe_text(addon_jenis),  # kolom BARU lagi, paling belakang setelah Kurir
                addon_qty,  # kolom BARU lagi, paling belakang setelah Addon_Jenis
                addon_total,  # kolom BARU lagi, paling belakang setelah Addon_Qty
                self._safe_text(paket_bundling_label),  # kolom BARU lagi, paling belakang setelah Addon_Total
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
                "Addon_Jenis": addon_jenis,
                "Addon_Qty": addon_qty,
                "Addon_Total": addon_total,
                "Paket_Bundling": paket_bundling_label,
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

        nama_target = _norm_nama(nama_customer)
        rows_to_delete = []
        for i, row in enumerate(all_values[1:], start=2):  # baris 1 = header, gspread 1-indexed
            if len(row) <= max(idx_minggu, idx_nama):
                continue
            row_nama = _norm_nama(row[idx_nama])
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

        nama_target = _norm_nama(nama_customer)
        rows_to_update = []
        for i, row in enumerate(all_values[1:], start=2):
            if len(row) <= max(idx_status, idx_minggu, idx_nama):
                continue
            row_nama = _norm_nama(row[idx_nama])
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
        Status-nya udah 'Terkirim' dari hasilnya, DAN (3) buang order yang
        Tanggal_Kirim-nya CUSTOM beda dari minggu_po ini (misal Minggu_PO-nya
        minggu ini tapi Tanggal_Kirim-nya di-custom ke hari/minggu lain) --
        soalnya order kayak gitu HARUSNYA cuma nongol di rekap tanggal
        kirimnya sendiri (/rekap <tanggal>), bukan ikut kehitung di rekap
        mingguan biasa cuma gara-gara Minggu_PO-nya kebetulan minggu ini.
        KHUSUS dipakai buat REKAP PRODUKSI (+ daftar DIKIRIM/KURIR/AMBIL),
        biar nggak keitung ulang order yang sebenernya udah kelar diproduksi
        & dikirim, ATAU yang tanggal kirimnya emang beda hari.

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
        hasil = []
        for o in self.get_orders_by_week(minggu_po):
            if str(o.get("Status", "")).strip().lower() == "terkirim":
                continue
            tanggal_kirim = str(o.get("Tanggal_Kirim", "")).strip()
            if tanggal_kirim and not self._minggu_po_cocok(tanggal_kirim, minggu_po):
                # Tanggal_Kirim custom-nya beda dari minggu_po yang lagi
                # direkap -- skip, biar cuma kejaring di /rekap <tanggal>
                # sendiri (get_pending_orders_by_tanggal_range).
                continue
            hasil.append(o)
        return hasil

    def get_pending_orders_by_tanggal_range(self, start_date: str, end_date: str):
        """Rekap produksi berdasarkan TANGGAL_KIRIM (BUKAN Minggu_PO) dalam
        rentang tanggal tertentu -- gabungan SEMUA customer yang tanggal
        kirimnya jatuh di rentang itu, nggak peduli Minggu_PO-nya beda-beda.
        Dipakai buat '/rekap 2026-08-29' atau '/rekap 2026-08-28:2026-08-29'.

        Berguna banget buat kasus tanggal kirim custom (besok/lusa/tanggal
        lain) -- customer kayak gitu SENGAJA udah di-skip dari rekap
        mingguan biasa (liat get_pending_orders_by_week), tapi tetep
        kejaring di sini selama Tanggal_Kirim-nya masuk rentang yang
        diminta.

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
        nama_target = _norm_nama(nama_customer)
        return [
            o for o in self.get_orders_by_week(minggu_po)
            if _norm_nama(o.get("Nama_Customer", "")) == nama_target
        ]

    @staticmethod
    def _cari_nama_by_prefix(records, nama_prefix):
        """Cari nama customer ASLI (persis apa adanya, BUKAN yang
        dinormalisasi) yang DIAWALI sama nama_prefix (case/spasi-insensitive)
        -- dipake sebagai fallback TERAKHIR kalau exact match (get_orders_by_
        customer_week/any_week) gagal, biar admin nggak perlu ngetik nama
        lengkap + embel2 (misal '/edit Bianca' cukup buat nemuin order
        'Bianca ( untuk pak Joshua )', nggak perlu ngetik semuanya).

        SENGAJA prefix match (nama HARUS diawali persis dari situ), BUKAN
        substring bebas di posisi mana pun -- biar nggak gampang nyasar
        nyantol ke nama yang nggak nyambung (misal ngetik 'Rian' jangan
        sampe ketarik ke 'Adrian' yang beda orang).

        Return: list nama ASLI yang unik & diurutin -- kalau hasilnya lebih
        dari 1 (ada beberapa customer beda yang nama-nya sama-sama diawali
        prefix itu), caller WAJIB minta admin sebutin nama lengkap yang mana
        (jangan asal pilih salah satu -- resiko nyasar ke order/customer
        yang salah buat dokumen keuangan kayak invoice)."""
        prefix = _norm_nama(nama_prefix)
        if not prefix:
            return []
        ditemukan = {}
        for o in records:
            asli = str(o.get("Nama_Customer", "")).strip()
            if asli and _norm_nama(asli).startswith(prefix):
                ditemukan[_norm_nama(asli)] = asli
        return sorted(ditemukan.values())

    def cari_nama_by_prefix_week(self, nama_prefix: str, minggu_po: str):
        """Versi minggu aktif dari _cari_nama_by_prefix -- lihat docstring-nya."""
        return self._cari_nama_by_prefix(self.get_orders_by_week(minggu_po), nama_prefix)

    def cari_nama_by_prefix_any_week(self, nama_prefix: str):
        """Versi SEMUA minggu dari _cari_nama_by_prefix -- lihat docstring-nya."""
        return self._cari_nama_by_prefix(self.get_all_orders(), nama_prefix)

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
        nama_target = _norm_nama(nama_customer)
        semua = [
            o for o in self.get_all_orders()
            if _norm_nama(o.get("Nama_Customer", "")) == nama_target
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

    def get_orders_by_customer_month(self, nama_customer: str, year: int, month: int):
        """Cari SEMUA order 1 customer yang Minggu_PO-nya jatuh di bulan/tahun
        tertentu -- BEDA sama get_orders_by_customer_any_week (yang cuma ambil
        Minggu_PO PALING BARU doang) dan get_pending_orders_by_customer_week
        (yang cuma minggu AKTIF & buang yang udah Terkirim). Ini khusus buat
        kasus admin minta invoice/surat jalan customer dari BULAN LAMA yang
        spesifik (misal "invoice apple bulan agustus") -- sebelum ini nggak
        ada jalan sama sekali buat nyari kayak gitu, jadi selalu nyasar balik
        ke minggu aktif/terbaru walau bulan yang diminta udah lewat lama.

        Status SENGAJA nggak difilter (order yang udah 'Terkirim' tetep
        diikutin) -- justru itu yang mau diliat lagi/direprint, beda tujuan
        sama pengecekan 'apa masih pending' yang dipakai fungsi2 minggu aktif.

        Return: list order (dict), belum dikelompokin per Minggu_PO -- kalau
        customer ternyata punya lebih dari 1 Minggu_PO dalam bulan yang sama,
        itu tanggung jawab caller buat dikelompokin sebelum di-generate jadi
        dokumen (lihat _kirim_dokumen_bulan_lama di bot.py)."""
        nama_target = _norm_nama(nama_customer)
        bulanan = self.get_orders_by_month(year, month)
        return [o for o in bulanan if _norm_nama(o.get("Nama_Customer", "")) == nama_target]

    def _filter_by_month_range(self, records, year_start: int, month_start: int, year_end: int, month_end: int):
        """Helper bersama buat get_orders_by_month_range &
        get_historis_orders_by_month_range -- filter list record APAPUN
        (asal ada kolom Minggu_PO) berdasarkan rentang bulan year_start-
        month_start sampai year_end-month_end (inklusif)."""
        start_key = year_start * 100 + month_start
        end_key = year_end * 100 + month_end
        result = []
        for o in records:
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

    def get_orders_by_month_range(self, year_start: int, month_start: int, year_end: int, month_end: int):
        """Filter berdasarkan Minggu_PO yang jatuh di rentang bulan year_start-month_start
        sampai year_end-month_end (inklusif). Buat laporan bulanan yang minta
        beberapa bulan sekaligus, misal '2 bulan ke belakang' atau 'Juli-Agustus'."""
        return self._filter_by_month_range(self.get_all_orders(), year_start, month_start, year_end, month_end)

    def get_historis_orders_by_month_range(self, year_start: int, month_start: int, year_end: int, month_end: int):
        """Sama kayak get_orders_by_month_range, TAPI bacanya dari tab
        OPSIONAL config.SHEET_RIWAYAT_HISTORIS ('Riwayat Historis') --
        tempat admin mindahin baris qty historis (dari SEBELUM order
        dicatet lewat bot) biar nggak ganggu pivot table/chart di Orders.
        KHUSUS dipakai /laporanbulanan biar qty lama yang belum kebayar ke
        supplier tetep ikut kehitung walau udah nggak ada di Orders lagi.

        Kalau tab-nya nggak ada (belum pernah dibikin, atau namanya beda),
        return list KOSONG aja -- JANGAN bikin /laporanbulanan ikutan error
        gara-gara tab opsional ini nggak ketemu."""
        try:
            ws = self.sheet.worksheet(config.SHEET_RIWAYAT_HISTORIS)
        except gspread.exceptions.WorksheetNotFound:
            return []
        records = self._normalize_records(ws)
        return self._filter_by_month_range(records, year_start, month_start, year_end, month_end)

    # ---------- PENGATURAN (key-value umum, dipertahanin buat setting lain
    # di masa depan) ----------
    def _get_or_create_pengaturan_ws(self):
        try:
            return self.sheet.worksheet(config.SHEET_PENGATURAN)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=config.SHEET_PENGATURAN, rows=20, cols=2)
            ws.update(values=[["Key", "Value"]], range_name="A1:B1")
            return ws

    def _read_pengaturan_value(self, ws, key):
        rows = ws.get_all_values()
        for row in rows[1:]:  # skip header
            if len(row) >= 2 and str(row[0]).strip().lower() == key.lower():
                return row[1]
        return None

    def _write_pengaturan_value(self, ws, key, value):
        rows = ws.get_all_values()
        for idx, row in enumerate(rows[1:], start=2):
            if len(row) >= 1 and str(row[0]).strip().lower() == key.lower():
                ws.update(values=[[value]], range_name=f"B{idx}")
                return
        ws.append_row([key, value])  # key belum ada -- tambahin baris baru

    # ---------- PAKET BUNDLING (multi-paket, tab Sheets config.SHEET_PAKET_BUNDLING) ----------
    # Kolom: Nama_Paket | Kategori | Rasa | Qty | Harga_Paket | Aktif | Kecuali_Rasa.
    # 1 baris = 1 slot komposisi; beberapa baris Nama_Paket sama = 1 paket
    # dengan beberapa slot. Rasa kosong = bebas pilih rasa apa aja dalam
    # kategori itu; Rasa keisi = item TETAP/terkunci (kayak dulu Dubai
    # Coklat). Kecuali_Rasa CUMA berlaku buat baris bebas-pilih (Rasa
    # kosong) -- isinya daftar rasa yang DIKECUALIKAN dari slot itu,
    # dipisah koma (misal "Bun Polos, Roti Polos"), biar admin bisa bilang
    # "8 roti bebas rasa kecuali bun polos" tanpa perlu ubah kategori
    # produk di PriceList. Harga_Paket & Aktif DIULANG di tiap baris
    # paket yang sama (denormalisasi sengaja -- lebih gampang dibaca/diedit
    # manual langsung di Sheets kalau admin perlu, nggak WAJIB lewat chat).

    _PAKET_BUNDLING_HEADER = ["Nama_Paket", "Kategori", "Rasa", "Qty", "Harga_Paket", "Aktif", "Kecuali_Rasa"]

    def _get_or_create_paket_bundling_ws(self):
        try:
            ws = self.sheet.worksheet(config.SHEET_PAKET_BUNDLING)
        except gspread.exceptions.WorksheetNotFound:
            ws = self.sheet.add_worksheet(title=config.SHEET_PAKET_BUNDLING, rows=50, cols=7)
            ws.update(values=[self._PAKET_BUNDLING_HEADER], range_name="A1:G1")
            # Migrasi 1x dari versi lama (single hardcoded bundle + toggle
            # global di tab Pengaturan) -- seed paket "Bundling Spesial"
            # (8 Roti bebas rasa + 1 Dubai Coklat = flat 150rb), Aktif-nya
            # ngikutin status toggle lama kalau ada, biar setting admin yang
            # udah pernah di-set nggak ilang gara-gara migrasi ini.
            aktif_lama = "FALSE"
            try:
                pengaturan_ws = self.sheet.worksheet(config.SHEET_PENGATURAN)
                val = self._read_pengaturan_value(pengaturan_ws, "bundling_enabled")
                if val and str(val).strip().upper() == "TRUE":
                    aktif_lama = "TRUE"
            except Exception:
                pass
            ws.update(values=[
                ["Bundling Spesial", "Roti", "", 8, 150000, aktif_lama, ""],
                ["Bundling Spesial", "Dubai", "Dubai Coklat", 1, 150000, aktif_lama, ""],
            ], range_name="A2:G3")
            return ws
        # Self-heal: sheet yang udah ada dari SEBELUM kolom Kecuali_Rasa
        # ditambahin (bikin paket bundling udah pernah dipakai admin) cuma
        # punya 6 kolom -- tambahin header G1 aja tanpa nyentuh data yang
        # udah ada, biar baris lama (Kecuali_Rasa kosong = nggak exclude
        # apa-apa, perilaku sama kayak sebelumnya) tetep aman.
        try:
            header_row = ws.row_values(1)
            if len(header_row) < 7 or str(header_row[6] if len(header_row) > 6 else "").strip() != "Kecuali_Rasa":
                ws.update(values=[["Kecuali_Rasa"]], range_name="G1")
        except Exception:
            pass
        return ws

    def _read_all_bundle_rows(self):
        ws = self._get_or_create_paket_bundling_ws()
        rows = ws.get_all_values()
        result = []
        for row in rows[1:]:
            if len(row) < 7:
                row = row + [""] * (7 - len(row))
            nama, kategori, rasa, qty, harga, aktif, kecuali_raw = row[:7]
            nama = str(nama).strip()
            if not nama:
                continue
            try:
                qty = int(qty)
            except (ValueError, TypeError):
                continue
            try:
                harga = int(harga)
            except (ValueError, TypeError):
                harga = 0
            kecuali = [s.strip() for s in str(kecuali_raw).split(",") if s.strip()]
            result.append({
                "nama": nama,
                "kategori": str(kategori).strip(),
                "rasa": str(rasa).strip() or None,
                "qty": qty,
                "harga": harga,
                "aktif": str(aktif).strip().upper() == "TRUE",
                "kecuali": kecuali,
            })
        return result

    def get_all_bundles(self, only_active: bool = False) -> list:
        """Return [{"nama":.., "harga":.., "aktif":.., "slots":[{"kategori":..,
        "rasa": .. atau None, "qty":.., "kecuali": [..]}, ...]}, ...] -- 1
        dict per paket (baris2 Sheets yang Nama_Paket-nya sama digabung
        jadi slots). "kecuali" cuma relevan/keisi buat slot bebas-pilih
        (rasa None) -- daftar nama rasa yang dikecualikan dari kategori itu."""
        rows = self._read_all_bundle_rows()
        by_nama = {}
        order = []
        for r in rows:
            if r["nama"] not in by_nama:
                by_nama[r["nama"]] = {"nama": r["nama"], "harga": r["harga"], "aktif": r["aktif"], "slots": []}
                order.append(r["nama"])
            by_nama[r["nama"]]["slots"].append({
                "kategori": r["kategori"],
                "rasa": r["rasa"],
                "qty": r["qty"],
                "kecuali": r["kecuali"],
            })
        bundles = [by_nama[n] for n in order]
        if only_active:
            bundles = [b for b in bundles if b["aktif"]]
        return bundles

    def get_bundle_by_name(self, nama: str):
        if not nama:
            return None
        nama_target = nama.strip().lower()
        for b in self.get_all_bundles():
            if b["nama"].strip().lower() == nama_target:
                return b
        return None

    def upsert_bundle(self, nama: str, harga: int, slots: list, aktif: bool = True):
        """Bikin paket BARU (nama belum ada) atau GANTI TOTAL definisi lama
        (nama udah ada -- semua baris slot lama punya nama itu dihapus,
        ditulis ulang dari 'slots' yang baru). Dipakai buat 'bikin paket
        baru' DAN 'ubah isi/komposisi paket X' lewat chat. Tiap slot bisa
        punya slot["kecuali"] = list nama rasa yang dikecualikan (cuma
        relevan kalau slot["rasa"] kosong/None -- bebas pilih)."""
        ws = self._get_or_create_paket_bundling_ws()
        rows = ws.get_all_values()
        nama_target = nama.strip().lower()
        header = rows[0] if rows else self._PAKET_BUNDLING_HEADER
        keep_rows = [header] + [
            row for row in rows[1:] if not row or str(row[0]).strip().lower() != nama_target
        ]
        new_rows = [
            [
                nama,
                s["kategori"],
                s.get("rasa") or "",
                s["qty"],
                harga,
                "TRUE" if aktif else "FALSE",
                ", ".join(s.get("kecuali") or []),
            ]
            for s in slots
        ]
        ws.clear()
        ws.update(values=keep_rows + new_rows, range_name="A1")

    def set_bundle_active(self, nama: str, aktif: bool) -> bool:
        """Return True kalau nama paketnya ketemu & keupdate, False kalau
        nggak ketemu sama sekali (caller kasih tau admin nama-nya salah)."""
        ws = self._get_or_create_paket_bundling_ws()
        rows = ws.get_all_values()
        nama_target = nama.strip().lower()
        found = False
        for idx, row in enumerate(rows[1:], start=2):
            if row and str(row[0]).strip().lower() == nama_target:
                ws.update(values=[["TRUE" if aktif else "FALSE"]], range_name=f"F{idx}")
                found = True
        return found

    def set_bundle_harga(self, nama: str, harga: int) -> bool:
        ws = self._get_or_create_paket_bundling_ws()
        rows = ws.get_all_values()
        nama_target = nama.strip().lower()
        found = False
        for idx, row in enumerate(rows[1:], start=2):
            if row and str(row[0]).strip().lower() == nama_target:
                ws.update(values=[[harga]], range_name=f"E{idx}")
                found = True
        return found

    def delete_bundle(self, nama: str) -> bool:
        """Hapus PERMANEN semua baris punya paket ini -- beda sama
        set_bundle_active(nama, False) yang cuma nonaktifin sementara
        (definisinya tetep ada, bisa dinyalain lagi kapan aja)."""
        ws = self._get_or_create_paket_bundling_ws()
        rows = ws.get_all_values()
        nama_target = nama.strip().lower()
        header = rows[0] if rows else self._PAKET_BUNDLING_HEADER
        keep_rows = [header]
        found = False
        for row in rows[1:]:
            if row and str(row[0]).strip().lower() == nama_target:
                found = True
                continue
            keep_rows.append(row)
        if found:
            ws.clear()
            ws.update(values=keep_rows, range_name="A1")
        return found

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

    # ---------- TAMBAH PRODUK BARU (via chat admin, fitur "produk_baru") ----------

    def get_existing_categories(self):
        """Return list kategori unik yang UDAH ADA di PriceList (urut abjad).
        Dipakai buat dikasih sebagai konteks ke AI pas nge-parsing 'produk
        baru' -- biar AI bisa nyocokin kategori yang disebut admin ke yang
        udah ada (kalau memang sama/mirip), bukan asal nganggep semuanya
        kategori baru."""
        return sorted({kategori for (kategori, _rasa) in self.get_price_map().keys()})

    def _row_dict_to_list(self, header_row, values_by_loose_name):
        """Susun list nilai row baru sesuai URUTAN header asli di sheet
        (header_row), dicocokin secara longgar (case/spasi/underscore-
        insensitive) ke values_by_loose_name (dict {nama_field_longgar:
        value}, key-nya harus hasil dari self._loose_key(...)). Kolom yang
        nggak dikenalin (misal ada kolom tambahan lain di sheet yang nggak
        kita isi) dibiarkan string kosong, biar nggak nge-geser/ngerusak
        kolom lain."""
        row = []
        for h in header_row:
            loose = self._loose_key(h)
            row.append(values_by_loose_name.get(loose, ""))
        return row

    def add_or_update_product(self, kategori, rasa, harga_jual, harga_dough=None):
        """Tambahin produk baru (kombinasi Kategori+Rasa) ke tab PriceList,
        ATAU -- kalau kombinasi itu udah ada -- UPDATE harga di baris yang
        sama (supaya nggak ada baris duplikat). Kalau kategorinya BENERAN
        baru (belum ada di tab SupplierDough) DAN harga_dough dikasih
        (nggak None/kosong), baris baru juga ditambahin ke SupplierDough.

        Dipanggil dari bot.py setelah admin nge-confirm preview 'produk
        baru' hasil parsing ai_parser.parse_produk_baru().

        Return dict:
          {
            "aksi": "tambah_baru" atau "update_harga",
            "harga_lama": int atau None (cuma keisi kalau aksi == "update_harga"),
            "kategori_baru": bool (True kalau kategori ini belum ada di SupplierDough sebelumnya),
            "supplier_dough_ditambah": bool,
          }
        """
        kategori = str(kategori).strip()
        rasa = str(rasa).strip()
        harga_jual = int(harga_jual)

        ws_price = self.sheet.worksheet(config.SHEET_PRICELIST)
        values = ws_price.get_all_values()
        header_row = values[0] if values else ["Kategori", "Rasa", "Harga"]

        loose_kategori = self._loose_key("Kategori")
        loose_rasa = self._loose_key("Rasa")
        loose_harga = self._loose_key("Harga")

        col_idx = {}
        for i, h in enumerate(header_row):
            col_idx.setdefault(self._loose_key(h), i)
        kategori_col = col_idx.get(loose_kategori)
        rasa_col = col_idx.get(loose_rasa)
        harga_col = col_idx.get(loose_harga)

        found_row_num = None
        harga_lama = None
        if kategori_col is not None and rasa_col is not None:
            for i, row in enumerate(values[1:], start=2):  # baris 1 = header (gspread 1-indexed)
                row_kategori = row[kategori_col].strip() if kategori_col < len(row) else ""
                row_rasa = row[rasa_col].strip() if rasa_col < len(row) else ""
                if row_kategori.lower() == kategori.lower() and row_rasa.lower() == rasa.lower():
                    found_row_num = i
                    if harga_col is not None and harga_col < len(row):
                        try:
                            harga_lama = int(row[harga_col])
                        except (ValueError, TypeError):
                            harga_lama = None
                    break

        if found_row_num is not None:
            if harga_col is not None:
                ws_price.update_cell(found_row_num, harga_col + 1, harga_jual)
            aksi = "update_harga"
        else:
            new_row = self._row_dict_to_list(header_row, {
                loose_kategori: kategori,
                loose_rasa: rasa,
                loose_harga: harga_jual,
            })
            ws_price.append_row(new_row, value_input_option="USER_ENTERED")
            aksi = "tambah_baru"

        # ---------- SupplierDough (cuma kalau kategorinya beneran baru) ----------
        existing_dough = self.get_dough_price_map()
        existing_dough_lower = {k.strip().lower() for k in existing_dough.keys()}
        kategori_baru = kategori.lower() not in existing_dough_lower

        supplier_dough_ditambah = False
        if kategori_baru and harga_dough not in (None, ""):
            try:
                harga_dough_int = int(harga_dough)
            except (ValueError, TypeError):
                harga_dough_int = None
            if harga_dough_int is not None:
                ws_dough = self.sheet.worksheet(config.SHEET_SUPPLIER_DOUGH)
                dough_values = ws_dough.get_all_values()
                dough_header = dough_values[0] if dough_values else ["Kategori", "Harga_Dough_Per_Unit"]
                new_dough_row = self._row_dict_to_list(dough_header, {
                    self._loose_key("Kategori"): kategori,
                    self._loose_key("Harga_Dough_Per_Unit"): harga_dough_int,
                })
                ws_dough.append_row(new_dough_row, value_input_option="USER_ENTERED")
                supplier_dough_ditambah = True

        return {
            "aksi": aksi,
            "harga_lama": harga_lama,
            "kategori_baru": kategori_baru,
            "supplier_dough_ditambah": supplier_dough_ditambah,
        }


# Cache koneksi biar nggak "kenalan ulang" ke Google tiap kali dipanggil
# (proses autentikasi itu yang bikin lambat kalau diulang terus).
_cached_client = None


def get_sheets_client() -> "SheetsClient":
    global _cached_client
    if _cached_client is None:
        _cached_client = SheetsClient()
    return _cached_client
