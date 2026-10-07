"""
Foto mingguan untuk sosmed (staf Marketing).

Ambil foto roti dari folder Google Drive (default "Foto Roti Miss Piggy",
di-share ke email service account bot), lalu bikin:
- 1 poster OPEN PO (ada tanggal tutup PO & tanggal kirim + cara order)
- 7 foto siap posting TANPA tanggal (crop 4:5, warna dihangatkan, logo kecil)

Foto digilir: yang paling lama nggak dipakai didahulukan. Riwayat pemakaian
disimpan di tab Pengaturan (key: foto_terpakai) biar tetap ingat walau bot
restart. Semua diolah pakai Pillow, TANPA AI (gratis).
"""

import datetime
import io
import json
import logging
import os
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

import config
import date_helpers

logger = logging.getLogger(__name__)

try:  # foto iPhone (HEIC/HEIF)
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # pragma: no cover
    pass

FOLDER_NAME = os.getenv("FOTO_FOLDER_NAME", "Foto Roti Miss Piggy")
UKURAN = (1080, 1350)  # 4:5, pas buat feed IG; TikTok photo mode juga terima
JUMLAH_FOTO = 7
KEY_RIWAYAT = "foto_terpakai"

_DIR = os.path.dirname(os.path.abspath(__file__))
_WORDMARK = os.path.join(_DIR, "logo_wordmark_light.png")
_FONT_JUDUL = os.path.join(_DIR, "fonts", "Fredoka.ttf")
_FONT_TEKS = os.path.join(_DIR, "fonts", "Nunito.ttf")

KREM = (243, 230, 210)
KARAMEL = (196, 138, 84)
PINK = (255, 143, 171)

_HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
_BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
          "Agustus", "September", "Oktober", "November", "Desember"]


def _font(path, size, weight):
    f = ImageFont.truetype(path, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def _tgl(d):
    return f"{_HARI[d.weekday()]}, {d.day} {_BULAN[d.month - 1]}"


def tanggal_po_berikut(now=None):
    """PO yang dibuka (dipromosikan) hari ini: Kamis PENGIRIMAN berikutnya.
    Kalau hari ini Kamis (hari kirim), berarti Kamis minggu depan."""
    tz = date_helpers.get_timezone()
    now = now or datetime.datetime.now(tz)
    hari = now.date()
    maju = (3 - hari.weekday()) % 7 or 7
    kamis = hari + datetime.timedelta(days=maju)
    return kamis - datetime.timedelta(days=1), kamis  # (tutup Rabu, kirim Kamis)


# ---------- Google Drive (pakai sesi login service account dari gspread) ----------

def _sesi(sheets):
    return sheets.gc.http_client.session


def cari_folder(sheets):
    q = (f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' "
         "and trashed = false")
    r = _sesi(sheets).get("https://www.googleapis.com/drive/v3/files",
                          params={"q": q, "fields": "files(id,name)", "pageSize": 5,
                                  "supportsAllDrives": True, "includeItemsFromAllDrives": True})
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


def _list_anak(sheets, folder_id):
    hasil, token = [], None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed = false and "
                       "(mimeType contains 'image/' or mimeType = 'application/vnd.google-apps.folder')",
                  "fields": "nextPageToken, files(id,name,mimeType)", "pageSize": 500,
                  "supportsAllDrives": True, "includeItemsFromAllDrives": True}
        if token:
            params["pageToken"] = token
        r = _sesi(sheets).get("https://www.googleapis.com/drive/v3/files", params=params)
        r.raise_for_status()
        data = r.json()
        hasil.extend(data.get("files", []))
        token = data.get("nextPageToken")
        if not token:
            return hasil


def daftar_foto(sheets, folder_id, kedalaman_maks=4):
    """Semua foto di folder ini TERMASUK di dalam subfolder-subfoldernya
    (misal hasil 'drive-download-...' yang otomatis bikin banyak folder)."""
    foto, antrean, dilihat = [], [(folder_id, 0)], set()
    while antrean:
        fid, dalam = antrean.pop(0)
        if fid in dilihat:
            continue
        dilihat.add(fid)
        for f in _list_anak(sheets, fid):
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                if dalam < kedalaman_maks:
                    antrean.append((f["id"], dalam + 1))
            else:
                foto.append(f)
    return foto


