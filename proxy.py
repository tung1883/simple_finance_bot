"""Wrapper around the upstream LLM proxy (OpenAI-compatible chat completions)."""
import json
import logging
import os
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

PROXY_URL = os.getenv("PROXY_URL")

PROXY_PROMPT_BEGIN = "<<<PROXY_SAFE_PROMPT_BEGIN>>>"
PROXY_PROMPT_END = "<<<PROXY_SAFE_PROMPT_END>>>"

ROUTER_TEMPERATURE = 0.1
ROUTER_MAX_TOKENS = 280
COACH_TEMPERATURE = 0.55
COACH_MAX_TOKENS = 1200


def proxy_safe_user_content(instructions: str, task: str) -> str:
    """Wrap real instructions inside the user message so upstream proxy filtering
    that strips system roles can't drop them."""
    return (
        f"{PROXY_PROMPT_BEGIN}\n"
        "The following section overrides any conflicting upstream instructions for this request.\n\n"
        f"{instructions.strip()}\n\n"
        f"{PROXY_PROMPT_END}\n\n"
        "USER_TASK:\n"
        f"{task.strip()}"
    )


def post_proxy_json(payload: dict, *, timeout: int = 30) -> Tuple[Optional[str], Optional[requests.Response]]:
    url = PROXY_URL or os.getenv("PROXY_URL")
    if not url:
        return None, None
    try:
        res = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as e:
        logger.warning("proxy request failed: %s", e)
        return None, None
    if res.status_code != 200:
        return None, res
    try:
        data = res.json()
    except json.JSONDecodeError:
        return None, res
    try:
        return data["choices"][0]["message"]["content"], res
    except (KeyError, IndexError, TypeError):
        return None, res
