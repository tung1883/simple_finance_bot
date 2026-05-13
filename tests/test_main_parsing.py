from parsing import parse_add_command, parse_amount


class TestParseAmount:
    def test_k_suffix(self):
        assert parse_amount("50k") == 50_000

    def test_tr_suffix(self):
        assert parse_amount("10tr") == 10_000_000

    def test_trieu_suffix(self):
        assert parse_amount("2trieu") == 2_000_000

    def test_decimal_k(self):
        assert parse_amount("1.5k") == 1500

    def test_plain_number(self):
        assert parse_amount("1500") == 1500

    def test_comma_decimal(self):
        assert parse_amount("1,5k") == 1500

    def test_zero_rejected(self):
        assert parse_amount("0") is None

    def test_blank_rejected(self):
        assert parse_amount("") is None

    def test_garbage_rejected(self):
        assert parse_amount("abc") is None


class TestParseAddCommand:
    def test_expense_with_category(self):
        r = parse_add_command("/add expense 50k ăn trưa")
        assert r == {"type": "expense", "amount": 50_000, "category": "ăn trưa"}

    def test_income_default_category(self):
        r = parse_add_command("/add income 10tr")
        assert r == {"type": "income", "amount": 10_000_000, "category": "other"}

    def test_bad_type_rejected(self):
        assert parse_add_command("/add weird 50k food") is None

    def test_bad_amount_rejected(self):
        assert parse_add_command("/add expense abc food") is None

    def test_too_few_parts(self):
        assert parse_add_command("/add expense") is None
