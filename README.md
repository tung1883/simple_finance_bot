# CashButler

Telegram bot for logging income and expenses, syncing to a **Google Sheet**, and chatting with an **AI finance coach** that understands your ledger and can pull **live web** results when needed.

## Features

- **Natural language entry** — log spending or income in plain language; an intent router classifies **finance** vs **chat** vs **bot help**.
- **Confirm flow** — parsed amounts show inline confirm/cancel before saving.
- **AI coach** — answers in Vietnamese or English; treats the ledger as **partial** (asks clarifying questions); optional tools: internal KB, ledger search, category totals, **web search**, and **fetch URL**.
- **Live web when it matters** — the router sets `needs_live_web` for messages that need fresh public info (e.g. news, rates); the bot can **prefetch search snippets** into the coach prompt so replies cite real sources.
- **Google Sheets** — each user links their own file via **`/linksheet`**. The bot builds a **Dashboard** tab (KPIs, expense breakdown, pie chart) and a formatted **Transactions** log; new rows append automatically.
- **Multi-user** — per-user SQLite ledger and sheet mapping.

## Commands

| Command | What it does |
|--------|----------------|
| `/start` | Welcome; shows sheet link or how to `/linksheet` if Google is configured |
| `/help` | Short command and usage summary |
| `/add` | Quick add: `/add expense 50k lunch` / `/add income 10tr salary` |
| `/summary` | Totals: income, expenses, balance |
| `/history` | Recent transactions |
| `/sheet` | Your linked spreadsheet URL |
| `/linksheet` | Connect a sheet you own (paste URL or spreadsheet ID after sharing the service account as **Editor**) |
| `/reset_chat` | Clear coach conversation history for you |

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Environment

Create `.env` (see `.env.example`):

```
TOKEN=your_telegram_bot_token
PROXY_URL=your_openai_compatible_chat_endpoint
```

The bot posts chat/router payloads to `PROXY_URL` as JSON (`messages`, `temperature`, `max_tokens`), same shape as typical OpenAI-compatible APIs.

### 3. Google Sheets (optional)

1. In Google Cloud, enable **Google Sheets API** and **Google Drive API** for the project that owns your service account.
2. Create a service account, download a JSON key.
3. Place **`google-service-account.json`** next to `main.py`, or set **`GOOGLE_SERVICE_ACCOUNT_FILE`** in `.env`.
4. In Google Drive, create a spreadsheet, **Share** → add the key’s **`client_email`** as **Editor**.
5. In Telegram, run **`/linksheet`** with the sheet URL or ID.

Optional `.env` knobs:

- **`GOOGLE_SHEETS_SHARE_EMAILS`** — comma-separated Gmail addresses the bot may invite (Drive permitting).
- **`WEB_SEARCH_ENABLED`**, **`WEB_SEARCH_MAX_RESULTS`** — DuckDuckGo-backed coach search.
- **`WEB_FETCH_*`** — guarded HTTP fetch for the coach (`fetch_url` tool).

Service accounts often have **no personal Drive storage**; linking **your** sheet avoids `files.create` quota issues.

### 4. Run

```bash
python main.py
```

Hot reload while editing Python or `finance_kb.json`:

```bash
python main.py --reload
# or
python dev_run.py
```
