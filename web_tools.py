"""
Web tools for the finance coach: DuckDuckGo-backed search and guarded HTTP fetch + extract.

Search uses `ddgs` when installed; otherwise (or on failure) the HTML endpoint via `requests`, which
works on Termux without native primp wheels.

The auto-fetch pipeline:
  When prefetch is requested (needs_live_web=True) and the search returns only news-site
  homepages or vague meta-descriptions, `auto_fetch_article` deterministically picks the top
  non-homepage URL and fetches its readable text. This avoids relying on the LLM to chain
  web_search → fetch_url, which is unreliable in practice.

Env:
  WEB_SEARCH_ENABLED — default on; false/0/off disables search.
  WEB_SEARCH_MAX_RESULTS — default 5, max 15.
  WEB_SEARCH_TIMEOUT_SEC — default 25 (HTML fallback and overall search).
  WEB_SEARCH_USER_AGENT — optional override for HTML search POST (must look like a normal browser;
    DuckDuckGo omits organic result markup for obvious bot identities).
  WEB_FETCH_ENABLED — default on; false disables fetch_url.
  WEB_FETCH_TIMEOUT_SEC — default 15.
  WEB_FETCH_MAX_BYTES — default 2_000_000 download cap.
  WEB_FETCH_MAX_CHARS — default 12000 text passed back to the model.
  WEB_AUTOFETCH_ENABLED — default on; kill switch for the deterministic auto-fetch.
  WEB_AUTOFETCH_TIMEOUT_SEC — default 8 (tighter than interactive fetch_url).
  WEB_AUTOFETCH_MAX_CHARS — default 6000.
  WEB_AUTOFETCH_MIN_HOMEPAGES — default 3 (of top 5 results) to trigger when summary intent absent.
"""
import html as html_module
import ipaddress
import logging
import os
import re
import socket
import unicodedata
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests

logger = logging.getLogger(__name__)


# DuckDuckGo serves a minimal HTML shell (no `.web-result` blocks) when the User-Agent looks like an
# obvious bot/scraper identity. Defaults to a short common Windows desktop string known to receive
# full organic markup; operators can override with WEB_SEARCH_USER_AGENT.
_DDGS_HTML_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name, "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def _env_int(name: str, default: int, min_v: int, max_v: int) -> int:
    try:
        n = int(os.getenv(name, str(default)) or default)
    except ValueError:
        n = default
    return max(min_v, min(max_v, n))


def web_search_enabled() -> bool:
    return _env_bool("WEB_SEARCH_ENABLED", True)


def web_fetch_enabled() -> bool:
    return _env_bool("WEB_FETCH_ENABLED", True)


def _host_blocked(hostname: str) -> Tuple[bool, str]:
    if not hostname:
        return True, "missing host"
    h = hostname.lower().strip("[]")
    if h == "localhost" or h.endswith(".localhost") or h.endswith(".local"):
        return True, "localhost / .local host not allowed"
    try:
        ip = ipaddress.ip_address(h)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            return True, "non-public IP"
    except ValueError:
        pass
    return False, ""


def _resolved_ips_blocked(hostname: str) -> Tuple[bool, str]:
    """Block SSRF to internal networks after DNS (hostname only)."""
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError as e:
        return True, f"DNS failed: {e}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            return True, f"host resolves to non-public IP ({ip_str})"
    return False, ""


def _env_search_timeout() -> int:
    return _env_int("WEB_SEARCH_TIMEOUT_SEC", 25, 10, 60)


def _strip_tags(s: str) -> str:
    if not s:
        return ""
    t = re.sub(r"<[^>]+>", " ", s)
    t = html_module.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _unwrap_ddg_redirect(href: str) -> str:
    if not href or not href.startswith("http"):
        return href
    host = (urlparse(href).hostname or "").lower()
    if "duckduckgo.com" not in host:
        return href
    qs = parse_qs(urlparse(href).query)
    for key in ("uddg", "u3"):
        if key in qs and qs[key]:
            return unquote(qs[key][0]).strip()
    return href


