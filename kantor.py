"""
Kantor virtual Miss Piggy: halaman pixel-art yang nunjukin "karyawan" bot
lagi ngerjain apa, LIVE dari aktivitas bot yang beneran.

Cara kerja:
- bot.py / web_order_server.py manggil kantor.catat(agent, teks) tiap ada
  kejadian (order dibaca, order tersimpan, /lunas, /rekap, dst).
- Kejadian disimpen di MEMORI (100 terakhir) -- hilang kalau bot restart,
  sengaja, biar nggak nambah beban ke Google Sheets.
- Halaman /kantor?k=<KANTOR_TOKEN> nanya /kantor/events tiap beberapa detik
  dan nganimasiin karakter sesuai kejadian baru.

Agent yang ada:
- "order"    = Pembaca Order (baca chat/screenshot/order web, simpan order)
- "kasir"    = Kasir (invoice, /lunas, /belumbayar)
- "produksi" = Produksi (rekap, surat jalan, /kirim, laporan bulanan)
- "keuangan" = Keuangan (/untung)
- "pelanggan" = Pelanggan Setia (/pelanggan)

Keamanan: halaman & data cuma bisa dibuka pakai token rahasia
(env KANTOR_TOKEN). Kalau env-nya kosong, fitur ini MATI total (404).
Nggak ada biaya AI sama sekali -- cuma baca catatan di memori + Sheets
(di-cache 60 detik).
"""

import asyncio
import collections
import hmac
import itertools
import logging
import os
import time

from aiohttp import web

logger = logging.getLogger(__name__)

KANTOR_TOKEN = os.getenv("KANTOR_TOKEN", "")
AGENTS = ("order", "kasir", "produksi", "keuangan", "pelanggan")

_events = collections.deque(maxlen=100)
_counter = itertools.count(1)
_stats_cache = {"ts": 0.0, "data": None}
_stats_lock = asyncio.Lock()

_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kantor.html")


def catat(agent: str, teks: str):
    """Catat 1 kejadian. AMAN dipanggil dari mana aja -- nggak pernah raise,
    biar kantor virtual nggak mungkin bikin alur order ikut error."""
    try:
        if agent not in AGENTS:
            agent = "order"
        _events.append({
            "id": next(_counter),
            "ts": int(time.time()),
            "agent": agent,
            "teks": str(teks)[:140],
        })
    except Exception:
        pass


def _authorized(request: web.Request) -> bool:
    k = request.query.get("k", "")
    return bool(KANTOR_TOKEN) and hmac.compare_digest(k, KANTOR_TOKEN)


def _hitung_stats():
    # Import di sini biar modul ini bisa di-import tanpa kredensial Sheets.
    import date_helpers
    import documents
    from sheets_client import get_sheets_client

    sheets = get_sheets_client()
    minggu_po = date_helpers.current_po_week_thursday()
    orders = sheets.get_orders_by_week(minggu_po)
    customer = {str(o.get("Nama_Customer", "")).strip().lower() for o in orders} - {""}
    belum = sheets.get_payment_groups()
    return {
        "minggu_po": minggu_po,
        "order_minggu_ini": len(customer),
        "belum_lunas": len(belum),
        "total_belum_lunas": sum(documents.hitung_total_order(g["orders"]) for g in belum),
    }


async def _get_stats():
    async with _stats_lock:
        if _stats_cache["data"] is None or time.time() - _stats_cache["ts"] > 60:
            try:
                _stats_cache["data"] = await asyncio.wait_for(asyncio.to_thread(_hitung_stats), timeout=20)
            except Exception as e:
                logger.warning(f"Kantor: gagal hitung stats: {e}")
            _stats_cache["ts"] = time.time()
        return _stats_cache["data"]


async def handle_page(request: web.Request):
    if not _authorized(request):
        raise web.HTTPNotFound()
    with open(_HTML_PATH, encoding="utf-8") as f:
        html = f.read()
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_events(request: web.Request):
    if not _authorized(request):
        raise web.HTTPNotFound()
    try:
        since = int(request.query.get("since", "0"))
    except ValueError:
        since = 0
    events = [e for e in _events if e["id"] > since]
    return web.json_response(
        {"events": events, "stats": await _get_stats(), "now": int(time.time())},
        headers={"Cache-Control": "no-store"},
    )


def daftar_route(web_app: web.Application):
    web_app.router.add_get("/kantor", handle_page)
    web_app.router.add_get("/kantor/events", handle_events)
