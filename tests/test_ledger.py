from datetime import date

from db import connect
from ledger import (
    add_transaction,
    all_user_chats,
    build_finance_context,
    get_recent_transactions,
    get_user_summary,
    month_to_date_spend_by_category,
    month_to_date_totals,
    record_user_chat,
    search_ledger_db,
    spending_by_category_block,
)


def _seed(user_id: int, amount: float, category: str, when: date, tx_type: str = "expense"):
    c = connect()
    c.execute(
        "INSERT INTO transactions (user_id, type, amount, category, time) VALUES (?, ?, ?, ?, ?)",
        (user_id, tx_type, amount, category, f"{when.isoformat()} 12:00:00"),
    )
    c.commit()


class TestAddTransaction:
    def test_round_trip(self):
        add_transaction(1, "expense", 50_000, "food")
        rows = get_recent_transactions(1)
        assert len(rows) == 1
        assert rows[0][0] == "expense"
        assert rows[0][1] == 50_000
        assert rows[0][2] == "food"


class TestSummary:
    def test_groups_by_type(self):
        add_transaction(1, "expense", 100, "food")
        add_transaction(1, "expense", 50, "food")
        add_transaction(1, "income", 1000, "salary")
        s = get_user_summary(1)
        assert s["expense"] == 150
        assert s["income"] == 1000


class TestSearch:
    def test_returns_no_match(self):
        assert "No transactions matched" in search_ledger_db(1, "ghost")

    def test_keyword_match(self):
        add_transaction(1, "expense", 100, "food")
        out = search_ledger_db(1, "food")
        assert "food" in out

    def test_empty_query_returns_recent(self):
        add_transaction(1, "expense", 100, "food")
        out = search_ledger_db(1, "")
        assert "food" in out


class TestSpendingByCategory:
    def test_empty(self):
        assert "No expense rows yet" in spending_by_category_block(1)

    def test_groups_and_orders(self):
        add_transaction(1, "expense", 100, "food")
        add_transaction(1, "expense", 500, "rent")
        out = spending_by_category_block(1)
        # rent should come before food (larger first)
        assert out.index("rent") < out.index("food")


class TestMtdAggregates:
    def test_month_to_date_only_current_month(self):
        today = date(2026, 6, 15)
        _seed(1, 200, "food", date(2026, 5, 28))  # previous month
        _seed(1, 100, "food", date(2026, 6, 5))
        _seed(1, 50, "transport", date(2026, 6, 8))
        mtd = month_to_date_spend_by_category(1, today=today)
        assert mtd == {"food": 100, "transport": 50}

    def test_totals(self):
        today = date(2026, 6, 15)
        _seed(1, 1_000_000, "salary", date(2026, 6, 1), tx_type="income")
        _seed(1, 200_000, "food", date(2026, 6, 5))
        totals = month_to_date_totals(1, today=today)
        assert totals == {"income": 1_000_000, "expense": 200_000}


class TestContextBlock:
    def test_includes_aggregates(self):
        add_transaction(1, "income", 5_000_000, "salary")
        add_transaction(1, "expense", 1_000_000, "food")
        out = build_finance_context(1)
        assert "5,000,000" in out
        assert "1,000,000" in out
        assert "Net (log only):" in out


class TestUserChat:
    def test_record_and_list(self):
        record_user_chat(1, 100)
        record_user_chat(2, 200)
        record_user_chat(1, 101)  # upsert
        chats = dict(all_user_chats())
        assert chats == {1: 101, 2: 200}
