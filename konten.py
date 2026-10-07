"""
Konten carousel otomatis (Ahli Strategi -> Marketing).

1. KATALOG FOTO: tiap foto di folder Drive dilihat AI SEKALI (Claude Haiku,
   murah) lalu diberi label: produk apa, deskripsi singkat, kualitas 1-5,
   cocok jadi cover atau nggak. Hasilnya disimpan di tab Sheets
   "Katalog Foto", jadi minggu berikutnya cukup baca tab itu; cuma foto baru
   yang dilihat lagi.
2. RENCANA: dari ide konten (laporan Ahli Strategi, atau ide dari admin),
   AI memilih foto dari katalog dan menulis isi tiap slide + caption.
3. RENDER: Marketing bikin slide carousel 4:5 (cover, isi, penutup ajakan
   order) pakai Pillow, lalu dikirim ke grup Konten.
"""

import asyncio
import base64
import datetime
import io
import json
import logging
import os

from PIL import Image, ImageDraw, ImageFilter, ImageOps

import config
import foto_mingguan as fm

logger = logging.getLogger(__name__)

TAB_KATALOG = "Katalog Foto"
HEADER = ["File_ID", "Nama_File", "Label", "Deskripsi", "Kualitas", "Cover", "Diperiksa", "Yakin"]
TAB_PANDUAN = "Panduan Foto"
# Ciri-ciri produk dari pemilik, biar AI nggak salah sebut. Bisa ditambah/
# diubah langsung di tab "Panduan Foto" di Sheets (1 baris = 1 aturan).
PANDUAN_DEFAULT = [
    "Roti bulat dengan pola sobekan/lipatan di atas (seperti bunga) adalah ROTI, BUKAN donat.",
    "DONAT hanya kalau bentuknya cincin dengan lubang di tengah dan digoreng.",
    "Roti PANJANG/lonjong yang isiannya DI DALAM (bagian atas polos/hanya sedikit isian terlihat) = Mocha Meises (isian meses) atau Cream Cheese (isian keju krim).",
    "Roti PANJANG dengan topping meses atau parutan keju PENUH menutupi bagian atas = produk BARU yang belum dijual: label wajib HOLD.",
    "Roti BULAT dengan taburan meses di atas = Roti Coklat (bukan Mocha Meises).",
    "Gorengan lonjong berlapis tepung roti (panir) = Risoles.",
    "Kalau ragu produk apa, isi label 'roti' (umum) dan yakin=false. Jangan menebak nama rasa.",
]
MAKS_PERIKSA_SEKALI = int(os.getenv("KATALOG_MAKS_PERIKSA", "60"))
W, H = fm.UKURAN


# ---------- katalog foto ----------

def _ws_katalog(sheets):
    import gspread
    try:
        return sheets.sheet.worksheet(TAB_KATALOG)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheets.sheet.add_worksheet(title=TAB_KATALOG, rows=500, cols=len(HEADER))
        ws.update(values=[HEADER], range_name="A1:H1")
        return ws


def baca_katalog(sheets):
    ws = _ws_katalog(sheets)
    rows = ws.get_all_values()
    hasil = {}
    for r in rows[1:]:
        r = r + [""] * (len(HEADER) - len(r))
        if r[0]:
            hasil[r[0]] = {"id": r[0], "nama": r[1], "label": r[2], "deskripsi": r[3],
                           "kualitas": int(r[4]) if r[4].isdigit() else 3,
                           "cover": r[5].strip().lower() in ("ya", "true", "1"),
                           "yakin": r[7].strip().lower() not in ("tidak", "false", "0")}
    return hasil


LABEL_HOLD = "HOLD"


def kode_foto(file_id):
    """Kode pendek foto (8 karakter awal ID Drive) -- ditaruh di nama file yang
    dikirim ke Telegram, biar admin bisa /hold dengan reply ke file itu."""
    return str(file_id)[:8]


def id_hold(sheets):
    try:
        return {fid for fid, f in baca_katalog(sheets).items() if f["label"].strip().upper() == LABEL_HOLD}
    except Exception:
        return set()


