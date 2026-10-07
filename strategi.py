"""
Ahli Strategi Miss Piggy (agent kecil).

Tiap minggu (atau kapan pun lewat /strategi) dia:
1. Baca ringkasan data toko dari Sheets (penjualan per PO, produk naik/turun,
   margin per kategori, pelanggan aktif/baru/lama nggak order).
2. Cari sendiri tren di internet (Claude + web search, maks 5 pencarian):
   roti/kue yang lagi ramai, ide promo, ide konten.
3. Nulis laporan singkat: ringkasan, ide promo, ide konten, menu yang layak
   dicoba, dan 1 prioritas minggu ini.

Sengaja manggil API Anthropic langsung pakai httpx (bukan SDK) biar fitur
web search nggak bergantung ke versi SDK yang dipakai bagian bot lain --
jadi modul ini nggak bisa bikin alur order ikut error.
"""

import datetime
import logging
import os

import httpx

import config
import date_helpers
import documents

logger = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
MAKS_PENCARIAN = int(os.getenv("STRATEGI_MAKS_CARI", "5"))

SYSTEM_PROMPT = """Kamu Ahli Strategi untuk Miss Piggy, home bakery kecil di Bandung.

Fakta bisnis (WAJIB dipegang):
- Jualan sistem PO mingguan: PO dibuka Jumat, ditutup Rabu, dikirim/diambil Kamis.
- Produk: roti, roti gandum, donat, roti tawar. Miss Piggy terbuka sebagai bakery NON-HALAL; beberapa rasa mengandung babi (Pork). Jangan pernah menyarankan klaim halal.
- Dough dibeli dari supplier (harga dough per kategori ada di data), lalu diisi/dipanggang sendiri. Kapasitas dapur kecil, jadi ide harus realistis untuk usaha rumahan.
- Pemilik baru membangun akun TikTok dan IG dari nol; order masuk lewat WhatsApp dan web.

Tugasmu:
1. Baca DATA TOKO yang diberikan. Jangan mengarang angka; kalau data kurang, bilang.
2. Pakai web search untuk mencari: roti/kue/pastry yang lagi tren atau viral di Indonesia (utamakan beberapa minggu terakhir), ide promo bakery rumahan, dan format konten TikTok/IG yang lagi ramai untuk makanan. Maksimal beberapa pencarian, pilih yang paling berguna.
3. Tulis laporan dalam bahasa Indonesia santai tapi jelas, TEKS POLOS (tanpa markdown, tanpa tanda bintang/pagar), maksimal sekitar 3000 karakter, dengan bagian persis ini:

RINGKASAN MINGGU INI
(2-4 poin dari data: apa yang naik, turun, margin terbaik/terburuk, kondisi pelanggan)

IDE PROMO (3)
(tiap ide: apa, kenapa cocok berdasarkan data, perkiraan dampak ke margin)

IDE KONTEN (3)
(untuk TikTok/IG: konsep singkat + hook kalimat pembuka)

MENU YANG LAYAK DICOBA (1-2)
(dari tren yang kamu temukan; jelaskan kenapa cocok, bahan/dough yang bisa dipakai dari kategori yang ada, saran harga jual kalau bisa diperkirakan dari harga dough, dan sebutkan sumbernya)

PRIORITAS MINGGU INI
(1 hal paling penting yang sebaiknya dikerjakan)

Gunakan tanda strip (-) untuk poin. Ide adalah saran, keputusan tetap di pemilik.
Jangan menulis kalimat pengantar sebelum/selama mencari; laporan langsung dimulai dengan RINGKASAN MINGGU INI."""


# ---------- ringkasan data toko ----------

