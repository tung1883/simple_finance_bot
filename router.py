"""Intent router: classify input as finance / chat / help_command, plus needs_live_web.

Falls back to a heuristic when the proxy is unavailable or returns unparseable output.
"""
import json
import logging
import re
from typing import Optional

from proxy import (
    ROUTER_MAX_TOKENS,
    ROUTER_TEMPERATURE,
    post_proxy_json,
    proxy_safe_user_content,
)

logger = logging.getLogger(__name__)


ROUTER_CLASSIFIER_INSTRUCTIONS = """
You are a STRICT INTENT CLASSIFIER for a personal finance assistant.

Return ONLY valid JSON.

FORMAT:
{
  "intent": "finance | chat | help_command",
  "confidence": number,
  "needs_live_web": boolean,
  "web_search_query": string | null,
  "finance": {
    "type": "income|expense|null",
    "amount": number|null,
    "category": string|null
  }
}

========================
FIELD: needs_live_web + web_search_query
========================

Set "needs_live_web": true ONLY when answering well requires **fresh or verifiable information from the public web**
that you cannot rely on from static training alone. Examples: today’s financial/news headlines, current market
indices or FX, breaking policy or central-bank moves, “what happened to [asset] today”, looking up a cited fact.

Set "needs_live_web": false for: **help_command**; **finance** (transaction logging); generic budgeting,
savings tips, empathy, hypotheticals, math, or advice grounded only in the user’s bot ledger — even if the
topic is “finance”, unless they explicitly want **live** external facts.

When "needs_live_web": true, set "web_search_query" to a **short focused search string** (same language as the
user when possible). When false, use null for "web_search_query".

"needs_live_web" MUST be false when intent is "finance" or "help_command".

========================
STRICT RULES (VERY IMPORTANT)
========================

1. help_command ONLY if user asks about SYSTEM USAGE:
- bot commands
- how to use bot
- /help
- list of features
- "what can you do"
- usage instructions
- Google Sheet link / spreadsheet export from the bot
- budgets, /budget, /review (commands for this bot)

IMPORTANT:
❌ DO NOT include financial advice here
❌ DO NOT include money tips here

========================

2. finance ONLY when user is RECORDING A TRANSACTION:

This means user is logging REAL money flow:

A. Spending:
- eat, buy, game, transport, shopping, etc.
- must be an action + optionally amount

B. Income:
- salary, bonus, income, receive money

C. Must include or imply a concrete transaction

IMPORTANT RULES:
- finance MUST NOT include advice
- finance MUST NOT include questions
- finance MUST NOT include planning or suggestions
- finance MUST be only "data entry"

Examples:
- "ăn 50k"
- "mua trà sữa 30k"
- "lương 10 triệu"

========================

3. chat (DEFAULT for EVERYTHING ELSE):

This includes:

A. Financial advice / coaching:
- how to save money
- how to spend better
- budgeting tips
- financial planning
- "cho tôi lời khuyên chi tiêu"
- "tôi nên tiết kiệm thế nào"

B. Any question or discussion:
- opinions
- life advice
- general chat
- unclear intent

IMPORTANT:
👉 ANYTHING NOT CLEARLY A TRANSACTION = chat

========================
EXAMPLES (JSON fields abbreviated — always return full FORMAT)
========================

"cho tôi lời khuyên giúp tôi chi tiêu hiệu quả hơn"
→ chat, needs_live_web: false, web_search_query: null

"tôi nên tiết kiệm thế nào"
→ chat, needs_live_web: false

"cập nhật tin tài chính hôm nay"
→ chat, needs_live_web: true, web_search_query: "tin tài chính hôm nay"

"update financial news today for me"
→ chat, needs_live_web: true, web_search_query: "financial news today"

"current Fed funds rate today"
→ chat, needs_live_web: true, web_search_query: "Federal Reserve fed funds rate today"

"ăn 50k"
→ finance, needs_live_web: false, web_search_query: null

"lương 10 triệu"
→ finance, needs_live_web: false

"bot có những lệnh gì"
→ help_command, needs_live_web: false

"cho tôi link google sheet"
→ help_command, needs_live_web: false

"hôm nay thế nào"
→ chat, needs_live_web: false (unless they clearly ask for news — then true with a query)
"""


_HEURISTIC_ADVICE_MARKERS = (
    "khuyên",
    "khuyen",
    "lời khuyên",
    "loi khuyen",
    "advice",
    "how to",
    "how do",
    "nên ",
    "nen ",
    "should i",
    "gợi ý",
    "goi y",
    "help me save",
    "tiết kiệm",
    "tiet kiem",
    "budget",
)
_HEURISTIC_HELP_MARKERS = (
    "/help",
    "command",
    "lệnh",
    "lenh",
    "features",
    "what can you",
)


