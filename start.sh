#!/usr/bin/env bash
# After clone, run: chmod +x start.sh && ./start.sh
# Termux: pkg install python (and git before cloning). Re-run this script after editing .env.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1 && ! command -v python >/dev/null 2>&1; then
  echo "Python is not installed. On Termux: pkg install python"
  exit 1
fi

PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"

if [ ! -d .venv ]; then
  echo "Creating virtual env in .venv …"
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q --upgrade pip
pip install -q -r requirements.txt

# ddgs is optional: web search uses DuckDuckGo HTML via requests when ddgs is not installed (e.g. Termux).
if ! pip install -q -r requirements-websearch.txt 2>/dev/null; then
  echo "Note: optional ddgs not installed — HTML search fallback is used (this is fine on Termux)."
fi

if [ ! -f .env ]; then
  if [ ! -f .env.example ]; then
    echo "Missing .env.example — broken checkout?"
    exit 1
  fi
  cp .env.example .env
  echo "Created .env from .env.example."
  echo "Edit .env: set TOKEN (Telegram) and PROXY_URL (LLM), add google-service-account.json if you use Sheets."
  echo "Then run ./start.sh again."
  exit 0
fi

if grep -qE '^[[:space:]]*TOKEN=BOT_TOKEN[[:space:]]*$' .env 2>/dev/null \
  || grep -qE '^[[:space:]]*PROXY_URL=OPEN_AI_PROXY[[:space:]]*$' .env 2>/dev/null; then
  echo "Edit .env: TOKEN and PROXY_URL must be set to real values (not the placeholders)."
  exit 1
fi

exec python main.py
