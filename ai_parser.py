"""
Parsing chat/foto order customer (dan input PO ke supplier) jadi data
terstruktur, pakai Claude. Hasil parse-nya SELALU ditunjukin ke admin dulu
buat dikonfirmasi sebelum disimpen ke Sheets -- supaya kalau AI salah baca,
gampang dikoreksi (lihat handle_confirm / handle_pending_correction di
bot.py).
"""

import base64
import json
import re

import anthropic

import config

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _extract_json(text):
    """Claude kadang bungkus JSON dengan kalimat lain / code fence -- ambil
    blok {...} pertama yang valid."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


def _catalog_context(price_list):
    lines = []
    for row in price_list:
        lines.append(
            f"- {row.get('Item_Code')} | {row.get('Nama')} | {row.get('Deskripsi')} | "
            f"kategori {row.get('Kategori')} | satuan {row.get('Satuan')} | "
            f"harga jual Rp{row.get('Harga_Jual')}"
        )
    return "\n".join(lines)


ORDER_SYSTEM_PROMPT = """Kamu adalah asisten admin toko plastik "{business_name}".
Tugas kamu: baca chat/foto order dari customer (biasanya berantakan, bahasa
santai/nyingkat) dan ubah jadi data terstruktur JSON.

KATALOG PRODUK YANG VALID (HARUS dipakai buat cocokin item_code -- JANGAN
pernah mengarang item_code atau harga yang gak ada di katalog ini):
{catalog}

CUSTOMER YANG SUDAH PERNAH ORDER (buat bantu cocokin nama, boleh juga nama
baru kalau memang customer baru):
{customers}

