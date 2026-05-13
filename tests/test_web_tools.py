from unittest.mock import MagicMock, patch

import pytest

import web_tools
from web_tools import (
    _ddg_html_text_results,
    _format_search_lines,
    _host_blocked,
    _normalize_fetch_url,
    _strip_html_fallback,
    _strip_tags,
    _unwrap_ddg_redirect,
    auto_fetch_article,
    is_likely_homepage,
    pick_article_url,
    run_fetch_url,
    run_web_search,
    wants_deep_summary,
    web_search_results,
)


class TestStripTags:
    def test_removes_tags(self):
        assert _strip_tags("<b>hi</b> world") == "hi world"

    def test_unescapes_entities(self):
        assert _strip_tags("a &amp; b") == "a & b"

    def test_collapses_whitespace(self):
        assert _strip_tags("  many   spaces\t<br>here  ") == "many spaces here"

    def test_blank(self):
        assert _strip_tags("") == ""


class TestUnwrapDdgRedirect:
    def test_unwraps_uddg(self):
        assert (
            _unwrap_ddg_redirect("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1")
            == "https://example.com/a?b=1"
        )

    def test_unwraps_u3(self):
        assert (
            _unwrap_ddg_redirect("https://duckduckgo.com/l/?u3=https%3A%2F%2Fexample.com%2F")
            == "https://example.com/"
        )

    def test_passthrough_non_ddg(self):
        assert _unwrap_ddg_redirect("https://example.com/foo") == "https://example.com/foo"

    def test_blank(self):
        assert _unwrap_ddg_redirect("") == ""


class TestHostBlocked:
    def test_blocks_localhost(self):
        bad, _ = _host_blocked("localhost")
        assert bad is True

    def test_blocks_local_tld(self):
        bad, _ = _host_blocked("printer.local")
        assert bad is True

    def test_blocks_private_ip(self):
        bad, _ = _host_blocked("10.0.0.1")
        assert bad is True

    def test_blocks_loopback_ip(self):
        bad, _ = _host_blocked("127.0.0.1")
        assert bad is True

    def test_allows_public_host(self):
        bad, _ = _host_blocked("example.com")
        assert bad is False

    def test_blocks_blank(self):
        bad, _ = _host_blocked("")
        assert bad is True


class TestNormalizeFetchUrl:
    def test_url_alone(self):
        assert _normalize_fetch_url("https://example.com/x") == "https://example.com/x"

    def test_url_with_prefix(self):
        assert _normalize_fetch_url("URL: https://example.com/x") == "https://example.com/x"

    def test_trailing_punctuation_stripped(self):
        assert _normalize_fetch_url("https://example.com/x).") == "https://example.com/x"

    def test_no_url(self):
        assert _normalize_fetch_url("just text") == ""

    def test_blank(self):
        assert _normalize_fetch_url("") == ""


class TestStripHtmlFallback:
    def test_drops_script_style(self):
        html = "<script>bad()</script><p>Hi</p><style>x</style>"
        assert _strip_html_fallback(html) == "Hi"

    def test_collapses(self):
        assert _strip_html_fallback("<p>a</p>\n\n<p>b</p>") == "a b"


class TestFormatSearchLines:
    def test_renders(self):
        out = _format_search_lines(
            "rates",
            [{"title": "Hello", "href": "https://x", "body": "snippet"}],
        )
        assert "rates" in out
        assert "Hello" in out
        assert "https://x" in out
        assert "snippet" in out


class TestDdgHtmlTextResults:
    def test_parses_results(self):
        page = (
            '<div class="results_links_deep web-result">'
            '<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2F">Title A</a>'
            '<a class="result__snippet" href="x">Body A</a>'
            "</div>"
            '<div class="results_links_deep web-result">'
            '<a class="result__a" href="https://b.com/">Title B</a>'
            '<a class="result__snippet" href="x">Body B</a>'
            "</div>"
        )
        mock = MagicMock()
        mock.text = page
        mock.raise_for_status = MagicMock()
        with patch.object(web_tools.requests, "post", return_value=mock):
            rows = _ddg_html_text_results("query", max_results=5)
        assert len(rows) == 2
        assert rows[0]["title"] == "Title A"
        assert rows[0]["href"] == "https://a.com/"
        assert rows[1]["href"] == "https://b.com/"

    def test_marker_missing_returns_empty(self):
        mock = MagicMock()
        mock.text = "<html>no results blocks</html>"
        mock.raise_for_status = MagicMock()
        with patch.object(web_tools.requests, "post", return_value=mock):
            assert _ddg_html_text_results("q", 5) == []


