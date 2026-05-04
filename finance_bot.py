import sqlite3
import json
import requests
import os
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ---------------- ENV ----------------
load_dotenv()
TOKEN = os.getenv("TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

if not TOKEN or not PROXY_URL:
    raise Exception("Missing TOKEN or PROXY_URL in .env")

# ---------------- DATABASE ----------------
conn = sqlite3.connect("finance.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    amount REAL,
    category TEXT,
    time TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

# Migrate old rows that have no user_id column
try:
    cursor.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER")
except Exception:
    pass

cursor.execute("""
CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    time TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()

pending = {}

# ---------------- HELP ----------------
def help_text():
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

📌 Commands:
• /add → thêm chi tiêu nhanh
• /summary → tổng thu chi
• /history → giao dịch gần nhất
• /reset_chat → xóa lịch sử hội thoại
"""

# ---------------- CHAT HISTORY DB ----------------
def get_chat_history(user_id, limit=12):
    cursor.execute("""
        SELECT role, content FROM chat_history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    rows = cursor.fetchall()
    return list(reversed(rows))

def save_chat_message(user_id, role, content):
    cursor.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content)
    )
    conn.commit()

def clear_chat_history(user_id):
    cursor.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
    conn.commit()

# ---------------- TRANSACTION CONTEXT ----------------
def get_user_summary(user_id):
    cursor.execute("""
        SELECT type, SUM(amount) FROM transactions
        WHERE user_id = ? OR user_id IS NULL
        GROUP BY type
    """, (user_id,))
    return dict(cursor.fetchall())

def get_recent_transactions(user_id, limit=20):
    cursor.execute("""
        SELECT type, amount, category, time FROM transactions
        WHERE user_id = ? OR user_id IS NULL
        ORDER BY id DESC
        LIMIT ?
    """, (user_id, limit))
    return cursor.fetchall()

def build_finance_context(user_id):
    summary = get_user_summary(user_id)
    transactions = get_recent_transactions(user_id)

    income = summary.get("income", 0)
    expense = summary.get("expense", 0)
    balance = income - expense

    if transactions:
        tx_lines = "\n".join(
            f"  - {r[0].upper()} {r[1]:,.0f} ({r[2]}) at {r[3]}"
            for r in transactions
        )
    else:
        tx_lines = "  (no transactions recorded yet)"

    return (
        f"FINANCIAL OVERVIEW:\n"
        f"  Total income:   {income:,.0f}\n"
        f"  Total expenses: {expense:,.0f}\n"
        f"  Balance:        {balance:,.0f}\n\n"
        f"RECENT TRANSACTIONS (newest first):\n{tx_lines}"
    )

# ---------------- AI ROUTER (INTENT ONLY) ----------------
def ai_router(text):
    payload = {
        "messages": [
            {
                "role": "user",
                "content": f"""
You are a STRICT INTENT CLASSIFIER for a personal finance assistant.

Return ONLY valid JSON.

FORMAT:
{{
  "intent": "finance | chat | help_command",
  "confidence": number,
  "finance": {{
    "type": "income|expense|null",
    "amount": number|null,
    "category": string|null
  }}
}}

========================
STRICT RULES (VERY IMPORTANT)
========================

1. help_command ONLY if user asks about SYSTEM USAGE:
- bot commands
- how to use bot
- /help
- list of features
- "what can you do"
- usage instructions

IMPORTANT:
❌ DO NOT include financial advice here
❌ DO NOT include money tips here

========================

2. finance ONLY when user is RECORDING A TRANSACTION:

This means user is logging REAL money flow:

A. Spending:
- eat, buy, game, transport, shopping, etc.
- must be an action + optionally amount

B. Income:
- salary, bonus, income, receive money

C. Must include or imply a concrete transaction

IMPORTANT RULES:
- finance MUST NOT include advice
- finance MUST NOT include questions
- finance MUST NOT include planning or suggestions
- finance MUST be only "data entry"

Examples:
- "ăn 50k"
- "mua trà sữa 30k"
- "lương 10 triệu"

========================

3. chat (DEFAULT for EVERYTHING ELSE):

This includes:

A. Financial advice / coaching:
- how to save money
- how to spend better
- budgeting tips
- financial planning
- "cho tôi lời khuyên chi tiêu"
- "tôi nên tiết kiệm thế nào"

B. Any question or discussion:
- opinions
- life advice
- general chat
- unclear intent

IMPORTANT:
👉 ANYTHING NOT CLEARLY A TRANSACTION = chat

========================
EXAMPLES
========================

"cho tôi lời khuyên giúp tôi chi tiêu hiệu quả hơn"
→ chat

"tôi nên tiết kiệm thế nào"
→ chat

"cách quản lý tiền tốt hơn"
→ chat

"ăn 50k"
→ finance

"lương 10 triệu"
→ finance

"bot có những lệnh gì"
→ help_command

"hôm nay thế nào"
→ chat

========================

INPUT:
<<<{text}>>>
"""
            }
        ],
        "temperature": 0.1,
        "max_tokens": 200
    }

    try:
        res = requests.post(PROXY_URL, json=payload, timeout=30)

        if res.status_code != 200:
            return fallback_router()

        data = res.json()
        content = data["choices"][0]["message"]["content"]

        print("RAW ROUTER:", content)

        # strip markdown code fences if present
        content = content.strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
        content = content.strip().rstrip("`").strip()

        return json.loads(content)

    except Exception as e:
        print("ROUTER ERROR:", e)
        return fallback_router()

# ---------------- FALLBACK ROUTER ----------------
def fallback_router():
    return {
        "intent": "chat",
        "confidence": 0.5,
        "finance": None
    }

# ---------------- FINANCE COACH CHAT ----------------
def ai_chat(text, user_id):
    history = get_chat_history(user_id, limit=12)
    finance_context = build_finance_context(user_id)

    system_prompt = (
        "You are a personal finance coach. You have access to the user's real transaction history below. "
        "Use it to give personalized, data-driven advice.\n\n"
        f"{finance_context}\n\n"
        "Guidelines:\n"
        "- Reference the user's actual numbers and spending patterns when relevant.\n"
        "- Be encouraging but honest — point out overspending if you see it.\n"
        "- Give concrete, actionable advice.\n"
        "- Keep responses concise (2-4 sentences unless a detailed plan is requested).\n"
        "- Reply in the same language the user writes in (Vietnamese or English)."
    )

    messages = []

    for role, content in history:
        messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": f"{system_prompt}\n\n---\n\nUser message:\n{text}"})

    payload = {
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 500
    }

    try:
        res = requests.post(PROXY_URL, json=payload, timeout=30)
        data = res.json()
        reply = data["choices"][0]["message"]["content"]

        save_chat_message(user_id, "user", text)
        save_chat_message(user_id, "assistant", reply)

        return reply
    except Exception as e:
        print("CHAT ERROR:", e)
        return "Sorry, I couldn't process that. Please try again."

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Finance bot ready.\n\nLog transactions naturally (e.g. \"ăn 50k\") "
        "or ask your finance coach anything. Type /help for more."
    )

# ---------------- HELP ----------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text())

# ---------------- MANUALLY ADD EXPENSE ------------
def parse_add_command(text):
    parts = text.split(" ", 3)

    if len(parts) < 3:
        return None

    _, type_, amount_str = parts[:3]
    category = parts[3] if len(parts) == 4 else "other"

    amount_str = amount_str.lower().replace(",", "")

    try:
        if "k" in amount_str:
            amount = float(amount_str.replace("k", "")) * 1000
        elif "tr" in amount_str:
            amount = float(amount_str.replace("tr", "")) * 1_000_000
        else:
            amount = float(amount_str)
    except:
        return None

    return {
        "type": type_,
        "amount": amount,
        "category": category
    }


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
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

    cursor.execute("""
        INSERT INTO transactions (user_id, type, amount, category)
        VALUES (?, ?, ?, ?)
    """, (user_id, parsed["type"], parsed["amount"], parsed["category"]))

    conn.commit()

    await update.message.reply_text(
        f"✅ Added: {parsed['type']} {parsed['amount']:,.0f} ({parsed['category']})"
    )

# ---------------- RESET CHAT ----------------
async def reset_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    clear_chat_history(user_id)
    await update.message.reply_text("🧹 Conversation history cleared.")

# ---------------- SUMMARY ----------------
async def summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    rows = get_user_summary(user_id)

    income = rows.get("income", 0)
    expense = rows.get("expense", 0)

    await update.message.reply_text(
        f"📊 SUMMARY\n\n"
        f"Income:  {income:,.0f}\n"
        f"Expense: {expense:,.0f}\n"
        f"Balance: {income - expense:,.0f}"
    )

# ---------------- HISTORY ----------------
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    rows = get_recent_transactions(user_id, limit=10)

    text = "📜 LAST TRANSACTIONS\n\n"
    for r in rows:
        text += f"{r[3]}\n{r[0].upper()} {r[1]:,.0f} ({r[2]})\n\n"

    if not rows:
        text += "(none yet)"

    await update.message.reply_text(text)

# ---------------- MESSAGE HANDLER ----------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    text = update.message.text

    result = ai_router(text)

    intent = result.get("intent", "chat")
    finance = result.get("finance") or {}

    print("INTENT:", intent)

    if intent == "help_command":
        await update.message.reply_text(help_text())
        return

    if intent == "chat":
        reply = ai_chat(text, user_id)
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
            "category": finance.get("category") or "other_expense"
        }

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Confirm", callback_data="confirm"),
                InlineKeyboardButton("Cancel", callback_data="cancel")
            ]
        ])

        await update.message.reply_text(
            f"💰 CONFIRM:\n{pending[user_id]}",
            reply_markup=keyboard
        )
        return

    await update.message.reply_text("I didn't understand. Try /help")

# ---------------- CALLBACK ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    await query.answer()

    if user_id not in pending:
        await query.edit_message_text("No pending transaction")
        return

    data = pending[user_id]

    if query.data == "confirm":
        cursor.execute(
            "INSERT INTO transactions (user_id, type, amount, category) VALUES (?, ?, ?, ?)",
            (user_id, data["type"], data["amount"], data["category"])
        )
        conn.commit()

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
    app.add_handler(CommandHandler("add", add_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