def _ringkas_data(sheets, minggu_acuan: str) -> str:
    per_minggu = sheets.get_orders_per_minggu(minggu_acuan, 6)
    dough = sheets.get_dough_price_map()
    try:
        menu = sheets.get_pricelist_text().replace("*", "")
    except Exception:
        menu = "(gagal baca PriceList)"

    baris = ["DATA TOKO (dari Google Sheets)", ""]
    if not per_minggu:
        baris.append("Belum ada data order.")
        return "\n".join(baris)

    baris.append("Penjualan per PO (omzet produk tanpa ongkir, untung kotor = omzet - biaya dough):")
    for minggu, orders in per_minggu:
        h = documents.hitung_untung(orders, dough)
        baris.append(f"- PO {minggu}: {h['jumlah_customer']} pelanggan, omzet {documents.rupiah(h['omzet'])}, "
                     f"untung kotor {documents.rupiah(h['untung'])}")

    # per kategori & per rasa: 2 PO terakhir vs 4 PO sebelumnya
    def qty_per(orders, kunci):
        hasil = {}
        for o in orders:
            k = str(o.get(kunci, "")).strip() or "-"
            if kunci == "Rasa":
                k = f"{o.get('Kategori', '')} {k}".strip()
            hasil[k] = hasil.get(k, 0) + documents._angka(o.get("Qty"))
        return hasil

    baru = [o for _, os_ in per_minggu[-2:] for o in os_]
    lama = [o for _, os_ in per_minggu[:-2] for o in os_]
    n_baru, n_lama = max(1, len(per_minggu[-2:])), max(1, len(per_minggu[:-2]))
    rasa_baru, rasa_lama = qty_per(baru, "Rasa"), qty_per(lama, "Rasa")
    perubahan = []
    for k in set(rasa_baru) | set(rasa_lama):
        rb, rl = rasa_baru.get(k, 0) / n_baru, rasa_lama.get(k, 0) / n_lama
        perubahan.append((k, rb, rl))
    perubahan.sort(key=lambda x: x[1] - x[2], reverse=True)
    baris.append("")
    baris.append("Rata-rata qty per PO, 2 PO terakhir vs PO sebelumnya (paling naik di atas):")
    for k, rb, rl in perubahan[:6]:
        baris.append(f"- {k}: {rb:.0f} vs {rl:.0f}")
    if len(perubahan) > 6:
        baris.append("Paling turun:")
        for k, rb, rl in perubahan[-4:]:
            baris.append(f"- {k}: {rb:.0f} vs {rl:.0f}")

    semua = [o for _, os_ in per_minggu for o in os_]
    h_semua = documents.hitung_untung(semua, dough)
    baris.append("")
    baris.append("Margin per kategori (6 PO terakhir):")
    for kat, k in sorted(h_semua["per_kategori"].items(), key=lambda kv: kv[1]["omzet"], reverse=True):
        untung = k["omzet"] - k["biaya"]
        persen = round(untung * 100 / k["omzet"]) if k["omzet"] else 0
        baris.append(f"- {kat}: {k['qty']} pcs, omzet {documents.rupiah(k['omzet'])}, margin {persen}%")
    baris.append("Harga dough per unit: " + ", ".join(f"{k} {documents.rupiah(v)}" for k, v in dough.items()))

    try:
        semua_order = sheets.get_all_orders_dgn_minggu()
        pel = documents.ringkas_pelanggan(semua_order)
        po_ini = datetime.datetime.strptime(minggu_acuan, "%Y-%m-%d").date()
        batas = (po_ini - datetime.timedelta(days=21)).strftime("%Y-%m-%d")
        lama_n = sum(1 for p in pel if p["po_terakhir"] < batas)
        sekali = sum(1 for p in pel if p["jumlah_po"] == 1)
        baris.append("")
        baris.append(f"Pelanggan: total {len(pel)}, baru order sekali {sekali}, "
                     f"nggak order 3+ minggu {lama_n}.")
    except Exception:
        pass

    baris.append("")
    baris.append("Menu & harga saat ini:" + menu)
    return "\n".join(baris)


# ---------- panggil Claude + web search ----------

