"""
Web tools for the finance coach: DuckDuckGo-backed search (`ddgs`) and guarded HTTP fetch + extract.

Env:
  WEB_SEARCH_ENABLED — default on; false/0/off disables search.
  WEB_SEARCH_MAX_RESULTS — default 5, max 15.
  WEB_FETCH_ENABLED — default on; false disables fetch_url.
  WEB_FETCH_TIMEOUT_SEC — default 15.
  WEB_FETCH_MAX_BYTES — default 2_000_000 download cap.
  WEB_FETCH_MAX_CHARS — default 12000 text passed back to the model.
"""
import ipaddress
import os
import re
import socket
from typing import Optional, Tuple
from urllib.parse import urlparse

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


def run_web_search(query: str) -> str:
    if not web_search_enabled():
        return "Web search is turned off (WEB_SEARCH_ENABLED)."

    q = (query or "").strip()
    if not q:
        return "web_search: empty query — pass keywords or a question in the tool query field."

    max_r = _env_int("WEB_SEARCH_MAX_RESULTS", 5, 1, 15)

    try:
        from ddgs import DDGS
    except ImportError:
        return (
            "Web search is not installed. Add dependency: ddgs "
            "(pip install ddgs — see requirements.txt)."
        )

    try:
        with DDGS() as ddgs:
            raw = ddgs.text(q, max_results=max_r)
            results = list(raw) if raw else []
    except Exception as e:
        return f"Web search request failed ({type(e).__name__}): {e}"

    if not results:
        return f"No web results returned for: {q}"

    lines = [
        f"Web search results for query: {q}",
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
