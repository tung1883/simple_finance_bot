"""Recurring-charge detection + month-end cashflow forecast.

Heuristic: bucket expense rows by (category, amount rounded to nearest 10% of itself);
treat the bucket as recurring if it has at least RECURRING_MIN_OCCURRENCES rows spread
across distinct calendar months, with the median day-gap between sorted occurrences in
RECURRING_MIN_GAP_DAYS..RECURRING_MAX_GAP_DAYS.

Tunable via env:
  RECURRING_MIN_OCCURRENCES  (default 2)
  RECURRING_MIN_GAP_DAYS     (default 25)
  RECURRING_MAX_GAP_DAYS     (default 35)
  RECURRING_AMOUNT_TOLERANCE (default 0.15 — ±15%)
"""
import math
import os
import statistics
from calendar import monthrange
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from db import connect
from ledger import month_to_date_totals


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except ValueError:
        return default


def _parse_time(s: str) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[: len(fmt) + 2].split(".")[0], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _expense_rows(user_id: int, since_days: int = 180) -> List[Tuple[str, float, datetime]]:
    cur = connect().cursor()
    cur.execute(
        """
        SELECT LOWER(IFNULL(category,'other')), amount, time
        FROM transactions
        WHERE user_id = ? AND type = 'expense'
          AND time >= date('now', ?)
        ORDER BY time ASC
        """,
        (user_id, f"-{int(since_days)} days"),
    )
    rows: List[Tuple[str, float, datetime]] = []
    for cat, amt, t in cur.fetchall():
        dt = _parse_time(t)
        if dt is None or amt is None:
            continue
        rows.append((cat, float(amt), dt))
    return rows


def _bucket_key(cat: str, amount: float, tolerance: float) -> Tuple[str, int]:
    """Coarse bucket id used by tests: same category and amounts within (1+tolerance)
    ratio of each other land in the same id. Log-spaced so the bucket size scales
    with the amount.
    """
    if amount <= 0:
        return cat, 0
    return cat, int(round(math.log(amount) / math.log(1 + tolerance)))


def _cluster_by_amount(
    items: List[Tuple[float, datetime]], tolerance: float
) -> List[List[Tuple[float, datetime]]]:
    """Greedy sweep over sorted amounts: chain adjacent items whose ratio is within
    1+tolerance of the previous one. Handles small ladders like (92, 100, 108) as
    one cluster while keeping (100, 500) split.
    """
    if not items:
        return []
    sorted_items = sorted(items, key=lambda i: i[0])
    clusters: List[List[Tuple[float, datetime]]] = [[sorted_items[0]]]
    for amt, dt in sorted_items[1:]:
        prev_amt = clusters[-1][-1][0]
        if prev_amt > 0 and amt <= prev_amt * (1 + tolerance):
            clusters[-1].append((amt, dt))
        else:
            clusters.append([(amt, dt)])
    return clusters


def detect_recurring(user_id: int, today: Optional[date] = None) -> List[Dict]:
    """Return one dict per detected recurring expense series."""
    today = today or date.today()
    min_occ = _env_int("RECURRING_MIN_OCCURRENCES", 2)
    min_gap = _env_int("RECURRING_MIN_GAP_DAYS", 25)
    max_gap = _env_int("RECURRING_MAX_GAP_DAYS", 35)
    tol = _env_float("RECURRING_AMOUNT_TOLERANCE", 0.15)

    rows = _expense_rows(user_id)
    if not rows:
        return []

    by_cat: Dict[str, List[Tuple[float, datetime]]] = {}
    for cat, amt, dt in rows:
        by_cat.setdefault(cat, []).append((amt, dt))

    results: List[Dict] = []
    for cat, items in by_cat.items():
        for cluster in _cluster_by_amount(items, tol):
            if len(cluster) < min_occ:
                continue
            cluster.sort(key=lambda o: o[1])
            months_seen = {(o[1].year, o[1].month) for o in cluster}
            if len(months_seen) < min_occ:
                continue
            gaps = [(cluster[i][1] - cluster[i - 1][1]).days for i in range(1, len(cluster))]
            if not gaps:
                continue
            median_gap = statistics.median(gaps)
            if not (min_gap <= median_gap <= max_gap):
                continue
            amounts = [o[0] for o in cluster]
            avg_amount = statistics.mean(amounts)
            last_dt = cluster[-1][1]
            billed_this_month = last_dt.year == today.year and last_dt.month == today.month
            results.append(
                {
                    "category": cat,
                    "avg_amount": avg_amount,
                    "occurrences": len(cluster),
                    "median_gap_days": median_gap,
                    "last_charge": last_dt.date().isoformat(),
                    "billed_this_month": billed_this_month,
                }
            )
    results.sort(key=lambda r: r["avg_amount"], reverse=True)
    return results


def format_recurring_lines(items: List[Dict]) -> str:
    if not items:
        return "(no recurring charges detected — need ≥2 months of similar transactions)"
    lines: List[str] = []
    for r in items:
        marker = "✓ billed" if r["billed_this_month"] else "⏳ pending"
        lines.append(
            f"  - {r['category']}: ~{r['avg_amount']:,.0f}/mo "
            f"({r['occurrences']}× over ~{r['median_gap_days']:.0f}d, last {r['last_charge']}, {marker})"
        )
    return "\n".join(lines)


def forecast_monthend_expense(user_id: int, today: Optional[date] = None) -> Dict:
    """Project total expense for the current calendar month.

    projection = MTD_spend + average_daily_burn_so_far * remaining_days
                 + sum(unbilled recurring this month)
    """
    today = today or date.today()
    days_in_month = monthrange(today.year, today.month)[1]
    remaining = max(0, days_in_month - today.day)

    totals = month_to_date_totals(user_id, today)
    mtd_expense = totals.get("expense", 0.0)
    mtd_income = totals.get("income", 0.0)
    avg_daily = (mtd_expense / today.day) if today.day > 0 else 0.0

    recurring = detect_recurring(user_id, today)
    unbilled = sum(r["avg_amount"] for r in recurring if not r["billed_this_month"])

    projected = mtd_expense + avg_daily * remaining + unbilled
    return {
        "today": today.isoformat(),
        "days_in_month": days_in_month,
        "days_remaining": remaining,
        "mtd_income": mtd_income,
        "mtd_expense": mtd_expense,
        "avg_daily_burn": avg_daily,
        "unbilled_recurring": unbilled,
        "projected_monthend_expense": projected,
        "projected_monthend_net": mtd_income - projected,
        "recurring": recurring,
    }


def format_forecast_block(user_id: int, today: Optional[date] = None) -> str:
    f = forecast_monthend_expense(user_id, today)
    lines = [
        "FORECAST (month-to-date + linear burn + unbilled recurring):",
        f"  Days into month:    {f['days_in_month'] - f['days_remaining']}/{f['days_in_month']}",
        f"  Spent so far:       {f['mtd_expense']:,.0f}",
        f"  Income so far:      {f['mtd_income']:,.0f}",
        f"  Avg daily burn:     {f['avg_daily_burn']:,.0f}",
        f"  Unbilled recurring: {f['unbilled_recurring']:,.0f}",
        f"  Projected expense:  {f['projected_monthend_expense']:,.0f}",
        f"  Projected net:      {f['projected_monthend_net']:,.0f}",
    ]
    return "\n".join(lines)