def cari_foto(sheets, kata=None, kode=None):
    """Cari foto di katalog: by kode pendek (dari nama file) atau kata kunci
    (semua kata harus ada di label/deskripsi/nama file)."""
    katalog = baca_katalog(sheets)
    if kode:
        return [f for fid, f in katalog.items() if fid.startswith(kode)]
    kata = [k.lower() for k in (kata or "").split() if k.strip()]
    if not kata:
        return []
    return [f for f in katalog.values()
            if all(k in f"{f['label']} {f['deskripsi']} {f['nama']}".lower() for k in kata)]


def set_label(sheets, file_ids, label):
    ws = _ws_katalog(sheets)
    rows = ws.get_all_values()
    import gspread
    sel = [gspread.Cell(i, 3, label) for i, r in enumerate(rows[1:], start=2) if r and r[0] in set(file_ids)]
    if sel:
        ws.update_cells(sel)
    return len(sel)


def _jpeg_kecil(img, sisi=768):
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((sisi, sisi), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return base64.b64encode(buf.getvalue()).decode()


def baca_panduan(sheets):
    """Aturan ciri produk dari tab 'Panduan Foto' (dibuat + diisi default
    kalau belum ada)."""
    import gspread
    try:
        ws = sheets.sheet.worksheet(TAB_PANDUAN)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheets.sheet.add_worksheet(title=TAB_PANDUAN, rows=50, cols=1)
        ws.update(values=[["Aturan (1 baris = 1 ciri produk, boleh ditambah/diubah)"]] +
                  [[a] for a in PANDUAN_DEFAULT], range_name=f"A1:A{len(PANDUAN_DEFAULT) + 1}")
        return list(PANDUAN_DEFAULT)
    isi = [r[0].strip() for r in ws.get_all_values()[1:] if r and r[0].strip()]
    # Migrasi sekali: aturan lama roti panjang (topping di atas = Mocha Meises)
    # ternyata salah -- ganti otomatis dengan 2 aturan yang benar.
    lama = ("Roti PANJANG/lonjong dengan topping meses cokelat di atas = Mocha Meises; "
            "roti panjang dengan parutan keju di atas = Cream Cheese.")
    if lama in isi:
        baru = [a for a in PANDUAN_DEFAULT if a.startswith("Roti PANJANG")]
        isi = [x for a in isi for x in (baru if a == lama else [a])]
        ws.batch_clear([f"A2:A{len(isi) + 20}"])
        ws.update(values=[[a] for a in isi], range_name=f"A2:A{len(isi) + 1}")
    return isi


def _ws_panduan(sheets):
    baca_panduan(sheets)  # pastikan tab ada
    return sheets.sheet.worksheet(TAB_PANDUAN)


def tambah_panduan(sheets, aturan):
    _ws_panduan(sheets).append_row([aturan], value_input_option="RAW")
    return baca_panduan(sheets)


def hapus_panduan(sheets, nomor):
    """nomor mulai dari 1 (sesuai daftar /panduan)."""
    ws = _ws_panduan(sheets)
    rows = ws.get_all_values()
    isi = [(i, r[0].strip()) for i, r in enumerate(rows[1:], start=2) if r and r[0].strip()]
    if not 1 <= nomor <= len(isi):
        return None
    baris, teks = isi[nomor - 1]
    ws.delete_rows(baris)
    return teks


def _lihat_foto(img, menu_text, panduan):
    from ai_parser import client, _safe_json_loads
    prompt = (
        "Ini foto produk bakery rumahan Miss Piggy. Daftar menu:\n" + menu_text +
        "\n\nCIRI-CIRI PRODUK DARI PEMILIK (WAJIB diikuti, lebih penting dari tebakanmu):\n- " +
        "\n- ".join(panduan) +
        "\n\nLihat bentuk produknya baik-baik (bulat/panjang/cincin, digoreng/dipanggang, topping). "
        "Balas HANYA JSON: {\"label\": nama produk sesuai ciri di atas (pakai nama dari menu "
        "kalau cocok; 'lainnya' kalau bukan foto produk), \"yakin\": true/false, "
        "\"deskripsi\": 1 kalimat singkat tentang isi foto (bentuk, topping, sudut, suasana), "
        "\"kualitas\": angka 1-5 (terang, tajam, menggoda untuk sosmed), "
        "\"cover\": true kalau cocok jadi slide pertama carousel}"
    )
    # Pakai model yang lebih teliti (Sonnet) -- cuma sekali per foto
    resp = client.with_options(timeout=60.0, max_retries=2).messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": _jpeg_kecil(img)}},
            {"type": "text", "text": prompt},
        ]}],
    )
    return _safe_json_loads(resp.content[0].text) or {}


