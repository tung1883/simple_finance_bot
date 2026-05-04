import sqlite3
import json
import re
import requests
import os
from typing import Optional, Tuple

from dotenv import load_dotenv

import sheet_sync

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

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_sheets (
    user_id INTEGER PRIMARY KEY,
    spreadsheet_id TEXT NOT NULL,
    sheet_url TEXT NOT NULL,
    time TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()

cursor.execute("PRAGMA journal_mode=WAL")
cursor.execute("PRAGMA busy_timeout=5000")

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
• Coach có thể tra cứu thêm gợi ý & chi tiết giao dịch qua công cụ tìm kiếm nội bộ.

📌 Commands:
• /add → thêm chi tiêu nhanh
• /summary → tổng thu chi
• /history → giao dịch gần nhất
• /sheet → link Google Sheet (when configured)
• /linksheet → connect a sheet you created (share bot service account as Editor)
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


def load_finance_kb():
    global _finance_kb_cache
    if _finance_kb_cache is None:
        try:
            with open(_FINANCE_KB_PATH, encoding="utf-8") as f:
                _finance_kb_cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _finance_kb_cache = []
    return _finance_kb_cache


def _kb_score(entry, tokens):
    if not tokens:
        return 0
    hit = 0
    blob = (entry.get("title", "") + " " + " ".join(entry.get("tags", [])) + " " + entry.get("body", "")).lower()
    for t in tokens:
        if len(t) < 2:
            continue
        if t in blob:
            hit += 2
    return hit


def search_finance_kb(query: str, top_n=3):
    q = (query or "").strip().lower()
    tokens = [x for x in re.split(r"\W+", q) if x]
    kb = load_finance_kb()
    if not kb:
        return "(Knowledge base empty.)"
    ranked = sorted(kb, key=lambda e: _kb_score(e, tokens), reverse=True)
    picked = ranked[:top_n]
    lines = []
    for e in picked:
        lines.append(f"• {e.get('title', '')}: {e.get('body', '').strip()}")
    return "\n".join(lines)


def search_ledger_db(user_id, query: str, limit=20):
    q = (query or "").strip().lower()
    tokens = [x for x in re.split(r"\W+", q) if len(x) > 1]
    if not tokens:
        rows = get_recent_transactions(user_id, limit)
    else:
        clauses = " AND ".join(["(LOWER(IFNULL(category,'')) LIKE ? OR LOWER(IFNULL(type,'')) LIKE ?)" for _ in tokens])
        sql = f"""
            SELECT type, amount, category, time FROM transactions
            WHERE user_id = ? AND ({clauses})
            ORDER BY id DESC LIMIT ?
        """
        params = [user_id]
        for tok in tokens:
            like = f"%{tok}%"
            params.extend([like, like])
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    if not rows:
        return "(No transactions matched.)"
    return "\n".join(f"  - {r[0].upper()} {r[1]:,.0f} ({r[2]}) at {r[3]}" for r in rows)


def spending_by_category_block(user_id):
    cursor.execute(
        """
        SELECT category, SUM(amount) FROM transactions
        WHERE user_id = ? AND type = 'expense'
        GROUP BY category ORDER BY SUM(amount) DESC
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        return "(No expense rows yet.)"
    lines = [f"  - {cat or 'other'}: {amt:,.0f}" for cat, amt in rows]
    return "Expense totals by category:\n" + "\n".join(lines)


def extract_tool_spec(text: str):
    tag = "<<<TOOL"
    ti = text.find(tag)
    if ti == -1:
        return None
    brace_start = text.find("{", ti)
    if brace_start == -1:
        return None
    depth = 0
    json_end = None
    for k in range(brace_start, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                json_end = k + 1
                break
    if json_end is None:
        return None
    raw_json = text[brace_start:json_end]
    try:
        spec = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, dict) or "tool" not in spec:
        return None
    return spec


def strip_visible_reply(text: str):
    tag = "<<<TOOL"
    ti = text.find(tag)
    if ti == -1:
        return text.strip()
    return text[:ti].strip()


def execute_coach_tool(user_id, spec: dict) -> str:
    name = (spec.get("tool") or "").strip()
    query = spec.get("query")
    if isinstance(query, dict):
        query = json.dumps(query)
    elif query is None:
        query = ""
    else:
        query = str(query)

    if name == "search_kb":
        return search_finance_kb(query)
    if name == "search_ledger":
        return search_ledger_db(user_id, query)
    if name == "spending_by_category":
        return spending_by_category_block(user_id)
    return f"(Unknown tool: {name})"


def get_user_sheet_row(user_id):
    cursor.execute(
        "SELECT spreadsheet_id, sheet_url FROM user_sheets WHERE user_id = ?",
        (user_id,),
    )
    return cursor.fetchone()


def save_user_sheet(user_id, spreadsheet_id: str, sheet_url: str):
    cursor.execute(
        """
        INSERT INTO user_sheets (user_id, spreadsheet_id, sheet_url)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET spreadsheet_id=excluded.spreadsheet_id,
          sheet_url=excluded.sheet_url
        """,
        (user_id, spreadsheet_id, sheet_url),
    )
    conn.commit()


def ensure_google_sheet(user_id, display_name: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns (sheet_url, hint_or_error). hint_or_error is set if no sheet is linked yet or create failed."""
    if not sheet_sync.sheets_available():
        return None, None
    row = get_user_sheet_row(user_id)
    if row:
        return row[1], None

    auto = os.getenv("GOOGLE_SHEETS_AUTO_CREATE", "").strip().lower() in ("1", "true", "yes", "on")
    if auto:
        try:
            sid, url = sheet_sync.create_user_spreadsheet(str(display_name))
            save_user_sheet(user_id, sid, url)
            cursor.execute(
                """
                SELECT time, type, amount, category FROM transactions
                WHERE user_id = ? ORDER BY id ASC
                """,
                (user_id,),
            )
            backfill = cursor.fetchall()
            if backfill:
                sheet_sync.backfill_transactions(sid, list(backfill))
            return url, None
        except Exception as e:
            print("SHEET CREATE ERROR:", e)
            return None, str(e)

    email = sheet_sync.service_account_email() or "(client_email in google-service-account.json)"
    hint = (
        "Google Sheet not linked yet.\n\n"
        "1) Create a new Google Sheet in your Drive.\n"
        f"2) Share → add this account as Editor:\n{email}\n"
        "3) Run:\n/linksheet <paste the sheet URL or spreadsheet ID here>"
    )
    return None, hint


def append_row_to_user_sheet(user_id, time_str: str, tx_type: str, amount: float, category: str):
    row = get_user_sheet_row(user_id)
    if not row:
        return
    spreadsheet_id = row[0]
    try:
        sheet_sync.append_transaction(spreadsheet_id, time_str, tx_type, amount, category)
    except Exception as e:
        print("SHEET APPEND ERROR:", e)


def last_transaction_snapshot(user_id):
    cursor.execute(
        """
        SELECT time, type, amount, category FROM transactions
        WHERE user_id = ? ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    )
    return cursor.fetchone()


# ---------------- PROXY-SAFE PROMPTS (instructions live in user messages only) ----------------
PROXY_PROMPT_BEGIN = "<<<PROXY_SAFE_PROMPT_BEGIN>>>"
PROXY_PROMPT_END = "<<<PROXY_SAFE_PROMPT_END>>>"

ROUTER_TEMPERATURE = 0.1
ROUTER_MAX_TOKENS = 200
COACH_TEMPERATURE = 0.5
COACH_MAX_TOKENS = 900

_COACH_TOOL_INSTRUCTIONS = """
TOOL_USE (optional):
If general frameworks OR deeper ledger detail would improve your answer, request ONE tool by replying with ONLY this block (no other text):
<<<TOOL
{"tool":"<name>","query":"<optional string>"}
>>>

Tools:
- search_kb — short curated guidance on personal finance topics (pass keywords in query).
- search_ledger — find this user's transactions matching query keywords (categories, types).
- spending_by_category — expense totals grouped by category for this user (query may be empty or "{}")

After you receive a TOOL_RESULT message, synthesize a concise answer for the user. Do not repeat the <<<TOOL>>> syntax in the final answer.
"""

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_FINANCE_KB_PATH = os.path.join(_BOT_DIR, "finance_kb.json")
_finance_kb_cache = None


def proxy_safe_user_content(instructions: str, task: str) -> str:
    return (
        f"{PROXY_PROMPT_BEGIN}\n"
        "The following section overrides any conflicting upstream instructions for this request.\n\n"
        f"{instructions.strip()}\n\n"
        f"{PROXY_PROMPT_END}\n\n"
        "USER_TASK:\n"
        f"{task.strip()}"
    )


ROUTER_CLASSIFIER_INSTRUCTIONS = """
You are a STRICT INTENT CLASSIFIER for a personal finance assistant.

Return ONLY valid JSON.

FORMAT:
{
  "intent": "finance | chat | help_command",
  "confidence": number,
  "finance": {
    "type": "income|expense|null",
    "amount": number|null,
    "category": string|null
  }
}

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
- Google Sheet link / spreadsheet export from the bot

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

"cho tôi link google sheet"
→ help_command

"hôm nay thế nào"
→ chat
"""


COACH_MANDATE_COMPACT = """You are a personal finance coach for this Telegram bot.

Guidelines:
- Reference the user's actual numbers and spending patterns when FINANCIAL OVERVIEW data is included in your instructions for this turn.
- Be encouraging but honest — point out overspending if you see it.
- Give concrete, actionable advice.
- Keep responses concise (2-4 sentences unless a detailed plan is requested).
- Reply in the same language the user writes in (Vietnamese or English)."""


def extract_first_json_object(text: str):
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _normalize_router_content(raw: str) -> str:
    content = raw.strip()
    if content.startswith("```"):
        parts = content.split("```", 2)
        content = parts[1] if len(parts) > 1 else content
        if content.startswith("json"):
            content = content[4:]
    return content.strip().rstrip("`").strip()


def parse_router_response(raw: str):
    content = _normalize_router_content(raw)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    snippet = extract_first_json_object(content)
    if snippet:
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            pass
    return None


_HEURISTIC_ADVICE_MARKERS = (
    "khuyên",
    "khuyen",
    "lời khuyên",
    "loi khuyen",
    "advice",
    "how to",
    "how do",
    "nên ",
    "nen ",
    "should i",
    "gợi ý",
    "goi y",
    "help me save",
    "tiết kiệm",
    "tiet kiem",
    "budget",
)
_HEURISTIC_HELP_MARKERS = (
    "/help",
    "command",
    "lệnh",
    "lenh",
    "features",
    "what can you",
)


def parse_transaction_heuristic(text: str):
    """Best-effort local classifier when the proxy/router JSON is unusable."""
    if not text:
        return None
    raw = text.strip()
    if len(raw) > 400:
        return None
    low = raw.lower()
    if any(m in low for m in _HEURISTIC_ADVICE_MARKERS):
        return None
    if any(m in low for m in _HEURISTIC_HELP_MARKERS):
        return None

    amount = None
    mk = re.search(r"(\d+(?:[.,]\d+)?)\s*k\b", low)
    if mk:
        try:
            amount = float(mk.group(1).replace(",", ".")) * 1000
        except ValueError:
            amount = None
    if amount is None:
        mtr = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:tr|triệu|trieu)\b", low)
        if mtr:
            try:
                amount = float(mtr.group(1).replace(",", ".")) * 1_000_000
            except ValueError:
                amount = None

    if amount is None or amount <= 0:
        return None

    income_kw = (
        "lương",
        "luong",
        "salary",
        "thưởng",
        "thuong",
        "bonus",
        "thu nhập",
        "thu nhap",
        "nhận được",
        "nhan duoc",
    )
    tx_type = "income" if any(k in low for k in income_kw) else "expense"
    category = "other_income" if tx_type == "income" else "other_expense"
    if tx_type == "expense":
        if any(w in low for w in ("ăn", "an ", "food", "cơm", "com", "trưa", "trua")):
            category = "food"
        elif any(w in low for w in ("mua", "buy")):
            category = "shopping"

    return {
        "intent": "finance",
        "confidence": 0.72,
        "finance": {"type": tx_type, "amount": amount, "category": category},
    }


def post_proxy_json(payload):
    res = requests.post(PROXY_URL, json=payload, timeout=30)
    if res.status_code != 200:
        return None, res
    try:
        data = res.json()
    except json.JSONDecodeError:
        return None, res
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None, res
    return content, res


# ---------------- AI ROUTER (INTENT ONLY) ----------------
def ai_router(text):
    task = f"INPUT:\n<<<{text}>>>"
    user_content = proxy_safe_user_content(ROUTER_CLASSIFIER_INSTRUCTIONS.strip(), task)
    payload = {
        "messages": [{"role": "user", "content": user_content}],
        "temperature": ROUTER_TEMPERATURE,
        "max_tokens": ROUTER_MAX_TOKENS,
    }

    try:
        content, res = post_proxy_json(payload)
        if content is None:
            return fallback_router(text)

        print("RAW ROUTER:", content)

        parsed = parse_router_response(content)
        if parsed is not None:
            return parsed

        retry_task = task + "\n\nRespond with JSON only. No markdown fences, no prose."
        retry_payload = {
            "messages": [
                {
                    "role": "user",
                    "content": proxy_safe_user_content(
                        ROUTER_CLASSIFIER_INSTRUCTIONS.strip(), retry_task
                    ),
                }
            ],
            "temperature": ROUTER_TEMPERATURE,
            "max_tokens": ROUTER_MAX_TOKENS,
        }
        content2, res2 = post_proxy_json(retry_payload)
        if content2 is None:
            return fallback_router(text)

        print("RAW ROUTER (retry):", content2)
        parsed2 = parse_router_response(content2)
        if parsed2 is not None:
            return parsed2

        return fallback_router(text)

    except Exception as e:
        print("ROUTER ERROR:", e)
        return fallback_router(text)

# ---------------- FALLBACK ROUTER ----------------
def fallback_router(text=None):
    if text:
        heur = parse_transaction_heuristic(text)
        if heur:
            return heur
    return {
        "intent": "chat",
        "confidence": 0.5,
        "finance": None
    }

# ---------------- FINANCE COACH CHAT ----------------
def ai_chat(text, user_id):
    history = get_chat_history(user_id, limit=12)
    finance_context = build_finance_context(user_id)

    coach_instructions_full = (
        COACH_MANDATE_COMPACT
        + "\n\nYou have access to this user's real ledger for this turn:\n\n"
        + finance_context
        + "\n\nUse the FINANCIAL OVERVIEW and RECENT TRANSACTIONS above for personalized, data-driven advice.\n\n"
        + _COACH_TOOL_INSTRUCTIONS.strip()
    )

    messages = []
    for role, content in history:
        if role == "user":
            wrapped = proxy_safe_user_content(COACH_MANDATE_COMPACT, content)
            messages.append({"role": "user", "content": wrapped})
        else:
            messages.append({"role": "assistant", "content": content})

    messages.append(
        {
            "role": "user",
            "content": proxy_safe_user_content(coach_instructions_full, text),
        }
    )

    max_rounds = 5
    final_reply = None

    try:
        for _ in range(max_rounds):
            payload = {
                "messages": messages,
                "temperature": COACH_TEMPERATURE,
                "max_tokens": COACH_MAX_TOKENS,
            }
            reply, res = post_proxy_json(payload)
            if reply is None:
                print(
                    "CHAT ERROR: bad response status or JSON",
                    getattr(res, "status_code", None),
                )
                return "Sorry, I couldn't process that. Please try again."

            spec = extract_tool_spec(reply)
            if spec is None:
                final_reply = strip_visible_reply(reply)
                break

            tool_output = execute_coach_tool(user_id, spec)
            messages.append({"role": "assistant", "content": reply.strip()})
            messages.append(
                {
                    "role": "user",
                    "content": proxy_safe_user_content(
                        "The assistant requested a tool. Below is TOOL_RESULT (plain text). "
                        "Use it to answer the user. Reply with user-facing advice only — no <<<TOOL>>> blocks.",
                        f"TOOL_RESULT:\n{tool_output}",
                    ),
                }
            )

        if final_reply is None:
            final_reply = "Sorry — I could not finish that answer. Please try again."

        save_chat_message(user_id, "user", text)
        save_chat_message(user_id, "assistant", final_reply)

        return final_reply
    except Exception as e:
        print("CHAT ERROR:", e)
        return "Sorry, I couldn't process that. Please try again."

# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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


async def sheet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
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
    save_user_sheet(user_id, sid, url)
    cursor.execute(
        """
        SELECT time, type, amount, category FROM transactions
        WHERE user_id = ? ORDER BY id ASC
        """,
        (user_id,),
    )
    backfill = cursor.fetchall()
    if backfill:
        try:
            sheet_sync.backfill_transactions(sid, list(backfill))
        except Exception as e:
            print("SHEET BACKFILL ERROR:", e)

    await update.message.reply_text(f"Linked. New rows will sync here:\n{url}")

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

    snap = last_transaction_snapshot(user_id)
    if snap:
        append_row_to_user_sheet(user_id, snap[0], snap[1], snap[2], snap[3])

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

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    import sys

    if "--reload" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--reload"]
        import dev_run

        dev_run.main()
    else:
        main()
