"""
Video pendek 9:16 (Reels / TikTok) dari foto-foto roti.

Tiap foto: efek zoom pelan (Ken Burns) + kotak tulisan nama roti + logo di
tengah atas, pindah antar foto pakai crossfade. Penutup: kartu "Yuk ikutan
PO!" dengan tanggal tutup & kirim + link order + WA.

Gambar disusun pakai Pillow, lalu di-encode jadi MP4 (H.264) oleh ffmpeg
lewat pipe. TANPA AI. Lagu dipilih sendiri di aplikasi waktu posting.
"""

import io
import logging
import os
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFilter, ImageOps

import foto_mingguan as fm

logger = logging.getLogger(__name__)

VW, VH = 1080, 1920
FPS = 24
DETIK_FOTO = 2.8
DETIK_TRANSISI = 0.5
DETIK_PENUTUP = 3.2


def _bungkus(draw, teks, font, lebar):
    kata, baris, skr = str(teks).split(), [], ""
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


def _dasar(img):
    """Foto dirapikan (terang/warna sama seperti foto mingguan), dibuat 12%
    lebih besar dari layar biar ada ruang buat zoom & geser pelan."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, (int(VW * 1.12), int(VH * 1.12)), Image.LANCZOS)
    return _rapikan_bebas(img)


def _rapikan_bebas(img):
    from PIL import ImageEnhance
    img = fm._terang_otomatis(img)
    img = ImageEnhance.Contrast(img).enhance(1.04)
    img = ImageEnhance.Color(img).enhance(1.10)
    img = Image.blend(img, Image.new("RGB", img.size, (255, 196, 140)), 0.05)
    return img.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=3))


def _lapisan_teks(judul, teks, posisi_bawah=True):
    """Lapisan transparan: kotak tulisan nama roti (di bawah)."""
    lap = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    d = ImageDraw.Draw(lap)
    m = 64
    f_j = fm._font(fm._FONT_JUDUL, 70, b"SemiBold")
    f_t = fm._font(fm._FONT_TEKS, 36, b"SemiBold")
    lebar_maks = VW - 2 * m - 80
    bj = _bungkus(d, judul, f_j, lebar_maks)[:2]
    bt = _bungkus(d, teks, f_t, lebar_maks)[:3] if teks else []
    tj, tt = 84, 50
    tinggi = 44 + len(bj) * tj + (12 + len(bt) * tt if bt else 0) + 36
    lebar_isi = max([d.textlength(x, font=f_j) for x in bj] + [d.textlength(x, font=f_t) for x in bt] + [240])
    lebar = min(VW - 2 * m, int(lebar_isi) + 96)
    x0 = (VW - lebar) // 2
    y0 = VH - 260 - tinggi if posisi_bawah else 300
    bayang = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    ImageDraw.Draw(bayang).rounded_rectangle([x0 + 4, y0 + 10, x0 + lebar + 4, y0 + tinggi + 10],
                                             radius=36, fill=(0, 0, 0, 60))
    lap.alpha_composite(bayang.filter(ImageFilter.GaussianBlur(12)))
    d = ImageDraw.Draw(lap)
    d.rounded_rectangle([x0, y0, x0 + lebar, y0 + tinggi], radius=36, fill=(251, 244, 233, 236))
    y = y0 + 38
    for b in bj:
        lw = d.textlength(b, font=f_j)
        d.text(((VW - lw) / 2, y), b, font=f_j, fill=fm.COKLAT)
        y += tj
    if bt:
        y += 12
        for b in bt:
            lw = d.textlength(b, font=f_t)
            d.text(((VW - lw) / 2, y), b, font=f_t, fill=(120, 86, 60))
            y += tt
    return lap


def _kartu_penutup(latar, tutup, kirim, web, wa):
    base = latar.resize((VW, VH)).filter(ImageFilter.GaussianBlur(22))
    base = Image.blend(base, Image.new("RGB", (VW, VH), (31, 32, 33)), 0.72).convert("RGBA")
    fm._tempel_logo(base, 480, (VW // 2, 420), jangkar="tengah-atas")
    d = ImageDraw.Draw(base)

    def tengah(teks, y, font, warna):
        lw = d.textlength(teks, font=font)
        d.text(((VW - lw) / 2, y), teks, font=font, fill=warna)

    tengah("Yuk ikutan PO!", 820, fm._font(fm._FONT_JUDUL, 100, b"SemiBold"), fm.KREM)
    f = fm._font(fm._FONT_TEKS, 42, b"Bold")
    tengah(f"Tutup {fm._tgl(tutup)}", 990, f, (236, 210, 175))
    tengah(f"Kirim & ambil {fm._tgl(kirim)}", 1050, f, (236, 210, 175))
    f2 = fm._font(fm._FONT_TEKS, 38, b"ExtraBold")
    for i, t in enumerate([x for x in (web, f"WA {wa}" if wa else "") if x]):
        lw = d.textlength(t, font=f2)
        y = 1200 + i * 110
        d.rounded_rectangle([(VW - lw) / 2 - 40, y, (VW + lw) / 2 + 40, y + 82], radius=41, fill=fm.KARAMEL)
        tengah(t, y + 18, f2, (255, 250, 242))
    return base.convert("RGB")


def _frame_zoom(dasar, t, arah):
    """t 0..1 -> potongan foto dengan zoom pelan (masuk / keluar bergantian)."""
    w, h = dasar.size
    skala = 1.0 + 0.10 * (t if arah == 0 else 1 - t)
    cw = w / 1.12 / skala
    ch = cw * VH / VW
    geser_x = (w - cw) * (0.5 + 0.3 * (t - 0.5) * (1 if arah == 0 else -1))
    geser_y = (h - ch) * 0.5
    return dasar.resize((VW, VH), Image.BILINEAR, box=(geser_x, geser_y, geser_x + cw, geser_y + ch))


def _lapisan_logo(contoh_frame, lebar=260, y=110):
    """Logo tengah atas (transparan, warna otomatis krem/gelap sesuai foto)
    dibuat SEKALI per foto sebagai lapisan, biar nggak dihitung tiap frame."""
    from PIL import ImageStat
    lap = Image.new("RGBA", (VW, VH), (0, 0, 0, 0))
    if not (os.path.exists(fm._LOGO_KREM) and os.path.exists(fm._LOGO_GELAP)):
        return lap
    tinggi = fm.tinggi_logo(lebar)
    x = (VW - lebar) // 2
    area = contoh_frame.crop((x, y, x + lebar, y + tinggi)).convert("L")
    terang = ImageStat.Stat(area).mean[0] > 140
    logo = Image.open(fm._LOGO_GELAP if terang else fm._LOGO_KREM).convert("RGBA").resize((lebar, tinggi), Image.LANCZOS)
    warna_halo = (255, 250, 242) if terang else (0, 0, 0)
    halo = Image.new("RGBA", (lebar + 40, tinggi + 40), (*warna_halo, 0))
    alpha = logo.getchannel("A").point(lambda v: int(v * (0.55 if terang else 0.6)))
    halo.paste(Image.new("RGBA", logo.size, (*warna_halo, 255)), (20, 20), alpha)
    lap.alpha_composite(halo.filter(ImageFilter.GaussianBlur(7)), (x - 20, y - 18))
    lap.alpha_composite(logo, (x, y))
    return lap


def buat_video(gambar_list, judul_list, teks_list, tutup, kirim, web="", wa=""):
    """gambar_list: list PIL Image (foto asli). Return BytesIO berisi MP4."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg belum terpasang di server")
    dasar = [_dasar(g) for g in gambar_list]
    lapisan = []
    for i, (j, t) in enumerate(zip(judul_list, teks_list)):
        lap = _lapisan_logo(_frame_zoom(dasar[i], 0, i % 2))
        lap.alpha_composite(_lapisan_teks(j, t))
        lapisan.append(lap)
    penutup = _kartu_penutup(dasar[0], tutup, kirim, web, wa)

    n_foto = int(DETIK_FOTO * FPS)
    n_trans = int(DETIK_TRANSISI * FPS)
    n_tutup = int(DETIK_PENUTUP * FPS)

    with tempfile.TemporaryDirectory() as tmp:
        keluar = os.path.join(tmp, "video.mp4")
        proses = subprocess.Popen(
            [ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{VW}x{VH}", "-r", str(FPS), "-i", "-",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", keluar],
            stdin=subprocess.PIPE,
        )

        def tulis(img):
            proses.stdin.write(img.convert("RGB").tobytes())

        def bingkai(i, k, total):
            fr = _frame_zoom(dasar[i], k / max(1, total - 1), i % 2).convert("RGBA")
            fr.alpha_composite(lapisan[i])
            return fr.convert("RGB")

        sebelumnya = None
        try:
            for i in range(len(dasar)):
                for k in range(n_foto):
                    fr = bingkai(i, k, n_foto)
                    if sebelumnya is not None and k < n_trans:
                        fr = Image.blend(sebelumnya, fr, (k + 1) / n_trans)
                    tulis(fr)
                    if k == n_foto - 1:
                        sebelumnya = fr
            for k in range(n_tutup):
                fr = penutup
                if k < n_trans:
                    fr = Image.blend(sebelumnya, penutup, (k + 1) / n_trans)
                tulis(fr)
        finally:
            proses.stdin.close()
            proses.wait(timeout=300)
        if proses.returncode != 0:
            raise RuntimeError("ffmpeg gagal bikin video")
        with open(keluar, "rb") as f:
            data = io.BytesIO(f.read())
    data.seek(0)
    return data
