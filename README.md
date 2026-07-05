# OpenAI Trading Bot

An OpenAI-driven Python trading bot scaffold that uses Lumibot as the execution layer, starts with Alpaca Paper Trading, and keeps risk controls in Python.

Core principle:

```text
OpenAI suggests.
Python validates.
Lumibot executes.
```

## Project Structure

```text
.
├── main.py
├── config.py
├── broker.py
├── strategy.py
├── risk_manager.py
├── scheduler.py
├── logger_config.py
├── storage.py
├── notifications/
│   ├── notifier.py
│   └── discord_notifier.py
├── prompts/
│   ├── system_prompt.md
│   └── user_prompt_template.md
├── requirements.txt
├── .env.example
└── README.md
```

## Safety Defaults

- `BOT_ENABLED=false` by default.
- `PAPER_TRADING=true` by default.
- The broker execution method is a placeholder and does not place orders yet.
- The AI decision method is a placeholder and returns `hold`.
- The bot avoids OpenAI calls when the US market is closed.
- Discord daily summaries are disabled by default.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

Fill in `.env` with your Alpaca Paper Trading and OpenAI keys before enabling the bot.

## Railway Deployment

Set the Railway start command to:

```bash
python main.py
```

Add the same environment variables from `.env.example` in the Railway project settings.

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_ENABLED` | `false` | Kill switch. Trading cycles are skipped unless this is true. |
| `PAPER_TRADING` | `true` | Keeps the bot in paper-trading mode. |
| `TRADING_INTERVAL_MINUTES` | `15` | How often to run a cycle during regular US market hours. |
| `MARKET_TIMEZONE` | `America/New_York` | Timezone used for market checks. |
| `OPENAI_API_KEY` | empty | OpenAI API key. |
| `OPENAI_MODEL` | `gpt-5-mini` | Placeholder model setting for future AI logic. |
| `ALPACA_API_KEY` | empty | Alpaca API key. |
| `ALPACA_SECRET_KEY` | empty | Alpaca secret key. |
| `ALPACA_PAPER_BASE_URL` | `https://paper-api.alpaca.markets` | Alpaca paper endpoint. |
| `MAX_POSITION_ALLOCATION_PERCENT` | `5` | Starter risk limit per suggested trade. |
| `MIN_CONFIDENCE` | `0.70` | Minimum AI confidence before a trade can pass risk checks. |
| `ALLOWED_SYMBOLS` | sample symbols | Optional comma-separated symbol allowlist. |
| `DISCORD_WEBHOOK_URL` | empty | Discord incoming webhook URL for summaries. |
| `DISCORD_DAILY_SUMMARY_ENABLED` | `false` | Enables one daily summary after regular US market close. |

## Trading Flow

1. Start the application.
2. Load configuration from environment variables.
3. Check whether `BOT_ENABLED` is true.
4. Check whether the US market is open.
5. If the market is closed, sleep and do not call OpenAI.
6. If the market is open, run a trading cycle every 15 minutes.
7. Collect broker/account/position/market data.
8. Call the placeholder AI decision function.
9. Pass the AI decision through the risk manager.
10. If approved, execute through Lumibot.
11. Log the result.
12. Continue until stopped.

## AI Decision Layer

`openai_logic.py` owns prompt loading, OpenAI API calls, JSON parsing, and Pydantic validation.

`strategy.py` calls it like this during a trading cycle:

```python
context = self._build_ai_context(snapshot)
decision = self._get_ai_client().get_decision(context)
risk_manager_input = decision.to_risk_manager_dict()
```

The AI layer never executes trades and never bypasses the risk manager.

## Discord Daily Summaries

Daily summaries are generated after regular US market close and sent once per trading day. The bot stores simple local JSON journal files under `data/` so app restarts do not resend the same day's summary.

Add these variables locally and in Railway:

```env
DISCORD_WEBHOOK_URL=
DISCORD_DAILY_SUMMARY_ENABLED=false
```

Set `DISCORD_DAILY_SUMMARY_ENABLED=true` when you are ready to send real summaries. Do not put webhook URLs in Git or the README.

The summary includes starting balance, ending balance, daily profit/loss, completed trades, top gain/loss trade when available, open positions, AI decision counts, and rejected trades.

To test with mock data:

```bash
python main.py --send-test-summary
```

To preview the message without sending it:

```bash
python main.py --send-test-summary --dry-run
```

## Next Steps

- Connect `broker.py` to Lumibot's Alpaca paper broker.
- Add durable trade and decision logging.
- Add tests for risk rules and scheduler behaviour.
