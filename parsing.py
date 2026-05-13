"""Amount + /add command parsers. Kept Telegram-free so tests don't need PTB installed."""
import re
from typing import Optional

_AMOUNT_RE = re.compile(r"^(\d+(?:[.,]\d+)?)(k|tr|triệu|trieu)?$", re.IGNORECASE)


def parse_amount(s: str) -> Optional[float]:
    """Parse VND-style amount strings: 50k, 10tr, 2trieu, 1500000, 1.5k, 1,5k.

    Comma is treated as a decimal separator (Vietnamese convention: "1,5k" = 1500).
    """
    s = (s or "").strip().lower().replace(",", ".")
    m = _AMOUNT_RE.match(s)
    if not m:
        try:
            v = float(s)
            return v if v > 0 else None
        except ValueError:
            return None
    num = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        num *= 1_000
    elif suffix in ("tr", "triệu", "trieu"):
        num *= 1_000_000
    return num if num > 0 else None


def parse_add_command(text: str) -> Optional[dict]:
    """Parse: /add expense 50k food → {type, amount, category}."""
    parts = (text or "").split(" ", 3)
    if len(parts) < 3:
        return None
    _, type_, amount_str = parts[:3]
    category = parts[3] if len(parts) == 4 else "other"
    amount = parse_amount(amount_str)
    if amount is None:
        return None
    if type_ not in ("expense", "income"):
        return None
    return {"type": type_, "amount": amount, "category": category}