def perbarui_katalog(sheets, ulang=False):
    """Periksa foto yang belum ada di katalog (maks MAKS_PERIKSA_SEKALI per
    jalan). ulang=True: hapus katalog lama & cek semua dari awal (dipakai
    setelah panduan produk diubah). Return (katalog_lengkap, jumlah_baru)."""
    folder = fm.cari_folder(sheets)
    if not folder:
        return {}, 0
    semua = fm.daftar_foto(sheets, folder)
    if ulang:
        _ws_katalog(sheets).batch_clear(["A2:H2000"])
        katalog = {}
    else:
        katalog = baca_katalog(sheets)
    baru = [f for f in semua if f["id"] not in katalog][:MAKS_PERIKSA_SEKALI]
    if not baru:
        return katalog, 0
    try:
        menu = sheets.get_pricelist_text().replace("*", "")
    except Exception:
        menu = "(roti, roti gandum, donat, roti tawar)"
    panduan = baca_panduan(sheets)
    ws = _ws_katalog(sheets)
    hari_ini = datetime.date.today().isoformat()
    baris = []
    total = len(baru)
    _progres(f"Cek {total} foto baru...")

    def periksa(f):
        try:
            return f, _lihat_foto(fm.unduh(sheets, f["id"]), menu, panduan)
        except Exception as e:
            logger.warning(f"Katalog: lewati {f.get('name')}: {e}")
            return f, None

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=5) as pool:
        for n, fut in enumerate(as_completed([pool.submit(periksa, f) for f in baru]), 1):
            f, info = fut.result()
            if n % 3 == 0 or n == total:
                _progres(f"Cek foto {n}/{total}")
            if info is None:
                continue
            kual = int(info.get("kualitas") or 3)
            data = {"id": f["id"], "nama": f.get("name", ""), "label": str(info.get("label") or "lainnya"),
                    "deskripsi": str(info.get("deskripsi") or ""), "kualitas": max(1, min(5, kual)),
                    "cover": bool(info.get("cover")), "yakin": info.get("yakin") is not False}
            katalog[f["id"]] = data
            baris.append([data["id"], data["nama"], data["label"], data["deskripsi"],
                          str(data["kualitas"]), "ya" if data["cover"] else "tidak", hari_ini,
                          "ya" if data["yakin"] else "tidak"])
    if baris:
        ws.append_rows(baris, value_input_option="RAW")
    return katalog, len(baris)


# ---------- rencana konten ----------

RENCANA_PROMPT = """Kamu staf Marketing & Konten Miss Piggy (home bakery Bandung, sistem PO mingguan, terbuka NON-HALAL; item (Pork) mengandung babi, jangan pernah klaim halal).

Dari IDE KONTEN yang diberikan, buat {jumlah} rencana carousel IG/TikTok yang bisa langsung dibuat dari FOTO yang tersedia.
Aturan:
- Pakai HANYA file_id dari daftar FOTO. Pilih foto yang labelnya cocok dengan isi slide; utamakan kualitas tinggi. Slide pertama pakai foto yang cocok jadi cover.
- Tiap carousel 4-5 slide foto (slide penutup ajakan order dibuat otomatis, jangan dimasukkan). Jangan pakai foto yang sama dua kali dalam satu carousel.
- "judul" slide maksimal 6 kata, "teks" maksimal 18 kata, bahasa santai. Jangan mengarang harga/promo yang tidak ada di data.
- Kalau ide butuh foto yang tidak ada (misal menu baru yang belum pernah dibuat), pilih ide lain yang fotonya ada.
- NAMA PRODUK di judul/teks/caption HARUS sama dengan label fotonya. Jangan menyebut foto roti sebagai donat atau sebaliknya. Kalau label foto 'roti' atau ditandai 'belum yakin', tulis secara umum ("roti Miss Piggy") tanpa menyebut rasa.
- Satu carousel sebaiknya satu tema produk yang konsisten (misal semua roti, atau semua donat), jangan dicampur dengan judul yang menyebut satu jenis saja.
- caption: maksimal 600 karakter + 6-10 hashtag.

Balas HANYA JSON:
{{"konten": [{{"judul_konten": "...", "alasan": "1 kalimat kenapa dipilih", "slides": [{{"foto": "file_id", "judul": "...", "teks": "..."}}], "caption": "..."}}]}}"""


