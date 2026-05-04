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

### 1. Quick start (recommended)

After cloning the repo:

```bash
chmod +x start.sh   # if your shell does not already mark it executable
./start.sh
```

On the first run, `start.sh` creates a **virtual environment** (`.venv`), installs **core** dependencies from `requirements.txt`, tries **optional** extras (see below), copies **`.env.example`** → **`.env`** if needed, then starts the bot.

Edit **`.env`** with your real **`TOKEN`** and **`PROXY_URL`**, then run **`./start.sh`** again.

### 2. Manual install (without `start.sh`)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optionally, on a machine where they install cleanly:

```bash
pip install -r requirements-extract.txt   # trafilatura (better fetch_url text extraction)
pip install -r requirements-websearch.txt # ddgs (alternative search backend)
```

### 3. Core vs optional Python packages

| File | Purpose |
|------|---------|
| `requirements.txt` | Bot, Sheets sync, **`requests`**-based web tools — enough to run everywhere. |
| `requirements-extract.txt` | **`trafilatura`** (pulls **`lxml`**) for higher-quality article extraction in **`fetch_url`**. Without it, the bot uses simple HTML stripping. |
| `requirements-websearch.txt` | **`ddgs`** (Rust **`primp`**) as the DuckDuckGo client. Without it, **web search still works** via the DuckDuckGo **HTML** endpoint and **`requests`** (friendly to **Termux/Android** where `pip install ddgs` often fails). |

`start.sh` installs the core file first, then attempts the optional files; failures are skipped with a short note.

### 4. Environment

Create `.env` (see `.env.example`), at minimum:

```
TOKEN=your_telegram_bot_token
PROXY_URL=your_openai_compatible_chat_endpoint
```

The bot posts chat/router payloads to `PROXY_URL` as JSON (`messages`, `temperature`, `max_tokens`), same shape as typical OpenAI-compatible APIs.

Optional `.env` knobs:

- **`WEB_SEARCH_ENABLED`**, **`WEB_SEARCH_MAX_RESULTS`** — coach web search toggles (DuckDuckGo-backed).
- **`WEB_SEARCH_TIMEOUT_SEC`**, **`WEB_SEARCH_USER_AGENT`** — timeout and UA for **HTML fallback** search.
- **`WEB_FETCH_*`** — guarded HTTP fetch for the coach (`fetch_url` tool).
- **`GOOGLE_SHEETS_*`** — Sheets behaviour (share emails, roles, etc.); see `.env.example`.

### 5. Google Sheets (optional)

1. In Google Cloud, enable **Google Sheets API** and **Google Drive API** for the project that owns your service account.
2. Create a service account, download a JSON key.
3. Either place **`google-service-account.json`** next to **`main.py`**, or point **`GOOGLE_SERVICE_ACCOUNT_FILE`** at the absolute path (e.g. **`/storage/emulated/0/Download/your-key.json`** on Android/Termux after granting storage permission to Termux).
4. In Google Drive, create a spreadsheet, **Share** → add the key’s **`client_email`** as **Editor**.
5. In Telegram, run **`/linksheet`** with the sheet URL or ID.

Service accounts often have **no personal Drive storage**; linking **your** sheet avoids `files.create` quota issues.

### 6. Run

With **`start.sh`** (handles venv activation):

```bash
./start.sh
```

Directly (activate **`.venv`** first):

```bash
python main.py
```

Hot reload while editing Python or `finance_kb.json`:

```bash
python main.py --reload
# or
python dev_run.py
```

### 7. Termux / Android (old phone)

```bash
pkg update
pkg install python git
git clone <repo-url>
cd personal_finance_bot
chmod +x start.sh
./start.sh
```

Use **`tmux`** if you want the session to stay running when you disconnect. Give Termux **storage access** if the service account JSON lives under **`Download`**. Android may stop background processes; set **Termux** to **unrestricted** battery where possible if the bot should run 24/7.

If **`pip install ddgs`** errors with **`maturin`** and **`unsupported android architecture: armv8l`**, do not try to fix the build — **`armv8l`** is effectively **32-bit ARM userland**, and **`primp`** (used by **`ddgs`**) does not ship wheels for Android. **`start.sh`** skips installing **`ddgs`** under Termux; **web search still works** via the built-in **HTML DuckDuckGo** path. Devices that support it can install **64-bit Termux** (`aarch64`, `uname -m` prints **`aarch64`**) if you prefer a broader ecosystem, but **`ddgs`/`primp` typically still fails on Termux** either way — rely on the HTML fallback here.

## Security

Do not commit **`.env`**, **`google-service-account.json`**, or real tokens. They are listed in **`.gitignore`**.
