"""
Pakai Claude buat ubah chat customer yang berantakan jadi data order terstruktur.
"""

import json
import base64
import datetime
import anthropic

import config
import date_helpers

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=30.0)

PARSE_SYSTEM_PROMPT_BASE = """Kamu adalah asisten admin toko roti "Miss Piggy".
Tugasmu HANYA satu: ubah chat customer (yang sering berantakan, tidak lengkap,
atau dicampur basa-basi) menjadi data order terstruktur dalam format JSON.

Balas HANYA dengan JSON valid, TANPA teks lain apapun -- tanpa penjelasan,
tanpa perhitungan yang ditulis keluar, tanpa markdown code fence. Kalau perlu
menghitung sesuatu (misal perkalian box, lihat aturan di bawah), lakukan
perhitungan itu di dalam kepalamu saja dan langsung tulis HASIL AKHIRNYA ke
field yang sesuai -- JANGAN tulis proses hitungnya sebagai teks di luar JSON.

Struktur JSON:
{
  "nama": "nama customer atau null kalau tidak disebut",
  "no_hp": "nomor hp atau null",
  "alamat": "alamat atau null",
  "metode": "Kirim" atau "Ambil" atau null kalau tidak jelas,
  "items_non_box": [
    {"kategori": "...", "rasa": "...", "qty": 0}
  ],
  "box_groups": [
    {"jumlah_box": 0, "items": [{"kategori": "...", "rasa": "...", "qty_per_box": 0}]}
  ],
  "ongkir": angka ongkir dalam rupiah kalau admin menyebutkannya (misal "ongkir 15rb" jadi 15000), atau null kalau tidak disebutkan,
  "peringatan_ai": "peringatan OTOMATIS dari kamu buat admin kalau ada yang perlu dicek (info kurang, nama rasa ambigu, dst), atau null kalau semua jelas -- PENTING: field ini BUKAN catatan packing dari customer, JANGAN pernah diisi permintaan/instruksi packing customer di sini (kalau customer minta packing khusus, itu masuk 'catatan' di bawah, bukan sini)",
  "catatan": "instruksi/permintaan packing yang BENERAN disebut customer sendiri (misal 'donat sama gula dipisah', 'jangan dibungkus plastik'), atau null kalau customer nggak minta apa-apa soal packing -- field ini nanti kesimpen ke Sheets & DICETAK di surat jalan buat kurir/packing, jadi JANGAN isi kesimpulan/analisis kamu sendiri di sini, HANYA permintaan packing yang eksplisit disebut customer",
  "kelengkapan": "lengkap" atau "kurang_lengkap",
  "paket_bundling_nama": "nama paket PERSIS sama seperti di daftar paket bundling AKTIF di bawah (kalau ada), diisi HANYA kalau customer JELAS-JELAS minta salah satu paket itu -- LIHAT ATURAN PAKET BUNDLING di bawah buat cara isi items_non_box-nya. null kalau customer nggak minta paket bundling apapun."
}

Kalau ada informasi penting yang tidak disebutkan customer (nama, alamat kalau kirim,
no hp, atau item pesanan kosong), set "kelengkapan" jadi "kurang_lengkap" dan sebutkan
apa yang kurang di field "peringatan_ai" (BUKAN "catatan" -- "catatan" khusus permintaan
packing dari customer). Ongkir yang belum disebutkan TIDAK menghalangi
"kelengkapan" jadi "lengkap" -- ongkir boleh diisi belakangan.

ATURAN KHUSUS SATUAN "BOX": kadang pesanan ditulis pakai satuan "box"/"dus"/
"paket"/"pax"/"bungkus" (semua ini sinonim, artinya sama) -- ada angka jumlah
box duluan, lalu daftar rasa dengan qty PER BOX.

PENTING BANGET -- JANGAN NGITUNG/NGALIKAN/NJUMLAHIN APAPUN SENDIRI. Tugasmu
CUMA laporin data MENTAH apa adanya ke 2 field terpisah, biar perkalian &
penjumlahannya dihitung SISTEM (bukan kamu) -- ini SENGAJA biar nggak ada
salah hitung:

1. "items_non_box" -- item yang ditulis TANPA keterangan box/dus/paket/pax/
   bungkus sama sekali (qty-nya udah final apa adanya, nggak perlu dikali).
   Contoh: "roti coklat 5" (tanpa box) -> masuk sini apa adanya: qty 5.

2. "box_groups" -- SEMUA kelompok yang pakai satuan box, ditulis APA ADANYA
   PERSIS kayak yang disebutkan customer, SATU per SATU per kelompok. qty_per_box
   itu angka ASLI per box (JANGAN dikalikan jumlah box, JANGAN dijumlahkan
   lintas kelompok, JANGAN diapa-apain -- tulis mentah aja). Kalau ada 3
   kelompok box yang beda, hasilnya 3 entry terpisah di "box_groups", titik.
   Kalau order ini sama sekali tidak pakai satuan box, "box_groups" harus
   berupa array kosong [] (bukan null).

Field "rasa" di "items_non_box" MAUPUN di dalam "box_groups" WAJIB SATU nama
produk yang valid dari daftar (lihat aturan pencocokan produk di bawah),
TIDAK BOLEH gabungan/kombinasi beberapa nama (misal JANGAN tulis "Baso
(Pork) (Ayam)"). Kalau ambigu, pilih SATU tebakan paling masuk akal dan
PAKAI NAMA YANG SAMA itu di semua tempat item itu muncul (baik di
items_non_box maupun di semua box_groups yang menyebutnya) -- jangan
improvisasi nama beda-beda di tempat berbeda buat item yang sama.

Contoh: kalau customer bilang "22 box isi baso ayam 1, piscok 1, ham cheese
3" dan "3 box isi charsiu 2, baso ayam 2" dan juga tambahan "roti coklat 5"
(tanpa box), maka:
- "items_non_box": [{"kategori":"Roti","rasa":"Coklat","qty":5}]
- "box_groups": [
    {"jumlah_box": 22, "items": [{"kategori":"Roti","rasa":"Baso ( Ayam )","qty_per_box":1}, {"kategori":"Roti","rasa":"Piscok","qty_per_box":1}, {"kategori":"Roti","rasa":"Ham Cheese","qty_per_box":3}]},
    {"jumlah_box": 3, "items": [{"kategori":"Roti","rasa":"Charsiu","qty_per_box":2}, {"kategori":"Roti","rasa":"Baso ( Ayam )","qty_per_box":2}]}
  ]
(Sistem yang bakal ngitung otomatis: baso ayam total 22+6=28, piscok 22, ham
cheese 66, charsiu 6, coklat 5 -- kamu TIDAK perlu ngitung ini sama sekali.)
"""

