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
import uuid

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import config

logger = logging.getLogger(__name__)

WEB_ORDER_SECRET = os.getenv("WEB_ORDER_SECRET", "")


def _is_delivery_metode(metode):
    """True kalau metode-nya berarti 'dikirim'. Order dari web selalu literal
    'Diantar', tapi helper ini disamain sama versi di bot.py/receipt.py (yang
    juga nerima 'Kirim' dari AI parser chat manual) biar konsisten kalau
    suatu saat vocab web ikut berubah juga."""
    m = (metode or "").strip().lower()
    return "antar" in m or "kirim" in m


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
        f"Tanggal Kirim: {parsed.get('tanggal_kirim') or '(default: Kamis PO minggu ini)'}\n"
        f"Items:\n{items_text}\n"
        f"Ongkir: {ongkir_text}\n"
        f"Catatan: {parsed.get('catatan') or '-'}\n"
    )


def _build_confirm_keyboard(parsed, order_id):
    # Order dari web nggak pernah ada input ongkir dari customer (mereka
    # cuma pilih "Diantar" doang), jadi khusus metode itu kita kasih tombol
    # buat admin isi ongkir DULU sebelum invoice & surat jalan ke-generate.
    # (handle_set_ongkir / handle_ongkir_input ada di bot.py, dipakai bareng
    # sama alur paste-chat manual juga.)
    #
    # order_id nempel di callback_data (samain formatnya sama
    # build_confirm_keyboard di bot.py: "confirm_order:<id>" dst) -- biar
    # tombol di order INI tetep nunjuk ke order INI sendiri walau ada order
    # web/manual LAIN yang numpuk nunggu diproses bareng. Tanpa ini, semua
    # order dari web bakal rebutan 1 slot 'pending_order' yang sama, dan
    # tombol di order lama bisa nyasar/ilang begitu order baru dateng.
    rows = [[
        InlineKeyboardButton("✅ Simpan & Generate", callback_data=f"confirm_order:{order_id}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"cancel_order:{order_id}"),
    ]]
    if _is_delivery_metode(parsed.get("metode")):
        rows.append([InlineKeyboardButton("✏️ Isi/Ubah Ongkir", callback_data=f"set_ongkir:{order_id}")])
    return InlineKeyboardMarkup(rows)


def _target_order(owner_ids):
    """Daftar (chat_id, message_thread_id) TUJUAN buat preview order BARU
    dari web -- disamain sama prioritas _tujuan_order() versi bot.py, biar
    order dari web & order dari paste/screenshot manual nyampe di TEMPAT
    YANG SAMA (topic 'ORDER' kalau di-setting, atau grup biasa, atau
    fallback DM ke tiap owner kalau GROUP_CHAT_ID belum ada sama sekali):
      1) TOPIC_ID_ORDER (topic di GROUP_CHAT_ID yang sama)
      2) GROUP_CHAT_ID_ORDER (grup terpisah)
      3) GROUP_CHAT_ID biasa (perilaku lama, tanpa topic)
      4) fallback: DM ke tiap owner satu-satu (kalau GROUP_CHAT_ID kosong)."""
    topic_id = getattr(config, "TOPIC_ID_ORDER", None)
    if topic_id and config.GROUP_CHAT_ID:
        return [(config.GROUP_CHAT_ID, topic_id)]
    group_chat_id_order = getattr(config, "GROUP_CHAT_ID_ORDER", None)
    if group_chat_id_order:
        return [(group_chat_id_order, None)]
    if config.GROUP_CHAT_ID:
        return [(config.GROUP_CHAT_ID, None)]
    return [(oid, None) for oid in owner_ids]


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

        owner_ids = config.OWNER_TELEGRAM_IDS or []
        if not owner_ids:
            logger.error("Web order masuk tapi OWNER_TELEGRAM_IDS kosong, nggak ada yang dikirimin.")
            return web.json_response({"ok": False, "error": "no_owner_configured"}, status=500)

        # Tiap order (web ATAU paste-chat manual di bot.py) disimpen dengan
        # ID sendiri-sendiri di bot_data["pending_orders"][order_id] --
        # BUKAN 1 slot tunggal kayak desain lama ("pending_order" doang).
        # bot_data itu satu tempat yang sama buat SEMUA admin/HP (jadi siapa
        # pun yang mijit tombol duluan bisa langsung kepake, sesuai 'tetep
        # bisa dihandle pake 2 hp'), DAN yang paling penting: kalau ada
        # beberapa order numpuk (misal beberapa customer submit lewat web
        # hampir bareng), mereka nggak saling timpa/ilang -- masing-masing
        # tombolnya bawa order_id sendiri (liat _build_confirm_keyboard).
        #
        # bot_data itu dict biasa yang mutable (beda sama user_data yang
        # read-only proxy kalau diakses dari luar handler), jadi aman ditulis
        # langsung kayak gini.
        order_id = uuid.uuid4().hex[:8]
        application.bot_data.setdefault("pending_orders", {})[order_id] = parsed
        application.bot_data["active_pending_order_id"] = order_id

        preview = _build_preview_text(parsed)
        keyboard = _build_confirm_keyboard(parsed, order_id)

        # Kirim preview-nya ke GRUP admin kalau ada (biar kelihatan bareng di
        # semua HP) -- ke topic/grup 'ORDER' khusus kalau udah di-setting
        # (_target_order), biar order dari web nyatu di tempat yang sama
        # sama order dari paste/screenshot manual. Kalau GROUP_CHAT_ID
        # belum di-set sama sekali, fallback kirim satu-satu ke tiap admin
        # secara pribadi (perilaku lama).
        sent_to_anyone = False
        for chat_id, thread_id in _target_order(owner_ids):
            try:
                sent = await application.bot.send_message(
                    chat_id=chat_id,
                    text=preview,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                    message_thread_id=thread_id,
                )
                sent_to_anyone = True
                # Simpen id pesan preview PERTAMA yang berhasil kekirim --
                # dipakai bot.py (_send_or_update_preview) biar koreksi teks
                # bebas belakangan bisa EDIT kartu ini di tempat, bukan
                # numpuk kirim kartu baru (sama kayak fix buat order dari
                # paste-chat/screenshot manual). Kalau order ini dikirim ke
                # BEBERAPA chat sekaligus (fallback DM per-admin waktu
                # GROUP_CHAT_ID belum di-setting), yang kesimpen cuma target
                # pertama -- admin lain tetep dapet notifikasi normal, cuma
                # kartu MEREKA nggak ikut ke-edit otomatis kalau ada
                # koreksi (fallback-nya kirim pesan baru kayak sebelumnya,
                # nggak ada regresi).
                if "_preview_chat_id" not in parsed:
                    parsed["_preview_chat_id"] = sent.chat_id
                    parsed["_preview_msg_id"] = sent.message_id
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
