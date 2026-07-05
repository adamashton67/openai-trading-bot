# OpenAI Trading Bot Roadmap

## Project Overview

**Goal:** Build an OpenAI-driven Python trading bot that uses Lumibot to execute trades through Alpaca Paper Trading. The application will be hosted on Railway and deployed automatically from GitHub.

### Technology Stack

- Python
- OpenAI API
- Lumibot
- Alpaca Paper Trading
- Railway
- GitHub
- Docker (optional later)
- GitHub Actions (optional later)

---

# Phase 1 - Define the Architecture

## Objectives

- Start with paper trading only
- Host on Railway
- Deploy from GitHub
- Use OpenAI for trade analysis
- Use Lumibot for trade execution
- Keep all risk management inside Python

### Core Principle

```
OpenAI suggests.
Python validates.
Lumibot executes.
```

---

# Phase 2 - Create the Project

Suggested structure:

```text
trading-bot/
├── main.py
├── strategy.py
├── openai_logic.py
├── risk_manager.py
├── broker.py
├── config.py
├── requirements.txt
├── railway.toml
├── .env.example
└── README.md
```

## Checklist

- [Done] Create GitHub repository
- [Done] Create virtual environment
- [Done] Install dependencies
- [Done] Commit initial project structure

---

# Phase 3 - Configure APIs

- [Done] Create Alpaca paper account
- [Done] Generate Alpaca API keys
- [Done] Generate OpenAI API key
- [Done] Create `.env`
- [Done] Create `.env.example`

Example:

```env
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
OPENAI_API_KEY=
PAPER_TRADING=true
BOT_ENABLED=false
```

---

# Phase 4 - Build the Trading Loop

1. Start application
2. Check BOT_ENABLED
3. Retrieve account information
4. Retrieve market data
5. Send context to OpenAI
6. Receive structured decision
7. Validate response
8. Apply risk rules
9. Execute through Lumibot
10. Log results
11. Wait until next cycle

Checklist:

- [ ] Build application entry point
- [ ] Build scheduler
- [ ] Build OpenAI client
- [ ] Build trade execution layer

---

# Phase 5 - OpenAI Decision Engine

Requirements:

- JSON responses only
- Validate every response
- Never allow OpenAI to execute trades directly

Example:

```json
{
  "symbol":"AAPL",
  "action":"buy",
  "confidence":0.74,
  "reason":"Momentum remains positive.",
  "suggested_allocation_percent":5
}
```

Checklist:

- [ ] Design prompt
- [ ] Validate JSON
- [ ] Handle invalid responses
- [ ] Log AI decisions

---

# Phase 6 - Risk Manager

- [ ] Bot enabled
- [ ] Paper trading enabled
- [ ] Market open
- [ ] Allowed symbol
- [ ] Confidence threshold
- [ ] Maximum position size
- [ ] Maximum daily loss
- [ ] Duplicate trade prevention
- [ ] Cash available

---

# Phase 7 - Logging

Record:

- Timestamp
- Account balance
- Positions
- OpenAI decision
- Risk outcome
- Trade executed
- Errors

Files:

```text
logs/bot.log
logs/trades.csv
logs/decisions.csv
```

---

# Phase 8 - Local Testing

- [ ] Verify Alpaca connection
- [ ] Verify OpenAI connection
- [ ] Verify Lumibot execution
- [ ] Verify logging
- [ ] Test rejected trades
- [ ] Test successful paper trades

---

# Phase 9 - Railway Deployment

- [ ] Create Railway project
- [ ] Connect GitHub
- [ ] Add environment variables
- [ ] Configure start command
- [ ] Deploy
- [ ] Verify logs

---

# Phase 10 - Monitoring

- [ ] Log startup
- [ ] Log every cycle
- [ ] Handle exceptions
- [ ] Daily summary
- [ ] Manual kill switch

Future:

- Telegram notifications
- Dashboard
- SQLite/Postgres
- Performance reporting

---

# Phase 11 - Continuous Improvement

## Version 1

- Basic paper trading
- Single strategy
- Railway deployment

## Version 2

- Better prompts
- Improved risk management
- Enhanced logging

## Version 3

- Backtesting
- Performance analytics
- Dashboard

## Version 4

- Multiple strategies
- Portfolio optimisation

## Version 5

- Live trading (only after extensive paper testing)

---

# Success Criteria

- Stable 24/7 operation
- Reliable GitHub → Railway deployments
- Safe execution through Python risk rules
- Comprehensive logging
- Consistent paper trading performance
