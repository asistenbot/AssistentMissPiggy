"""
Server HTTP kecil buat nerima order dari halaman web (Netlify) dan
masukin ke alur konfirmasi Telegram yang udah ada -- sama persis kayak
kalau admin paste chat customer manual, cuma tanpa perlu tebak-tebakan AI
lagi soalnya datanya udah terstruktur dari form web.

File ini SENGAJA dipisah dari bot.py dan nggak nyentuh logic Sheets /
invoice / surat jalan sama sekali. Yang dilakuin cuma:
1. Terima JSON order dari web (dijaga pake secret token).
2. Nyiapin 'pending_order' persis kayak yang disiapin handle_text().
3. Kirim preview + tombol Konfirmasi/Batal ke Telegram admin.
4. Begitu admin klik Konfirmasi, handle_confirm() di bot.py yang ambil alih
   (kode itu nggak diubah sama sekali).

Jalanin: dipanggil otomatis dari on_startup() di bot.py, nggak perlu
dijalanin manual.
"""

import logging
import os

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config

logger = logging.getLogger(__name__)

WEB_ORDER_SECRET = os.getenv("WEB_ORDER_SECRET", "")


def _build_preview_text(parsed):
    items_text = "\n".join(
        f"  - {i.get('rasa')} ({i.get('kategori')}) x{i.get('qty')}"
        for i in parsed.get("items", [])
    ) or "  (belum ada item terdeteksi)"

    ongkir = parsed.get("ongkir") or 0
    ongkir_text = (
        "Rp" + format(int(ongkir), ",").replace(",", ".")
        if ongkir else "belum diisi (Rp0)"
    )

    return (
        f"*Order Baru dari Web:*\n"
        f"Nama: {parsed.get('nama') or '-'}\n"
        f"No HP: {parsed.get('no_hp') or '-'}\n"
        f"Alamat: {parsed.get('alamat') or '-'}\n"
        f"Metode: {parsed.get('metode') or '-'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {ongkir_text}\n"
        f"Catatan: {parsed.get('catatan') or '-'}\n"
    )


def _build_confirm_keyboard(parsed):
    # Order dari web nggak pernah ada input ongkir dari customer (mereka
    # cuma pilih "Diantar" doang), jadi khusus metode itu kita kasih tombol
    # buat admin isi ongkir DULU sebelum invoice & surat jalan ke-generate.
    # (handle_set_ongkir / handle_ongkir_input ada di bot.py, dipakai bareng
    # sama alur paste-chat manual juga.)
    rows = [[
        InlineKeyboardButton("✅ Simpan & Generate", callback_data="confirm_order"),
        InlineKeyboardButton("❌ Batal", callback_data="cancel_order"),
    ]]
    if (parsed.get("metode") or "").strip().lower() == "diantar":
        rows.append([InlineKeyboardButton("✏️ Isi/Ubah Ongkir", callback_data="set_ongkir")])
    return InlineKeyboardMarkup(rows)


def _parse_items(items_raw):
    items = []
    for it in items_raw or []:
        try:
            kategori = str(it["kategori"]).strip()
            rasa = str(it["rasa"]).strip()
            qty = int(it["qty"])
        except (KeyError, ValueError, TypeError):
            continue
        if kategori and rasa and qty > 0:
            items.append({"kategori": kategori, "rasa": rasa, "qty": qty})
    return items


def create_web_order_app(application):
    """application = instance Application yang sama dari bot.py."""

    async def handle_web_order(request: web.Request):
        secret = request.headers.get("X-Web-Order-Secret", "")
        if not WEB_ORDER_SECRET or secret != WEB_ORDER_SECRET:
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid_json"}, status=400)

        nama = (body.get("nama") or "").strip()
        no_hp = (body.get("no_hp") or "").strip()
        alamat = (body.get("alamat") or "").strip()
        metode = (body.get("metode") or "").strip()
        items = _parse_items(body.get("items"))

        if not (nama and alamat and no_hp and metode and items):
            return web.json_response({"ok": False, "error": "incomplete_order"}, status=400)

        parsed = {
            "nama": nama,
            "no_hp": no_hp,
            "alamat": alamat,
            "metode": metode,
            "items": items,
            "ongkir": int(body.get("ongkir") or 0),
            "catatan": (body.get("catatan") or "").strip() or None,
            "kelengkapan": "lengkap",
        }

        preview = _build_preview_text(parsed)
        keyboard = _build_confirm_keyboard(parsed)

        owner_ids = config.OWNER_TELEGRAM_IDS or []
        if not owner_ids:
            logger.error("Web order masuk tapi OWNER_TELEGRAM_IDS kosong, nggak ada yang dikirimin.")
            return web.json_response({"ok": False, "error": "no_owner_configured"}, status=500)

        # Siapin pending_order buat SEMUA admin (siapa pun yang mijit tombol
        # Konfirmasi nanti, dari HP mana pun, datanya udah ada).
        #
        # CATATAN: application.user_data itu read-only (MappingProxyType) di
        # python-telegram-bot 21.x kalau diakses dari luar handler biasa --
        # sengaja dibikin gitu biar plugin/luar nggak asal ubah. Storage
        # aslinya ada di application._user_data (defaultdict(dict)), jadi
        # buat nulis dari luar handler context, kita akses langsung ke situ.
        for owner_id in owner_ids:
            application._user_data[owner_id]["pending_order"] = parsed

        # Kirim preview-nya ke GRUP admin kalau ada (biar kelihatan bareng di
        # semua HP), kalau GROUP_CHAT_ID belum di-set baru fallback kirim
        # satu-satu ke tiap admin secara pribadi.
        sent_to_anyone = False
        targets = [config.GROUP_CHAT_ID] if config.GROUP_CHAT_ID else owner_ids
        for chat_id in targets:
            try:
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=preview,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
                sent_to_anyone = True
            except Exception as e:
                logger.error(f"Gagal kirim order web ke chat {chat_id}: {e}")

        if not sent_to_anyone:
            return web.json_response({"ok": False, "error": "failed_to_notify_owner"}, status=502)

        return web.json_response({"ok": True})

    async def handle_health(request: web.Request):
        return web.json_response({"ok": True, "service": "web-order"})

    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as exc:
                # aiohttp punya beberapa error "normal" yang dilempar sebagai
                # exception (mis. 404). Tetep dianggap response biasa biar
                # header CORS di bawah nempel.
                resp = exc
            except Exception:
                # Error nggak terduga (kayak bug mappingproxy kemarin) --
                # jangan biarin request mati begitu aja tanpa header CORS,
                # soalnya kalau gitu browser cuma bakal bilang "gagal
                # terhubung" padahal request-nya sebenernya nyampe.
                logger.exception("Unhandled error di web-order handler")
                resp = web.json_response({"ok": False, "error": "internal_error"}, status=500)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Web-Order-Secret"
        return resp

    web_app = web.Application(middlewares=[cors_middleware])
    web_app.router.add_post("/web-order", handle_web_order)
    web_app.router.add_route("OPTIONS", "/web-order", handle_web_order)
    web_app.router.add_get("/health", handle_health)
    return web_app


async def start_web_order_server(application):
    web_app = create_web_order_app(application)
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web order server jalan di port {port}")