PARSE_CATALOG_INSTRUCTION = """

Ini daftar produk yang BENERAN ADA di toko (format Kategori: daftar rasa):
{catalog_text}

ATURAN PENTING soal mencocokkan item pesanan ke daftar di atas:
1. Cocokkan nama yang disebut customer ke rasa yang PERSIS ada di daftar (boleh
   toleransi typo/ejaan kecil, misal "meses" cocok ke "Meises"). Ini juga
   berlaku buat SINGKATAN umum, misal "piscok" cocok ke "Pisang Coklat" dan
   "pisju"/"pisket" cocok ke "Pisang Keju" -- kategorinya (Roti atau Roti
   Gandum) tetap ditentuin dari konteks sama kayak biasa (lihat aturan 2 di
   bawah kalau nggak jelas kategorinya yang mana).
2. CEK DULU daftar "ATURAN TETAP" di bagian bawah (kalau ada) -- itu aturan
   default yang FIX buat sebutan tertentu (misal "Baso" polos tanpa
   keterangan Ayam/Pork), jadi kalau sebutannya cocok ke salah satu ATURAN
   TETAP, langsung pakai itu, JANGAN dianggap ambigu dan JANGAN minta
   konfirmasi lagi.
   Kalau nama yang disebut customer BISA COCOK ke lebih dari satu produk DAN
   TIDAK ada di ATURAN TETAP -- entah itu di kategori BERBEDA (misal "coklat"
   ada sebagai rasa di kategori Roti DAN Roti Gandum yang harganya beda),
   ATAU beberapa VARIAN dalam kategori yang SAMA -- JANGAN ASAL TEBAK dan
   JANGAN PERNAH menggabungkan nama beberapa pilihan jadi satu string aneh
   (misal JANGAN tulis "Nama (VarianA) (VarianB)" atau sejenisnya -- itu
   BUKAN nama produk yang valid dan tidak akan cocok ke manapun di sistem).
   Field "rasa" WAJIB selalu berisi PERSIS SATU nama yang ada di daftar
   produk, tidak boleh gabungan. Kalau ambigu: pilih SATU kandidat yang
   paling masuk akal dari konteks sebagai tebakan, TAPI set "kelengkapan"
   jadi "kurang_lengkap" dan di "peringatan_ai" (BUKAN "catatan") sebutkan
   jelas: item mana yang ambigu dan pilihan-pilihan yang ada apa aja, biar
   admin bisa konfirmasi ulang ke customer.
3. Kalau nama yang disebut customer TIDAK ADA sama sekali di daftar produk
   (misal nyebut "Donat Coklat" padahal yang ada cuma "Donat Coklat Celup"),
   tetap masukkan tebakan yang paling mendekati (SATU nama valid dari daftar,
   bukan gabungan), TAPI set "kelengkapan" jadi "kurang_lengkap" dan jelaskan
   di "peringatan_ai" (BUKAN "catatan") bahwa nama itu tidak ada persis di
   daftar dan apa kemungkinan yang dimaksud.
4. Field "kategori" dan "rasa" di output HARUS ditulis PERSIS sama seperti di
   daftar produk (termasuk kapitalisasi), bukan hasil tebakan bebas -- ini
   berlaku juga untuk "kategori"/"rasa" di dalam "box_groups".
{alias_text}"""


def _build_alias_text():
    aliases = getattr(config, "PRODUCT_ALIASES", None)
    if not aliases:
        return ""
    lines = [
        "\nATURAN TETAP (PRIORITAS PALING TINGGI, bukan kasus ambigu -- JANGAN "
        "tandai kurang_lengkap atau minta konfirmasi buat kasus-kasus di bawah "
        "ini, langsung terapkan):"
    ]
    for alias in aliases:
        lines.append(
            f'- Kalau customer bilang "{alias["sebutan"]}", itu PASTI maksudnya '
            f'kategori "{alias["kategori"]}" rasa "{alias["rasa"]}". Langsung '
            f'pakai ini tanpa ragu.'
        )
    return "\n".join(lines)

PARSE_SYSTEM_PROMPT = PARSE_SYSTEM_PROMPT_BASE

# Instruksi tambahan KHUSUS dipasang di ATAS system prompt yang sama pas
# input-nya GAMBAR (screenshot), bukan teks -- biar Claude tau harus "baca"
# gambarnya dulu (OCR + pahami konteks chat-nya), baru diproses persis kayak
# alur teks biasa (JSON output-nya format-nya SAMA PERSIS, nggak diubah).
PARSE_IMAGE_PREFIX = """PENTING: input dari user kali ini berupa GAMBAR SCREENSHOT
(bukan teks langsung) -- biasanya screenshot chat WhatsApp customer yang
di-forward admin. Baca semua teks yang ada di gambar itu (nama, alamat, no HP,
item pesanan, dst), lalu proses PERSIS sama kayak instruksi di bawah ini biar
hasilnya konsisten sama alur order dari teks biasa.

"""


def _prepare_catalog_prompt(system_prompt, catalog):
    if not catalog:
        return system_prompt
    by_kategori = {}
    for kategori, rasa in catalog:
        by_kategori.setdefault(kategori, []).append(rasa)
    catalog_lines = [f"{k}: {', '.join(v)}" for k, v in by_kategori.items()]
    catalog_text = "\n".join(catalog_lines)
    return system_prompt + PARSE_CATALOG_INSTRUCTION.format(catalog_text=catalog_text, alias_text=_build_alias_text())


