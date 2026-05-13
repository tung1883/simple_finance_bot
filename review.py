"""Monthly review composition — combines ledger, budgets, recurring, forecast."""
from datetime import date
from typing import Optional

from budgets import budget_status
from ledger import month_to_date_spend_by_category, month_to_date_totals
from recurring import detect_recurring, forecast_monthend_expense, format_recurring_lines


def _top_categories(mtd: dict, n: int = 5):
    return sorted(mtd.items(), key=lambda kv: kv[1], reverse=True)[:n]


def build_monthly_review(user_id: int, today: Optional[date] = None) -> str:
    today = today or date.today()
    totals = month_to_date_totals(user_id, today)
    mtd = month_to_date_spend_by_category(user_id, today)
    forecast = forecast_monthend_expense(user_id, today)
    budgets = budget_status(user_id, today)
    recurring = detect_recurring(user_id, today)

    income = totals.get("income", 0.0)
    expense = totals.get("expense", 0.0)
    net = income - expense

    lines = [
        f"📅 Monthly review — {today.strftime('%B %Y')}",
        f"(day {today.day} of {forecast['days_in_month']})",
        "",
        "💰 Totals so far:",
        f"  Income:  {income:,.0f}",
        f"  Expense: {expense:,.0f}",
        f"  Net:     {net:,.0f}",
        "",
    ]

    top = _top_categories(mtd)
    if top:
        lines.append("🏷 Top categories:")
        for cat, amt in top:
            pct = (amt / expense * 100.0) if expense > 0 else 0.0
            lines.append(f"  - {cat}: {amt:,.0f} ({pct:.0f}%)")
        lines.append("")

    if budgets:
        lines.append("🎯 Budget status:")
        for b in budgets:
            flag = ""
            if b["over_actual"]:
                flag = "  ⚠ over"
            elif b["over_projected"]:
                flag = "  ⚠ on pace to exceed"
            lines.append(
                f"  - {b['category']}: {b['spent']:,.0f} / {b['monthly_limit']:,.0f}"
                f" ({b['percent']:.0f}%){flag}"
            )
        lines.append("")
    else:
        lines.append("🎯 No budgets set yet — try `/budget set food 2000k`.")
        lines.append("")

    lines.append("🔁 Recurring outflows:")
    lines.append(format_recurring_lines(recurring))
    lines.append("")

    lines.append("🔮 Forecast (month end):")
    lines.append(f"  Projected expense: {forecast['projected_monthend_expense']:,.0f}")
    lines.append(f"  Projected net:     {forecast['projected_monthend_net']:,.0f}")
    if forecast["unbilled_recurring"] > 0:
        lines.append(
            f"  (includes {forecast['unbilled_recurring']:,.0f} of recurring "
            "charges expected before month end)"
        )

    return "\n".join(lines).rstrip()
