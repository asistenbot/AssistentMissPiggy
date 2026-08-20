"""
Pakai Claude buat ubah chat customer yang berantakan jadi data order terstruktur.
"""

import json
import datetime
import anthropic

import config
import date_helpers

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=30.0)

PARSE_SYSTEM_PROMPT = """Kamu adalah asisten admin toko roti "Miss Piggy".
Tugasmu HANYA satu: ubah chat customer (yang sering berantakan, tidak lengkap,
atau dicampur basa-basi) menjadi data order terstruktur dalam format JSON.

Kategori produk yang valid: Roti, Roti Gandum, Donat, Roti Tawar, Bun Polos, Roti Tawar Loaf.
Kalau customer sebut nama rasa tanpa kategori jelas, tebak kategori paling masuk akal
(misal "coklat" / "keju" biasanya kategori Roti, kecuali disebut donat/gandum/tawar).

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

EDIT_SYSTEM_PROMPT = """Kamu adalah asisten admin toko roti "Miss Piggy".
Customer punya order yang SUDAH ADA, dan sekarang admin mau UBAH order itu
(nambah item, ngurangin qty, hapus item, ganti item, atau ubah ongkir).

Kategori produk yang valid: Roti, Roti Gandum, Donat, Roti Tawar, Bun Polos, Roti Tawar Loaf.

Tugasmu: hitung ulang dan hasilkan DAFTAR ITEM FINAL (versi lengkap SETELAH
perubahan diterapkan) -- bukan cuma daftar perubahannya doang.

Balas HANYA dengan JSON valid, tanpa teks lain, tanpa markdown code fence:

{
  "items": [{"kategori": "...", "rasa": "...", "qty": 0}],
  "ongkir": angka ongkir baru dalam rupiah KALAU admin menyebutkan mau ubah ongkir, atau null kalau ongkir tidak disinggung sama sekali (biar dipertahankan nilai lama),
  "catatan": "ringkasan perubahan yang dilakukan, singkat dan jelas"
}

Kalau instruksinya "hapus X" atau qty item di-set jadi 0, JANGAN masukkan item itu
ke daftar final. Item yang tidak disebut sama sekali dalam instruksi TETAP dipertahankan
qty aslinya (jangan dihapus kalau tidak diminta).
"""

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


def parse_customer_chat(raw_text: str) -> dict:
    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1000,
            system=PARSE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": raw_text}],
        )
    except Exception as e:
        return {
            "nama": None, "no_hp": None, "alamat": None, "metode": None,
            "items": [], "catatan": f"Gagal hubungi AI: {e}. Coba kirim ulang.",
            "kelengkapan": "kurang_lengkap",
        }

    text = response.content[0].text.strip()
    # Jaga-jaga kalau model tetap kasih code fence
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "nama": None, "no_hp": None, "alamat": None, "metode": None,
            "items": [], "catatan": "Gagal parsing otomatis, isi manual ya.",
            "kelengkapan": "kurang_lengkap",
        }


def parse_order_edit(existing_items: list, instruction: str) -> dict:
    """
    existing_items = [{"kategori": str, "rasa": str, "qty": int}, ...]
    instruction = teks bebas dari admin, misal "tambah donat gula 5, ham cheese jadi 20"

    Return: {"items": [...daftar final...], "catatan": "ringkasan perubahan"}
    """
    existing_text = "\n".join(
        f"- {i['rasa']} ({i['kategori']}) x{i['qty']}" for i in existing_items
    ) or "(kosong)"

    user_message = f"Order yang sudah ada:\n{existing_text}\n\nInstruksi perubahan:\n{instruction}"

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1000,
            system=EDIT_SYSTEM_PROMPT,
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
