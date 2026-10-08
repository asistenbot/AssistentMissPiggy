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
import re

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
    data = _safe_json_loads(resp.content[0].text)
    if isinstance(data, list):  # kadang AI membalas [ {...} ]
        data = next((x for x in data if isinstance(x, dict)), None)
    return data if isinstance(data, dict) else {}


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
            info = _lihat_foto(fm.unduh(sheets, f["id"]), menu, panduan)
            return f, (info if info else None)
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
            try:
                kual = int(info.get("kualitas") or 3)
            except (TypeError, ValueError):
                kual = 3
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


def _kartu(base, judul, teks, cover=False):
    """Kotak tulisan ringkas, ditaruh di bagian foto yang paling 'sepi'
    (atas, di bawah logo, atau bawah) biar nggak nutup roti."""
    d = ImageDraw.Draw(base)
    m, lebar_maks = 56, W - 2 * 56 - 80
    f_j = fm._font(fm._FONT_JUDUL, 62 if cover else 48, b"SemiBold")
    f_t = fm._font(fm._FONT_TEKS, 30 if cover else 28, b"SemiBold")
    bj = _bungkus(d, judul, f_j, lebar_maks)[:2]
    bt = _bungkus(d, teks, f_t, lebar_maks)[:3] if teks else []
    tj, tt = (72 if cover else 58), 40
    tinggi = 36 + len(bj) * tj + (10 + len(bt) * tt if bt else 0) + 30
    lebar_isi = max([d.textlength(x, font=f_j) for x in bj] + [d.textlength(x, font=f_t) for x in bt] + [200])
    lebar = min(W - 2 * m, int(lebar_isi) + 80)
    logo_bawah = fm.LOGO_Y + fm.tinggi_logo(230 if cover else 170)
    zona = fm.zona_sepi(base, tinggi, logo_bawah + 24, m)
    y0 = logo_bawah + 24 if zona == "atas" else H - m - tinggi
    x0 = (W - lebar) // 2
    bayang = Image.new("RGBA", fm.UKURAN, (0, 0, 0, 0))
    ImageDraw.Draw(bayang).rounded_rectangle([x0 + 3, y0 + 8, x0 + lebar + 3, y0 + tinggi + 8], radius=30, fill=(0, 0, 0, 55))
    base.alpha_composite(bayang.filter(ImageFilter.GaussianBlur(11)))
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([x0, y0, x0 + lebar, y0 + tinggi], radius=30, fill=(251, 244, 233, 232))
    y = y0 + 30
    for b_ in bj:
        lw = d.textlength(b_, font=f_j)
        d.text(((W - lw) / 2, y), b_, font=f_j, fill=fm.COKLAT)
        y += tj
    if bt:
        y += 10
        for b_ in bt:
            lw = d.textlength(b_, font=f_t)
            d.text(((W - lw) / 2, y), b_, font=f_t, fill=(120, 86, 60))
            y += tt


def _pil_kanan_atas(base, teks, warna_latar, warna_teks):
    d = ImageDraw.Draw(base)
    f = fm._font(fm._FONT_TEKS, 26, b"ExtraBold")
    lw = d.textlength(teks, font=f)
    d.rounded_rectangle([W - 48 - lw - 40, 52, W - 48, 100], radius=24, fill=warna_latar)
    d.text((W - 48 - lw - 20, 59), teks, font=f, fill=warna_teks)