def _build_bundling_rules_text(active_bundles):
    """Bangun blok instruksi paket bundling secara DINAMIS dari daftar paket
    yang lagi AKTIF di Sheets (liat sheets_client.get_all_bundles(only_active=True))
    -- BUKAN di-hardcode lagi kayak versi lama (dulu cuma 1 paket "Bundling
    Spesial"). Admin bisa nambah/ubah/nonaktifin paket kapan aja lewat chat
    ke bot, jadi prompt AI ini WAJIB ikut paket yang lagi aktif SEKARANG,
    bukan definisi lama yang ke-hardcode di kode.

    Dipakai lewat concatenation biasa (BUKAN str.format() ke seluruh system
    prompt) soalnya system prompt penuh contoh JSON yang isinya kurung
    kurawal -- format() ke situ bakal error/kacau. Fungsi ini return teks
    biasa yang tinggal ditempel (+) di akhir prompt."""
    if not active_bundles:
        return (
            "\n\nNggak ada paket bundling yang lagi aktif sekarang -- JANGAN "
            'PERNAH isi "paket_bundling_nama" (selalu null), proses semua '
            "request bundling/paket dari customer sebagai order item biasa "
            "apa adanya (item-nya kemungkinan besar nggak bakal ketemu exact "
            "match di PriceList kalau memang bukan produk beneran -- tandain "
            'kurang_lengkap + jelasin di "peringatan_ai" kalau begitu).'
        )
    lines = [
        "\n\nATURAN PAKET BUNDLING -- ini daftar paket yang lagi AKTIF sekarang. "
        'Kalau customer JELAS minta salah satu, set "paket_bundling_nama" PERSIS '
        'nama paketnya (sama persis termasuk kapitalisasi), dan isi '
        '"items_non_box" SESUAI SLOT paket itu (JANGAN pakai box_groups buat ini):'
    ]
    for b in active_bundles:
        harga_text = "Rp" + format(int(b["harga"]), ",").replace(",", ".")
        slot_descs = []
        for s in b.get("slots", []):
            if s.get("rasa"):
                slot_descs.append(
                    f'TEPAT {s["qty"]} pcs kategori "{s["kategori"]}" rasa "{s["rasa"]}" '
                    f"(item tetap, WAJIB persis segini, jangan sampai lupa/ketinggalan)"
                )
            else:
                slot_descs.append(
                    f'{s["qty"]} pcs kategori "{s["kategori"]}" (PERSIS kategori ini, '
                    f"boleh campur rasa apa aja dalam kategori itu sesuai request customer)"
                )
        lines.append(f'- "{b["nama"]}" = flat {harga_text}. Isinya: ' + "; ".join(slot_descs) + ".")
    lines.append(
        "Kalau customer nggak nyebut rincian rasa buat slot yang bebas pilih "
        'rasa ("bebas"/"campur"/nggak nyebut sama sekali), JANGAN ngarang/nebak '
        "rincian rasanya sendiri -- isi 1 baris qty PENUH slot itu dengan rasa "
        'yang paling umum/polos di kategori itu, DAN set "kelengkapan": '
        '"kurang_lengkap" + jelaskan di "peringatan_ai" bahwa rincian rasa '
        "paket ini belum diisi customer, admin perlu konfirmasi/ubah manual.\n"
        "JANGAN tambahin item lain di luar slot-slot paket yang diminta itu "
        "buat order yang sama (kalau customer nyebut item TAMBAHAN di luar "
        'paketnya, berarti bukan paket murni -- set "paket_bundling_nama": '
        "null, proses semua itemnya apa adanya kayak order biasa, JANGAN "
        "dipaksa jadi paket).\n"
        "Total harga paket FLAT (BUKAN dijumlah dari harga satuan PriceList) "
        "-- sistem yang ngitung/nge-set harganya sendiri belakangan, kamu "
        "TIDAK perlu (dan JANGAN) mikirin harga sama sekali buat kasus ini."
    )
    return "\n".join(lines)


def _empty_parse_result(pesan_error):
    """pesan_error (misal 'Gagal hubungi AI: ...') masuk ke 'peringatan_ai'
    (info buat admin doang), BUKAN ke 'catatan' -- 'catatan' HARUS selalu
    kosong di sini soalnya belum ada apa-apa yang berhasil diparse sama
    sekali, jangan sampai pesan error nyasar kesimpen/keprint ke surat jalan
    seolah-olah itu permintaan packing dari customer."""
    return {
        "nama": None, "no_hp": None, "alamat": None, "metode": None,
        "items": [], "box_groups": [], "catatan": None,
        "peringatan_ai": pesan_error, "kelengkapan": "kurang_lengkap",
        "paket_bundling_nama": None,
    }


def _compute_final_items(items_non_box, box_groups):
    """Hitung field "items" FINAL (qty per box dikali jumlah box, digabung
    lintas kelompok) di sini, PAKAI PYTHON -- BUKAN diserahkan ke AI kayak
    sebelumnya. Ini yang bikin qty selalu akurat 100%, soalnya perkalian &
    penjumlahan biasa nggak pernah salah kalau dihitung kode, beda sama AI
    yang kadang keliru pas harus mikirin banyak hal sekaligus (pernah
    kejadian: 3 kelompok box, salah satu rasa muncul di 3 kelompok, hasil
    akhirnya AI keliru jumlahin -- padahal breakdown per kelompoknya sendiri
    udah bener di penjelasan dia).

    items_non_box = [{"kategori":.., "rasa":.., "qty":..}, ...] (qty final apa adanya)
    box_groups = [{"jumlah_box":.., "items":[{"kategori":.., "rasa":.., "qty_per_box":..}]}, ...]

    Return: list [{"kategori":.., "rasa":.., "qty":..}, ...] siap dipakai
    sama alur yang udah ada (preview, simpan ke Sheets, dst)."""
    totals = {}  # {(kategori, rasa): qty}

    for it in (items_non_box or []):
        kategori = it.get("kategori")
        rasa = it.get("rasa")
        if not kategori or not rasa:
            continue
        try:
            qty = int(it.get("qty") or 0)
        except (ValueError, TypeError):
            qty = 0
        key = (kategori, rasa)
        totals[key] = totals.get(key, 0) + qty

    for grp in (box_groups or []):
        try:
            jumlah_box = int(grp.get("jumlah_box") or 0)
        except (ValueError, TypeError):
            jumlah_box = 0
        for it in grp.get("items", []):
            kategori = it.get("kategori")
            rasa = it.get("rasa")
            if not kategori or not rasa:
                continue
            try:
                qty_per_box = int(it.get("qty_per_box") or 0)
            except (ValueError, TypeError):
                qty_per_box = 0
            key = (kategori, rasa)
            totals[key] = totals.get(key, 0) + (qty_per_box * jumlah_box)

    return [
        {"kategori": kategori, "rasa": rasa, "qty": qty}
        for (kategori, rasa), qty in totals.items()
    ]


