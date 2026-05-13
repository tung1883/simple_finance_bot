from router import (
    extract_first_json_object,
    fallback_router,
    normalize_router_result,
    parse_router_response,
    parse_transaction_heuristic,
)


class TestExtractFirstJsonObject:
    def test_finds_simple_object(self):
        assert extract_first_json_object('hello {"a":1} world') == '{"a":1}'

    def test_handles_nested(self):
        s = 'noise {"a":{"b":[1,2]}} trailing'
        assert extract_first_json_object(s) == '{"a":{"b":[1,2]}}'

    def test_returns_none_when_absent(self):
        assert extract_first_json_object("no braces here") is None


class TestParseRouterResponse:
    def test_plain_json(self):
        assert parse_router_response('{"intent":"chat"}') == {"intent": "chat"}

    def test_fenced_json(self):
        raw = '```json\n{"intent":"finance"}\n```'
        assert parse_router_response(raw) == {"intent": "finance"}

    def test_prose_wrapped_json(self):
        raw = 'Sure! Here you go: {"intent":"help_command","confidence":0.9} ok?'
        assert parse_router_response(raw) == {"intent": "help_command", "confidence": 0.9}

    def test_unparseable_returns_none(self):
        assert parse_router_response("garbage no json") is None


class TestNormalizeRouterResult:
    def test_defaults_when_none(self):
        out = normalize_router_result(None)
        assert out["intent"] == "chat"
        assert out["finance"] is None
        assert out["needs_live_web"] is False
        assert out["web_search_query"] is None

    def test_invalid_intent_coerced_to_chat(self):
        out = normalize_router_result({"intent": "weird"})
        assert out["intent"] == "chat"

    def test_needs_live_web_forced_false_when_finance(self):
        out = normalize_router_result({"intent": "finance", "needs_live_web": True})
        assert out["needs_live_web"] is False

    def test_needs_live_web_string_truthy(self):
        out = normalize_router_result({"intent": "chat", "needs_live_web": "true"})
        assert out["needs_live_web"] is True

    def test_blank_query_becomes_none(self):
        out = normalize_router_result({"intent": "chat", "web_search_query": "  "})
        assert out["web_search_query"] is None

    def test_non_dict_finance_dropped(self):
        out = normalize_router_result({"intent": "finance", "finance": "nope"})
        assert out["finance"] is None


class TestHeuristic:
    def test_vnd_k_expense(self):
        r = parse_transaction_heuristic("ăn 50k")
        assert r is not None
        assert r["finance"]["type"] == "expense"
        assert r["finance"]["amount"] == 50_000
        assert r["finance"]["category"] == "food"

    def test_vnd_tr_income(self):
        r = parse_transaction_heuristic("lương 10tr")
        assert r is not None
        assert r["finance"]["type"] == "income"
        assert r["finance"]["amount"] == 10_000_000

    def test_advice_markers_skip(self):
        assert parse_transaction_heuristic("tôi nên tiết kiệm thế nào với 50k") is None

    def test_help_markers_skip(self):
        assert parse_transaction_heuristic("what commands do you support 50k") is None

    def test_no_amount(self):
        assert parse_transaction_heuristic("ăn trưa") is None

    def test_blank(self):
        assert parse_transaction_heuristic("") is None

    def test_long_input_skipped(self):
        assert parse_transaction_heuristic("ăn 50k " + "x" * 500) is None


class TestFallbackRouter:
    def test_finance_when_heuristic_hits(self):
        r = fallback_router("ăn 50k")
        assert r["intent"] == "finance"

    def test_chat_default(self):
        r = fallback_router("hello there")
        assert r["intent"] == "chat"
        assert r["needs_live_web"] is False