def slide_foto(img, judul, teks, nomor, cover=False):
    base = fm._rapikan(img).convert("RGBA")
    _kartu(base, judul, teks, cover=cover)
    fm._tempel_logo(base, 230 if cover else 170, (W // 2, fm.LOGO_Y), jangkar="tengah-atas")
    if cover:
        _pil_kanan_atas(base, "geser  »", (251, 244, 233, 235), fm.COKLAT)
    else:
        _pil_kanan_atas(base, nomor, fm.PINK, (255, 255, 255))
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


# ---------- tanya dulu ke admin, baru bikin ----------

KEY_TANYA = "konten_tanya"
_TANYA = None  # cache {str(message_id): data}


def _muat_tanya(sheets):
    global _TANYA
    if _TANYA is None:
        try:
            ws = sheets._get_or_create_pengaturan_ws()
            _TANYA = json.loads(sheets._read_pengaturan_value(ws, KEY_TANYA) or "{}")
        except Exception:
            _TANYA = {}
    return _TANYA


def _simpan_tanya(sheets):
    try:
        data = dict(list(_TANYA.items())[-6:])  # simpan 6 terakhir aja
        ws = sheets._get_or_create_pengaturan_ws()
        sheets._write_pengaturan_value(ws, KEY_TANYA, json.dumps(data))
    except Exception as e:
        logger.warning(f"Gagal simpan pertanyaan konten: {e}")


def lembar_kontak(sheets, slides):
    """Gambar ringkas: semua foto pilihan berjajar dengan nomor besar."""
    n = len(slides)
    kol = 3 if n > 4 else 2
    tw, th = 360, 450
    baris = (n + kol - 1) // kol
    kanvas = Image.new("RGB", (kol * tw + (kol + 1) * 16, baris * th + (baris + 1) * 16), (246, 236, 223))
    d = ImageDraw.Draw(kanvas)
    f = fm._font(fm._FONT_JUDUL, 64, b"SemiBold")
    for i, s_ in enumerate(slides):
        img = ImageOps.fit(ImageOps.exif_transpose(fm.unduh(sheets, s_["foto"])).convert("RGB"), (tw, th), Image.LANCZOS)
        x = 16 + (i % kol) * (tw + 16)
        y = 16 + (i // kol) * (th + 16)
        kanvas.paste(img, (x, y))
        d.ellipse([x + 14, y + 14, x + 104, y + 104], fill=fm.PINK, outline=(255, 255, 255), width=5)
        t = str(i + 1)
        lw = d.textlength(t, font=f)
        d.text((x + 59 - lw / 2, y + 22), t, font=f, fill=(255, 255, 255))
    return fm.ke_jpeg(kanvas)


def siapkan_pertanyaan(sheets, ide_text, jumlah=2):
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
    hasil = []
    for k in rencana["konten"]:
        for s_ in k["slides"]:
            info = katalog.get(s_["foto"], {})
            s_["tebakan"] = info.get("label", "roti") if info.get("yakin", True) else "belum yakin"
        hasil.append({"rencana": k, "gambar": lembar_kontak(sheets, k["slides"])})
    return {"daftar": hasil, "foto_baru": baru, "total_katalog": len(katalog)}


async def kirim_konten(bot, sheets, ide_text, chat_id=None, thread_id=None, jumlah=2):
    """Langkah 1: pilih foto, lalu TANYA admin roti apa saja di foto itu.
    Carousel baru dibuat setelah admin balas (lihat proses_jawaban)."""
    try:
        import kantor
        kantor.mulai("marketing", "Pilih foto buat carousel")
    except Exception:
        pass
    if chat_id is None:
        chat_id, thread_id = tujuan_marketing()
    if not chat_id:
        return "Grup tujuan konten belum di-setting."
    try:
        hasil = await asyncio.wait_for(asyncio.to_thread(siapkan_pertanyaan, sheets, ide_text, jumlah), timeout=900)
    except asyncio.TimeoutError:
        _selesai("Timeout")
        return "Milih foto kelamaan (timeout). Coba /konten lagi."
    except Exception as e:
        logger.exception("Gagal siapkan pertanyaan konten")
        _selesai("Gagal")
        return f"Gagal bikin konten: {e}"
    if "error" in hasil:
        _selesai("Gagal")
        return hasil["error"]
    if hasil["foto_baru"]:
        await _kirim(bot.send_message, chat_id=chat_id, message_thread_id=thread_id,
                     text=f"🔎 {hasil['foto_baru']} foto baru sudah dicek & masuk katalog "
                          f"(total {hasil['total_katalog']} foto).")
    tanya = _muat_tanya(sheets)
    for n, item in enumerate(hasil["daftar"], 1):
        r = item["rencana"]
        tebakan = "\n".join(f"{i}. {s_['tebakan']}" for i, s_ in enumerate(r["slides"], 1))
        caption = (f"🎠 Ide carousel {n}: {r.get('judul_konten', '')}\n\n"
                   f"Foto ini roti apa aja? Tebakanku:\n{tebakan}\n\n"
                   "REPLY pesan ini:\n"
                   "• kalau benar semua: ok\n"
                   "• kalau ada yang salah, tulis nomornya, misal:\n  1 roti coklat\n  3 mocha meises\n"
                   "• foto yang nggak mau dipakai: 2 skip\n"
                   "• nggak mau ide ini: batal")
        pesan = await _kirim(bot.send_photo, chat_id=chat_id, message_thread_id=thread_id,
                             photo=item["gambar"], caption=caption[:1024])
        tanya[str(pesan.message_id)] = {"chat_id": chat_id, "thread_id": thread_id,
                                        "ide": ide_text[:1500], "rencana": r}
    await asyncio.to_thread(_simpan_tanya, sheets)
    _selesai("Nunggu konfirmasi rasa dari bos")
    return None


def cek_tanya(sheets, message_id):
    return _muat_tanya(sheets).get(str(message_id))


def baca_jawaban(teks, jumlah):
    """'ok' -> {} (semua tebakan benar). '1 roti coklat\n2 skip' -> {1: 'roti coklat', 2: None}."""
    t = teks.strip()
    if re.search(r"(?i)\b(batal\w*|cancel|ga\s*usah|gak\s*usah|nggak\s*usah|ngga\s*usah|tidak\s*usah|skip\s*semua)\b", t) \
            and not re.search(r"\d", t):
        return "batal"
    if re.fullmatch(r"(?i)\s*(ok|oke|okey|okay|ya|yes|sip|betul|bener|benar|udah benar|lanjut|gas)\W*", t):
        return {}
    hasil = {}
    for m_ in re.finditer(r"(?:^|[\n,;])\s*(\d{1,2})\s*[\.\):=\-]?\s*([^\n,;]+)", t):
        no, nama = int(m_.group(1)), m_.group(2).strip()
        if 1 <= no <= jumlah and nama:
            hasil[no] = None if re.fullmatch(r"(?i)(skip|hapus|jangan|buang|ga usah|gausah|nggak|no)", nama) else nama
    return hasil if hasil else None


TULIS_PROMPT = """Kamu staf Marketing & Konten Miss Piggy (home bakery Bandung, sistem PO mingguan, terbuka NON-HALAL; item (Pork) mengandung babi, jangan pernah klaim halal).
Tulis isi carousel IG/TikTok. Daftar FOTO bernomor beserta NAMA PRODUKnya sudah dikonfirmasi pemilik.
Tulis:
- "judul_cover": judul menarik untuk slide pertama (maks 5 kata), boleh umum tentang roti Miss Piggy.
- "deskripsi": untuk SETIAP nomor foto, 1 kalimat singkat (maks 12 kata) yang cocok untuk produk di nomor itu SAJA. Jangan sebut produk lain. Kunci = nomor foto.
- "caption": maksimal 600 karakter + 6-10 hashtag, sebut produk-produknya sesuai nama dari pemilik.
Jangan mengarang harga/promo. Bahasa santai.
Balas HANYA JSON: {"judul_konten": "...", "judul_cover": "...", "deskripsi": {"1": "...", "2": "..."}, "caption": "..."}"""


def _rapikan_nama(nama):
    """'roti ham n cheese' -> 'Roti Ham n Cheese' (kata sambung tetap kecil)."""
    kecil = {"n", "dan", "&", "isi", "rasa", "with", "and"}
    kata = str(nama).strip().split()
    return " ".join(k if (k.lower() in kecil and i > 0) else (k[:1].upper() + k[1:]) for i, k in enumerate(kata))


def buat_dari_jawaban(sheets, data, jawaban):
    from ai_parser import client, _safe_json_loads
    r = data["rencana"]
    slides = []
    koreksi = {}
    for i, s_ in enumerate(r["slides"], 1):
        if i in jawaban and jawaban[i] is None:
            continue
        nama = jawaban.get(i) or s_.get("tebakan") or "roti"
        if nama == "belum yakin":
            nama = "roti Miss Piggy"
        if i in jawaban:
            koreksi.setdefault(nama, []).append(s_["foto"])
        slides.append({"foto": s_["foto"], "produk": nama})
    if len(slides) < 2:
        return {"error": "Fotonya tinggal kurang dari 2, carousel nggak jadi dibuat. Coba /konten lagi."}
    try:
        menu = sheets.get_pricelist_text().replace("*", "")
    except Exception:
        menu = ""
    isi = (f"IDE: {data.get('ide', '')}\n\nMENU:\n{menu}\n\nFOTO (nomor: nama produk):\n" +
           "\n".join(f"{i}: {s_['produk']}" for i, s_ in enumerate(slides, 1)))
    resp = client.with_options(timeout=120.0, max_retries=1).messages.create(
        model=config.CLAUDE_MODEL, max_tokens=1500, system=TULIS_PROMPT,
        messages=[{"role": "user", "content": isi}])
    tulis = _safe_json_loads(resp.content[0].text)
    if isinstance(tulis, list):
        tulis = next((x for x in tulis if isinstance(x, dict)), None)
    if not isinstance(tulis, dict):
        return {"error": "AI gagal nulis isi carousel. Balas lagi pesan tadi dengan 'ok' buat coba ulang."}
    desk = tulis.get("deskripsi") or {}
    if isinstance(desk, list):  # jaga-jaga kalau AI balas list
        desk = {str(i): v for i, v in enumerate(desk, 1)}
    for i, s_ in enumerate(slides, 1):
        nama = _rapikan_nama(s_["produk"])
        teks_ = str(desk.get(str(i)) or "").strip()
        if i == 1:
            # cover: judul menarik dari AI, nama produk foto ini di baris bawah
            s_["judul"] = str(tulis.get("judul_cover") or nama)
            s_["teks"] = nama + (f" · {teks_}" if teks_ else "")
        else:
            # judul = NAMA DARI PEMILIK, persis (nggak bisa bergeser)
            s_["judul"] = nama
            s_["teks"] = teks_
    rencana = {"judul_konten": tulis.get("judul_konten") or r.get("judul_konten", ""),
               "slides": slides, "caption": tulis.get("caption") or r.get("caption", "")}
    # ajari katalog: label dari pemilik = paling benar
    for nama, ids in koreksi.items():
        try:
            set_label(sheets, ids, nama)
        except Exception:
            pass
    return {"rencana": rencana, "gambar": render_carousel(sheets, rencana)}


async def proses_jawaban(bot, sheets, message_id, teks):
    """Langkah 2: admin sudah balas -> bikin & kirim carousel.
    Return teks balasan singkat untuk admin."""
    from telegram import InputMediaDocument
    data = cek_tanya(sheets, message_id)
    if not data:
        return None
    jawaban = baca_jawaban(teks, len(data["rencana"]["slides"]))
    if jawaban == "batal":
        _TANYA.pop(str(message_id), None)
        await asyncio.to_thread(_simpan_tanya, sheets)
        return "🗑️ Oke, ide carousel ini dibatalin."
    if jawaban is None:
        return ("Aku belum ngerti balasannya 🙏 Tulis 'ok' kalau tebakan benar semua, atau per nomor, misal:\n"
                "1 roti coklat\n2 skip")
    try:
        import kantor
        kantor.mulai("marketing", "Desain carousel sesuai rasa dari bos")
    except Exception:
        pass
    try:
        hasil = await asyncio.wait_for(asyncio.to_thread(buat_dari_jawaban, sheets, data, jawaban), timeout=600)
    except Exception as e:
        logger.exception("Gagal bikin carousel dari jawaban")
        _selesai("Gagal")
        return f"Gagal bikin carousel: {e}"
    if "error" in hasil:
        _selesai("Gagal")
        return hasil["error"]
    r = hasil["rencana"]
    ids = [s_["foto"] for s_ in r["slides"]] + [r["slides"][0]["foto"]]
    media = [InputMediaDocument(media=b, filename=f"Carousel_slide{i + 1}_{kode_foto(ids[i])}.jpg")
             for i, b in enumerate(hasil["gambar"])]
    chat_id, thread_id = data["chat_id"], data.get("thread_id")
    await _kirim(bot.send_media_group, chat_id=chat_id, message_thread_id=thread_id, media=media)
    await _kirim(bot.send_message, chat_id=chat_id, message_thread_id=thread_id,
                 text=(f"🎠 {r['judul_konten']}\n\nCaption siap pakai:\n\n{r['caption']}\n\n"
                       "Posting slide sesuai urutan nomor file. Lagu pilih sendiri di aplikasi.")[:4000])
    _TANYA.pop(str(message_id), None)
    await asyncio.to_thread(_simpan_tanya, sheets)
    _selesai("Carousel terkirim ke grup Konten")
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
