"""Telegram entry point — command handlers + glue. Business logic lives in modules:
db, ledger, budgets, recurring, review, coach, router, scheduler, kb, proxy, web_tools.
"""
import logging
import os
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import scheduler
import sheet_sync
from budgets import clear_budget, format_budget_status_message, set_budget
from coach import ai_chat
from db import connect
from ledger import (
    add_transaction,
    get_recent_transactions,
    get_user_sheet_row,
    get_user_summary,
    get_user_transactions_asc,
    last_transaction_snapshot,
    record_user_chat,
    save_user_sheet,
)
from parsing import parse_add_command, parse_amount
from review import build_monthly_review
from router import ai_router

load_dotenv()
TOKEN = os.getenv("TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

if not TOKEN or not PROXY_URL:
    raise Exception("Missing TOKEN or PROXY_URL in .env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Silence httpx GETs/POSTs on every Telegram poll cycle.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Boot the database so schema is created before any handler fires.
connect()

pending: dict[int, dict] = {}


# ---------------- HELP ----------------
def help_text() -> str:
    return """
💰 Finance Bot

📌 Quick add:
• /add expense 50k ăn trưa
• /add income 10tr lương

📌 Natural input (AI auto):
• ăn 50k
• chơi game 100k
• lương 10 triệu

📌 Ask AI coach:
• tôi nên tiết kiệm thế nào?
• tôi chi tiêu có ổn không?
• Coach có thể tra cứu thêm gợi ý & chi tiết giao dịch qua công cụ tìm kiếm nội bộ.

📌 Commands:
• /add → thêm chi tiêu nhanh
• /summary → tổng thu chi
• /history → giao dịch gần nhất
• /budget → list budgets; /budget set <cat> <amount>; /budget clear <cat>
• /review → monthly review (totals, top categories, budgets, recurring, forecast)
• /sheet → link Google Sheet (when configured)
• /linksheet → connect a sheet you created (share bot service account as Editor)
• /reset_chat → xóa lịch sử hội thoại
"""


# ---------------- SHEET HELPERS ----------------
def ensure_google_sheet(user_id, display_name: str) -> Tuple[Optional[str], Optional[str]]:
    if not sheet_sync.sheets_available():
        return None, None
    row = get_user_sheet_row(user_id)
    if row:
        return row[1], None

    auto = os.getenv("GOOGLE_SHEETS_AUTO_CREATE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if auto:
        try:
            sid, url = sheet_sync.create_user_spreadsheet(str(display_name))
            save_user_sheet(user_id, sid, url)
            backfill = get_user_transactions_asc(user_id)
            if backfill:
                sheet_sync.backfill_transactions(sid, list(backfill))
            return url, None
        except Exception as e:
            log.exception("sheet create error: %s", e)
            return None, str(e)

    email = sheet_sync.service_account_email() or "(client_email in google-service-account.json)"
    hint = (
        "Google Sheet not linked yet.\n\n"
        "1) Create a new Google Sheet in your Drive.\n"
        f"2) Share → add this account as Editor:\n{email}\n"
        "3) Run:\n/linksheet <paste the sheet URL or spreadsheet ID here>"
    )
    return None, hint


def append_row_to_user_sheet(user_id, time_str: str, tx_type: str, amount: float, category: str) -> None:
    row = get_user_sheet_row(user_id)
    if not row:
        return
    spreadsheet_id = row[0]
    try:
        sheet_sync.append_transaction(spreadsheet_id, time_str, tx_type, amount, category)
    except Exception as e:
        log.warning("sheet append error: %s", e)


# ---------------- HANDLERS: meta ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user_chat(user.id, update.effective_chat.id)
    msg = (
        "👋 Finance bot ready.\n\nLog transactions naturally (e.g. \"ăn 50k\") "
        "or ask your finance coach anything. Type /help for more."
    )
    url, sheet_err = ensure_google_sheet(user.id, user.username or str(user.id))
    if url:
        msg += f"\n\n📗 Google Sheet (sync): {url}"
    elif sheet_err:
        msg += f"\n\n📗 {sheet_err}"
    await update.message.reply_text(msg)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    record_user_chat(update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(help_text())


async def reset_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from chat_history import clear_chat_history

    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    clear_chat_history(user_id)
    await update.message.reply_text("🧹 Conversation history cleared.")


async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    rows = get_user_summary(user_id)
    income = rows.get("income", 0)
    expense = rows.get("expense", 0)
    await update.message.reply_text(
        f"📊 SUMMARY\n\n"
        f"Income:  {income:,.0f}\n"
        f"Expense: {expense:,.0f}\n"
        f"Balance: {income - expense:,.0f}"
    )


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    rows = get_recent_transactions(user_id, limit=10)
    text = "📜 LAST TRANSACTIONS\n\n"
    for r in rows:
        text += f"{r[3]}\n{r[0].upper()} {r[1]:,.0f} ({r[2]})\n\n"
    if not rows:
        text += "(none yet)"
    await update.message.reply_text(text)


# ---------------- HANDLERS: sheet ----------------
async def sheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_user_chat(user.id, update.effective_chat.id)
    url, sheet_err = ensure_google_sheet(user.id, user.username or str(user.id))
    if url:
        await update.message.reply_text(f"📗 Your spreadsheet:\n{url}")
    elif sheet_err:
        await update.message.reply_text(f"📗 Google Sheets\n\n{sheet_err}")
    else:
        await update.message.reply_text(
            "Google Sheets is not configured. Put your service-account JSON as "
            "google-service-account.json in the bot folder, or set GOOGLE_SERVICE_ACCOUNT_FILE in .env. "
            "Install: google-auth + google-api-python-client."
        )


async def linksheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not sheet_sync.sheets_available():
        await update.message.reply_text(
            "Google Sheets is not configured. Add google-service-account.json (or GOOGLE_SERVICE_ACCOUNT_FILE)."
        )
        return

    arg = " ".join(context.args).strip()
    if not arg:
        email = sheet_sync.service_account_email() or "(open google-service-account.json → client_email)"
        await update.message.reply_text(
            "Usage: /linksheet <sheet URL or spreadsheet ID>\n\n"
            "1) Create a Google Sheet.\n"
            f"2) Share — add this account as Editor:\n{email}\n"
            "3) Send:\n/linksheet https://docs.google.com/spreadsheets/d/…"
        )
        return

    sid = sheet_sync.parse_spreadsheet_id(arg)
    if not sid:
        await update.message.reply_text(
            "Could not find a spreadsheet ID. Paste the full docs.google.com link, or the ID only."
        )
        return

    try:
        sid, url = sheet_sync.prepare_linked_spreadsheet(sid)
    except Exception as e:
        await update.message.reply_text(f"Could not link that spreadsheet:\n{e}")
        return

    user_id = update.effective_user.id
    record_user_chat(user_id, update.effective_chat.id)
    save_user_sheet(user_id, sid, url)
    backfill = get_user_transactions_asc(user_id)
    if backfill:
        try:
            sheet_sync.backfill_transactions(sid, list(backfill))
        except Exception as e:
            log.exception("sheet backfill error: %s", e)
    await update.message.reply_text(f"Linked. New rows will sync here:\n{url}")


# ---------------- HANDLERS: add ----------------
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    text = update.message.text
    parsed = parse_add_command(text)
    if not parsed:
        await update.message.reply_text(
            "❗ Sai format\n"
            "Dùng:\n"
            "/add expense 50k ăn trưa\n"
            "/add income 10tr lương"
        )
        return

    add_transaction(user_id, parsed["type"], parsed["amount"], parsed["category"])
    snap = last_transaction_snapshot(user_id)
    if snap:
        append_row_to_user_sheet(user_id, snap[0], snap[1], snap[2], snap[3])
    await update.message.reply_text(
        f"✅ Added: {parsed['type']} {parsed['amount']:,.0f} ({parsed['category']})"
    )


# ---------------- HANDLERS: budget + review ----------------
async def budget_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    args = context.args or []
    if not args:
        await update.message.reply_text(format_budget_status_message(user_id))
        return
    sub = args[0].lower()
    if sub == "set":
        if len(args) < 3:
            await update.message.reply_text(
                "Usage: /budget set <category> <amount>\n"
                "  /budget set food 2000k\n"
                "  /budget set total 10tr"
            )
            return
        category = args[1].lower()
        amount = parse_amount(args[2])
        if amount is None:
            await update.message.reply_text("Could not parse the amount (try 2000k, 10tr, 1500000).")
            return
        set_budget(user_id, category, amount)
        await update.message.reply_text(
            f"✅ Budget set: {category} = {amount:,.0f} / month"
        )
        return
    if sub == "clear":
        if len(args) < 2:
            await update.message.reply_text("Usage: /budget clear <category>")
            return
        ok = clear_budget(user_id, args[1].lower())
        await update.message.reply_text(
            f"✅ Cleared {args[1].lower()}" if ok else f"No budget for {args[1].lower()}."
        )
        return
    await update.message.reply_text(
        "Usage:\n"
        "  /budget — list budgets and MTD usage\n"
        "  /budget set <category> <amount>\n"
        "  /budget clear <category>"
    )


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    await update.message.reply_text(build_monthly_review(user_id))


# ---------------- HANDLERS: free text ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    record_user_chat(user_id, update.effective_chat.id)
    text = update.message.text

    result = ai_router(text)
    intent = result.get("intent", "chat")
    finance = result.get("finance") or {}
    log.info("intent=%s needs_live_web=%s", intent, result.get("needs_live_web"))

    if intent == "help_command":
        await update.message.reply_text(help_text())
        return

    if intent == "chat":
        reply = ai_chat(
            text,
            user_id,
            needs_live_web=bool(result.get("needs_live_web")),
            web_search_query=result.get("web_search_query"),
        )
        await update.message.reply_text(reply)
        return

    if intent == "finance":
        amount = finance.get("amount")
        if amount is None:
            await update.message.reply_text("❗ Không thấy số tiền rõ ràng (vd: 50k, 100k)")
            return
        pending[user_id] = {
            "type": finance.get("type") or "expense",
            "amount": amount,
            "category": finance.get("category") or "other_expense",
        }
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm", callback_data="confirm"),
                    InlineKeyboardButton("Cancel", callback_data="cancel"),
                ]
            ]
        )
        await update.message.reply_text(
            f"💰 CONFIRM:\n{pending[user_id]}", reply_markup=keyboard
        )
        return

    await update.message.reply_text("I didn't understand. Try /help")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    if user_id not in pending:
        await query.edit_message_text("No pending transaction")
        return
    data = pending[user_id]
    if query.data == "confirm":
        add_transaction(user_id, data["type"], data["amount"], data["category"])
        snap = last_transaction_snapshot(user_id)
        if snap:
            append_row_to_user_sheet(user_id, snap[0], snap[1], snap[2], snap[3])
        del pending[user_id]
        await query.edit_message_text("✅ Saved")
    elif query.data == "cancel":
        del pending[user_id]
        await query.edit_message_text("❌ Cancelled")


# ---------------- MAIN ----------------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("summary", summary))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("reset_chat", reset_chat))
    app.add_handler(CommandHandler("sheet", sheet_command))
    app.add_handler(CommandHandler("linksheet", linksheet_command))
    app.add_handler(CommandHandler("add", add_command))
    app.add_handler(CommandHandler("budget", budget_command))
    app.add_handler(CommandHandler("review", review_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler.attach(app)

    log.info("Bot running…")
    app.run_polling()


if __name__ == "__main__":
    import sys

    if "--reload" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--reload"]
        import dev_run

        dev_run.main()
    else:
        main()