def rencanakan(ide_text, katalog, menu_text, jumlah=2):
    from ai_parser import client, _safe_json_loads
    foto_bagus = sorted(katalog.values(), key=lambda f: -f["kualitas"])
    foto_bagus = [f for f in foto_bagus if f["label"].lower() != "lainnya"
                  and f["label"].strip().upper() != LABEL_HOLD and f["kualitas"] >= 2][:50]
    if len(foto_bagus) < 4:
        return {"error": "Foto di katalog masih kurang (minimal 4 foto roti yang jelas)."}
    daftar = "\n".join(
        f"- {f['id']} | {f['label']}{'' if f.get('yakin', True) else ' (belum yakin)'} | kualitas {f['kualitas']}{' | cover' if f['cover'] else ''} | {f['deskripsi']}"
        for f in foto_bagus
    )
    isi = f"IDE KONTEN:\n{ide_text}\n\nMENU:\n{menu_text}\n\nFOTO TERSEDIA:\n{daftar}"
    # Rencana butuh mikir lebih lama dari parsing order -> batas tunggu 2 menit
    resp = client.with_options(timeout=120.0, max_retries=1).messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2200,
        system=RENCANA_PROMPT.format(jumlah=jumlah),
        messages=[{"role": "user", "content": isi}],
    )
    data = _safe_json_loads(resp.content[0].text)
    if not isinstance(data, dict) or not data.get("konten"):
        return {"error": "Rencana konten dari AI nggak kebaca. Coba lagi."}
    sah = set(katalog)
    hasil = []
    for k in data["konten"][:jumlah]:
        slides = [s for s in k.get("slides", []) if s.get("foto") in sah][:5]
        if len(slides) >= 2:
            k["slides"] = slides
            hasil.append(k)
    if not hasil:
        return {"error": "AI nggak berhasil memilih foto yang cocok. Coba lagi atau tambah foto."}
    return {"konten": hasil}


# ---------- render slide ----------

def _bungkus(draw, teks, font, lebar):
    kata, baris, skr = teks.split(), [], ""
    for k in kata:
        coba = (skr + " " + k).strip()
        if draw.textlength(coba, font=font) <= lebar:
            skr = coba
        else:
            if skr:
                baris.append(skr)
            skr = k
    if skr:
        baris.append(skr)
    return baris


def _kartu(base, judul, teks, nomor=None, besar=False):
    d = ImageDraw.Draw(base)
    m, lebar = 56, W - 2 * 56 - 96
    f_j = fm._font(fm._FONT_JUDUL, 74 if besar else 56, b"SemiBold")
    f_t = fm._font(fm._FONT_TEKS, 34 if besar else 32, b"SemiBold")
    bj = _bungkus(d, judul, f_j, lebar)[:3]
    bt = _bungkus(d, teks, f_t, lebar)[:4] if teks else []
    tj, tt = (84 if besar else 66), 44
    tinggi = 70 + len(bj) * tj + (18 + len(bt) * tt if bt else 0) + 46
    y0 = H - m - tinggi
    bayang = Image.new("RGBA", fm.UKURAN, (0, 0, 0, 0))
    ImageDraw.Draw(bayang).rounded_rectangle([m + 4, y0 + 10, W - m + 4, H - m + 10], radius=34, fill=(0, 0, 0, 70))
    base.alpha_composite(bayang.filter(ImageFilter.GaussianBlur(12)))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([m, y0, W - m, H - m], radius=34, fill=(251, 244, 233, 242))
    y = y0 + 44
    for b in bj:
        d.text((m + 48, y), b, font=f_j, fill=fm.COKLAT)
        y += tj
    if bt:
        y += 18
        for b in bt:
            d.text((m + 48, y), b, font=f_t, fill=(120, 86, 60))
            y += tt
    if nomor:
        f_n = fm._font(fm._FONT_TEKS, 26, b"ExtraBold")
        lw = d.textlength(nomor, font=f_n)
        d.rounded_rectangle([W - m - lw - 64, y0 - 26, W - m - 24, y0 + 22], radius=24, fill=fm.PINK)
        d.text((W - m - lw - 44, y0 - 20), nomor, font=f_n, fill=(255, 255, 255))