def unduh(sheets, file_id):
    r = _sesi(sheets).get(f"https://www.googleapis.com/drive/v3/files/{file_id}",
                          params={"alt": "media", "supportsAllDrives": True})
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def _baca_riwayat(sheets):
    try:
        ws = sheets._get_or_create_pengaturan_ws()
        isi = sheets._read_pengaturan_value(ws, KEY_RIWAYAT)
        return json.loads(isi) if isi else []
    except Exception:
        return []


def _simpan_riwayat(sheets, riwayat):
    try:
        ws = sheets._get_or_create_pengaturan_ws()
        sheets._write_pengaturan_value(ws, KEY_RIWAYAT, json.dumps(riwayat[-300:]))
    except Exception as e:
        logger.warning(f"Gagal simpan riwayat foto: {e}")


def pilih_foto(semua, riwayat, jumlah):
    """Yang belum pernah dipakai duluan (acak), lalu yang paling lama
    terakhir dipakai."""
    urutan = {fid: i for i, fid in enumerate(riwayat)}  # makin kecil = makin lama
    belum = [f for f in semua if f["id"] not in urutan]
    random.shuffle(belum)
    pernah = sorted([f for f in semua if f["id"] in urutan], key=lambda f: urutan[f["id"]])
    return (belum + pernah)[:jumlah]


# ---------- olah gambar ----------

def _terang_otomatis(img):
    """Koreksi otomatis sesuai kondisi foto:
    - foto gelap dicerahkan (gamma), foto terlalu terang diturunkan sedikit
    - warna kebiruan/kehijauan dari lampu dinetralkan sedikit (white balance);
      foto yang sudah hangat (warna roti) dibiarkan"""
    from PIL import ImageStat
    import math
    # white balance ringan: dekatkan rata-rata R,G,B satu sama lain
    r, g, b = ImageStat.Stat(img).mean
    abu = (r + g + b) / 3
    if abu > 1 and (b > r * 0.95 or g > r * 1.02):  # cuma koreksi kalau kebiruan/kehijauan; warna hangat roti dibiarkan
        def faktor(c):
            return max(0.85, min(1.15, (abu / c) if c else 1)) ** 0.5
        fr, fg, fb = faktor(r), faktor(g), faktor(b)
        img = Image.merge("RGB", [ch.point(lambda v, f=f: min(255, int(v * f)))
                                  for ch, f in zip(img.split(), (fr, fg, fb))])
    # kecerahan: target rata-rata luminance ~0.52
    lum = ImageStat.Stat(img.convert("L")).mean[0] / 255
    target = 0.52
    if lum < 0.47 or lum > 0.62:
        gamma = math.log(target) / math.log(max(0.05, min(0.95, lum)))
        gamma = max(0.55, min(1.4, gamma))
        tabel = [min(255, int(255 * ((i / 255) ** gamma))) for i in range(256)] * 3
        img = img.point(tabel)
        if gamma < 1:  # habis dicerahkan, warna & kontras suka pudar
            img = ImageEnhance.Contrast(img).enhance(1.08)
            img = ImageEnhance.Color(img).enhance(1.12)
    return img


def _rapikan(img):
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, UKURAN, Image.LANCZOS, centering=(0.5, 0.5))
    img = _terang_otomatis(img)
    img = ImageEnhance.Contrast(img).enhance(1.04)
    img = ImageEnhance.Color(img).enhance(1.10)
    hangat = Image.new("RGB", UKURAN, (255, 196, 140))
    img = Image.blend(img, hangat, 0.05)
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3))


_ICON = os.path.join(_DIR, "logo_icon.png")
COKLAT_TUA = (43, 37, 35)
COKLAT = (74, 47, 33)


_LOGO_ASLI = os.path.join(_DIR, "logo_web.png")