def _ddg_html_text_results(query: str, max_results: int) -> List[dict[str, Any]]:
    """DuckDuckGo HTML endpoint + stdlib/regex parsing (no ddgs/primp)."""
    url = "https://html.duckduckgo.com/html/"
    ua = (os.getenv("WEB_SEARCH_USER_AGENT") or "").strip() or _DDGS_HTML_DEFAULT_UA
    r = requests.post(
        url,
        data={"q": query, "b": ""},
        headers={"User-Agent": ua},
        timeout=_env_search_timeout(),
    )
    r.raise_for_status()
    page = r.text
    marker = "results_links_deep web-result"
    if marker not in page:
        return []

    parts = page.split(marker)
    rows: List[dict[str, Any]] = []
    for chunk in parts[1:]:
        if "result--ad" in chunk[:500]:
            continue
        tm = re.search(
            r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            chunk,
            re.DOTALL | re.IGNORECASE,
        )
        if not tm:
            continue
        href_raw = (tm.group(1) or "").strip()
        title_html = tm.group(2) or ""
        sm = re.search(
            r'class="result__snippet"[^>]*href="[^"]*"[^>]*>(.*?)</a>',
            chunk,
            re.DOTALL | re.IGNORECASE,
        )
        body_html = sm.group(1) if sm else ""
        href = _unwrap_ddg_redirect(href_raw)
        rows.append(
            {
                "title": _strip_tags(title_html),
                "href": href,
                "body": _strip_tags(body_html),
            }
        )
        if len(rows) >= max_results:
            break
    return rows


def _format_search_lines(query: str, results: List[dict[str, Any]]) -> str:
    lines = [
        f"Web search results for query: {query}",
        "(Snippets — verify on source pages; dates may be missing.)\n",
    ]
    for i, r in enumerate(results, 1):
        title = (r.get("title") or "").strip()
        href = (r.get("href") or r.get("url") or "").strip()
        body = (r.get("body") or "").strip().replace("\n", " ")
        lines.append(f"{i}. {title}")
        if href:
            lines.append(f"   URL: {href}")
        if body:
            lines.append(f"   {body}")
        lines.append("")
    return "\n".join(lines).strip()


def web_search_results(
    query: str, max_results: Optional[int] = None
) -> List[dict[str, Any]]:
    """Structured search. Raises on failure. Returns [{title, href, body}, ...].

    Tries `ddgs` first when installed, falls back to the HTML endpoint. Both code paths
    produce the same dict shape, so callers don't need to branch.
    """
    if not web_search_enabled():
        raise RuntimeError("web search disabled (WEB_SEARCH_ENABLED)")
    q = (query or "").strip()
    if not q:
        raise ValueError("empty query")

    max_r = max_results or _env_int("WEB_SEARCH_MAX_RESULTS", 5, 1, 15)

    try:
        from ddgs import DDGS  # type: ignore

        with DDGS() as ddgs:
            raw = ddgs.text(q, max_results=max_r)
            return list(raw) if raw else []
    except ImportError:
        pass
    except Exception as e:
        logger.debug("ddgs runtime failure, falling back to HTML: %s", e)

    return _ddg_html_text_results(q, max_r)


def run_web_search(query: str) -> str:
    """Tool-facing wrapper for the coach. Catches errors → user-readable string."""
    if not web_search_enabled():
        return "Web search is turned off (WEB_SEARCH_ENABLED)."
    q = (query or "").strip()
    if not q:
        return "web_search: empty query — pass keywords or a question in the tool query field."
    try:
        results = web_search_results(q)
    except Exception as e:
        return f"Web search failed ({type(e).__name__}): {e}"
    if not results:
        return f"No web results returned for: {q}"
    return _format_search_lines(q, results)


# ---------- Homepage/section heuristics + summary-intent detection ----------


# Known section/category paths across the major Vietnamese + English news sites we see in
# DuckDuckGo results. Single-segment paths matching these aren't articles.
_SECTION_PATHS = frozenset(
    {
        "",
        "/",
        "/en",
        "/vi",
        "/vn",
        "/fr",
        "/de",
        "/ja",
        "/ko",
        "/zh",
        "/news",
        "/home",
        "/index",
        "/index.html",
        "/index.php",
        "/thoi-su",
        "/kinh-doanh",
        "/kinh-te",
        "/the-thao",
        "/giai-tri",
        "/suc-khoe",
        "/giao-duc",
        "/the-gioi",
        "/phap-luat",
        "/cong-nghe",
        "/khoa-hoc",
        "/du-lich",
        "/oto-xe-may",
        "/y-kien",
        "/tam-su",
        "/tin-moi-nhat",
        "/tin-moi",
        "/tin-tuc",
        "/tin-tuc-trong-ngay-c46.html",
        "/business",
        "/sports",
        "/entertainment",
        "/health",
        "/tech",
        "/world",
        "/politics",
        "/opinion",
    }
)


