"""Per-user monthly budget limits.

`category` is lowercased. The reserved key `total` means "all expenses combined".

Layout:
- set_budget(user_id, category, monthly_limit): upsert.
- clear_budget(user_id, category): remove one.
- list_budgets(user_id): all rows.
- budget_status(user_id): list of dicts joining each budget with MTD spend.
- build_budget_block(user_id): text block injected into coach context.
- over_budget_alerts(user_id): items projected to blow their monthly cap (used by scheduler).
"""
from calendar import monthrange
from datetime import date
from typing import Dict, List, Optional

from db import connect
from ledger import month_to_date_spend_by_category, month_to_date_totals


def set_budget(user_id: int, category: str, monthly_limit: float) -> None:
    cat = (category or "").strip().lower() or "total"
    c = connect()
    c.execute(
        """
        INSERT INTO budgets (user_id, category, monthly_limit, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, category) DO UPDATE SET monthly_limit=excluded.monthly_limit,
          updated_at=CURRENT_TIMESTAMP
        """,
        (user_id, cat, float(monthly_limit)),
    )
    c.commit()


def clear_budget(user_id: int, category: str) -> bool:
    cat = (category or "").strip().lower()
    if not cat:
        return False
    c = connect()
    cur = c.execute("DELETE FROM budgets WHERE user_id = ? AND category = ?", (user_id, cat))
    c.commit()
    return cur.rowcount > 0


def list_budgets(user_id: int) -> List[Dict]:
    cur = connect().cursor()
    cur.execute(
        "SELECT category, monthly_limit FROM budgets WHERE user_id = ? ORDER BY category",
        (user_id,),
    )
    return [{"category": row[0], "monthly_limit": row[1]} for row in cur.fetchall()]


def _projection_ratio(today: date) -> float:
    """Day-of-month / days-in-month, used to compare MTD spend against the budget."""
    days_in_month = monthrange(today.year, today.month)[1]
    return today.day / days_in_month


def budget_status(user_id: int, today: Optional[date] = None) -> List[Dict]:
    today = today or date.today()
    mtd = month_to_date_spend_by_category(user_id, today)
    total_spent = sum(mtd.values())
    out: List[Dict] = []
    for b in list_budgets(user_id):
        cat = b["category"]
        limit = b["monthly_limit"]
        spent = total_spent if cat == "total" else mtd.get(cat, 0.0)
        pct = (spent / limit * 100.0) if limit > 0 else 0.0
        proj = (spent / _projection_ratio(today)) if _projection_ratio(today) > 0 else spent
        out.append(
            {
                "category": cat,
                "monthly_limit": limit,
                "spent": spent,
                "percent": pct,
                "projected_monthend": proj,
                "over_projected": proj > limit,
                "over_actual": spent > limit,
            }
        )
    return out


def build_budget_block(user_id: int, today: Optional[date] = None) -> str:
    rows = budget_status(user_id, today)
    if not rows:
        return "BUDGETS: (none set — user can `/budget set <category> <amount>` to define one)"
    lines = ["BUDGETS (month-to-date vs monthly limit):"]
    for r in rows:
        flag = ""
        if r["over_actual"]:
            flag = " ⚠ over"
        elif r["over_projected"]:
            flag = " ⚠ projected over"
        lines.append(
            f"  - {r['category']}: {r['spent']:,.0f} / {r['monthly_limit']:,.0f} "
            f"({r['percent']:.0f}%; projected {r['projected_monthend']:,.0f}){flag}"
        )
    return "\n".join(lines)


def over_budget_alerts(user_id: int, today: Optional[date] = None) -> List[Dict]:
    """Categories that are over (actual or projected). Excludes the first few days
    of the month where projections are noisy."""
    today = today or date.today()
    if today.day < 5:
        return [r for r in budget_status(user_id, today) if r["over_actual"]]
    return [r for r in budget_status(user_id, today) if r["over_actual"] or r["over_projected"]]


def format_budget_status_message(user_id: int, today: Optional[date] = None) -> str:
    rows = budget_status(user_id, today)
    if not rows:
        totals = month_to_date_totals(user_id, today)
        return (
            "No budgets set.\n\n"
            f"Month-to-date: spent {totals.get('expense', 0):,.0f}, "
            f"received {totals.get('income', 0):,.0f}.\n\n"
            "Set one with: /budget set <category> <amount>\n"
            "  e.g. /budget set food 2000k\n"
            "       /budget set total 10tr"
        )
    lines = ["📊 Budgets (this month):"]
    for r in rows:
        flag = ""
        if r["over_actual"]:
            flag = "  ⚠ over"
        elif r["over_projected"]:
            flag = "  ⚠ on pace to exceed"
        lines.append(
            f"• {r['category']}: {r['spent']:,.0f} / {r['monthly_limit']:,.0f}"
            f" ({r['percent']:.0f}%){flag}"
        )
    lines.append("")
    lines.append("Commands: /budget set <cat> <amount>, /budget clear <cat>")
    return "\n".join(lines)
