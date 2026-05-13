from datetime import date

from budgets import set_budget
from db import connect
from review import build_monthly_review


def _seed(user_id: int, amount: float, category: str, when: date, tx_type: str = "expense"):
    c = connect()
    c.execute(
        "INSERT INTO transactions (user_id, type, amount, category, time) VALUES (?, ?, ?, ?, ?)",
        (user_id, tx_type, amount, category, f"{when.isoformat()} 12:00:00"),
    )
    c.commit()


class TestMonthlyReview:
    def test_smoke_empty(self):
        text = build_monthly_review(1, today=date(2026, 6, 15))
        assert "Monthly review" in text
        assert "Income:" in text
        assert "Recurring outflows" in text

    def test_includes_totals(self):
        today = date(2026, 6, 15)
        _seed(1, 10_000_000, "salary", date(2026, 6, 1), tx_type="income")
        _seed(1, 500_000, "food", date(2026, 6, 3))
        text = build_monthly_review(1, today=today)
        assert "10,000,000" in text
        assert "500,000" in text

    def test_includes_budget_when_set(self):
        today = date(2026, 6, 15)
        _seed(1, 600_000, "food", date(2026, 6, 5))
        set_budget(1, "food", 1_000_000)
        text = build_monthly_review(1, today=today)
        assert "food" in text
        assert "1,000,000" in text

    def test_includes_recurring_when_detected(self):
        today = date(2026, 6, 15)
        for d in (date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5)):
            _seed(1, 100_000, "netflix", d)
        text = build_monthly_review(1, today=today)
        assert "netflix" in text