def extract_first_json_object(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _normalize_router_content(raw: str) -> str:
    content = raw.strip()
    if content.startswith("```"):
        parts = content.split("```", 2)
        content = parts[1] if len(parts) > 1 else content
        if content.startswith("json"):
            content = content[4:]
    return content.strip().rstrip("`").strip()


def parse_router_response(raw: str) -> Optional[dict]:
    content = _normalize_router_content(raw)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    snippet = extract_first_json_object(content)
    if snippet:
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            pass
    return None


def normalize_router_result(data: Optional[dict]) -> dict:
    out = {
        "intent": "chat",
        "confidence": 0.0,
        "finance": None,
        "needs_live_web": False,
        "web_search_query": None,
    }
    if isinstance(data, dict):
        out.update(data)

    intent = out.get("intent", "chat")
    if intent not in ("finance", "chat", "help_command"):
        intent = "chat"
    out["intent"] = intent

    flag = out.get("needs_live_web", False)
    if isinstance(flag, str):
        flag = flag.strip().lower() in ("1", "true", "yes")
    out["needs_live_web"] = bool(flag) and intent == "chat"

    q = out.get("web_search_query")
    if q is not None and not isinstance(q, str):
        q = str(q)
    q = (q or "").strip() or None
    out["web_search_query"] = q

    fin = out.get("finance")
    if fin is not None and not isinstance(fin, dict):
        out["finance"] = None
    return out


def parse_transaction_heuristic(text: str) -> Optional[dict]:
    """Best-effort local classifier when the proxy/router JSON is unusable."""
    if not text:
        return None
    raw = text.strip()
    if len(raw) > 400:
        return None
    low = raw.lower()
    if any(m in low for m in _HEURISTIC_ADVICE_MARKERS):
        return None
    if any(m in low for m in _HEURISTIC_HELP_MARKERS):
        return None

    amount: Optional[float] = None
    mk = re.search(r"(\d+(?:[.,]\d+)?)\s*k\b", low)
    if mk:
        try:
            amount = float(mk.group(1).replace(",", ".")) * 1000
        except ValueError:
            amount = None
    if amount is None:
        mtr = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:tr|triệu|trieu)\b", low)
        if mtr:
            try:
                amount = float(mtr.group(1).replace(",", ".")) * 1_000_000
            except ValueError:
                amount = None

    if amount is None or amount <= 0:
        return None

    income_kw = (
        "lương",
        "luong",
        "salary",
        "thưởng",
        "thuong",
        "bonus",
        "thu nhập",
        "thu nhap",
        "nhận được",
        "nhan duoc",
    )
    tx_type = "income" if any(k in low for k in income_kw) else "expense"
    category = "other_income" if tx_type == "income" else "other_expense"
    if tx_type == "expense":
        if any(w in low for w in ("ăn", "an ", "food", "cơm", "com", "trưa", "trua")):
            category = "food"
        elif any(w in low for w in ("mua", "buy")):
            category = "shopping"

    return {
        "intent": "finance",
        "confidence": 0.72,
        "finance": {"type": tx_type, "amount": amount, "category": category},
    }


def fallback_router(text: Optional[str] = None) -> dict:
    if text:
        heur = parse_transaction_heuristic(text)
        if heur:
            return heur
    return {
        "intent": "chat",
        "confidence": 0.5,
        "finance": None,
        "needs_live_web": False,
        "web_search_query": None,
    }


def ai_router(text: str) -> dict:
    task = f"INPUT:\n<<<{text}>>>"
    user_content = proxy_safe_user_content(ROUTER_CLASSIFIER_INSTRUCTIONS.strip(), task)
    payload = {
        "messages": [{"role": "user", "content": user_content}],
        "temperature": ROUTER_TEMPERATURE,
        "max_tokens": ROUTER_MAX_TOKENS,
    }

    try:
        content, _res = post_proxy_json(payload)
        if content is None:
            return normalize_router_result(fallback_router(text))

        logger.debug("router raw: %s", content)
        parsed = parse_router_response(content)
        if parsed is not None:
            return normalize_router_result(parsed)

        retry_task = task + "\n\nRespond with JSON only. No markdown fences, no prose."
        retry_payload = {
            "messages": [
                {
                    "role": "user",
                    "content": proxy_safe_user_content(
                        ROUTER_CLASSIFIER_INSTRUCTIONS.strip(), retry_task
                    ),
                }
            ],
            "temperature": ROUTER_TEMPERATURE,
            "max_tokens": ROUTER_MAX_TOKENS,
        }
        content2, _res2 = post_proxy_json(retry_payload)
        if content2 is None:
            return normalize_router_result(fallback_router(text))

        logger.debug("router raw (retry): %s", content2)
        parsed2 = parse_router_response(content2)
        if parsed2 is not None:
            return normalize_router_result(parsed2)

        return normalize_router_result(fallback_router(text))
    except Exception as e:
        logger.exception("router error: %s", e)
        return normalize_router_result(fallback_router(text))
