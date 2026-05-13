"""Proactive scheduling — daily budget-overage alerts + weekly monthly review.

Uses python-telegram-bot's JobQueue (which itself uses APScheduler under the hood when
installed via the [job-queue] extra). The bot remains a polling Telegram client; the
JobQueue is the same event loop, so we don't introduce a second runtime.

Configure via env:
  BOT_PROACTIVE_ENABLED   default true; set to 0/false to disable.
  BOT_TZ                  default Asia/Ho_Chi_Minh.
  BOT_DAILY_CHECK_HOUR    local hour (0-23) for budget alerts, default 9.
  BOT_WEEKLY_REVIEW_DOW   0=Mon … 6=Sun for the weekly review, default 0 (Monday).
  BOT_WEEKLY_REVIEW_HOUR  local hour for the weekly review, default 8.
"""
import logging
import os
from datetime import datetime, time
from typing import Optional

from telegram.ext import Application, ContextTypes

from budgets import over_budget_alerts
from db import connect
from ledger import all_user_chats
from review import build_monthly_review

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        n = int(os.getenv(name) or default)
    except ValueError:
        n = default
    return max(lo, min(hi, n))


def _tz():
    name = (os.getenv("BOT_TZ") or "Asia/Ho_Chi_Minh").strip()
    try:
        from zoneinfo import ZoneInfo  # py3.9+

        return ZoneInfo(name)
    except Exception:
        try:
            import pytz

            return pytz.timezone(name)
        except Exception:
            logger.warning("falling back to naive local time; BOT_TZ=%s unresolved", name)
            return None


def _period_key_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _period_key_week() -> str:
    iso = datetime.now().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _already_sent(user_id: int, kind: str, period_key: str) -> bool:
    cur = connect().cursor()
    cur.execute(
        "SELECT 1 FROM proactive_alerts WHERE user_id=? AND kind=? AND period_key=?",
        (user_id, kind, period_key),
    )
    return cur.fetchone() is not None


def _mark_sent(user_id: int, kind: str, period_key: str) -> None:
    c = connect()
    c.execute(
        """
        INSERT OR IGNORE INTO proactive_alerts (user_id, kind, period_key)
        VALUES (?, ?, ?)
        """,
        (user_id, kind, period_key),
    )
    c.commit()


async def _send(application: Application, chat_id: int, text: str) -> bool:
    try:
        await application.bot.send_message(chat_id=chat_id, text=text)
        return True
    except Exception as e:
        logger.warning("proactive send failed chat_id=%s: %s", chat_id, e)
        return False


async def daily_budget_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    period = _period_key_month()
    for user_id, chat_id in all_user_chats():
        try:
            alerts = over_budget_alerts(user_id)
        except Exception as e:
            logger.exception("budget check failed user=%s: %s", user_id, e)
            continue
        for a in alerts:
            kind = f"budget:{a['category']}"
            if _already_sent(user_id, kind, period):
                continue
            msg = (
                "⚠ Budget alert\n"
                f"Category: {a['category']}\n"
                f"Spent: {a['spent']:,.0f} / {a['monthly_limit']:,.0f} ({a['percent']:.0f}%)\n"
                f"Projected month-end: {a['projected_monthend']:,.0f}"
            )
            if await _send(app, chat_id, msg):
                _mark_sent(user_id, kind, period)


async def weekly_review_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    app = context.application
    period = _period_key_week()
    for user_id, chat_id in all_user_chats():
        if _already_sent(user_id, "weekly_review", period):
            continue
        try:
            text = build_monthly_review(user_id)
        except Exception as e:
            logger.exception("review build failed user=%s: %s", user_id, e)
            continue
        if await _send(app, chat_id, text):
            _mark_sent(user_id, "weekly_review", period)


def attach(application: Application) -> None:
    if not _env_bool("BOT_PROACTIVE_ENABLED", True):
        logger.info("proactive scheduler disabled (BOT_PROACTIVE_ENABLED)")
        return
    jq = application.job_queue
    if jq is None:
        logger.warning(
            "JobQueue not available — install python-telegram-bot[job-queue] for proactive features"
        )
        return

    tz = _tz()
    daily_hour = _env_int("BOT_DAILY_CHECK_HOUR", 9, 0, 23)
    weekly_dow = _env_int("BOT_WEEKLY_REVIEW_DOW", 0, 0, 6)
    weekly_hour = _env_int("BOT_WEEKLY_REVIEW_HOUR", 8, 0, 23)

    jq.run_daily(
        daily_budget_check,
        time=time(hour=daily_hour, tzinfo=tz),
        name="daily_budget_check",
    )
    jq.run_daily(
        weekly_review_job,
        time=time(hour=weekly_hour, tzinfo=tz),
        days=(weekly_dow,),
        name="weekly_review_job",
    )
    logger.info(
        "scheduler attached: daily %02d:00, weekly dow=%d %02d:00 tz=%s",
        daily_hour,
        weekly_dow,
        weekly_hour,
        tz or "naive",
    )
