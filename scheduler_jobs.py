"""
Jadwal otomatis:
- Tiap Rabu jam 15:00, 16:00, 19:00 WIB -> kirim rekap produksi ke owner
- Tanggal 1 tiap bulan jam 09:00 WIB -> kirim laporan bulanan supplier (bulan sebelumnya)
"""

import datetime
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import date_helpers
import documents
from sheets_client import SheetsClient

logger = logging.getLogger(__name__)


def setup_scheduler(bot):
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    for hour, minute in config.AUTO_RECAP_TIMES:
        scheduler.add_job(
            send_auto_recap,
            CronTrigger(day_of_week=config.PO_CUTOFF_DAY, hour=hour, minute=minute,
                        timezone=config.TIMEZONE),
            args=[bot],
            id=f"auto_recap_{hour}{minute}",
        )

    scheduler.add_job(
        send_auto_monthly_report,
        CronTrigger(day=config.MONTHLY_REPORT_DAY, hour=config.MONTHLY_REPORT_HOUR,
                    minute=config.MONTHLY_REPORT_MINUTE, timezone=config.TIMEZONE),
        args=[bot],
        id="auto_monthly_report",
    )

    scheduler.start()
    return scheduler


async def send_auto_recap(bot):
    try:
        sheets = SheetsClient()
        minggu_po = date_helpers.current_po_week_thursday()
        orders = sheets.get_orders_by_week(minggu_po)
        text = documents.build_production_recap(minggu_po, orders)
        await bot.send_message(chat_id=config.OWNER_TELEGRAM_ID, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Gagal kirim auto recap")


async def send_auto_monthly_report(bot):
    try:
        sheets = SheetsClient()
        tz = date_helpers.get_timezone()
        now = datetime.datetime.now(tz)
        year, month = date_helpers.previous_month(now.year, now.month)
        orders = sheets.get_orders_by_month(year, month)
        dough_price_map = sheets.get_dough_price_map()
        text = documents.build_monthly_supplier_report(year, month, orders, dough_price_map)
        await bot.send_message(chat_id=config.OWNER_TELEGRAM_ID, text=text, parse_mode="Markdown")
    except Exception:
        logger.exception("Gagal kirim auto laporan bulanan")
