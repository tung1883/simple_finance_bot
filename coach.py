"""Coach LLM loop: prompt assembly, tool dispatch, multi-round tool calls."""
import json
import logging
from datetime import date
from typing import Optional

from budgets import build_budget_block, budget_status
from chat_history import get_chat_history, save_chat_message
from kb import search_finance_kb
from ledger import build_finance_context, search_ledger_db, spending_by_category_block
from proxy import (
    COACH_MAX_TOKENS,
    COACH_TEMPERATURE,
    post_proxy_json,
    proxy_safe_user_content,
)
from recurring import format_recurring_lines, detect_recurring, format_forecast_block
from review import build_monthly_review
from web_tools import run_fetch_url, run_web_search, web_search_enabled

logger = logging.getLogger(__name__)


COACH_MANDATE_COMPACT = """You are an experienced personal finance coach on Telegram (not a robot that only reacts to one table).

How to treat the numbers:
- Anything labeled as logged in this bot is a PARTIAL sample. Do not infer “total income”, “you have no income”, “your balance is X”, or “you only spend on Y” from aggregates alone.
- If the log shows no income or skewed spending, acknowledge that and ASK whether they track salary, side income, family support, irregular cash flows, or spending elsewhere — before you draw conclusions.
- Distinguish confirmed facts (what appears in the log) from assumptions; say what you would need to know to be sure.

Coaching style (human-like):
- Start from their actual question and context; use the log to illustrate patterns when relevant, not as the whole story.
- When advice could depend on missing facts (goals, timeline, dependants, job stability, emergency fund, existing debt, risk tolerance, currency/locale), ask 1–3 focused questions OR offer conditional guidance (“If A, then …; if B, then …”).
- Before telling them to cut specific categories (e.g. food, entertainment), check what is discretionary vs fixed for them, and whether they want aggressive saving or sustainable habits.
- For investing, large purchases, or debt payoff: mention basics (emergency buffer, high-interest debt first, match employer plans where applicable) and avoid overconfident product picks without knowing their situation.
- Be warm and direct, not preachy. Offer a short structure when helpful: clarify → prioritize → one or two next steps.

Use the BUDGETS, FORECAST, and RECURRING blocks if present — they give hard targets to coach against. When the user is over (or projected over) a budget, name the category and the gap.

Length: default to a tight answer; use a short paragraph plus bullets when they need a plan or several questions. Reply in the same language the user writes in (Vietnamese or English).

Web tools (intelligent use):
- You decide per message: use web_search / fetch_url only when fresh or external facts clearly add value. For empathy, budgeting talk, or ledger-only questions, respond without web tools.
- When you do use web tools, summarize only what came back, name sources (titles + URLs), and avoid overclaiming; snippets and extracts can be incomplete or wrong.
- If a "LIVE WEB (prefetched)" block appears in your instructions for this turn, you MUST treat it as the web_search result: summarize those headlines/snippets with links. Never answer "I have no real-time data" when that block contains results."""


COACH_MANDATE_LITE = """You are a personal finance coach on Telegram. Treat any ledger in the conversation as partial bot-logged data only — do not assume it is their full financial life. Ask clarifying questions when advice depends on missing facts. Use web_search/fetch_url only when the user clearly needs up-to-date public facts, not for every reply. Reply in the user's language (Vietnamese or English)."""


COACH_TOOL_INSTRUCTIONS = """
TOOL_USE (optional):
Request ONE tool by replying with ONLY this block (no other text):
<<<TOOL
{"tool":"<name>","query":"<optional string>"}
>>>

When to use tools (be selective — most replies need no tool):
- Default: answer from the conversation, ledger context, budgets, and general coaching. Do NOT call web_search on every message.
- If your instructions for this turn already include a "LIVE WEB (prefetched)" section with results, summarize that and do NOT call web_search again for the same need.
- search_kb — curated finance_kb snippets; use for generic frameworks when useful.
- search_ledger — this user’s logged transactions (keywords).
- spending_by_category — expense totals by category from the log.
- budget_status — current budgets vs month-to-date spend.
- recurring_summary — detected recurring outflows + month-end forecast.
- monthly_review — assembled monthly review (totals, top categories, budgets, recurring, forecast).
- web_search — ONLY when you need time-sensitive PUBLIC info and there is NO prefetched LIVE WEB block with usable results (news, rates, policy, markets, fact-checking). Skip for pure habits/emotions/ledger-only coaching.
- fetch_url — ONLY after you have a specific http(s) URL (from the user or from web_search) and the snippets are not enough; one credible article or official page. Pass the full URL in "query".

Do not chain unnecessarily: often one tool is enough; fetch_url at most one follow-up for one link.

Tools:
- search_kb — keywords → short curated guidance.
- search_ledger — keywords → matching logged rows.
- spending_by_category — query often "{}"; grouped expense totals.
- budget_status — query often "{}"; budgets and MTD usage.
- recurring_summary — query "{}"; recurring + forecast.
- monthly_review — query "{}"; full monthly summary block.
- web_search — focused search query string; cite titles/URLs you use; never invent results.
- fetch_url — full URL string; summarize only extracted text; note if extraction failed.

After TOOL_RESULT, synthesize for the user; do not repeat <<<TOOL>>>.
"""


