"""Transaction CRUD + aggregations + finance context block for coach prompts."""
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from db import connect


def add_transaction(user_id: int, tx_type: str, amount: float, category: str) -> int:
    c = connect()
    cur = c.cursor()
    cur.execute(
        "INSERT INTO transactions (user_id, type, amount, category) VALUES (?, ?, ?, ?)",
        (user_id, tx_type, amount, category),
    )
    c.commit()
    return cur.lastrowid or 0


def get_user_summary(user_id: int) -> Dict[str, float]:
    cur = connect().cursor()
    cur.execute(
        """
        SELECT type, SUM(amount) FROM transactions
        WHERE user_id = ? OR user_id IS NULL
        GROUP BY type
        """,
        (user_id,),
    )
    return {row[0]: row[1] or 0.0 for row in cur.fetchall()}


def get_recent_transactions(user_id: int, limit: int = 20) -> List[Tuple]:
    cur = connect().cursor()
    cur.execute(
        """
        SELECT type, amount, category, time FROM transactions
        WHERE user_id = ? OR user_id IS NULL
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return cur.fetchall()


def last_transaction_snapshot(user_id: int) -> Optional[Tuple]:
    cur = connect().cursor()
    cur.execute(
        """
        SELECT time, type, amount, category FROM transactions
        WHERE user_id = ? ORDER BY id DESC LIMIT 1
        """,
        (user_id,),
    )
    return cur.fetchone()


def get_user_transactions_asc(user_id: int) -> List[Tuple]:
    """All rows ascending — used for sheet backfill."""
    cur = connect().cursor()
    cur.execute(
        """
        SELECT time, type, amount, category FROM transactions
        WHERE user_id = ? ORDER BY id ASC
        """,
        (user_id,),
    )
    return cur.fetchall()


def search_ledger_db(user_id: int, query: str, limit: int = 20) -> str:
    q = (query or "").strip().lower()
    tokens = [x for x in re.split(r"\W+", q) if len(x) > 1]
    cur = connect().cursor()
    if not tokens:
        rows = get_recent_transactions(user_id, limit)
    else:
        clauses = " AND ".join(
            ["(LOWER(IFNULL(category,'')) LIKE ? OR LOWER(IFNULL(type,'')) LIKE ?)" for _ in tokens]
        )
        sql = f"""
            SELECT type, amount, category, time FROM transactions
            WHERE user_id = ? AND ({clauses})
            ORDER BY id DESC LIMIT ?
        """
        params: List = [user_id]
        for tok in tokens:
            like = f"%{tok}%"
            params.extend([like, like])
        params.append(limit)
        cur.execute(sql, params)
        rows = cur.fetchall()
    if not rows:
        return "(No transactions matched.)"
    return "\n".join(f"  - {r[0].upper()} {r[1]:,.0f} ({r[2]}) at {r[3]}" for r in rows)


def spending_by_category_block(user_id: int) -> str:
    cur = connect().cursor()
    cur.execute(
        """
        SELECT category, SUM(amount) FROM transactions
        WHERE user_id = ? AND type = 'expense'
        GROUP BY category ORDER BY SUM(amount) DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return "(No expense rows yet.)"
    lines = [f"  - {cat or 'other'}: {amt:,.0f}" for cat, amt in rows]
    return "Expense totals by category:\n" + "\n".join(lines)


def month_to_date_spend_by_category(
    user_id: int, today: Optional[date] = None
) -> Dict[str, float]:
    """Sum of expenses logged so far this calendar month, keyed by category (lowercased)."""
    today = today or date.today()
    start = today.replace(day=1).isoformat()
    cur = connect().cursor()
    cur.execute(
        """
        SELECT LOWER(IFNULL(category,'other')), SUM(amount)
        FROM transactions
        WHERE user_id = ? AND type = 'expense' AND time >= ?
        GROUP BY LOWER(IFNULL(category,'other'))
        """,
        (user_id, start),
    )
    return {row[0]: row[1] or 0.0 for row in cur.fetchall()}


def month_to_date_totals(user_id: int, today: Optional[date] = None) -> Dict[str, float]:
    today = today or date.today()
    start = today.replace(day=1).isoformat()
    cur = connect().cursor()
    cur.execute(
        """
        SELECT type, SUM(amount) FROM transactions
        WHERE user_id = ? AND time >= ?
        GROUP BY type
        """,
        (user_id, start),
    )
    return {row[0]: row[1] or 0.0 for row in cur.fetchall()}


def build_finance_context(user_id: int) -> str:
    summary = get_user_summary(user_id)
    transactions = get_recent_transactions(user_id)

    income = summary.get("income", 0)
    expense = summary.get("expense", 0)
    balance = income - expense

    if transactions:
        tx_lines = "\n".join(
            f"  - {r[0].upper()} {r[1]:,.0f} ({r[2]}) at {r[3]}" for r in transactions
        )
    else:
        tx_lines = "  (no transactions recorded yet)"

    return (
        "LEDGER SNAPSHOT (IMPORTANT):\n"
        "  This is ONLY what the user has logged inside this bot — not their bank balance, payslip, "
        "cash, investments, debts, or spending outside the bot. Missing income here does NOT mean they "
        "have no income; missing expenses does NOT mean they spend only what you see.\n\n"
        f"AGGREGATES FROM BOT LOG (partial picture):\n"
        f"  Income logged:   {income:,.0f}\n"
        f"  Expenses logged: {expense:,.0f}\n"
        f"  Net (log only):   {balance:,.0f}\n\n"
        f"RECENT LOGGED TRANSACTIONS (newest first, capped):\n{tx_lines}"
    )


def record_user_chat(user_id: int, chat_id: int) -> None:
    """Remember (user_id, chat_id) for proactive scheduler pings."""
    c = connect()
    c.execute(
        """
        INSERT INTO user_chat (user_id, chat_id, last_seen)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
          last_seen=CURRENT_TIMESTAMP
        """,
        (user_id, chat_id),
    )
    c.commit()


def all_user_chats() -> List[Tuple[int, int]]:
    cur = connect().cursor()
    cur.execute("SELECT user_id, chat_id FROM user_chat")
    return cur.fetchall()


# ---- Google Sheet linkage (kept here because it's per-user state alongside the ledger) ----


def get_user_sheet_row(user_id: int) -> Optional[Tuple[str, str]]:
    cur = connect().cursor()
    cur.execute(
        "SELECT spreadsheet_id, sheet_url FROM user_sheets WHERE user_id = ?",
        (user_id,),
    )
    return cur.fetchone()


def save_user_sheet(user_id: int, spreadsheet_id: str, sheet_url: str) -> None:
    c = connect()
    c.execute(
        """
        INSERT INTO user_sheets (user_id, spreadsheet_id, sheet_url)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET spreadsheet_id=excluded.spreadsheet_id,
          sheet_url=excluded.sheet_url
        """,
        (user_id, spreadsheet_id, sheet_url),
    )
    c.commit()