def _safe_json_loads(raw_text):
    """Parse JSON dari balasan AI dengan toleransi ekstra. Kadang model
    (apalagi kalau instruksinya minta dia "mikir" dulu, misal ngitung box)
    tetap nyempilin teks di luar JSON walau udah diminta jangan -- daripada
    langsung gagal total, coba ekstrak blok JSON-nya aja (dari '{' pertama
    sampai '}' terakhir) sebelum menyerah. Return None kalau tetap gagal."""
    text = raw_text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


EDIT_SYSTEM_PROMPT_BASE = """Kamu adalah asisten admin toko roti "Miss Piggy".
Customer punya order yang SUDAH ADA, dan sekarang admin mau UBAH order itu
(nambah item, ngurangin qty, hapus item, ganti item, atau ubah ongkir).

Tugasmu: hitung ulang dan hasilkan DAFTAR ITEM FINAL (versi lengkap SETELAH
perubahan diterapkan) -- bukan cuma daftar perubahannya doang.

Balas HANYA dengan JSON valid, TANPA teks lain apapun -- tanpa penjelasan,
tanpa perhitungan yang ditulis keluar, tanpa markdown code fence:

{
  "items": [{"kategori": "...", "rasa": "...", "qty": 0}],
  "ongkir": angka ongkir baru dalam rupiah KALAU admin menyebutkan mau ubah ongkir, atau null kalau ongkir tidak disinggung sama sekali (biar dipertahankan nilai lama),
  "catatan": "ringkasan perubahan yang dilakukan, singkat dan jelas -- kalau ada item yang namanya ambigu (cocok ke lebih dari satu produk beda kategori/harga), sebutkan jelas pilihannya di sini"
}

Kalau instruksinya "hapus X" atau qty item di-set jadi 0, JANGAN masukkan item itu
ke daftar final. Item yang tidak disebut sama sekali dalam instruksi TETAP dipertahankan
qty aslinya (jangan dihapus kalau tidak diminta).

ATURAN KHUSUS SATUAN "BOX": kalau instruksi menyebutkan pola "X box/pax/bungkus
isi rasa qty, rasa qty" (jumlah box duluan, qty PER BOX -- "box"/"dus"/"paket"/
"pax"/"bungkus" semua sinonim), kalikan qty tiap rasa dengan jumlah box-nya
(hitung diam-diam, jangan ditulis prosesnya) sebelum ditambahkan/digabungkan
ke daftar item final. Kalau jumlah box cuma 1, tidak perlu dikali. Kalau ada
beberapa kelompok box berbeda, hitung tiap kelompok sendiri-sendiri lalu
gabungkan rasa yang sama.

PENTING soal nama rasa: CEK DULU daftar "ATURAN TETAP" di bagian bawah
(kalau ada) -- itu aturan default FIX buat sebutan tertentu (misal "Baso"
polos tanpa keterangan Ayam/Pork), jadi kalau sebutannya cocok ke salah
satu ATURAN TETAP, langsung pakai itu, JANGAN dianggap ambigu. Field "rasa"
WAJIB selalu SATU nama produk yang valid (persis sesuai daftar produk),
TIDAK BOLEH digabung jadi satu string aneh kalau ambigu (misal JANGAN tulis
"Nama (VarianA) (VarianB)"). Kalau nama yang disebut bisa berarti lebih
dari satu varian DAN TIDAK ada di ATURAN TETAP, pilih SATU yang paling
masuk akal dan sebutkan di "catatan" bahwa ini ambigu & perlu dikonfirmasi
ke customer.
"""

EDIT_SYSTEM_PROMPT = EDIT_SYSTEM_PROMPT_BASE

