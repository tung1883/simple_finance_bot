# Finance Telegram Bot (AI Coach)

A Telegram bot that helps users track expenses and get personalized AI financial advice based on their spending behavior.

## Features
- Log income & expenses
- AI intent detection (finance / chat / command help)
- Personal finance coach (advice based on user data)
- Multi-user support

## Commands
- `/start` – Start bot
- `/help` – Show help message
- `/summary` – Show total income, expenses, balance
- `/history` – Show last transactions

## Setup
### 1. Install dependencies
```bash
pip install python-telegram-bot python-dotenv requests
```

### 2. Create .env
```
TOKEN=your_telegram_bot_token
PROXY_URL=your_ai_api_endpoint
```

### 3. Run bot
``` bash
python bot.py
```