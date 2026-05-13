"""Per-user chat history persistence."""
from typing import List, Tuple

from db import connect


def get_chat_history(user_id: int, limit: int = 12) -> List[Tuple[str, str]]:
    cur = connect().cursor()
    cur.execute(
        """
        SELECT role, content FROM chat_history
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    return list(reversed(cur.fetchall()))


def save_chat_message(user_id: int, role: str, content: str) -> None:
    c = connect()
    c.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)",
        (user_id, role, content),
    )
    c.commit()


def clear_chat_history(user_id: int) -> None:
    c = connect()
    c.execute("DELETE FROM chat_history WHERE user_id = ?", (user_id,))
    c.commit()
