"""
Helper buat nentuin "Minggu PO" = tanggal Kamis pengiriman yang lagi berjalan.

Logika: PO dibuka Sabtu - Rabu, dikirim Kamis. Jadi kalau hari ini
Sabtu/Minggu/Senin/Selasa/Rabu/Kamis-sebelum-jam-produksi, itu semua masuk
ke Kamis TERDEKAT DI DEPAN (atau hari ini kalau kebetulan hari Kamis).
"""

import datetime
import pytz

import config

WEEKDAY_THU = 3  # Monday=0 ... Sunday=6


def get_timezone():
    return pytz.timezone(config.TIMEZONE)


def current_po_week_thursday(reference: datetime.datetime = None) -> str:
    tz = get_timezone()
    now = reference or datetime.datetime.now(tz)
    days_ahead = (WEEKDAY_THU - now.weekday()) % 7
    thursday = now + datetime.timedelta(days=days_ahead)
    return thursday.strftime("%Y-%m-%d")


def previous_month(year: int, month: int):
    if month == 1:
        return year - 1, 12
    return year, month - 1