def extract_tool_spec(text: str) -> Optional[dict]:
    tag = "<<<TOOL"
    ti = text.find(tag)
    if ti == -1:
        return None
    brace_start = text.find("{", ti)
    if brace_start == -1:
        return None
    depth = 0
    json_end = None
    for k in range(brace_start, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                json_end = k + 1
                break
    if json_end is None:
        return None
    raw_json = text[brace_start:json_end]
    try:
        spec = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, dict) or "tool" not in spec:
        return None
    return spec


def strip_visible_reply(text: str) -> str:
    tag = "<<<TOOL"
    ti = text.find(tag)
    if ti == -1:
        return text.strip()
    return text[:ti].strip()


def execute_coach_tool(user_id: int, spec: dict) -> str:
    name = (spec.get("tool") or "").strip()
    query = spec.get("query")
    if isinstance(query, dict):
        query = json.dumps(query)
    elif query is None:
        query = ""
    else:
        query = str(query)

    if name == "search_kb":
        return search_finance_kb(query)
    if name == "search_ledger":
        return search_ledger_db(user_id, query)
    if name == "spending_by_category":
        return spending_by_category_block(user_id)
    if name == "budget_status":
        rows = budget_status(user_id)
        if not rows:
            return "(no budgets set)"
        return "\n".join(
            f"  - {r['category']}: spent {r['spent']:,.0f} / limit {r['monthly_limit']:,.0f} "
            f"({r['percent']:.0f}%, projected {r['projected_monthend']:,.0f})"
            for r in rows
        )
    if name == "recurring_summary":
        return (
            "Recurring:\n"
            + format_recurring_lines(detect_recurring(user_id))
            + "\n\n"
            + format_forecast_block(user_id)
        )
    if name == "monthly_review":
        return build_monthly_review(user_id)
    if name == "web_search":
        return run_web_search(query)
    if name == "fetch_url":
        return run_fetch_url(query)
    return f"(Unknown tool: {name})"


def prefetch_live_web_for_coach(
    user_message: str,
    needs_live_web: bool,
    web_search_query: Optional[str] = None,
) -> str:
    """Inject web_search results when the intent router set needs_live_web (chat only)."""
    if not needs_live_web or not web_search_enabled():
        return ""
    q = (web_search_query or "").strip() or (user_message or "").strip()[:220]
    if not q:
        return ""
    try:
        blob = run_web_search(q)
    except Exception as e:
        blob = f"(web_search error: {type(e).__name__}: {e})"
    return (
        "\n\n=== LIVE WEB (prefetched — intent router set needs_live_web; REQUIRED: summarize in the "
        "user's language with bullet points, source titles, and URLs from the text below. Do NOT reply "
        "that you lack real-time data unless prefetch failed or returned no results.) ===\n"
        f"{blob}\n=== END LIVE WEB ===\n"
    )


def ai_chat(
    text: str,
    user_id: int,
    *,
    needs_live_web: bool = False,
    web_search_query: Optional[str] = None,
) -> str:
    history = get_chat_history(user_id, limit=12)
    finance_context = build_finance_context(user_id)
    budget_block = build_budget_block(user_id)

    today = date.today().isoformat()
    coach_instructions_full = (
        COACH_MANDATE_COMPACT
        + f"\n\nToday's date (server): {today}\n\n"
        + "Context for this turn (use as hints, not as their complete finances):\n\n"
        + finance_context
        + "\n\n"
        + budget_block
        + "\n\nUse tools only when justified (see tool policy). In your reply, separate what the log shows from what you still need to ask them.\n\n"
        + COACH_TOOL_INSTRUCTIONS.strip()
        + prefetch_live_web_for_coach(text, needs_live_web, web_search_query)
    )

    messages = []
    for role, content in history:
        if role == "user":
            wrapped = proxy_safe_user_content(COACH_MANDATE_LITE, content)
            messages.append({"role": "user", "content": wrapped})
        else:
            messages.append({"role": "assistant", "content": content})

    messages.append(
        {
            "role": "user",
            "content": proxy_safe_user_content(coach_instructions_full, text),
        }
    )

    max_rounds = 5
    final_reply = None

    try:
        for _ in range(max_rounds):
            payload = {
                "messages": messages,
                "temperature": COACH_TEMPERATURE,
                "max_tokens": COACH_MAX_TOKENS,
            }
            reply, res = post_proxy_json(payload)
            if reply is None:
                logger.warning("chat: bad proxy response status=%s", getattr(res, "status_code", None))
                return "Sorry, I couldn't process that. Please try again."

            spec = extract_tool_spec(reply)
            if spec is None:
                final_reply = strip_visible_reply(reply)
                break

            tool_output = execute_coach_tool(user_id, spec)
            messages.append({"role": "assistant", "content": reply.strip()})
            messages.append(
                {
                    "role": "user",
                    "content": proxy_safe_user_content(
                        "The assistant requested a tool. Below is TOOL_RESULT (plain text). "
                        "Use it to answer the user. Separate log facts from open questions; avoid overconfident claims. "
                        "For web_search/fetch_url output, treat text as provisional and cite sources. "
                        "Reply with user-facing advice only — no <<<TOOL>>> blocks.",
                        f"TOOL_RESULT:\n{tool_output}",
                    ),
                }
            )

        if final_reply is None:
            final_reply = "Sorry — I could not finish that answer. Please try again."

        save_chat_message(user_id, "user", text)
        save_chat_message(user_id, "assistant", final_reply)

        return final_reply
    except Exception as e:
        logger.exception("chat error: %s", e)
        return "Sorry, I couldn't process that. Please try again."
