"""
Web tools for the finance coach: DuckDuckGo-backed search and guarded HTTP fetch + extract.

Search uses `ddgs` when installed; otherwise (or on failure) the HTML endpoint via `requests`, which
works on Termux without native primp wheels.

Env:
  WEB_SEARCH_ENABLED — default on; false/0/off disables search.
  WEB_SEARCH_MAX_RESULTS — default 5, max 15.
  WEB_SEARCH_TIMEOUT_SEC — default 25 (HTML fallback and overall search).
  WEB_SEARCH_USER_AGENT — optional identity for HTML search POST.
  WEB_FETCH_ENABLED — default on; false disables fetch_url.
  WEB_FETCH_TIMEOUT_SEC — default 15.
  WEB_FETCH_MAX_BYTES — default 2_000_000 download cap.
  WEB_FETCH_MAX_CHARS — default 12000 text passed back to the model.
"""
import html as html_module
import ipaddress
import os
import re
import socket
from typing import Any, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import requests


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
    ua = os.getenv(
        "WEB_SEARCH_USER_AGENT",
        "Mozilla/5.0 (compatible; CashButlerFinanceBot/1.0; +https://github.com/)",
    ).strip()
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


def run_web_search(query: str) -> str:
    if not web_search_enabled():
        return "Web search is turned off (WEB_SEARCH_ENABLED)."

    q = (query or "").strip()
    if not q:
        return "web_search: empty query — pass keywords or a question in the tool query field."

    max_r = _env_int("WEB_SEARCH_MAX_RESULTS", 5, 1, 15)

    results: List[dict[str, Any]] = []
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            raw = ddgs.text(q, max_results=max_r)
            results = list(raw) if raw else []
    except ImportError:
        try:
            results = _ddg_html_text_results(q, max_r)
        except Exception as e:
            return f"Web search failed ({type(e).__name__}): {e}"
    except Exception as e:
        try:
            results = _ddg_html_text_results(q, max_r)
        except Exception as e2:
            return (
                f"Web search request failed ({type(e).__name__}): {e}; "
                f"HTML fallback: ({type(e2).__name__}): {e2}"
            )

    if not results:
        return f"No web results returned for: {q}"
    return _format_search_lines(q, results)


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


def run_fetch_url(query: str) -> str:
    if not web_fetch_enabled():
        return "fetch_url is turned off (WEB_FETCH_ENABLED)."

    url = _normalize_fetch_url(query)
    if not url:
        return (
            "fetch_url: pass a full http(s) URL in the query field "
            "(e.g. https://example.com/article)."
        )

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "fetch_url: only http and https URLs are allowed."

    host = parsed.hostname
    bad, reason = _host_blocked(host or "")
    if bad:
        return f"fetch_url: blocked ({reason})."

    bad, reason = _resolved_ips_blocked(host or "")
    if bad:
        return f"fetch_url: blocked ({reason})."

    timeout = _env_int("WEB_FETCH_TIMEOUT_SEC", 15, 5, 60)
    max_bytes = _env_int("WEB_FETCH_MAX_BYTES", 2_000_000, 50_000, 5_000_000)
    max_chars = _env_int("WEB_FETCH_MAX_CHARS", 12000, 2000, 50_000)

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
        return f"fetch_url: download failed ({type(e).__name__}): {e}"

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
        return "fetch_url: empty response body."

    enc = getattr(r, "encoding", None) or "utf-8"
    html = raw.decode(enc, errors="replace")

    text: Optional[str] = None
    try:
        import trafilatura

        text = trafilatura.extract(html, url=url, include_comments=False)
    except ImportError:
        text = None
    if not text:
        text = _strip_html_fallback(html)

    text = (text or "").strip()
    if not text:
        return "fetch_url: could not extract readable text (page may be JavaScript-only or blocked)."

    clipped = text[:max_chars]
    tail = f"\n\n[… truncated to {max_chars} chars; full page capped at {max_bytes} bytes …]"
    if len(text) > max_chars:
        clipped += tail

    return f"Fetched: {url}\nContent-Type: {r.headers.get('Content-Type', '?')}\n\n{clipped}"
