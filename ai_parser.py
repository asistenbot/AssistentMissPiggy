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

Balas HANYA dengan JSON valid, tanpa teks lain, tanpa markdown code fence, dengan struktur:

{
  "nama": "nama customer atau null kalau tidak disebut",
  "no_hp": "nomor hp atau null",
  "alamat": "alamat atau null",
  "metode": "Kirim" atau "Ambil" atau null kalau tidak jelas,
  "items": [
    {"kategori": "...", "rasa": "...", "qty": 0}
  ],
  "ongkir": angka ongkir dalam rupiah kalau admin menyebutkannya (misal "ongkir 15rb" jadi 15000), atau null kalau tidak disebutkan,
  "catatan": "hal lain yang perlu diperhatikan admin, atau null",
  "kelengkapan": "lengkap" atau "kurang_lengkap"
}

Kalau ada informasi penting yang tidak disebutkan customer (nama, alamat kalau kirim,
no hp, atau item pesanan kosong), set "kelengkapan" jadi "kurang_lengkap" dan sebutkan
apa yang kurang di field "catatan". Ongkir yang belum disebutkan TIDAK menghalangi
"kelengkapan" jadi "lengkap" -- ongkir boleh diisi belakangan.
"""

PARSE_CATALOG_INSTRUCTION = """

Ini daftar produk yang BENERAN ADA di toko (format Kategori: daftar rasa):
{catalog_text}

ATURAN PENTING soal mencocokkan item pesanan ke daftar di atas:
1. Cocokkan nama yang disebut customer ke rasa yang PERSIS ada di daftar (boleh
   toleransi typo/ejaan kecil, misal "meses" cocok ke "Meises").
2. Kalau nama yang disebut customer BISA COCOK ke lebih dari satu produk di
   kategori BERBEDA (misal "coklat" ada sebagai rasa di kategori Roti DAN Roti
   Gandum yang harganya beda, atau "meses" mirip "Mocha Meises" di Roti TAPI
   juga mirip "Meises" di Donat) -- JANGAN ASAL TEBAK. Tetap masukkan ke items
   dengan tebakan yang paling masuk akal dari konteks, TAPI set "kelengkapan"
   jadi "kurang_lengkap" dan di "catatan" sebutkan jelas: item mana yang ambigu
   dan pilihan-pilihan kategorinya apa aja, biar admin bisa konfirmasi ulang.
3. Kalau nama yang disebut customer TIDAK ADA sama sekali di daftar produk
   (misal nyebut "Donat Coklat" padahal yang ada cuma "Donat Coklat Celup"),
   tetap masukkan tebakan yang paling mendekati, TAPI set "kelengkapan" jadi
   "kurang_lengkap" dan jelaskan di "catatan" bahwa nama itu tidak ada persis
   di daftar dan apa kemungkinan yang dimaksud.
4. Field "kategori" dan "rasa" di output HARUS ditulis PERSIS sama seperti di
   daftar produk (termasuk kapitalisasi), bukan hasil tebakan bebas.
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


def _empty_parse_result(catatan):
    return {
        "nama": None, "no_hp": None, "alamat": None, "metode": None,
        "items": [], "catatan": catatan, "kelengkapan": "kurang_lengkap",
    }


EDIT_SYSTEM_PROMPT_BASE = """Kamu adalah asisten admin toko roti "Miss Piggy".
Customer punya order yang SUDAH ADA, dan sekarang admin mau UBAH order itu
(nambah item, ngurangin qty, hapus item, ganti item, atau ubah ongkir).

Tugasmu: hitung ulang dan hasilkan DAFTAR ITEM FINAL (versi lengkap SETELAH
perubahan diterapkan) -- bukan cuma daftar perubahannya doang.

Balas HANYA dengan JSON valid, tanpa teks lain, tanpa markdown code fence:

{
  "items": [{"kategori": "...", "rasa": "...", "qty": 0}],
  "ongkir": angka ongkir baru dalam rupiah KALAU admin menyebutkan mau ubah ongkir, atau null kalau ongkir tidak disinggung sama sekali (biar dipertahankan nilai lama),
  "catatan": "ringkasan perubahan yang dilakukan, singkat dan jelas -- kalau ada item yang namanya ambigu (cocok ke lebih dari satu produk beda kategori/harga), sebutkan jelas pilihannya di sini"
}

Kalau instruksinya "hapus X" atau qty item di-set jadi 0, JANGAN masukkan item itu
ke daftar final. Item yang tidak disebut sama sekali dalam instruksi TETAP dipertahankan
qty aslinya (jangan dihapus kalau tidak diminta).
"""

