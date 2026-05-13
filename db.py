"""SQLite connection and schema for the finance bot.

Single shared module-level connection (check_same_thread=False — PTB runs handlers in
an asyncio thread pool and the scheduler fires on the event loop; sqlite serializes
writes via busy_timeout).

Schema is additive: existing rows are preserved; new tables are CREATE TABLE IF NOT EXISTS.
"""
import os
import sqlite3
from typing import Optional


_DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance.db")


def _open(path: str) -> sqlite3.Connection:
    c = sqlite3.connect(path, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


_conn: Optional[sqlite3.Connection] = None


def connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Return the process-wide sqlite connection, opening it on first call."""
    global _conn
    if _conn is None:
        _conn = _open(path or os.getenv("FINANCE_DB_PATH") or _DEFAULT_DB_PATH)
        _bootstrap_schema(_conn)
    return _conn


def reset_for_tests(path: str = ":memory:") -> sqlite3.Connection:
    """Replace the shared connection (tests only)."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
    _conn = _open(path)
    _bootstrap_schema(_conn)
    return _conn


def _bootstrap_schema(c: sqlite3.Connection) -> None:
    cur = c.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        type TEXT,
        amount REAL,
        category TEXT,
        time TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # Legacy rows from before user_id was added: keep them addressable by NULL user_id.
    try:
        cur.execute("ALTER TABLE transactions ADD COLUMN user_id INTEGER")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        time TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_sheets (
        user_id INTEGER PRIMARY KEY,
        spreadsheet_id TEXT NOT NULL,
        sheet_url TEXT NOT NULL,
        time TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS budgets (
        user_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        monthly_limit REAL NOT NULL,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, category)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_chat (
        user_id INTEGER PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        last_seen TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS proactive_alerts (
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        period_key TEXT NOT NULL,
        sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, kind, period_key)
    )
    """)
    c.commit()