INTENT_SYSTEM_PROMPT = """Kamu adalah router perintah untuk bot admin toko roti "Miss Piggy".
Hari ini tanggal: {today}.

Baca pesan dari ADMIN (bukan dari customer), tentukan MAKSUD admin, balas HANYA
JSON valid tanpa teks lain, tanpa markdown code fence:

{{
  "intent": salah satu dari "rekap_produksi", "laporan_bulanan", "pricelist", "produk_baru", "edit_order", "invoice", "surat_jalan", "order_baru",
  "nama_customer": "nama customer yang disebut (kalau ada), atau null",
  "tanggal_mulai_rekap": "format YYYY-MM-DD (hitung dari hari ini {today} kalau istilahnya relatif kayak 'hari ini'/'besok'/'lusa') KALAU intent-nya rekap_produksi DAN admin minta rekap untuk TANGGAL/RENTANG TANGGAL tertentu (misal 'rekap produksi besok', 'rekap produksi hari ini', 'rekap produksi sampe besok', 'rekap produksi hari ini dan besok') -- ini tanggal AWAL rentangnya (atau tanggal tunggal kalau cuma 1 hari). Kalau permintaannya rekap biasa TANPA tanggal spesifik, atau rekap by NAMA CUSTOMER, biarkan null.",
  "tanggal_akhir_rekap": "format YYYY-MM-DD, isi HANYA kalau ada RENTANG tanggal (misal 'sampe besok' dari hari ini berarti tanggal_akhir_rekap = besok; 'hari ini dan besok' juga rentang 2 hari). Kalau cuma 1 hari tunggal, biarkan null (tanggal_mulai_rekap doang yang dipakai).",
  "bulan_mulai": "format YYYY-MM (pakai tahun {today} kalau nggak disebut eksplisit) kalau admin minta laporan bulanan buat 1 bulan tertentu ATAU ini bulan AWAL dari sebuah rentang (misal 'dari Januari sampai Agustus' -> bulan_mulai Januari), atau null kalau nggak disebut sama sekali / minta bulan ini",
  "bulan_akhir": "format YYYY-MM, isi HANYA kalau admin eksplisit minta RENTANG beberapa bulan (misal 'Januari sampai Agustus', 'Jan - Agustus', 'dari bulan 1 ke bulan 8') -- isi bulan AKHIR rentangnya. Kalau cuma minta 1 bulan doang (bukan rentang), biarkan null.",
  "bulan_invoice": "format YYYY-MM, isi HANYA kalau intent-nya invoice ATAU surat_jalan DAN admin JELAS minta dokumen customer dari BULAN TERTENTU yang udah lewat (bukan minggu aktif sekarang) -- dua pola yang dianggap JELAS: (1) ada kata 'bulan' diikuti nama/angka bulan (misal 'invoice apple bulan agustus'), ATAU (2) referensi customer-nya LEBIH DARI 1 KATA dan kata TERAKHIR-nya persis nama bulan (misal 'invoice apple agustus' -> nama_customer cuma 'apple', bulan_invoice bulan Agustus). Kalau tahun nggak disebut, pakai tahun {today}. JANGAN isi ini kalau referensi customer-nya CUMA 1 KATA doang yang kebetulan mirip nama bulan (misal 'surat jalan juni' TETAP nama_customer 'Juni' TANPA bulan_invoice -- liat aturan di bawah, itu kemungkinan besar nama orang beneran, bukan permintaan bulan). null kalau nggak relevan / minta minggu aktif seperti biasa.",
  "instruksi_edit": "kalau intent-nya edit_order, tulis ulang instruksi perubahannya (item apa ditambah/dikurangi/dihapus dan jumlahnya), atau null"
}}

Panduan milih intent:
- "minta rekap produksi", "rekap dong", "mau liat rekap", "udah berapa pesanan masuk" -> rekap_produksi. Ada 3 variasi:
  1. Kalau ADA tanggal/rentang tanggal spesifik disebut (misal "rekap produksi besok", "rekap produksi sampe besok", "rekap produksi hari ini dan besok", "rekap produksi tanggal 29") -> isi tanggal_mulai_rekap (dan tanggal_akhir_rekap kalau rentang), JANGAN isi nama_customer.
  2. Kalau ADA nama customer tertentu disebut (misal "minta rekap produksi Ci Meyvany") -> isi nama_customer, JANGAN isi tanggal_mulai_rekap.
  3. Kalau nggak disebut tanggal maupun nama -> biarkan nama_customer dan tanggal_mulai_rekap dua-duanya null, rekapnya jadi gabungan minggu aktif seperti biasa.
- "laporan bulanan", "rekap bulanan", "mau tau total bulan ini", "berapa yang harus dibayar ke supplier" -> laporan_bulanan (kalau admin sebut RENTANG bulan, misal "laporan bulanan dari Januari sampai Agustus", "laporan bulanan Jan - Agustus", "minta laporan bulan 1-3", "laporan bulan 1 sampai 3", isi bulan_mulai DAN bulan_akhir sesuai rentangnya -- ANGKA bulan (1=Januari, 2=Februari, dst sampai 12=Desember) harus dikonversi ke nomor bulan yang sama, cuma beda cara nulis; kalau cuma 1 bulan/nggak disebut, cukup isi bulan_mulai. Kalau TAHUN nggak disebut sama sekali (baik nama bulan maupun angka), pakai tahun {today} secara default -- JANGAN nebak tahun lain.)
- "harga berapa", "price list", "liat catalog/katalog", "kirim daftar harga" -> pricelist
- Admin bilang mau NAMBAHIN produk/rasa/kategori BARU ke daftar harga toko
  (BUKAN order customer) -- kata kunci: "produk baru", "tambah produk",
  "nambah produk", "varian baru", "rasa baru", "ada menu baru", "masukin ke
  pricelist", biasanya diikuti nama produk + harga (misal "produk baru
  Dubai Coklat kategori Dubai harga jual 35000 harga dough 20000") ->
  produk_baru. JANGAN isi nama_customer buat intent ini (detail produknya
  diekstrak terpisah, bukan di sini).
- Kalau nyebut nama customer TERTENTU dan maksudnya ubah pesanan yang SUDAH ADA
  (kata kunci: tambah, nambah, kurang, kurangin, hapus, ganti, ubah, edit, jadi) -> edit_order
- "invoice buat X", "minta invoice X", "invoice-nya X mana" -> invoice (isi nama_customer)
- "surat jalan X", "suratjalan buat X" -> surat_jalan (isi nama_customer) -- PENTING: kata yang PERSIS muncul setelah "surat jalan"/"suratjalan"/"invoice" itu HAMPIR SELALU nama customer, WALAUPUN kebetulan sama kayak nama bulan (Januari-Desember) atau kata umum lainnya. Contoh: "surat jalan juni" -> intent surat_jalan, nama_customer "Juni" (BUKAN merujuk ke bulan Juni, itu nama orang). Jangan biarkan kemiripan sama nama bulan bikin nama_customer jadi kosong/null.
  TAPI kalau referensi customer-nya LEBIH DARI 1 KATA dan kata TERAKHIR persis nama bulan (atau ada kata "bulan" eksplisit sebelumnya), itu BUKAN lagi nama customer 1 kata yang ambigu -- kata bulan di akhir itu beneran permintaan BULAN, pisahin: nama_customer cuma bagian namanya doang, sisanya masuk bulan_invoice (lihat definisi field-nya di atas). Contoh: "invoice apple agustus" -> nama_customer "apple", bulan_invoice bulan Agustus tahun {today}. "invoice apple bulan agustus" -> sama persis. Bedain dari "surat jalan juni" (1 kata doang, TETAP dianggap nama orang, bulan_invoice null).
- Nyebut nama customer TERTENTU dan cuma mau NGELIAT/NGECEK pesanan dia
  (bukan ubah), kayak "lihat orderan X", "liat order X", "orderan X apa aja",
  "cek pesanan X", "orderannya X mana" -> invoice (isi nama_customer) --
  soalnya invoice udah nampilin rincian lengkap order customer itu (item,
  qty, alamat, metode, total)
- Kalau pesan itu isinya DATA PESANAN BARU (nama, alamat, item pesanan dari customer
  yang baru mau order, biasanya di-copy-paste dari chat customer) -> order_baru
- Kalau nggak jelas / cuma basa-basi / ambigu -> order_baru (paling aman, tetap
  diproses dan admin bisa lihat hasilnya)
"""