def _normalize_for_match(s: str) -> str:
    """Casefold + strip combining diacritics. Vietnamese-aware: tóm tắt → tom tat."""
    if not s:
        return ""
    folded = s.casefold()
    nfd = unicodedata.normalize("NFD", folded)
    return "".join(c for c in nfd if not unicodedata.combining(c))


def is_likely_homepage(url: str, title: str = "", body: str = "") -> bool:
    """Composite heuristic: URL path is a known section, OR the body is too thin AND the
    title looks like a bare site name.
    """
    if not url:
        return True
    try:
        parsed = urlparse(url)
    except ValueError:
        return True

    path = (parsed.path or "/").rstrip("/")
    if path == "":
        path = "/"
    if path in _SECTION_PATHS:
        return True

    # Body-based fallback: thin snippet + title matches host's distinctive token.
    body_clean = (body or "").strip()
    if len(body_clean) < 120:
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        tokens = [t for t in host.split(".") if t]
        # The leftmost label is usually the brand: vnexpress.net → "vnexpress",
        # baomoi.com → "baomoi". Skip generic ones like "html", "m", "www".
        generic = {"html", "m", "mobile", "amp"}
        brand = next((t for t in tokens if t not in generic and len(t) > 2), tokens[0] if tokens else "")
        if brand:
            title_norm = _normalize_for_match(title or "")
            brand_norm = _normalize_for_match(brand)
            if brand_norm and brand_norm in title_norm:
                return True
    return False


# Lowercased, diacritic-stripped tokens that mean "the user wants a deeper write-up
# than a list of links". Used by `wants_deep_summary`.
_DEEP_SUMMARY_TOKENS = tuple(
    _normalize_for_match(t)
    for t in (
        "summarize",
        "summary",
        "analyze",
        "analysis",
        "details",
        "detail",
        "in-depth",
        "in depth",
        "explain",
        "overview",
        "breakdown",
        "recap",
        "highlight",
        "deep dive",
        "what happened",
        "what's happening",
        "tóm tắt",
        "phân tích",
        "chi tiết",
        "giải thích",
        "tin chính",
        "tin nổi bật",
        "tổng hợp",
        "điểm tin",
        "đánh giá",
    )
)


def wants_deep_summary(text: str) -> bool:
    """True if the user's message asks for a deep summary/analysis (not just sources)."""
    if not text:
        return False
    norm = _normalize_for_match(text)
    return any(tok in norm for tok in _DEEP_SUMMARY_TOKENS)


