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


def daftar_foto(sheets, folder_id):
    hasil, token = [], None
    while True:
        params = {"q": f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false",
                  "fields": "nextPageToken, files(id,name)", "pageSize": 200,
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

def _rapikan(img):
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, UKURAN, Image.LANCZOS, centering=(0.5, 0.5))
    img = ImageEnhance.Brightness(img).enhance(1.05)
    img = ImageEnhance.Contrast(img).enhance(1.06)
    img = ImageEnhance.Color(img).enhance(1.10)
    hangat = Image.new("RGB", UKURAN, (255, 196, 140))
    img = Image.blend(img, hangat, 0.05)
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3))


def _logo(img, lebar_rel=0.30, margin=44, pojok="kanan-bawah"):
    if not os.path.exists(_WORDMARK):
        return img
    logo = Image.open(_WORDMARK).convert("RGBA")
    lebar = int(UKURAN[0] * lebar_rel)
    logo = logo.resize((lebar, int(logo.height * lebar / logo.width)), Image.LANCZOS)
    bayangan = Image.new("RGBA", logo.size, (0, 0, 0, 0))
    bayangan.putalpha(logo.getchannel("A").point(lambda a: int(a * 0.55)))
    bayangan = bayangan.filter(ImageFilter.GaussianBlur(6))
    if pojok == "kanan-bawah":
        pos = (UKURAN[0] - lebar - margin, UKURAN[1] - logo.height - margin)
    else:
        pos = (margin, margin)
    base = img.convert("RGBA")
    base.alpha_composite(bayangan, (pos[0] + 3, pos[1] + 4))
    base.alpha_composite(logo, pos)
    return base.convert("RGB")


def foto_siap_posting(img):
    """Foto tanpa tanggal: rapikan + logo kecil di pojok."""
    return _logo(_rapikan(img))


def _gradasi(img, dari_y, tinggi, alpha_maks):
    lapis = Image.new("RGBA", UKURAN, (0, 0, 0, 0))
    d = ImageDraw.Draw(lapis)
    for i in range(tinggi):
        a = int(alpha_maks * (i / tinggi) ** 1.3)
        d.line([(0, dari_y + i), (UKURAN[0], dari_y + i)], fill=(28, 18, 14, a))
    base = img.convert("RGBA")
    base.alpha_composite(lapis)
    return base


def poster_open_po(img, tutup, kirim, web="", wa=""):
    base = _gradasi(_rapikan(img), 520, UKURAN[1] - 520, 235)
    atas = Image.new("RGBA", UKURAN, (0, 0, 0, 0))
    da = ImageDraw.Draw(atas)
    for i in range(260):
        da.line([(0, i), (UKURAN[0], i)], fill=(28, 18, 14, int(150 * (1 - i / 260))))
    base.alpha_composite(atas)
    d = ImageDraw.Draw(base)
    x = 72

    # label kecil + judul besar
    f_label = _font(_FONT_TEKS, 34, b"ExtraBold")
    label = "PRE-ORDER MINGGU INI"
    lw = d.textlength(label, font=f_label)
    d.rounded_rectangle([x, 760, x + lw + 44, 816], radius=28, fill=PINK)
    d.text((x + 22, 766), label, font=f_label, fill=(60, 20, 34))

    f_judul = _font(_FONT_JUDUL, 190, b"Bold")
    d.text((x + 6, 834 + 6), "OPEN PO", font=f_judul, fill=(0, 0, 0, 110))
    d.text((x, 834), "OPEN PO", font=f_judul, fill=KREM)

    f_info = _font(_FONT_TEKS, 46, b"Bold")
    f_info2 = _font(_FONT_TEKS, 40, b"SemiBold")
    y = 1062
    d.text((x, y), f"Tutup: {_tgl(tutup)}", font=f_info, fill=KREM)
    d.text((x, y + 62), f"Kirim/ambil: {_tgl(kirim)} · {config.DELIVERY_WINDOW.replace(':', '.')}",
           font=f_info2, fill=(236, 210, 175))

    f_kecil = _font(_FONT_TEKS, 34, b"Bold")
    kontak = "   ·   ".join(t for t in (web, f"WA {wa}" if wa else "") if t)
    if kontak:
        d.line([(x, y + 136), (UKURAN[0] - x, y + 136)], fill=(*KARAMEL, 255), width=3)
        d.text((x, y + 152), kontak, font=f_kecil, fill=KREM)

    hasil = base.convert("RGB")
    return _logo(hasil, lebar_rel=0.34, margin=56, pojok="kiri-atas")


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
            "jumlah_folder": len(semua)}


async def kirim_paket(bot, chat_id, sheets, thread_id=None):
    """Bikin paket foto lalu kirim ke chat. Return pesan error atau None."""
    import asyncio
    from telegram import InputMediaDocument
    try:
        import kantor
        kantor.catat("marketing", "Siapin poster Open PO + 7 foto sosmed")
    except Exception:
        pass
    paket = await asyncio.wait_for(asyncio.to_thread(siapkan_paket, sheets), timeout=180)
    if "error" in paket:
        return paket["error"]
    kirim = paket["kirim"]
    await bot.send_document(
        chat_id=chat_id, message_thread_id=thread_id, document=paket["poster"],
        filename=f"OpenPO_{kirim:%Y-%m-%d}.jpg",
        caption=(f"📣 Poster OPEN PO (tutup {_tgl(paket['tutup'])}, kirim {_tgl(kirim)}).\n"
                 "Caption-nya bisa minta lewat /promo."),
    )
    if paket["foto"]:
        media = [InputMediaDocument(media=b, filename=f"MissPiggy_{kirim:%Y%m%d}_{i + 1}.jpg")
                 for i, b in enumerate(paket["foto"])]
        media[-1] = InputMediaDocument(
            media=paket["foto"][-1], filename=f"MissPiggy_{kirim:%Y%m%d}_{len(paket['foto'])}.jpg",
            caption=(f"📸 {len(paket['foto'])} foto siap posting minggu ini (tanpa tanggal), "
                     "ukuran 4:5 buat IG & TikTok. Total foto di folder: "
                     f"{paket['jumlah_folder']}."),
        )
        await bot.send_media_group(chat_id=chat_id, message_thread_id=thread_id, media=media)
    if len(paket["foto"]) < JUMLAH_FOTO:
        await bot.send_message(
            chat_id=chat_id, message_thread_id=thread_id,
            text=(f"ℹ️ Foto di folder baru {paket['jumlah_folder']}, jadi minggu ini cuma "
                  f"{len(paket['foto'])} foto sosmed. Tambah foto di folder biar tiap minggu dapat 7 "
                  "dan nggak cepat berulang."),
        )
    return None
