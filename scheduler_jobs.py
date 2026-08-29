"""
Jadwal otomatis:
- Tanggal 1 tiap bulan jam 09:00 WIB -> kirim laporan bulanan supplier (bulan sebelumnya)

(Auto rekap produksi mingguan tiap Rabu SUDAH DIMATIKAN atas permintaan admin --
sekarang admin minta rekap produksi manual aja lewat /rekap kapan pun perlu.)
"""
import datetime
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import config
import date_helpers
import documents
from sheets_client import get_sheets_client
logger = logging.getLogger(__name__)
def setup_scheduler(bot):
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    scheduler.add_job(
        send_auto_monthly_report,
        CronTrigger(day=config.MONTHLY_REPORT_DAY, hour=config.MONTHLY_REPORT_HOUR,
                    minute=config.MONTHLY_REPORT_MINUTE, timezone=config.TIMEZONE),
        args=[bot],
        id="auto_monthly_report",
    )
    scheduler.start()
    return scheduler
def _target_chat_ids():
    """Kalau GROUP_CHAT_ID di-setting, kirim ke situ aja (1x, semua admin
    liat bareng). Kalau nggak, fallback ke kirim ke tiap admin satu-satu."""
    if config.GROUP_CHAT_ID:
        return [config.GROUP_CHAT_ID]
    return config.OWNER_TELEGRAM_IDS
async def send_auto_recap(bot):
    try:
        sheets = get_sheets_client()
        minggu_po = date_helpers.current_po_week_thursday()
        orders = sheets.get_orders_by_week(minggu_po)
        text = documents.build_production_recap(minggu_po, orders)
        for chat_id in _target_chat_ids():
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Gagal kirim auto recap")
async def send_auto_monthly_report(bot):
    try:
        sheets = get_sheets_client()
        tz = date_helpers.get_timezone()
        now = datetime.datetime.now(tz)
        year, month = date_helpers.previous_month(now.year, now.month)
        orders = sheets.get_orders_by_month(year, month)
        dough_price_map = sheets.get_dough_price_map()
        periode_label = documents.format_periode_label(year, month, year, month)
        text = documents.build_monthly_supplier_report(periode_label, orders, dough_price_map)
        for chat_id in _target_chat_ids():
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Gagal kirim auto laporan bulanan")
