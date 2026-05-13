"""Curated finance knowledge base loaded from finance_kb.json."""
import json
import os
import re
from typing import List

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
_FINANCE_KB_PATH = os.path.join(_BOT_DIR, "finance_kb.json")
_cache: List[dict] | None = None


def load_finance_kb() -> List[dict]:
    global _cache
    if _cache is None:
        try:
            with open(_FINANCE_KB_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            _cache = []
    return _cache


def _score(entry: dict, tokens: List[str]) -> int:
    if not tokens:
        return 0
    blob = (
        entry.get("title", "")
        + " "
        + " ".join(entry.get("tags", []))
        + " "
        + entry.get("body", "")
    ).lower()
    hits = 0
    for t in tokens:
        if len(t) < 2:
            continue
        if t in blob:
            hits += 2
    return hits


def search_finance_kb(query: str, top_n: int = 3) -> str:
    q = (query or "").strip().lower()
    tokens = [x for x in re.split(r"\W+", q) if x]
    kb = load_finance_kb()
    if not kb:
        return "(Knowledge base empty.)"
    ranked = sorted(kb, key=lambda e: _score(e, tokens), reverse=True)
    picked = ranked[:top_n]
    return "\n".join(f"• {e.get('title', '')}: {e.get('body', '').strip()}" for e in picked)