def parse_customer_chat(raw_text: str, catalog: list = None, active_bundles: list = None) -> dict:
    """
    catalog = list of (kategori, rasa) yang beneran ada di PriceList, opsional.
    Kalau dikasih, AI bakal cocokin item pesanan ke produk asli & nandain
    kalau ada yang ambigu -- jauh lebih akurat daripada nebak generik.

    active_bundles = hasil sheets_client.get_all_bundles(only_active=True),
    opsional. Kalau dikasih, AI ikut dikasih tau paket bundling apa aja yang
    lagi aktif SEKARANG (bisa lebih dari 1) biar bisa nangkep request
    customer yang minta salah satu paket itu -- liat _build_bundling_rules_text.
    """
    system_prompt = _prepare_catalog_prompt(PARSE_SYSTEM_PROMPT_BASE, catalog)
    system_prompt += _build_bundling_rules_text(active_bundles)

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception as e:
        return _empty_parse_result(f"Gagal hubungi AI: {e}. Coba kirim ulang.")

    result = _safe_json_loads(response.content[0].text)
    if result is None:
        return _empty_parse_result("Gagal parsing otomatis, isi manual ya.")
    result.setdefault("box_groups", [])
    result.setdefault("paket_bundling_nama", None)
    result["items"] = _compute_final_items(result.get("items_non_box"), result.get("box_groups"))
    # "catatan" sekarang KHUSUS permintaan packing yang BENERAN disebut
    # customer di chat-nya (misal "donat sama gula dipisah ya") -- boleh
    # keisi dari parse ini kalau customer emang nyebut sendiri, soalnya itu
    # beneran mau kesimpen ke Sheets & keprint ke surat jalan. "peringatan_ai"
    # itu WADAH TERPISAH buat catetan otomatis AI (info kurang, item
    # ambigu, dst) -- liat prompt di atas, field ini yang sekarang dipakai
    # buat itu, BUKAN "catatan" lagi (dulu sebelum dipisah, 2 hal ini
    # numplek jadi 1 field "catatan" -- itu sebabnya surat jalan sempet
    # ikut nyetak analisis AI kayak "Cream meisses ditafsirkan sebagai
    # Cream Cheese..." padahal itu bukan permintaan packing dari customer).
    result.setdefault("peringatan_ai", None)
    if not (isinstance(result.get("catatan"), str) and result["catatan"].strip()):
        result["catatan"] = None
    return result


def parse_customer_chat_image(image_bytes: bytes, media_type: str = "image/jpeg",
                               caption: str = None, catalog: list = None, active_bundles: list = None) -> dict:
    """
    Sama kayak parse_customer_chat, TAPI input-nya SCREENSHOT (misal admin
    forward/kirim screenshot chat WA customer langsung ke bot, bukan
    copy-paste teksnya). Claude yang "baca" isi gambarnya (nama, alamat, no
    HP, item pesanan, dst), lalu diproses lewat system prompt YANG SAMA
    persis kayak alur teks -- jadi hasil JSON-nya konsisten & bisa langsung
    dipakai di alur preview/konfirmasi yang udah ada, nggak perlu kode
    terpisah di bot.py buat nanganin hasilnya.

    media_type: MIME type gambarnya -- Telegram selalu ngirim foto sebagai
    JPEG (bahkan kalau aslinya PNG/screenshot), jadi "image/jpeg" aman
    dipakai sebagai default.
    caption: teks tambahan yang mungkin ditulis admin BARENG foto-nya (kalau
    ada) -- ikut dikirim ke AI biar info yang kepisah antara gambar & caption
    (misal "ongkir 15rb" ditulis di caption, bukan kelihatan di screenshot)
    nggak ilang.

    active_bundles = sama kayak parameter di parse_customer_chat (paket
    bundling yang lagi aktif sekarang, opsional).
    """
    system_prompt = _prepare_catalog_prompt(PARSE_SYSTEM_PROMPT_BASE, catalog)
    system_prompt += _build_bundling_rules_text(active_bundles)

    instruksi = PARSE_IMAGE_PREFIX
    if caption:
        instruksi += f'Catatan tambahan yang ditulis admin bareng foto ini: "{caption}"\n\n'
    instruksi += "Baca gambar di atas dan ubah jadi data order terstruktur sesuai format yang diminta."

    content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.b64encode(image_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": instruksi},
    ]

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        return _empty_parse_result(f"Gagal hubungi AI: {e}. Coba kirim ulang.")

    result = _safe_json_loads(response.content[0].text)
    if result is None:
        return _empty_parse_result("Gagal baca gambar otomatis, isi manual ya.")
    result.setdefault("box_groups", [])
    result.setdefault("paket_bundling_nama", None)
    result["items"] = _compute_final_items(result.get("items_non_box"), result.get("box_groups"))
    # "catatan" sekarang KHUSUS permintaan packing yang BENERAN disebut
    # customer di chat-nya (misal "donat sama gula dipisah ya") -- boleh
    # keisi dari parse ini kalau customer emang nyebut sendiri, soalnya itu
    # beneran mau kesimpen ke Sheets & keprint ke surat jalan. "peringatan_ai"
    # itu WADAH TERPISAH buat catetan otomatis AI (info kurang, item
    # ambigu, dst) -- liat prompt di atas, field ini yang sekarang dipakai
    # buat itu, BUKAN "catatan" lagi (dulu sebelum dipisah, 2 hal ini
    # numplek jadi 1 field "catatan" -- itu sebabnya surat jalan sempet
    # ikut nyetak analisis AI kayak "Cream meisses ditafsirkan sebagai
    # Cream Cheese..." padahal itu bukan permintaan packing dari customer).
    result.setdefault("peringatan_ai", None)
    if not (isinstance(result.get("catatan"), str) and result["catatan"].strip()):
        result["catatan"] = None
    return result