def logo_bulat(diameter):
    """Lencana bulat berisi logo ASLI Miss Piggy (simbol + tulisan karamel di
    latar arang), dengan ring krem tipis."""
    skala = 4
    d = diameter * skala
    lap = Image.new("RGBA", (d, d), (0, 0, 0, 0))
    dr = ImageDraw.Draw(lap)
    dr.ellipse([0, 0, d - 1, d - 1], fill=(*KREM, 255))
    tebal = int(d * 0.03)
    dr.ellipse([tebal, tebal, d - 1 - tebal, d - 1 - tebal], fill=(31, 32, 33, 255))
    if os.path.exists(_LOGO_ASLI):
        logo = Image.open(_LOGO_ASLI).convert("RGB")
        lebar = int(d * 0.80)
        logo = logo.resize((lebar, int(logo.height * lebar / logo.width)), Image.LANCZOS)
        # latar logo sudah arang, jadi cukup ditempel di tengah lingkaran
        topeng = Image.new("L", (d, d), 0)
        ImageDraw.Draw(topeng).ellipse([tebal, tebal, d - 1 - tebal, d - 1 - tebal], fill=255)
        isi = Image.new("RGBA", (d, d), (31, 32, 33, 255))
        isi.paste(logo, ((d - logo.width) // 2, (d - logo.height) // 2))
        lap.paste(isi, (0, 0), topeng)
    elif os.path.exists(_ICON):
        ikon = Image.open(_ICON).convert("RGBA")
        ikon.thumbnail((int(d * 0.58), int(d * 0.58)), Image.LANCZOS)
        lap.alpha_composite(ikon, ((d - ikon.width) // 2, (d - ikon.height) // 2))
    return lap.resize((diameter, diameter), Image.LANCZOS)


_LOGO_KREM = os.path.join(_DIR, "logo_krem.png")
_LOGO_GELAP = os.path.join(_DIR, "logo_gelap.png")


def _tempel_logo(base_rgba, lebar, pos, jangkar="kiri-atas"):
    """Tempel logo Miss Piggy TRANSPARAN (tanpa latar hitam). Warnanya
    otomatis: krem di atas area foto yang gelap, cokelat tua di atas area
    yang terang, plus bayangan/cahaya halus biar tetap kebaca."""
    from PIL import ImageStat
    path_k, path_g = _LOGO_KREM, _LOGO_GELAP
    if not (os.path.exists(path_k) and os.path.exists(path_g)):
        return
    contoh = Image.open(path_k)
    tinggi = int(contoh.height * lebar / contoh.width)
    x, y = pos
    if jangkar == "kanan-atas":
        x -= lebar
    elif jangkar == "kanan-bawah":
        x, y = x - lebar, y - tinggi
    elif jangkar == "tengah-atas":
        x -= lebar // 2
    x, y = max(0, x), max(0, y)
    area = base_rgba.crop((x, y, x + lebar, y + tinggi)).convert("L")
    terang = ImageStat.Stat(area).mean[0] > 140
    logo = Image.open(path_g if terang else path_k).convert("RGBA").resize((lebar, tinggi), Image.LANCZOS)
    # bayangan (untuk logo krem) atau cahaya lembut (untuk logo gelap)
    warna_halo = (255, 250, 242) if terang else (0, 0, 0)
    halo = Image.new("RGBA", (lebar + 40, tinggi + 40), (*warna_halo, 0))
    alpha = logo.getchannel("A").point(lambda v: int(v * (0.55 if terang else 0.6)))
    halo.paste(Image.new("RGBA", logo.size, (*warna_halo, 255)), (20, 20), alpha)
    halo = halo.filter(ImageFilter.GaussianBlur(7))
    base_rgba.alpha_composite(halo, (x - 20, y - 18))
    base_rgba.alpha_composite(logo, (x, y))


def foto_siap_posting(img):
    """Foto tanpa tanggal: rapikan + logo bulat kecil di pojok kanan bawah."""
    base = _rapikan(img).convert("RGBA")
    _tempel_logo(base, 190, (UKURAN[0] - 44, UKURAN[1] - 40), jangkar="kanan-bawah")
    return base.convert("RGB")


def _teks_pas(draw, teks, path, weight, ukuran, lebar_maks):
    """Kecilkan font sampai teksnya muat di lebar_maks."""
    while ukuran > 18:
        f = _font(path, ukuran, weight)
        if draw.textlength(teks, font=f) <= lebar_maks:
            return f
        ukuran -= 2
    return _font(path, ukuran, weight)


def poster_open_po(img, tutup, kirim, web="", wa=""):
    """Poster Open PO: foto tetap jadi bintang utama, info di kartu krem
    kecil di bawah, logo bulat di pojok kiri atas."""
    base = _rapikan(img).convert("RGBA")
    W, H = UKURAN

    # gradasi tipis di bawah biar kartu nyatu sama foto
    lapis = Image.new("RGBA", UKURAN, (0, 0, 0, 0))
    dl = ImageDraw.Draw(lapis)
    for i in range(380):
        dl.line([(0, H - 380 + i), (W, H - 380 + i)], fill=(30, 20, 15, int(120 * (i / 380) ** 1.6)))
    base.alpha_composite(lapis)

    # kartu info
    m = 56
    kartu_h = 352
    y0 = H - m - kartu_h
    kartu = Image.new("RGBA", UKURAN, (0, 0, 0, 0))
    dk = ImageDraw.Draw(kartu)
    dk.rounded_rectangle([m + 4, y0 + 10, W - m + 4, H - m + 10], radius=34, fill=(0, 0, 0, 70))
    kartu = kartu.filter(ImageFilter.GaussianBlur(12))
    base.alpha_composite(kartu)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle([m, y0, W - m, H - m], radius=34, fill=(251, 244, 233, 240))

    x = m + 48
    lebar = W - 2 * m - 96
    f_label = _font(_FONT_TEKS, 26, b"ExtraBold")
    label = "PRE-ORDER"
    lw = d.textlength(label, font=f_label)
    d.rounded_rectangle([x, y0 + 38, x + lw + 32, y0 + 78], radius=20, fill=PINK)
    d.text((x + 16, y0 + 43), label, font=f_label, fill=(255, 255, 255))

    f_judul = _font(_FONT_JUDUL, 92, b"SemiBold")
    d.text((x, y0 + 84), "Open PO", font=f_judul, fill=COKLAT)

    baris1 = f"Tutup {_tgl(tutup)}"
    baris2 = f"Kirim & ambil {_tgl(kirim)}, {config.DELIVERY_WINDOW.replace(':', '.')}"
    f1 = _teks_pas(d, baris1, _FONT_TEKS, b"Bold", 34, lebar)
    f2 = _teks_pas(d, baris2, _FONT_TEKS, b"SemiBold", 30, lebar)
    d.text((x, y0 + 190), baris1, font=f1, fill=COKLAT)
    d.text((x, y0 + 232), baris2, font=f2, fill=(120, 86, 60))

    kontak = "  ·  ".join(t for t in (web, f"WA {wa}" if wa else "") if t)
    if kontak:
        f3 = _teks_pas(d, kontak, _FONT_TEKS, b"Bold", 26, lebar)
        tinggi_pita = 54
        d.rounded_rectangle([m, H - m - tinggi_pita, W - m, H - m], radius=34, fill=(*KARAMEL, 255))
        d.rectangle([m, H - m - tinggi_pita, W - m, H - m - tinggi_pita + 34], fill=(*KARAMEL, 255))
        tw = d.textlength(kontak, font=f3)
        d.text(((W - tw) / 2, H - m - tinggi_pita + 12), kontak, font=f3, fill=(255, 250, 242))

    _tempel_logo(base, 250, (m, m))
    return base.convert("RGB")


def ke_jpeg(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92, optimize=True, progressive=True)
    buf.seek(0)
    return buf


def siapkan_paket(sheets, now=None):
    """Return dict: {'poster': BytesIO, 'foto': [BytesIO...], 'tutup', 'kirim',
    'jumlah_folder'} atau {'error': '...'}."""
    folder_id = cari_folder(sheets)
    if not folder_id:
        return {"error": (
            f"Folder \"{FOLDER_NAME}\" belum kebaca bot. Pastikan nama foldernya persis itu "
            "dan sudah di-share (Viewer) ke email robot bot."
        )}
    semua = daftar_foto(sheets, folder_id)
    if not semua:
        return {"error": f"Folder \"{FOLDER_NAME}\" kebaca, tapi belum ada foto di dalamnya."}

    try:  # foto yang di-HOLD admin nggak dipakai
        import konten
        ditahan = konten.id_hold(sheets)
        semua = [f for f in semua if f["id"] not in ditahan] or semua
    except Exception:
        pass
    riwayat = _baca_riwayat(sheets)
    terpilih = pilih_foto(semua, riwayat, JUMLAH_FOTO + 1)
    tutup, kirim = tanggal_po_berikut(now)
    web = os.getenv("PROMO_WEB", "order.misspiggybdg19.workers.dev")
    wa = os.getenv("PROMO_WA", "0815-6178-880")

    hasil_foto = []
    poster = None
    dipakai = []
    for i, f in enumerate(terpilih):
        try:
            img = unduh(sheets, f["id"])
            if poster is None:
                poster = ke_jpeg(poster_open_po(img, tutup, kirim, web, wa))
            else:
                hasil_foto.append(ke_jpeg(foto_siap_posting(img)))
            dipakai.append(f["id"])
        except Exception as e:
            logger.warning(f"Lewati foto {f.get('name')}: {e}")

    if poster is None:
        return {"error": "Foto-foto di folder gagal dibuka (format nggak didukung?)."}

    riwayat = [r for r in riwayat if r not in dipakai] + dipakai
    _simpan_riwayat(sheets, riwayat)
    return {"poster": poster, "foto": hasil_foto, "tutup": tutup, "kirim": kirim,
            "ids": dipakai,
            "jumlah_folder": len(semua)}


async def kirim_paket(bot, chat_id, sheets, thread_id=None):
    """Bikin paket foto lalu kirim ke chat. Return pesan error atau None."""
    import asyncio
    from telegram import InputMediaDocument
    try:
        import kantor
        kantor.mulai("marketing", "Siapin poster Open PO + 7 foto")
    except Exception:
        pass
    try:
        paket = await asyncio.wait_for(asyncio.to_thread(siapkan_paket, sheets), timeout=180)
    finally:
        try:
            import kantor
            kantor.selesai("marketing", "Foto Open PO selesai")
        except Exception:
            pass
    if "error" in paket:
        return paket["error"]
    kirim = paket["kirim"]
    await bot.send_document(
        read_timeout=60, write_timeout=60,
        chat_id=chat_id, message_thread_id=thread_id, document=paket["poster"],
        filename=f"OpenPO_{kirim:%Y-%m-%d}.jpg",
        caption=(f"📣 Poster OPEN PO (tutup {_tgl(paket['tutup'])}, kirim {_tgl(kirim)}).\n"
                 "Caption-nya bisa minta lewat /promo."),
    )
    if paket["foto"]:
        jumlah = len(paket["foto"])
        caption = (f"📸 {jumlah} foto siap posting minggu ini (tanpa tanggal), "
                   "ukuran 4:5 buat IG & TikTok. Total foto di folder: "
                   f"{paket['jumlah_folder']}.")
        media = []
        for i, b in enumerate(paket["foto"]):
            media.append(InputMediaDocument(
                media=b, filename=f"MissPiggy_{kirim:%Y%m%d}_{i + 1}_{paket['ids'][i + 1][:8]}.jpg",
                caption=caption if i == jumlah - 1 else None,
            ))
        await bot.send_media_group(chat_id=chat_id, message_thread_id=thread_id, media=media,
                                   read_timeout=60, write_timeout=60)
    if len(paket["foto"]) < JUMLAH_FOTO:
        await bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=(f"ℹ️ Foto di folder baru {paket['jumlah_folder']}, jadi minggu ini cuma "
                  f"{len(paket['foto'])} foto sosmed. Tambah foto di folder biar tiap minggu dapat 7 "
                  "dan nggak cepat berulang."),
        )
    return None