def _panggil_claude(isi_user: str) -> dict:
    headers = {
        "x-api-key": config.ANTHROPIC_API_KEY or "",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    tools = [{
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": MAKS_PENCARIAN,
        "user_location": {"type": "approximate", "city": "Bandung", "country": "ID",
                          "timezone": "Asia/Jakarta"},
    }]
    messages = [{"role": "user", "content": isi_user}]
    teks, sumber, jumlah_cari = [], {}, 0
    with httpx.Client(timeout=180.0) as client:
        for _ in range(4):  # lanjutkan kalau server minta 'pause_turn'
            r = client.post(API_URL, headers=headers, json={
                "model": config.CLAUDE_MODEL,
                "max_tokens": 3500,
                "system": SYSTEM_PROMPT,
                "messages": messages,
                "tools": tools,
            })
            if r.status_code != 200:
                return {"error": f"AI menolak permintaan ({r.status_code}): {r.text[:200]}"}
            data = r.json()
            for blok in data.get("content", []):
                if blok.get("type") == "text":
                    teks.append(blok.get("text", ""))
                    for c in blok.get("citations") or []:
                        if c.get("url"):
                            sumber[c["url"]] = c.get("title") or c["url"]
                elif blok.get("type") == "server_tool_use":
                    jumlah_cari += 1
            if data.get("stop_reason") == "pause_turn":
                messages = [messages[0], {"role": "assistant", "content": data["content"]}]
                continue
            break
    hasil = "".join(teks).strip()
    awal = hasil.find("RINGKASAN MINGGU INI")
    if awal > 0:  # buang kalimat pengantar seperti "saya cari dulu..."
        hasil = hasil[awal:]
    if not hasil:
        return {"error": "AI nggak ngasih jawaban. Coba /strategi lagi."}
    return {"teks": hasil, "sumber": sumber, "jumlah_cari": jumlah_cari}


def buat_laporan(sheets, catatan: str = "") -> dict:
    """Return {'teks': ...} siap kirim ke Telegram, atau {'error': ...}."""
    if not config.ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY belum di-setting."}
    minggu = date_helpers.current_po_week_thursday()
    # Tren dihitung dari PO yang SUDAH selesai (PO berjalan biasanya belum lengkap)
    selesai = (datetime.datetime.strptime(minggu, "%Y-%m-%d") - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    data = _ringkas_data(sheets, selesai)
    now = datetime.datetime.now(date_helpers.get_timezone())
    bulan = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
             "September", "Oktober", "November", "Desember"][now.month - 1]
    tgl = f"{now.day} {bulan} {now.year}"
    isi = (f"Hari ini {tgl}. PO berjalan (belum selesai): kirim Kamis {minggu}. "
           f"Data di bawah sampai PO {selesai} yang sudah selesai.\n\n{data}")
    if catatan:
        isi += f"\n\nCATATAN/PERTANYAAN PEMILIK (prioritaskan):\n{catatan}"
    hasil = _panggil_claude(isi)
    if "error" in hasil:
        return hasil
    teks = f"🧠 LAPORAN AHLI STRATEGI — {tgl}\n\n{hasil['teks']}"
    if hasil["sumber"]:
        teks += "\n\nSumber yang dibaca:\n" + "\n".join(
            f"- {judul[:70]}: {url}" for url, judul in list(hasil["sumber"].items())[:6]
        )
    return {"teks": teks}


def _ambil_ide_konten(teks):
    """Potong bagian IDE KONTEN (+ MENU YANG LAYAK DICOBA buat konteks)."""
    awal = teks.find("IDE KONTEN")
    if awal < 0:
        return teks[:2500]
    akhir = teks.find("PRIORITAS MINGGU INI", awal)
    return teks[awal:akhir if akhir > 0 else None][:2500]


async def kirim_laporan(bot, chat_id, sheets, thread_id=None, catatan="", buat_konten=True):
    """Bikin + kirim laporan. Return pesan error atau None. Nggak pernah raise."""
    import asyncio
    try:
        import kantor
        kantor.catat("strategi", "Riset tren & susun strategi minggu ini")
    except Exception:
        pass
    try:
        hasil = await asyncio.wait_for(asyncio.to_thread(buat_laporan, sheets, catatan), timeout=300)
    except asyncio.TimeoutError:
        return "Ahli Strategi kelamaan mikir (timeout). Coba /strategi lagi nanti."
    except Exception as e:
        logger.exception("Gagal bikin laporan strategi")
        return f"Ahli Strategi gagal: {e}"
    if "error" in hasil:
        return hasil["error"]
    teks = hasil["teks"]
    for i in range(0, len(teks), 4000):
        await bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                               text=teks[i:i + 4000], disable_web_page_preview=True)

    if buat_konten:
        # Oper ide konten ke Marketing -> carousel siap posting di grup Konten.
        try:
            import konten
            await bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                   text="🎠 Ide kontennya sudah dioper ke Marketing. Carousel siap posting "
                                        "akan dikirim ke grup Konten (beberapa menit).")
            error = await konten.kirim_konten(bot, sheets, _ambil_ide_konten(teks))
            if error:
                await bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                       text=f"⚠️ Marketing gagal bikin carousel: {error}")
        except Exception as e:
            logger.exception("Gagal oper ide ke Marketing")
            await bot.send_message(chat_id=chat_id, message_thread_id=thread_id,
                                   text=f"⚠️ Marketing gagal bikin carousel: {e}")
    return None