class TestRunWebSearch:
    def test_disabled_returns_message(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")
        assert "turned off" in run_web_search("anything")

    def test_empty_query(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        assert "empty query" in run_web_search("")

    def test_catches_search_failure(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        with patch.object(
            web_tools, "web_search_results", side_effect=RuntimeError("boom")
        ):
            out = run_web_search("query")
        assert "Web search failed" in out
        assert "boom" in out


class TestWebSearchResults:
    def test_disabled_raises(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")
        with pytest.raises(RuntimeError):
            web_search_results("anything")

    def test_empty_query_raises(self):
        with pytest.raises(ValueError):
            web_search_results("")

    def test_returns_structured_dicts(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        page = (
            '<div class="results_links_deep web-result">'
            '<a class="result__a" href="https://a.com/article-1">Title A</a>'
            '<a class="result__snippet" href="x">Body A</a>'
            "</div>"
        )
        mock = MagicMock(text=page, raise_for_status=MagicMock())
        with patch.dict("sys.modules", {"ddgs": None}):  # force HTML path
            with patch.object(web_tools.requests, "post", return_value=mock):
                results = web_search_results("query")
        assert len(results) == 1
        assert results[0]["title"] == "Title A"
        assert results[0]["href"] == "https://a.com/article-1"


class TestIsLikelyHomepage:
    @pytest.mark.parametrize(
        "url",
        [
            "https://vnexpress.net/",
            "https://vnexpress.net",
            "https://vnexpress.net/en",
            "https://vnexpress.net/kinh-doanh",
            "https://vietnamnet.vn/thoi-su",
            "https://www.24h.com.vn/tin-tuc-trong-ngay-c46.html",
            "https://example.com/news",
        ],
    )
    def test_returns_true_for_known_section_paths(self, url):
        assert is_likely_homepage(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://vnexpress.net/2026/05/14/article-slug-here-123456.html",
            "https://example.com/news/2026/some-headline-789",
            "https://vietnamnet.vn/thoi-su/abc-xyz-12345",
            "https://baomoi.com/some-deep/path/here/article-id-9876.html",
        ],
    )
    def test_returns_false_for_article_paths(self, url):
        assert is_likely_homepage(url) is False

    def test_thin_body_with_brand_title_is_homepage(self):
        assert (
            is_likely_homepage(
                "https://vnexpress.net/some-unknown-path-not-in-list",
                title="VnExpress - Tin nhanh Việt Nam",
                body="",
            )
            is True
        )

    def test_thick_body_overrides_brand_signal(self):
        long_body = (
            "This is a detailed news article about Vietnam's economy with at least one "
            "hundred and twenty characters of meaningful content to override the homepage signal."
        )
        assert (
            is_likely_homepage(
                "https://vnexpress.net/some-unknown-path-not-in-list",
                title="VnExpress",
                body=long_body,
            )
            is False
        )

    def test_blank_url_returns_true(self):
        assert is_likely_homepage("") is True


class TestWantsDeepSummary:
    @pytest.mark.parametrize(
        "text",
        [
            "summarize the news today",
            "analyze the latest events",
            "give me an in-depth view",
            "tóm tắt tin tức hôm nay",
            "TÓM TẮT",
            "phân tích tin tài chính",
            "phan tich (without diacritics)",
            "chi tiết về vụ việc",
            "chi tiet ve viec do",
            "giải thích giúp tôi",
            "tổng hợp tin",
        ],
    )
    def test_true_for_summary_intent(self, text):
        assert wants_deep_summary(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "hôm nay thế nào",
            "ăn 50k",
            "lương 10 triệu",
            "what time is it",
            "",
        ],
    )
    def test_false_otherwise(self, text):
        assert wants_deep_summary(text) is False


class TestPickArticleUrl:
    def test_picks_article_over_homepage(self):
        results = [
            {"href": "https://vnexpress.net/", "title": "VnExpress", "body": ""},
            {
                "href": "https://vnexpress.net/2026/05/14/article-headline.html",
                "title": "Headline",
                "body": "Article excerpt here.",
            },
        ]
        picked = pick_article_url(results)
        assert picked is not None
        assert "article-headline.html" in picked["href"]

    def test_returns_none_when_all_homepages(self):
        results = [
            {"href": "https://vnexpress.net/", "title": "VnExpress", "body": ""},
            {"href": "https://baomoi.com/", "title": "Báo Mới", "body": ""},
        ]
        assert pick_article_url(results) is None

    def test_returns_none_for_empty_list(self):
        assert pick_article_url([]) is None

    def test_skips_results_without_url(self):
        results = [
            {"href": "", "title": "blank"},
            {"href": "https://example.com/article-1", "title": "ok", "body": "x" * 150},
        ]
        picked = pick_article_url(results)
        assert picked is not None
        assert picked["href"] == "https://example.com/article-1"


class TestAutoFetchArticle:
    def _good_fetch(self, *args, **kwargs):
        return ("Article body text here.", "text/html", None)

    def _bad_fetch(self, *args, **kwargs):
        return (None, None, "could not extract readable text")

    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("WEB_AUTOFETCH_ENABLED", "1")
        monkeypatch.setenv("WEB_FETCH_ENABLED", "1")

    def test_fires_on_summary_intent_with_article_present(self):
        results = [
            {"href": "https://vnexpress.net/", "title": "VnExpress", "body": ""},
            {
                "href": "https://vnexpress.net/2026/05/14/some-article.html",
                "title": "Headline",
                "body": "ok",
            },
        ]
        with patch.object(web_tools, "_do_fetch_url", self._good_fetch):
            out = auto_fetch_article("query", results, "summarize this for me")
        assert out is not None
        url, text = out
        assert "some-article.html" in url
        assert "Article body text here." in text

    def test_fires_when_top_5_are_mostly_homepages(self, monkeypatch):
        monkeypatch.setenv("WEB_AUTOFETCH_MIN_HOMEPAGES", "3")
        results = [
            {"href": "https://vnexpress.net/", "title": "VnExpress", "body": ""},
            {"href": "https://baomoi.com/", "title": "Báo Mới", "body": ""},
            {"href": "https://vietnamnet.vn/", "title": "VietnamNet", "body": ""},
            {
                "href": "https://example.com/article-1.html",
                "title": "Headline",
                "body": "ok",
            },
        ]
        with patch.object(web_tools, "_do_fetch_url", self._good_fetch):
            out = auto_fetch_article("query", results, "tin tức hôm nay")
        assert out is not None

    def test_skips_when_no_summary_intent_and_few_homepages(self, monkeypatch):
        monkeypatch.setenv("WEB_AUTOFETCH_MIN_HOMEPAGES", "3")
        results = [
            {
                "href": "https://example.com/article-1.html",
                "title": "Headline",
                "body": "ok",
            },
            {
                "href": "https://example.com/article-2.html",
                "title": "Other",
                "body": "ok",
            },
        ]
        mock_fetch = MagicMock(return_value=("text", "text/html", None))
        with patch.object(web_tools, "_do_fetch_url", mock_fetch):
            out = auto_fetch_article("query", results, "what time is it")
        assert out is None
        mock_fetch.assert_not_called()

    def test_returns_none_when_no_article_candidate(self):
        results = [
            {"href": "https://vnexpress.net/", "title": "VnExpress", "body": ""},
            {"href": "https://baomoi.com/", "title": "Báo Mới", "body": ""},
        ]
        mock_fetch = MagicMock(return_value=("text", "text/html", None))
        with patch.object(web_tools, "_do_fetch_url", mock_fetch):
            out = auto_fetch_article("query", results, "summarize this")
        assert out is None
        mock_fetch.assert_not_called()

    def test_returns_none_on_fetch_failure(self):
        results = [
            {
                "href": "https://example.com/article-1.html",
                "title": "Headline",
                "body": "ok",
            },
        ]
        with patch.object(web_tools, "_do_fetch_url", self._bad_fetch):
            out = auto_fetch_article("query", results, "summarize this")
        assert out is None

    def test_kill_switch_disables(self, monkeypatch):
        monkeypatch.setenv("WEB_AUTOFETCH_ENABLED", "0")
        results = [
            {
                "href": "https://example.com/article-1.html",
                "title": "Headline",
                "body": "ok",
            },
        ]
        mock_fetch = MagicMock(return_value=("text", "text/html", None))
        with patch.object(web_tools, "_do_fetch_url", mock_fetch):
            out = auto_fetch_article("query", results, "summarize this")
        assert out is None
        mock_fetch.assert_not_called()

    def test_passes_tight_budget_to_fetch(self, monkeypatch):
        monkeypatch.setenv("WEB_AUTOFETCH_TIMEOUT_SEC", "5")
        monkeypatch.setenv("WEB_AUTOFETCH_MAX_CHARS", "3000")
        results = [
            {
                "href": "https://example.com/article-1.html",
                "title": "Headline",
                "body": "ok",
            },
        ]
        captured = {}

        def spy(url, *, timeout, max_bytes, max_chars):
            captured["timeout"] = timeout
            captured["max_chars"] = max_chars
            return ("text", "text/html", None)

        with patch.object(web_tools, "_do_fetch_url", spy):
            auto_fetch_article("q", results, "summarize")
        assert captured["timeout"] == 5
        assert captured["max_chars"] == 3000


class TestRunFetchUrlOverrides:
    def test_override_kwargs_propagate(self, monkeypatch):
        monkeypatch.setenv("WEB_FETCH_ENABLED", "1")
        captured = {}

        def spy(url, *, timeout, max_bytes, max_chars):
            captured["timeout"] = timeout
            captured["max_chars"] = max_chars
            return ("text", "text/html", None)

        with patch.object(web_tools, "_do_fetch_url", spy):
            run_fetch_url(
                "https://example.com/x", timeout_override=7, max_chars_override=4000
            )
        assert captured["timeout"] == 7
        assert captured["max_chars"] == 4000
