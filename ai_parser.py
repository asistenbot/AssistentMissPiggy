"""
Pakai Claude buat ubah chat customer yang berantakan jadi data order terstruktur.
"""

import json
import anthropic

import config

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
  "catatan": "hal lain yang perlu diperhatikan admin, atau null",
  "kelengkapan": "lengkap" atau "kurang_lengkap"
}

Kalau ada informasi penting yang tidak disebutkan customer (nama, alamat kalau kirim,
no hp, atau item pesanan kosong), set "kelengkapan" jadi "kurang_lengkap" dan sebutkan
apa yang kurang di field "catatan".
"""

EDIT_SYSTEM_PROMPT = """Kamu adalah asisten admin toko roti "Miss Piggy".
Customer punya order yang SUDAH ADA, dan sekarang admin mau UBAH order itu
(nambah item, ngurangin qty, hapus item, atau ganti item).

Kategori produk yang valid: Roti, Roti Gandum, Donat, Roti Tawar, Bun Polos, Roti Tawar Loaf.

Tugasmu: hitung ulang dan hasilkan DAFTAR ITEM FINAL (versi lengkap SETELAH
perubahan diterapkan) -- bukan cuma daftar perubahannya doang.

Balas HANYA dengan JSON valid, tanpa teks lain, tanpa markdown code fence:

{
  "items": [{"kategori": "...", "rasa": "...", "qty": 0}],
  "catatan": "ringkasan perubahan yang dilakukan, singkat dan jelas"
}

Kalau instruksinya "hapus X" atau qty item di-set jadi 0, JANGAN masukkan item itu
ke daftar final. Item yang tidak disebut sama sekali dalam instruksi TETAP dipertahankan
qty aslinya (jangan dihapus kalau tidak diminta).
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