def pick_article_url(results: List[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Return the highest-ranked result whose URL is not a likely homepage, else None."""
    for r in results or []:
        url = (r.get("href") or r.get("url") or "").strip()
        if not url:
            continue
        title = r.get("title") or ""
        body = r.get("body") or ""
        if not is_likely_homepage(url, title, body):
            return r
    return None


def auto_fetch_article(
    query: str,
    results: List[dict[str, Any]],
    user_message: str,
) -> Optional[Tuple[str, str]]:
    """Deterministically fetch one article when prefetch snippets won't cut it.

    Returns (url, extracted_text) on success, None otherwise. On fetch failure or empty
    extraction we return None (drop the block, fall back to snippets).
    """
    if not _env_bool("WEB_AUTOFETCH_ENABLED", True):
        return None
    if not web_fetch_enabled():
        return None
    if not results:
        return None

    top = results[:5]
    homepages = sum(
        1
        for r in top
        if is_likely_homepage(
            (r.get("href") or r.get("url") or ""),
            r.get("title") or "",
            r.get("body") or "",
        )
    )
    min_homepages = _env_int("WEB_AUTOFETCH_MIN_HOMEPAGES", 3, 1, 10)
    if not (wants_deep_summary(user_message) or homepages >= min_homepages):
        return None

    candidate = pick_article_url(top)
    if not candidate:
        return None
    url = (candidate.get("href") or candidate.get("url") or "").strip()
    if not url:
        return None

    timeout = _env_int("WEB_AUTOFETCH_TIMEOUT_SEC", 8, 3, 30)
    max_bytes = _env_int("WEB_FETCH_MAX_BYTES", 2_000_000, 50_000, 5_000_000)
    max_chars = _env_int("WEB_AUTOFETCH_MAX_CHARS", 6000, 1000, 30_000)

    text, _ctype, err = _do_fetch_url(
        url, timeout=timeout, max_bytes=max_bytes, max_chars=max_chars
    )
    if err or not text:
        logger.info("auto_fetch_article dropped (%s): %s", err or "no text", url)
        return None
    return (url, text)


# ---------- fetch_url internals ----------


def _normalize_fetch_url(query: str) -> str:
    s = (query or "").strip()
    if not s:
        return ""
    # Allow "URL: https://..." pasted from search results
    m = re.search(r"https?://[^\s<>\"']+", s)
    if m:
        return m.group(0).rstrip(").,;'")
    if s.startswith("http://") or s.startswith("https://"):
        return s.split()[0].rstrip(").,;'")
    return ""


def _strip_html_fallback(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _do_fetch_url(
    url: str, *, timeout: int, max_bytes: int, max_chars: int
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Lower-level fetch. Returns (text, content_type, error). text is None on failure.

    error is a short user-readable phrase ("blocked (...)", "download failed (...)", etc.)
    so both `run_fetch_url` and `auto_fetch_article` can format consistently.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None, None, "only http and https URLs are allowed"

    host = parsed.hostname
    bad, reason = _host_blocked(host or "")
    if bad:
        return None, None, f"blocked ({reason})"
    bad, reason = _resolved_ips_blocked(host or "")
    if bad:
        return None, None, f"blocked ({reason})"

    headers = {
        "User-Agent": os.getenv(
            "WEB_FETCH_USER_AGENT",
            "CashButlerFinanceBot/1.0 (+https://github.com/)",
        ).strip()
    }

    try:
        r = requests.get(
            url,
            timeout=timeout,
            headers=headers,
            stream=True,
            allow_redirects=True,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        return None, None, f"download failed ({type(e).__name__}): {e}"

    total = 0
    parts: list[bytes] = []
    try:
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                break
            parts.append(chunk)
    finally:
        r.close()

    raw = b"".join(parts)
    if not raw:
        return None, None, "empty response body"

    enc = getattr(r, "encoding", None) or "utf-8"
    html = raw.decode(enc, errors="replace")

    text: Optional[str] = None
    try:
        import trafilatura  # type: ignore

        text = trafilatura.extract(html, url=url, include_comments=False)
    except ImportError:
        text = None
    if not text:
        text = _strip_html_fallback(html)

    text = (text or "").strip()
    if not text:
        return None, None, "could not extract readable text (page may be JavaScript-only or blocked)"

    clipped = text[:max_chars]
    if len(text) > max_chars:
        clipped += (
            f"\n\n[… truncated to {max_chars} chars; full page capped at {max_bytes} bytes …]"
        )
    return clipped, r.headers.get("Content-Type", "?"), None


def run_fetch_url(
    query: str,
    *,
    timeout_override: Optional[int] = None,
    max_chars_override: Optional[int] = None,
) -> str:
    """Tool-facing fetch. Returns formatted string; error states become readable text."""
    if not web_fetch_enabled():
        return "fetch_url is turned off (WEB_FETCH_ENABLED)."

    url = _normalize_fetch_url(query)
    if not url:
        return (
            "fetch_url: pass a full http(s) URL in the query field "
            "(e.g. https://example.com/article)."
        )

    timeout = timeout_override or _env_int("WEB_FETCH_TIMEOUT_SEC", 15, 5, 60)
    max_bytes = _env_int("WEB_FETCH_MAX_BYTES", 2_000_000, 50_000, 5_000_000)
    max_chars = max_chars_override or _env_int("WEB_FETCH_MAX_CHARS", 12000, 2000, 50_000)

    text, ctype, err = _do_fetch_url(
        url, timeout=timeout, max_bytes=max_bytes, max_chars=max_chars
    )
    if err:
        return f"fetch_url: {err}."
    return f"Fetched: {url}\nContent-Type: {ctype or '?'}\n\n{text}"
