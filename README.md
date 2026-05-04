# CashButler
A Telegram bot that helps users track expenses and get personalized AI financial advice based on their spending behavior.

## Features
- Log income & expenses
- AI intent detection (finance / chat / command help)
- Personal finance coach (ledger-aware advice + optional internal search tools)
- Optional Google Sheet per user (you create the file, share it with the bot service account, `/linksheet`)
- Multi-user support

## Commands
- `/start` – Start bot (shows Sheet link or steps to `/linksheet` when Google is configured)
- `/help` – Show help message
- `/summary` – Show total income, expenses, balance
- `/history` – Show last transactions
- `/sheet` – Show your spreadsheet URL
- `/linksheet` – Connect a Google Sheet you own (after sharing the service account as Editor)

## Setup
### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Create .env
```
TOKEN=your_telegram_bot_token
PROXY_URL=your_ai_api_endpoint
```

Optional — Google Sheets sync:

1. Create a Google Cloud project, enable **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, download its JSON key.
3. Save the key as **`google-service-account.json`** in the project folder (same folder as `finance_bot.py`), **or** set `GOOGLE_SERVICE_ACCOUNT_FILE` in `.env` to its path.
4. Each user: create a **Google Sheet**, click **Share**, add the service account email (from the JSON, `client_email`) as **Editor**, then in Telegram run **`/linksheet`** with the sheet URL or spreadsheet ID. The bot adds/uses a **Transactions** tab and writes rows there.
5. **Optional — extra collaborators:** `GOOGLE_SHEETS_SHARE_EMAILS` lets the service account invite Gmail addresses (may fail on some user-owned files if Drive denies ACL changes). Use **`GOOGLE_SHEETS_SHARE_ROLE=reader`** if you only need view access for those addresses.
6. *(Legacy)* `GOOGLE_SHEETS_PUBLIC_LINK=true` — “anyone with the link” reader (less private).
7. *(Legacy)* `GOOGLE_SHEETS_AUTO_CREATE=true` — try **Drive API** `files.create` per user instead of `/linksheet` (requires **non-zero** Drive quota on the service account; many accounts have **none**).

**Note:** Service accounts often have **no** personal Drive quota. The default is **user-owned sheet + `/linksheet`**, which only needs **Sheets API** + **Editor** share to the service account.

### 3. Run bot
```bash
python finance_bot.py
```

Development (restart bot when `*.py` or `finance_kb.json` changes):
```bash
python finance_bot.py --reload
# or
python dev_run.py
```
