from datetime import date, datetime, timedelta

from db import connect
from recurring import (
    _bucket_key,
    detect_recurring,
    forecast_monthend_expense,
)


def _ts(d: date) -> str:
    return f"{d.isoformat()} 12:00:00"


def _insert(user_id: int, amount: float, category: str, when: date, tx_type: str = "expense"):
    c = connect()
    c.execute(
        "INSERT INTO transactions (user_id, type, amount, category, time) VALUES (?, ?, ?, ?, ?)",
        (user_id, tx_type, amount, category, _ts(when)),
    )
    c.commit()


class TestBucketKey:
    def test_same_amount_same_bucket(self):
        assert _bucket_key("netflix", 100, 0.15) == _bucket_key("netflix", 100, 0.15)

    def test_close_amounts_share_bucket(self):
        # 100 and 105 within ±15% width should land in the same bucket.
        assert _bucket_key("netflix", 100, 0.15) == _bucket_key("netflix", 105, 0.15)

    def test_distant_amounts_split(self):
        assert _bucket_key("food", 100, 0.15) != _bucket_key("food", 500, 0.15)

    def test_different_categories_split(self):
        assert _bucket_key("food", 100, 0.15) != _bucket_key("transport", 100, 0.15)


class TestDetectRecurring:
    def test_detects_monthly_subscription(self):
        today = date(2026, 6, 15)
        for delta_months in range(3):
            d = date(2026, 6 - delta_months, 5)
            _insert(1, 100_000, "netflix", d)
        results = detect_recurring(1, today=today)
        assert len(results) == 1
        r = results[0]
        assert r["category"] == "netflix"
        assert r["occurrences"] == 3
        assert 25 <= r["median_gap_days"] <= 35

    def test_single_occurrence_not_recurring(self):
        today = date(2026, 6, 15)
        _insert(1, 100_000, "netflix", date(2026, 5, 5))
        assert detect_recurring(1, today=today) == []

    def test_irregular_gap_skipped(self):
        # Two charges 60 days apart — not monthly.
        today = date(2026, 6, 15)
        _insert(1, 100_000, "rare", date(2026, 4, 5))
        _insert(1, 100_000, "rare", date(2026, 6, 5))
        assert detect_recurring(1, today=today) == []

    def test_amount_variance_within_tolerance(self):
        today = date(2026, 6, 15)
        # ±10% wiggle room — within default 15% tolerance.
        for i, d in enumerate([date(2026, 4, 5), date(2026, 5, 5), date(2026, 6, 5)]):
            _insert(1, 100_000 + (i - 1) * 8_000, "gym", d)
        results = detect_recurring(1, today=today)
        assert len(results) == 1
        assert results[0]["category"] == "gym"

    def test_billed_this_month_flag(self):
        today = date(2026, 6, 20)
        _insert(1, 100_000, "spotify", date(2026, 4, 5))
        _insert(1, 100_000, "spotify", date(2026, 5, 5))
        _insert(1, 100_000, "spotify", date(2026, 6, 5))
        r = detect_recurring(1, today=today)[0]
        assert r["billed_this_month"] is True

    def test_unbilled_this_month(self):
        # Latest charge in May; June not yet hit.
        today = date(2026, 6, 20)
        _insert(1, 100_000, "spotify", date(2026, 3, 5))
        _insert(1, 100_000, "spotify", date(2026, 4, 5))
        _insert(1, 100_000, "spotify", date(2026, 5, 5))
        r = detect_recurring(1, today=today)[0]
        assert r["billed_this_month"] is False


class TestForecast:
    def test_empty_returns_zeros(self):
        f = forecast_monthend_expense(1, today=date(2026, 6, 15))
        assert f["mtd_expense"] == 0
        assert f["projected_monthend_expense"] == 0

    def test_linear_projection(self):
        # Spent 300 by day 10 of a 30-day month, no recurring → projected = 300 * (30/10) = 900.
        today = date(2026, 6, 10)
        _insert(1, 300, "food", date(2026, 6, 5))
        f = forecast_monthend_expense(1, today=today)
        assert f["mtd_expense"] == 300
        assert abs(f["projected_monthend_expense"] - 900) < 1

    def test_unbilled_recurring_added(self):
        today = date(2026, 6, 10)
        # Recurring 200/mo seen Apr+May, not yet billed in June.
        _insert(1, 200, "spotify", date(2026, 4, 5))
        _insert(1, 200, "spotify", date(2026, 5, 5))
        # Random food spend so MTD isn't zero.
        _insert(1, 100, "food", date(2026, 6, 8))
        f = forecast_monthend_expense(1, today=today)
        assert f["unbilled_recurring"] == 200
        # projection includes 100 MTD + 100/10 * 20 remaining + 200 unbilled = 100 + 200 + 200 = 500
        assert abs(f["projected_monthend_expense"] - 500) < 1