def parse_order_edit(existing_items: list, instruction: str, catalog: list = None) -> dict:
    """
    existing_items = [{"kategori": str, "rasa": str, "qty": int}, ...]
    instruction = teks bebas dari admin, misal "tambah donat gula 5, ham cheese jadi 20"
    catalog = list of (kategori, rasa) yang beneran ada di PriceList, opsional.

    Return: {"items": [...daftar final...], "catatan": "ringkasan perubahan"}
    """
    existing_text = "\n".join(
        f"- {i['rasa']} ({i['kategori']}) x{i['qty']}" for i in existing_items
    ) or "(kosong)"

    user_message = f"Order yang sudah ada:\n{existing_text}\n\nInstruksi perubahan:\n{instruction}"

    system_prompt = _prepare_catalog_prompt(EDIT_SYSTEM_PROMPT_BASE, catalog)

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        return {"items": existing_items, "catatan": f"Gagal hubungi AI: {e}. Coba lagi."}

    result = _safe_json_loads(response.content[0].text)
    if result is None:
        return {"items": existing_items, "catatan": "Gagal parsing perubahan, coba lagi dengan kalimat lebih jelas."}
    if "items" not in result:
        result["items"] = existing_items
    return result


def classify_intent(raw_text: str) -> dict:
    """
    Tebak maksud admin dari kalimat bebas, biar nggak wajib pakai command '/'.
    Return dict dengan key: intent, nama_customer, bulan, instruksi_edit.
    Kalau gagal/nggak yakin, default ke 'order_baru' (paling aman).
    """
    default = {
        "intent": "order_baru", "nama_customer": None,
        "tanggal_mulai_rekap": None, "tanggal_akhir_rekap": None,
        "bulan_mulai": None, "bulan_akhir": None, "bulan_invoice": None,
        "instruksi_edit": None,
    }

    try:
        tz = date_helpers.get_timezone()
        today_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")
        system_prompt = INTENT_SYSTEM_PROMPT.format(today=today_str)

        response = client.messages.create(
            model=config.CLAUDE_MODEL_FAST,
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception:
        return default

    result = _safe_json_loads(response.content[0].text)
    if result is None:
        return default

    for key, val in default.items():
        result.setdefault(key, val)
    return result


PRODUK_BARU_SYSTEM_PROMPT = """Kamu adalah asisten admin toko roti "Miss Piggy".
Admin mau NAMBAHIN PRODUK BARU (kategori dan/atau rasa baru) ke daftar harga
toko, ditulis lewat kalimat bebas (BUKAN order customer). Tugasmu HANYA:
ekstrak data produk barunya jadi JSON terstruktur.

Balas HANYA dengan JSON valid, TANPA teks lain apapun, tanpa penjelasan,
tanpa markdown code fence:

{{
  "kategori": "nama kategori produknya, PERSIS sama kayak salah satu di daftar kategori yang sudah ada di bawah kalau memang cocok kesitu, atau nama kategori BARU (tulis apa adanya sesuai yang disebut admin) kalau memang belum ada di daftar",
  "rasa": "nama rasa/varian produknya",
  "harga_jual": angka harga jual ke customer dalam rupiah (misal '35rb'/'35ribu' jadi 35000), atau null kalau admin tidak menyebutkan sama sekali,
  "harga_dough": angka harga dough/bahan baku dari supplier dalam rupiah, atau null kalau admin tidak menyebutkan,
  "kelengkapan": "lengkap" atau "kurang_lengkap"
}}

Set "kelengkapan" jadi "kurang_lengkap" KALAU salah satu dari kategori, rasa,
atau harga_jual tidak disebutkan sama sekali oleh admin -- field yang tidak
disebutkan itu diisi null, JANGAN mengarang/menebak nilainya sendiri.
harga_dough SELALU boleh null (opsional, tidak menghalangi "kelengkapan" jadi
"lengkap") -- kalau kategorinya BENERAN baru (tidak ada di daftar kategori
yang sudah ada) dan admin tidak menyebutkan harga_dough, itu TETAP dianggap
"lengkap" (sistem yang akan mengingatkan admin belakangan soal harga dough
buat kategori baru itu, bukan tugasmu di sini).

Kategori yang SUDAH ADA sekarang di toko: {existing_categories}
"""


def parse_produk_baru(raw_text: str, existing_categories: list = None) -> dict:
    """Ekstrak data 'produk baru' (kategori, rasa, harga jual, harga dough)
    dari kalimat bebas admin, misal "produk baru Dubai Coklat kategori
    Dubai harga jual 35000 harga dough 20000" -- dipanggil setelah
    classify_intent() mendeteksi intent == "produk_baru".

    existing_categories: list nama kategori yang udah ada di PriceList
    (dari sheets_client.get_existing_categories()), dikasih sebagai
    konteks ke AI biar dia bisa nyocokin kategori yang disebut admin ke
    yang udah ada (kalau memang sama) alih-alih nganggep semuanya baru.

    Return dict: {"kategori", "rasa", "harga_jual", "harga_dough",
    "kelengkapan", "error"} -- "error" cuma keisi (string) kalau
    beneran gagal hubungi AI/parsing, dipakai bot.py buat nampilin
    pesan gagal ke admin."""
    existing_text = ", ".join(existing_categories) if existing_categories else "(belum ada data kategori)"
    system_prompt = PRODUK_BARU_SYSTEM_PROMPT.format(existing_categories=existing_text)

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception as e:
        return {
            "kategori": None, "rasa": None, "harga_jual": None, "harga_dough": None,
            "kelengkapan": "kurang_lengkap", "error": f"Gagal hubungi AI: {e}. Coba lagi.",
        }

    result = _safe_json_loads(response.content[0].text)
    if result is None:
        return {
            "kategori": None, "rasa": None, "harga_jual": None, "harga_dough": None,
            "kelengkapan": "kurang_lengkap",
            "error": "Gagal parsing otomatis, coba tulis ulang lebih jelas (kategori, rasa, harga jual).",
        }
    result.setdefault("kategori", None)
    result.setdefault("rasa", None)
    result.setdefault("harga_jual", None)
    result.setdefault("harga_dough", None)
    result.setdefault("kelengkapan", "kurang_lengkap")
    result.setdefault("error", None)
    return result


BUNDLE_DEFINISI_SYSTEM_PROMPT = """Kamu adalah asisten admin toko roti "Miss Piggy".
Admin mau BIKIN PAKET BUNDLING BARU atau UBAH KOMPOSISI/HARGA paket bundling
yang udah ada, ditulis lewat kalimat bebas (BUKAN order customer). Tugasmu
HANYA: ekstrak data paketnya jadi JSON terstruktur.

Balas HANYA dengan JSON valid, TANPA teks lain apapun, tanpa penjelasan,
tanpa markdown code fence:

{{
  "nama_paket": "nama paket bundling-nya, tulis apa adanya persis sesuai yang disebut admin (misal 'Paket Lebaran'), atau null kalau admin sama sekali tidak menyebut nama",
  "harga": angka harga TOTAL paket (flat, bukan per pcs) dalam rupiah, misal '200rb'/'200ribu' jadi 200000, atau null kalau tidak disebutkan,
  "slots": [
    {{
      "kategori": "nama kategori produk PERSIS sama kayak salah satu di daftar kategori yang sudah ada di bawah kalau cocok, atau apa adanya kalau memang kategori itu belum ada",
      "rasa": "nama rasa/varian SPESIFIK kalau slot ini WAJIB/FIXED rasa tertentu (misal 'Dubai Coklat'), atau null kalau slot ini bebas pilih rasa apa aja dalam kategori itu (misal admin bilang 'bebas rasa'/'campur'/nggak nyebut rasa)",
      "qty": angka jumlah pcs untuk slot ini
    }}
  ],
  "kelengkapan": "lengkap" atau "kurang_lengkap",
  "peringatan_ai": "penjelasan singkat kalau ada bagian yang kurang jelas/ambigu/kemungkinan salah tangkap, atau null kalau semua jelas"
}}

Set "kelengkapan" jadi "kurang_lengkap" KALAU salah satu dari nama_paket,
harga, atau slots (list-nya kosong / nggak ada satupun slot yang bisa
diekstrak) tidak disebutkan sama sekali oleh admin -- field yang tidak
disebutkan itu diisi null (untuk nama_paket/harga) atau [] (untuk slots),
JANGAN mengarang/menebak nilainya sendiri. Setiap slot WAJIB ada "kategori"
dan "qty" yang jelas -- kalau admin menyebutkan komposisi yang kategorinya
nggak jelas/ambigu, JANGAN dipaksa ekstrak jadi slot, lebih baik skip slot
itu dan jelaskan di "peringatan_ai".

Kategori yang SUDAH ADA sekarang di toko: {existing_categories}
Paket bundling yang SUDAH ADA sekarang (kalau admin maksudnya UBAH salah
satu dari ini, cocokkan "nama_paket" PERSIS sama kayak nama yang sudah
ada ini, jangan bikin nama baru yang mirip-mirip): {existing_bundle_names}
"""


def parse_bundle_definition(raw_text: str, catalog: list = None, existing_bundle_names: list = None) -> dict:
    """Ekstrak data definisi 'paket bundling' (nama, harga flat, komposisi/slot)
    dari kalimat bebas admin, misal "bikin paket baru namanya Paket Lebaran,
    harga 200rb, isinya 6 pcs roti gandum bebas rasa sama 4 pcs donat bebas
    rasa" -- dipanggil setelah classify_intent() (atau deterministic keyword
    match di bot.py) mendeteksi admin lagi mau bikin/ubah paket bundling.

    catalog: list kategori produk (dari sheets_client.get_catalog_list()),
    dipakai buat ekstrak nama kategori yang ada di daftar 'existing_categories'
    biar AI nyocokin nama kategori yang disebut admin ke yang udah ada.
    existing_bundle_names: list nama paket bundling yang udah ada sekarang
    (dari sheets_client.get_all_bundles()), dikasih sebagai konteks biar AI
    bisa nyocokin kalau admin maksudnya UBAH paket yang udah ada, bukan bikin
    paket baru dengan nama yang mirip2 doang.

    Return dict: {"nama_paket", "harga", "slots": [{"kategori","rasa","qty"}],
    "kelengkapan", "peringatan_ai", "error"} -- "error" cuma keisi (string)
    kalau beneran gagal hubungi AI/parsing, dipakai bot.py buat nampilin
    pesan gagal ke admin."""
    if catalog:
        # catalog = list of (kategori, rasa) tuples (liat sheets_client.get_catalog_list),
        # SAMA formatnya kayak yang dipakai parse_customer_chat/_prepare_catalog_prompt --
        # BUKAN list of dict.
        existing_categories = sorted({kategori for kategori, _rasa in catalog if kategori})
    else:
        existing_categories = []
    existing_categories_text = ", ".join(existing_categories) if existing_categories else "(belum ada data kategori)"
    existing_bundle_names_text = ", ".join(existing_bundle_names) if existing_bundle_names else "(belum ada paket bundling)"
    system_prompt = BUNDLE_DEFINISI_SYSTEM_PROMPT.format(
        existing_categories=existing_categories_text,
        existing_bundle_names=existing_bundle_names_text,
    )

    empty_result = {
        "nama_paket": None, "harga": None, "slots": [],
        "kelengkapan": "kurang_lengkap", "peringatan_ai": None,
    }

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=600,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception as e:
        return {**empty_result, "error": f"Gagal hubungi AI: {e}. Coba lagi."}

    result = _safe_json_loads(response.content[0].text)
    if result is None:
        return {
            **empty_result,
            "error": "Gagal parsing otomatis, coba tulis ulang lebih jelas (nama paket, harga, isi paketnya).",
        }
    result.setdefault("nama_paket", None)
    result.setdefault("harga", None)
    result.setdefault("slots", [])
    result.setdefault("kelengkapan", "kurang_lengkap")
    result.setdefault("peringatan_ai", None)
    result.setdefault("error", None)
    return result