def slide_foto(img, judul, teks, nomor, cover=False):
    base = fm._rapikan(img).convert("RGBA")
    lap = Image.new("RGBA", fm.UKURAN, (0, 0, 0, 0))
    dl = ImageDraw.Draw(lap)
    for i in range(420):
        dl.line([(0, H - 420 + i), (W, H - 420 + i)], fill=(30, 20, 15, int(110 * (i / 420) ** 1.6)))
    base.alpha_composite(lap)
    _kartu(base, judul, teks, nomor=None if cover else nomor, besar=cover)
    if cover:
        fm._tempel_logo(base, 230, (56, 56))
        d = ImageDraw.Draw(base)
        f = fm._font(fm._FONT_TEKS, 28, b"ExtraBold")
        t = "geser  »"
        lw = d.textlength(t, font=f)
        d.rounded_rectangle([W - 56 - lw - 48, 70, W - 56, 122], radius=26, fill=(251, 244, 233, 235))
        d.text((W - 56 - lw - 24, 78), t, font=f, fill=fm.COKLAT)
    else:
        fm._tempel_logo(base, 150, (W - 44, 44), jangkar="kanan-atas")
    return base.convert("RGB")


def slide_penutup(img_latar, tutup, kirim, web, wa):
    base = fm._rapikan(img_latar).filter(ImageFilter.GaussianBlur(18))
    base = Image.blend(base, Image.new("RGB", fm.UKURAN, (31, 32, 33)), 0.72).convert("RGBA")
    d = ImageDraw.Draw(base)
    fm._tempel_logo(base, 420, (W // 2, 210), jangkar="tengah-atas")
    d = ImageDraw.Draw(base)

    def tengah(teks, y, font, warna):
        lw = d.textlength(teks, font=font)
        d.text(((W - lw) / 2, y), teks, font=font, fill=warna)

    tengah("Yuk ikutan PO!", 560, fm._font(fm._FONT_JUDUL, 84, b"SemiBold"), fm.KREM)
    f = fm._font(fm._FONT_TEKS, 36, b"Bold")
    tengah(f"Tutup {fm._tgl(tutup)}", 700, f, (236, 210, 175))
    tengah(f"Kirim & ambil {fm._tgl(kirim)}", 752, f, (236, 210, 175))
    f2 = fm._font(fm._FONT_TEKS, 34, b"ExtraBold")
    for i, t in enumerate([x for x in (web, f"WA {wa}" if wa else "") if x]):
        lw = d.textlength(t, font=f2)
        y = 880 + i * 92
        d.rounded_rectangle([(W - lw) / 2 - 36, y, (W + lw) / 2 + 36, y + 70], radius=35, fill=fm.KARAMEL)
        tengah(t, y + 14, f2, (255, 250, 242))
    return base.convert("RGB")


def render_carousel(sheets, rencana):
    tutup, kirim = fm.tanggal_po_berikut()
    web = os.getenv("PROMO_WEB", "order.misspiggybdg19.workers.dev")
    wa = os.getenv("PROMO_WA", "0815-6178-880")
    slides = rencana["slides"]
    total = len(slides) + 1
    hasil, latar = [], None
    for i, s in enumerate(slides):
        img = fm.unduh(sheets, s["foto"])
        latar = latar or img
        hasil.append(fm.ke_jpeg(slide_foto(img, s.get("judul", ""), s.get("teks", ""),
                                           f"{i + 1}/{total}", cover=(i == 0))))
    hasil.append(fm.ke_jpeg(slide_penutup(latar, tutup, kirim, web, wa)))
    return hasil


# ---------- alur lengkap + kirim ----------

def tujuan_marketing():
    if config.GROUP_CHAT_ID_MARKETING:
        return config.GROUP_CHAT_ID_MARKETING, None
    if getattr(config, "TOPIC_ID_MARKETING", None) and config.GROUP_CHAT_ID:
        return config.GROUP_CHAT_ID, config.TOPIC_ID_MARKETING
    if config.GROUP_CHAT_ID:
        return config.GROUP_CHAT_ID, None
    return (config.OWNER_TELEGRAM_IDS[0] if config.OWNER_TELEGRAM_IDS else None), None


def siapkan_konten(sheets, ide_text, jumlah=2):
    katalog, baru = perbarui_katalog(sheets)
    if not katalog:
        return {"error": "Folder foto belum kebaca atau masih kosong."}
    try:
        menu = sheets.get_pricelist_text().replace("*", "")
    except Exception:
        menu = ""
    _progres("Pilih foto yang cocok...")
    rencana = rencanakan(ide_text, katalog, menu, jumlah)
    if "error" in rencana:
        return rencana
    paket = []
    for k in rencana["konten"]:
        _progres(f"Desain carousel: {k.get('judul_konten', '')}"[:40])
        try:
            paket.append({"rencana": k, "gambar": render_carousel(sheets, k)})
        except Exception as e:
            logger.exception("Gagal render carousel")
            paket.append({"rencana": k, "error": str(e)})
    return {"paket": paket, "foto_baru": baru, "total_katalog": len(katalog)}


async def kirim_konten(bot, sheets, ide_text, chat_id=None, thread_id=None, jumlah=2):
    """Bikin & kirim carousel ke grup Konten. Return pesan error atau None."""
    from telegram import InputMediaDocument
    try:
        import kantor
        kantor.mulai("marketing", "Pilih foto & bikin carousel")
    except Exception:
        pass
    if chat_id is None:
        chat_id, thread_id = tujuan_marketing()
    if not chat_id:
        return "Grup tujuan konten belum di-setting."
    try:
        hasil = await asyncio.wait_for(asyncio.to_thread(siapkan_konten, sheets, ide_text, jumlah), timeout=600)
    except asyncio.TimeoutError:
        _selesai("Timeout")
        return "Bikin konten kelamaan (timeout). Coba /konten lagi."
    except Exception as e:
        logger.exception("Gagal siapkan konten")
        _selesai("Gagal")
        return f"Gagal bikin konten: {e}"
    if "error" in hasil:
        _selesai("Gagal")
        return hasil["error"]
    if hasil["foto_baru"]:
        await _kirim(bot.send_message, chat_id=chat_id, message_thread_id=thread_id,
                               text=f"🔎 {hasil['foto_baru']} foto baru sudah dicek & masuk katalog "
                                    f"(total {hasil['total_katalog']} foto).")
    for n, p in enumerate(hasil["paket"], 1):
        r = p["rencana"]
        if "error" in p:
            await _kirim(bot.send_message, chat_id=chat_id, message_thread_id=thread_id,
                                   text=f"⚠️ Carousel \"{r.get('judul_konten', '')}\" gagal dibuat: {p['error']}")
            continue
        ids = [s["foto"] for s in r["slides"]] + [r["slides"][0]["foto"]]
        media = [InputMediaDocument(media=b, filename=f"Carousel{n}_slide{i + 1}_{kode_foto(ids[i])}.jpg")
                 for i, b in enumerate(p["gambar"])]
        await _kirim(bot.send_media_group, chat_id=chat_id, message_thread_id=thread_id, media=media)
        teks = (f"🎠 CAROUSEL {n}: {r.get('judul_konten', '')}\n"
                f"Kenapa: {r.get('alasan', '-')}\n\n"
                f"Caption siap pakai:\n\n{r.get('caption', '')}\n\n"
                "Posting slide sesuai urutan nomor file. Lagu pilih sendiri di aplikasi.")
        await _kirim(bot.send_message, chat_id=chat_id, message_thread_id=thread_id, text=teks[:4000])
    _selesai(f"{len(hasil['paket'])} carousel terkirim ke grup Konten")
    return None


async def _kirim(fungsi, **kw):
    """Kirim ke Telegram dengan batas waktu longgar + coba ulang 3x kalau
    koneksi Telegram lagi lambat (TimedOut/NetworkError)."""
    from telegram.error import NetworkError, TimedOut
    kw.setdefault("read_timeout", 60)
    kw.setdefault("write_timeout", 60)
    for i in range(3):
        try:
            return await fungsi(**kw)
        except (TimedOut, NetworkError):
            if i == 2:
                raise
            await asyncio.sleep(3 * (i + 1))


def _selesai(teks):
    try:
        import kantor
        kantor.selesai("marketing", teks)
    except Exception:
        pass


def _progres(teks):
    try:
        import kantor
        kantor.progres("marketing", teks)
    except Exception:
        pass
