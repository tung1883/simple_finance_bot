from datetime import date

from budgets import (
    budget_status,
    clear_budget,
    list_budgets,
    over_budget_alerts,
    set_budget,
)
from db import connect


def _seed_expense(user_id: int, amount: float, category: str, day: int, month: int = None, year: int = None):
    today = date.today()
    m = month or today.month
    y = year or today.year
    ts = f"{y:04d}-{m:02d}-{day:02d} 12:00:00"
    c = connect()
    c.execute(
        "INSERT INTO transactions (user_id, type, amount, category, time) VALUES (?, 'expense', ?, ?, ?)",
        (user_id, amount, category, ts),
    )
    c.commit()


class TestSetAndList:
    def test_upsert(self):
        set_budget(1, "food", 1000)
        set_budget(1, "food", 2000)
        rows = list_budgets(1)
        assert rows == [{"category": "food", "monthly_limit": 2000}]

    def test_category_lowercased(self):
        set_budget(1, "FOOD", 500)
        assert list_budgets(1)[0]["category"] == "food"

    def test_clear(self):
        set_budget(1, "food", 1000)
        assert clear_budget(1, "food") is True
        assert list_budgets(1) == []

    def test_clear_missing_returns_false(self):
        assert clear_budget(1, "ghost") is False

    def test_per_user(self):
        set_budget(1, "food", 1000)
        set_budget(2, "food", 5000)
        assert list_budgets(1) == [{"category": "food", "monthly_limit": 1000}]
        assert list_budgets(2) == [{"category": "food", "monthly_limit": 5000}]


class TestStatus:
    def test_no_budgets_returns_empty(self):
        assert budget_status(1) == []

    def test_under_limit(self):
        today = date.today().replace(day=15)
        _seed_expense(1, 300, "food", day=10)
        set_budget(1, "food", 1000)
        rows = budget_status(1, today=today)
        assert len(rows) == 1
        row = rows[0]
        assert row["category"] == "food"
        assert row["spent"] == 300
        assert row["over_actual"] is False

    def test_actual_over(self):
        today = date.today().replace(day=15)
        _seed_expense(1, 1500, "food", day=10)
        set_budget(1, "food", 1000)
        rows = budget_status(1, today=today)
        assert rows[0]["over_actual"] is True

    def test_projected_over(self):
        # Day 15 of a 30-day month: spent 600 → projected 1200 → over 1000.
        today = date(2026, 6, 15)  # June: 30 days
        _seed_expense(1, 600, "food", day=10, month=6, year=2026)
        set_budget(1, "food", 1000)
        rows = budget_status(1, today=today)
        assert rows[0]["over_projected"] is True
        assert rows[0]["over_actual"] is False

    def test_total_aggregates_all_categories(self):
        today = date(2026, 6, 15)
        _seed_expense(1, 400, "food", day=5, month=6, year=2026)
        _seed_expense(1, 300, "transport", day=7, month=6, year=2026)
        set_budget(1, "total", 1000)
        rows = budget_status(1, today=today)
        total_row = [r for r in rows if r["category"] == "total"][0]
        assert total_row["spent"] == 700


class TestOverBudgetAlerts:
    def test_early_month_only_actual_overs(self):
        today = date(2026, 6, 3)  # day < 5 → only actual overs
        _seed_expense(1, 1200, "food", day=1, month=6, year=2026)
        set_budget(1, "food", 1000)
        alerts = over_budget_alerts(1, today=today)
        assert len(alerts) == 1
        assert alerts[0]["category"] == "food"

    def test_early_month_skips_projected_only(self):
        today = date(2026, 6, 3)
        _seed_expense(1, 200, "food", day=1, month=6, year=2026)
        set_budget(1, "food", 1000)
        alerts = over_budget_alerts(1, today=today)
        assert alerts == []

    def test_mid_month_includes_projected(self):
        today = date(2026, 6, 15)
        _seed_expense(1, 700, "food", day=10, month=6, year=2026)  # projected 1400
        set_budget(1, "food", 1000)
        alerts = over_budget_alerts(1, today=today)
        assert len(alerts) == 1