EDIT_SYSTEM_PROMPT = EDIT_SYSTEM_PROMPT_BASE

INTENT_SYSTEM_PROMPT = """Kamu adalah router perintah untuk bot admin toko roti "Miss Piggy".
Hari ini tanggal: {today}.

Baca pesan dari ADMIN (bukan dari customer), tentukan MAKSUD admin, balas HANYA
JSON valid tanpa teks lain, tanpa markdown code fence:

{{
  "intent": salah satu dari "rekap_produksi", "laporan_bulanan", "pricelist", "edit_order", "invoice", "surat_jalan", "order_baru",
  "nama_customer": "nama customer yang disebut (kalau ada), atau null",
  "bulan": "format YYYY-MM kalau admin sebut bulan tertentu buat laporan bulanan, atau null kalau tidak disebut / minta bulan ini",
  "instruksi_edit": "kalau intent-nya edit_order, tulis ulang instruksi perubahannya (item apa ditambah/dikurangi/dihapus dan jumlahnya), atau null"
}}

Panduan milih intent:
- "minta rekap produksi", "rekap dong", "mau liat rekap", "udah berapa pesanan masuk" -> rekap_produksi
- "laporan bulanan", "rekap bulanan", "mau tau total bulan ini", "berapa yang harus dibayar ke supplier" -> laporan_bulanan
- "harga berapa", "price list", "liat catalog/katalog", "kirim daftar harga" -> pricelist
- Kalau nyebut nama customer TERTENTU dan maksudnya ubah pesanan yang SUDAH ADA
  (kata kunci: tambah, nambah, kurang, kurangin, hapus, ganti, ubah, edit, jadi) -> edit_order
- "invoice buat X", "minta invoice X", "invoice-nya X mana" -> invoice (isi nama_customer)
- "surat jalan X", "suratjalan buat X" -> surat_jalan (isi nama_customer)
- Kalau pesan itu isinya DATA PESANAN BARU (nama, alamat, item pesanan dari customer
  yang baru mau order, biasanya di-copy-paste dari chat customer) -> order_baru
- Kalau nggak jelas / cuma basa-basi / ambigu -> order_baru (paling aman, tetap
  diproses dan admin bisa lihat hasilnya)
"""


def parse_customer_chat(raw_text: str, catalog: list = None) -> dict:
    """
    catalog = list of (kategori, rasa) yang beneran ada di PriceList, opsional.
    Kalau dikasih, AI bakal cocokin item pesanan ke produk asli & nandain
    kalau ada yang ambigu -- jauh lebih akurat daripada nebak generik.
    """
    system_prompt = _prepare_catalog_prompt(PARSE_SYSTEM_PROMPT_BASE, catalog)

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception as e:
        return _empty_parse_result(f"Gagal hubungi AI: {e}. Coba kirim ulang.")

    text = response.content[0].text.strip()
    # Jaga-jaga kalau model tetap kasih code fence
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _empty_parse_result("Gagal parsing otomatis, isi manual ya.")


def parse_customer_chat_image(image_bytes: bytes, media_type: str = "image/jpeg",
                               caption: str = None, catalog: list = None) -> dict:
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
    """
    system_prompt = _prepare_catalog_prompt(PARSE_SYSTEM_PROMPT_BASE, catalog)

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
            max_tokens=1200,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        return _empty_parse_result(f"Gagal hubungi AI: {e}. Coba kirim ulang.")

    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return _empty_parse_result("Gagal baca gambar otomatis, isi manual ya.")


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
            max_tokens=1200,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        return {"items": existing_items, "catatan": f"Gagal hubungi AI: {e}. Coba lagi."}

    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(text)
        if "items" not in result:
            result["items"] = existing_items
        return result
    except json.JSONDecodeError:
        return {"items": existing_items, "catatan": "Gagal parsing perubahan, coba lagi dengan kalimat lebih jelas."}


def classify_intent(raw_text: str) -> dict:
    """
    Tebak maksud admin dari kalimat bebas, biar nggak wajib pakai command '/'.
    Return dict dengan key: intent, nama_customer, bulan, instruksi_edit.
    Kalau gagal/nggak yakin, default ke 'order_baru' (paling aman).
    """
    default = {"intent": "order_baru", "nama_customer": None, "bulan": None, "instruksi_edit": None}

    try:
        tz = date_helpers.get_timezone()
        today_str = datetime.datetime.now(tz).strftime("%Y-%m-%d")
        system_prompt = INTENT_SYSTEM_PROMPT.format(today=today_str)

        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=300,
            system=system_prompt,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception:
        return default

    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return default

    for key, val in default.items():
        result.setdefault(key, val)
    return result
