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
    run_web_search,
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
