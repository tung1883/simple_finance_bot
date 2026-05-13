"""Integration tests for `coach.prefetch_live_web_for_coach`.

The function combines web_search results with a deterministic auto-fetch. We mock the
underlying web_tools functions instead of HTTP so tests don't hit the network.
"""
from unittest.mock import patch

import pytest

import coach
import web_tools


SAMPLE_RESULTS = [
    {"href": "https://vnexpress.net/", "title": "VnExpress", "body": ""},
    {"href": "https://baomoi.com/", "title": "Báo Mới", "body": ""},
    {
        "href": "https://vnexpress.net/2026/05/14/article-headline.html",
        "title": "Headline of the day",
        "body": "Short snippet",
    },
]


class TestPrefetchGuards:
    def test_returns_empty_when_not_needed(self):
        out = coach.prefetch_live_web_for_coach("hi", needs_live_web=False)
        assert out == ""

    def test_returns_empty_when_search_disabled(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "0")
        out = coach.prefetch_live_web_for_coach("hi", needs_live_web=True)
        assert out == ""

    def test_returns_empty_when_no_query(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        out = coach.prefetch_live_web_for_coach("", needs_live_web=True)
        assert out == ""


class TestPrefetchSnippetsOnly:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        monkeypatch.setenv("WEB_AUTOFETCH_ENABLED", "1")

    def test_snippets_block_present_without_enrichment(self):
        # Single article result, no summary intent, only one homepage → no enrichment.
        results = [
            {
                "href": "https://example.com/article-1.html",
                "title": "Headline",
                "body": "Body content here.",
            },
        ]
        with patch.object(web_tools, "web_search_results", return_value=results), \
             patch.object(web_tools, "auto_fetch_article", return_value=None):
            out = coach.prefetch_live_web_for_coach(
                "what time is it", needs_live_web=True
            )
        assert "LIVE WEB" in out
        assert "SEARCH SNIPPETS" in out
        assert "PREFETCHED ARTICLE" not in out
        assert "https://example.com/article-1.html" in out

    def test_does_not_instruct_llm_to_fetch_url(self):
        """Regression guard: the old prompt told the LLM to call fetch_url on thin
        snippets. After the deterministic auto-fetch, that instruction is gone — keeping
        it would cause double-fetches."""
        results = SAMPLE_RESULTS
        with patch.object(web_tools, "web_search_results", return_value=results), \
             patch.object(web_tools, "auto_fetch_article", return_value=None):
            out = coach.prefetch_live_web_for_coach(
                "anything", needs_live_web=True
            )
        assert "call fetch_url" not in out.lower()
        assert "and call fetch_url" not in out.lower()


class TestPrefetchWithEnrichment:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        monkeypatch.setenv("WEB_AUTOFETCH_ENABLED", "1")

    def test_article_block_appended_when_enrichment_fires(self):
        fetched_url = "https://vnexpress.net/2026/05/14/article-headline.html"
        article_text = "This is the extracted article body with real news content."
        with patch.object(web_tools, "web_search_results", return_value=SAMPLE_RESULTS), \
             patch.object(
                 web_tools, "auto_fetch_article", return_value=(fetched_url, article_text)
             ):
            out = coach.prefetch_live_web_for_coach(
                "summarize today's news", needs_live_web=True
            )
        assert "SEARCH SNIPPETS" in out
        assert "PREFETCHED ARTICLE" in out
        assert article_text in out
        assert fetched_url in out

    def test_article_block_appears_after_snippets(self):
        fetched_url = "https://example.com/article.html"
        article_text = "Article body."
        with patch.object(web_tools, "web_search_results", return_value=SAMPLE_RESULTS), \
             patch.object(
                 web_tools, "auto_fetch_article", return_value=(fetched_url, article_text)
             ):
            out = coach.prefetch_live_web_for_coach(
                "summarize", needs_live_web=True
            )
        assert out.index("SEARCH SNIPPETS") < out.index("PREFETCHED ARTICLE")


class TestPrefetchFailureModes:
    def test_search_exception_captured(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        with patch.object(
            web_tools, "web_search_results", side_effect=RuntimeError("boom")
        ):
            out = coach.prefetch_live_web_for_coach("anything", needs_live_web=True)
        assert "prefetch failed" in out.lower()
        assert "RuntimeError" in out

    def test_empty_results_block(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_ENABLED", "1")
        with patch.object(web_tools, "web_search_results", return_value=[]):
            out = coach.prefetch_live_web_for_coach(
                "very obscure", needs_live_web=True
            )
        assert "no results" in out.lower()