Balikin HANYA JSON dengan struktur persis seperti ini, tanpa teks lain:
{{
  "nama_customer": "nama customer, judul huruf besar tiap kata",
  "no_hp": "nomor HP kalau disebut, kalau tidak ada string kosong",
  "alamat": "alamat kalau disebut, kalau tidak ada string kosong",
  "metode": "Kirim" atau "Ambil" (tebak dari konteks, default \"Kirim\" kalau gak jelas),
  "items": [
    {{"item_code": "KODE_DARI_KATALOG", "nama_item": "nama sesuai katalog", "qty": angka}}
  ],
  "ongkir": 0,
  "catatan": "catatan buat admin kalau ada yang ambigu/gak yakin, kalau tidak ada string kosong",
  "perlu_konfirmasi_manual": false
}}

Aturan penting:
- qty HARUS angka (number), bukan string.
- Kalau ada item yang disebut tapi TIDAK ketemu di katalog / ambigu bisa lebih
  dari 1 kandidat, tetap masukin item itu dengan item_code kosong ("") dan
  nama_item = apa yang disebut customer, terus set perlu_konfirmasi_manual
  jadi true dan jelasin di "catatan".
- Ongkir default 0 kecuali disebutin jelas nominalnya.
- Jangan hitung subtotal/total, itu dihitung sistem lain.
"""


def parse_order_text(text, price_list, customer_names):
    prompt = ORDER_SYSTEM_PROMPT.format(
        business_name=config.BUSINESS_NAME,
        catalog=_catalog_context(price_list),
        customers=", ".join(customer_names) if customer_names else "(belum ada)",
    )
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2000,
        system=prompt,
        messages=[{"role": "user", "content": f"Chat order dari customer:\n\n{text}"}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)


def parse_order_image(image_bytes, media_type, price_list, customer_names):
    prompt = ORDER_SYSTEM_PROMPT.format(
        business_name=config.BUSINESS_NAME,
        catalog=_catalog_context(price_list),
        customers=", ".join(customer_names) if customer_names else "(belum ada)",
    )
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2000,
        system=prompt,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {
                    "type": "text",
                    "text": "Ini foto/screenshot order dari customer. Baca isinya dan ubah jadi JSON sesuai instruksi.",
                },
            ],
        }],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)


PO_SYSTEM_PROMPT = """Kamu adalah asisten admin toko plastik "{business_name}"
yang lagi bikin Purchase Order (PO) BELANJA BAHAN ke supplier (bukan order
dari customer). Baca teks dari admin dan ubah jadi JSON.

SUPPLIER YANG SUDAH PERNAH DIPAKAI:
{suppliers}

Balikin HANYA JSON dengan struktur persis seperti ini, tanpa teks lain:
{{
  "nama_supplier": "nama supplier, judul huruf besar tiap kata",
  "items": [
    {{"nama_item": "nama barang yang dibeli", "qty": angka, "satuan": "KG/Piece/dll", "harga_satuan": angka harga beli per satuan}}
  ],
  "catatan": "catatan kalau ada yang ambigu, kalau tidak ada string kosong"
}}

Aturan: qty dan harga_satuan HARUS angka. Kalau harga gak disebutin, isi 0
dan jelasin di catatan supaya admin isi manual.
"""


def parse_po_text(text, supplier_names):
    prompt = PO_SYSTEM_PROMPT.format(
        business_name=config.BUSINESS_NAME,
        suppliers=", ".join(supplier_names) if supplier_names else "(belum ada)",
    )
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1500,
        system=prompt,
        messages=[{"role": "user", "content": text}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)


INTENT_SYSTEM_PROMPT = """Kamu router pesan buat bot admin toko plastik
"{business_name}". Setiap pesan teks bebas yang diketik admin (BUKAN
command yang diawali "/") harus kamu klasifikasikan mau ngapain, supaya
admin gak perlu apal command kaku -- boleh nanya sesantai apapun.

Balikin HANYA JSON persis struktur ini, tanpa teks lain:
{{
  "intent": salah satu dari daftar di bawah,
  "target": "nama customer/supplier atau nomor invoice yang disebut, string kosong kalau gak ada",
  "bulan": "format YYYY-MM kalau ada bulan/tahun disebut (mis. 'bulan lalu', 'Juli 2026'), string kosong kalau gak disebut -> berarti bulan berjalan",
  "jumlah": angka nominal uang yang disebut (buat bayar utang), 0 kalau gak ada
}}

Daftar intent yang valid:
- "order" -- ini order/pesanan dari CUSTOMER (beli barang dari kita)
- "po" -- ini niat BELANJA/PO ke SUPPLIER SEKARANG (kita yang mau beli bahan,
  nyebut barang + QTY yang mau dibeli), biasanya ada kata "PO", "belanja ke",
  "order ke supplier", "stok dari <nama supplier> <qty barang>". Kalau CUMA
  ngasih tau/masukin daftar harga dari supplier TANPA qty barang yang mau
  dibeli, itu bukan "po" -- itu "update_harga" (lihat di bawah).
- "report_piutang" -- nanya piutang / tagihan customer yang belum dibayar
- "report_utang" -- nanya utang ke supplier yang belum dibayar
- "report_kas_bulanan" -- nanya laporan kas / laba rugi / untung rugi bulanan
- "report_pricelist" -- nanya daftar harga produk
- "mark_lunas" -- bilang customer tertentu udah bayar / lunas
- "bayar_utang" -- bilang udah bayar/cicil ke supplier tertentu
- "lihat_invoice" -- minta liat/cetak ulang invoice customer tertentu
- "lihat_suratjalan" -- minta liat/cetak ulang surat jalan customer tertentu
- "edit_order" -- admin mau NGOREKSI/BETULIN data order/invoice yang SUDAH
  kesimpen (misal salah ketik nama customer, salah alamat, salah no HP),
  BUKAN bikin order baru. Ciri-cirinya: diawali kata "edit", "ganti",
  "betulin", "koreksi", "salah tadi", dsb, dan TIDAK nyebutin barang/qty
  yang dibeli sama sekali. Kalau pesannya cuma nyebut 1-2 kata nama orang
  atau tempat tanpa daftar barang (misal cuma "edit Grandia Hotel"), itu
  edit_order, BUKAN order.
- "batal_order" -- admin mau BATALIN/HAPUS SELURUH order/invoice yang SUDAH
  kesimpen (misal order test yang mau dibuang, atau customer batal jadi
  beli), BUKAN betulin satu-dua data yang salah ketik (itu edit_order).
  Ciri-cirinya: kata "hapus", "batalin", "cancel", "gak jadi", diikuti
  referensi ke order/invoice/customer tertentu, TANPA nyebut field
  spesifik apa yang mau diganti isinya.
- "update_harga" -- admin mau UBAH HARGA JUAL dan/atau HARGA BELI produk di
  katalog/PriceList (bukan order dari customer, bukan PO/belanja ke
  supplier). Ciri-cirinya: nyebut nama/kode barang + harga/angka rupiah,
  pakai kata kayak "harga", "naikin", "turunin", "sekarang", "ganti harga",
  "update harga", "masukin harga/price list", dan TIDAK nyebut QTY barang
  yang lagi mau DIBELI/dipesan sekarang. PENTING: nyebut nama SUPPLIER itu
  BOLEH dan TETEP update_harga selama cuma sebagai SUMBER/ASAL data harga
  (misal "harga dari supplier X, tolong masukin", "update harga beli dari
  price list Y", foto daftar harga dengan caption nyebut nama tokonya) --
  yang bikin ini JADI "po" adalah kalau ada QTY barang yang mau dibeli
  sekarang (lihat penjelasan "po" di atas). Contoh update_harga: "harga
  tulip naik jadi 17000", "PP bening 40x60 sekarang 30rb", "update harga
  TUL-01 jadi 16500", "harga dari supplier CSB 087853077492, tolong
  masukin, harga beli yg di kolom include", foto price list dengan caption
  "daftar harga supplier baru, masukin ya".
- "lainnya" -- basa-basi / gak jelas maksudnya / gak masuk kategori manapun

Kalau ragu antara "order" dan intent lain, PILIH "order" (lebih aman salah
nanya balik daripada order customer keskip) -- KECUALI kalau pesannya
diawali kata edit/ganti/betulin/koreksi dan gak nyebut barang (itu
edit_order), diawali hapus/batalin/cancel tanpa nyebut field spesifik
(itu batal_order), atau nyebut barang/daftar harga + kata "masukin"/
"update"/"harga" TANPA qty barang yang mau dibeli sekarang (itu
update_harga, WALAUPUN ada nama supplier disebut sebagai sumber datanya).
Kalau pesan cuma sapaan atau gak jelas sama sekali, pilih "lainnya".
"""


def classify_intent(text):
    prompt = INTENT_SYSTEM_PROMPT.format(business_name=config.BUSINESS_NAME)
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=300,
        system=prompt,
        messages=[{"role": "user", "content": text}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)


EDIT_ORDER_SYSTEM_PROMPT = """Kamu asisten admin toko plastik "{business_name}".
Admin barusan mau NGOREKSI/BETULIN salah satu data di order yang SUDAH
kesimpen (bukan bikin order baru, bukan batalin/hapus order -- kalau
instruksinya "hapus"/"batalin"/"cancel" TANPA nyebut field yang mau
diganti, itu BUKAN urusan kamu, biarin semua field kosong). Data order
yang mau dikoreksi saat ini:

{current}

Baca instruksi koreksi dari admin, terus balikin HANYA JSON persis struktur
ini, tanpa teks lain:
{{
  "nama_customer": "nilai baru kalau nama customer mau diganti, string kosong kalau TIDAK diganti",
  "no_hp": "nilai baru kalau no HP mau diganti, string kosong kalau TIDAK diganti",
  "alamat": "nilai baru kalau alamat mau diganti, string kosong kalau TIDAK diganti",
  "metode": "'Kirim' atau 'Ambil' kalau metode mau diganti, string kosong kalau TIDAK diganti",
  "items": [
    {{
      "item_code": "kode item (dari daftar ITEM DI ORDER INI di atas) yang mau diganti",
      "qty_baru": angka qty baru, 0 kalau qty item ini TIDAK diganti,
      "harga_satuan_baru": angka harga satuan BARU khusus buat order ini aja, 0 kalau TIDAK diganti
    }}
  ]
}}

Aturan penting:
- Kalau instruksinya cuma nyebut satu-dua kata nama/tempat tanpa penjelasan
  lain (misal admin cuma ngetik "edit Grandia Hotel"), itu HAMPIR PASTI
  maksudnya mau ganti NAMA CUSTOMER jadi nama itu -- bukan field lain.
- JANGAN mengarang perubahan buat field yang gak disebut sama sekali,
  biarin string kosong / array kosong.
- KHUSUS nama_customer: kalau nilai baru yang dimaksud admin SAMA PERSIS
  (case-insensitive) dengan nama customer yang udah kesimpen sekarang,
  berarti gak ada yang perlu diubah -- biarin nama_customer string kosong,
  JANGAN balikin nilai yang sama sebagai "perubahan".
- "items" cuma diisi kalau admin EKSPLISIT minta ganti QTY dan/atau HARGA
  SATUAN salah satu barang yang UDAH ada di order ini (misal "qty jadi
  25kg", "yang tulip jadi 10 pack aja", "harganya jadi 18000",
  "plastik sampah harganya di update jadi 16000"). Kalau order cuma punya
  1 macam barang dan admin nyebut qty/harga baru tanpa nama barang, itu
  barang itu yang dimaksud. JANGAN nambah barang baru atau hapus barang
  yang gak disebut. "harga_satuan_baru" di sini CUMA ngubah harga di order
  INI SAJA (buat invoice-nya), BUKAN ngubah harga jual permanen di
  katalog/PriceList (itu urusan lain, di luar tugas kamu).
- PENTING: kalau admin nyebut "harganya di update" / "harganya berubah"
  TAPI GAK NYEBUT ANGKA BARUNYA SAMA SEKALI, JANGAN NEBAK angkanya --
  biarin harga_satuan_baru 0 (gak diganti) buat item itu, biar admin
  diminta nyebutin angkanya secara eksplisit.
"""


def parse_order_correction(instruction_text, current_order_desc):
    prompt = EDIT_ORDER_SYSTEM_PROMPT.format(
        business_name=config.BUSINESS_NAME,
        current=current_order_desc,
    )
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=300,
        system=prompt,
        messages=[{"role": "user", "content": instruction_text}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)


PRICE_UPDATE_SYSTEM_PROMPT = """Kamu asisten admin toko plastik "{business_name}".
Admin mau UPDATE HARGA JUAL dan/atau HARGA BELI satu atau beberapa produk di
katalog (PriceList) -- BUKAN order dari customer, BUKAN PO ke supplier.

KATALOG PRODUK SAAT INI:
{catalog}

Baca instruksi dari admin, balikin HANYA JSON persis struktur ini, tanpa
teks lain:
{{
  "items": [
    {{
      "item_code": "KODE_DARI_KATALOG kalau ketemu jelas, string kosong kalau item gak ketemu/ambigu",
      "nama_disebut": "nama barang persis seperti disebut admin",
      "harga_jual": angka harga jual BARU, 0 kalau harga jual TIDAK disebut/diubah,
      "harga_beli": angka harga beli BARU, 0 kalau harga beli TIDAK disebut/diubah
    }}
  ]
}}

Aturan:
- Kalau admin cuma nyebut satu angka harga tanpa bilang itu harga
  beli/modal/dari supplier, anggap itu HARGA JUAL (harga ke customer).
- Boleh lebih dari 1 item dalam 1 pesan (misal admin bilang "tulip sama
  sampah naik semua 2000").
- Kalau nama barang yang disebut gak ketemu jelas di katalog (atau
  ambigu, bisa lebih dari 1 kandidat), tetap masukin ke items dengan
  item_code kosong ("") biar admin dikasih tau gak ketemu -- jangan
  mengarang item_code.
"""


def parse_price_update(text, price_list):
    prompt = PRICE_UPDATE_SYSTEM_PROMPT.format(
        business_name=config.BUSINESS_NAME,
        catalog=_catalog_context(price_list),
    )
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1000,
        system=prompt,
        messages=[{"role": "user", "content": text}],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)


PRICE_UPDATE_IMAGE_SYSTEM_PROMPT = """Kamu asisten admin toko plastik "{business_name}".
Admin barusan kirim FOTO daftar harga dari SUPPLIER (bukan order dari
customer, bukan daftar harga jual kita sendiri). Tugas kamu: baca foto ini
baris per baris, cocokin tiap barang ke katalog produk kita, terus tentuin
HARGA BELI (harga modal, dari supplier ke kita) yang baru buat tiap barang.
Kalau di foto ada nama perusahaan/toko supplier-nya (biasanya di bagian
atas/kop surat foto), catat juga nama itu.

KATALOG PRODUK KITA SAAT INI:
{catalog}

Balikin HANYA JSON persis struktur ini, tanpa teks lain:
{{
  "nama_supplier": "nama perusahaan/toko supplier yang tertulis di foto (kop/header), string kosong kalau gak keliatan jelas",
  "items": [
    {{
      "item_code": "KODE_DARI_KATALOG kalau ketemu jelas, string kosong kalau item gak ketemu/ambigu",
      "nama_disebut": "nama barang persis seperti tertulis di foto",
      "harga_jual": 0,
      "harga_beli": angka harga beli/modal yang tertulis di foto buat barang ini
    }}
  ]
}}

Aturan:
- Harga yang tertulis di foto daftar harga supplier ini SELALU dianggap
  HARGA BELI (harga_beli) -- harga_jual SELALU 0 di sini, JANGAN diisi,
  itu urusan admin nentuin sendiri nanti.
- Baca SEMUA baris/item yang kebaca di foto, jangan cuma yang pertama.
- Cocokin tiap baris ke item_code yang paling sesuai di katalog kita
  (berdasarkan nama/ukuran/kategori). Kalau ada barang di foto yang gak
  ketemu jelas di katalog kita (atau ambigu), tetap masukin ke items
  dengan item_code kosong ("") biar admin dikasih tau -- jangan mengarang
  item_code.
- JANGAN mengarang nama_supplier kalau emang gak keliatan jelas di foto,
  biarin string kosong.
"""


def parse_price_update_image(image_bytes, media_type, price_list):
    prompt = PRICE_UPDATE_IMAGE_SYSTEM_PROMPT.format(
        business_name=config.BUSINESS_NAME,
        catalog=_catalog_context(price_list),
    )
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    client = _get_client()
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1500,
        system=prompt,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                },
                {
                    "type": "text",
                    "text": "Ini foto daftar harga dari supplier. Baca isinya dan ubah jadi JSON sesuai instruksi.",
                },
            ],
        }],
    )
    raw = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(raw)
