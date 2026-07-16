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
├── database.py
├── notifications/
│   ├── notifier.py
│   └── discord_notifier.py
├── tests/
├── prompts/
│   ├── system_prompt.md
│   └── user_prompt_template.md
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── README.md
```

## Safety Defaults

- `BOT_ENABLED=false` by default.
- `PAPER_TRADING=true` by default.
- `DRY_RUN=true` by default.
- Alpaca paper execution remains blocked unless all safety gates pass.
- The AI decision layer validates OpenAI JSON before risk checks.
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

For local tests:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

## Railway Deployment

Set the Railway start command to:

```bash
python main.py
```

Add the same environment variables from `.env.example` in the Railway project settings.

For persistent SQLite storage on Railway, mount a Railway volume at `/data` and set:

```env
DATABASE_PATH=/data/trading_bot.db
```

## Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_ENABLED` | `false` | Kill switch. Trading cycles are skipped unless this is true. |
| `PAPER_TRADING` | `true` | Keeps the bot in paper-trading mode. |
| `DRY_RUN` | `true` | Extra safety gate. Risk manager rejects trades unless this is false. |
| `BOT_VERSION` | `local` | Optional label shown in Discord summaries, useful for Railway build or commit identifiers. |
| `TRADING_INTERVAL_MINUTES` | `15` | How often to run a cycle during regular US market hours. |
| `POSITION_MANAGEMENT_ENABLED` | `false` | Enables broker-only deterministic management of existing positions. |
| `POSITION_MANAGEMENT_INTERVAL_MINUTES` | `5` | How often to manage open positions without scanning or calling OpenAI. |
| `MARKET_TIMEZONE` | `America/New_York` | Timezone used for market checks. |
| `OPENAI_API_KEY` | empty | OpenAI API key. |
| `OPENAI_MODEL` | `gpt-5-mini` | Placeholder model setting for future AI logic. |
| `ALPACA_API_KEY` | empty | Alpaca API key. |
| `ALPACA_SECRET_KEY` | empty | Alpaca secret key. |
| `ALPACA_PAPER_BASE_URL` | `https://paper-api.alpaca.markets` | Alpaca paper endpoint. |
| `MAX_POSITION_ALLOCATION_PERCENT` | `5` | Starter risk limit per suggested trade. |
| `MAX_OPEN_POSITIONS` | `10` | Maximum distinct held or pending-entry symbols after a new BUY. |
| `MAX_TOTAL_INVESTED_PERCENT` | `60` | Maximum portfolio percentage invested after held positions, pending BUYs, and a new BUY. |
| `MIN_CONFIDENCE` | `0.70` | Minimum AI confidence before a trade can pass risk checks. |
| `ALLOWED_SYMBOLS` | sample symbols | Optional comma-separated symbol allowlist. |
| `DYNAMIC_WATCHLIST_ENABLED` | `false` | Enables the scanner-built analysis watchlist during each trading cycle. |
| `BROAD_MARKET_SCAN_ENABLED` | `false` | Uses Alpaca tradable US equity assets for a broader scanner before selecting the final watchlist. |
| `BROAD_MARKET_MAX_SYMBOLS` | `1000` | Maximum liquid broad-scan candidates evaluated before final ranking. |
| `MAX_SCANNER_CANDIDATES_AFTER_FILTERS` | `1000` | Maximum broad-scan candidates sent into indicator calculation after asset and price filters. |
| `ALPACA_DATA_FEED` | `iex` | Alpaca market data feed for broad scanner bars. |
| `BROAD_SCAN_DATA_BATCH_SIZE` | `200` | Symbols requested per native Alpaca broad-scan bar batch. |
| `MIN_STOCK_PRICE` | `5` | Minimum current price for broad-scan candidates. |
| `MIN_AVERAGE_VOLUME` | `500000` | Minimum 20-day average volume for broad-scan candidates when available. |
| `EXCLUDE_ETFS` | `true` | Excludes ETF-like assets from the broad scan where identifiable. |
| `WATCHLIST_SIZE` | `20` | Maximum scanner-selected symbols sent to OpenAI. |
| `SCANNER_UNIVERSE` | sample symbols | Comma-separated US stock symbols for scanner v1. |
| `DISCORD_WEBHOOK_URL` | empty | Discord incoming webhook URL for summaries. |
| `DISCORD_DAILY_SUMMARY_ENABLED` | `false` | Enables one daily summary after regular US market close. |
| `DATABASE_PATH` | `trading_bot.db` | SQLite database path. Use `/data/trading_bot.db` on Railway with a mounted volume. |
| `DECISION_HISTORY_LIMIT` | `20` | Recent AI decisions included in OpenAI historical context. |
| `EXECUTION_HISTORY_LIMIT` | `20` | Recent executions included in OpenAI historical context. |
| `PORTFOLIO_HISTORY_LIMIT` | `20` | Recent portfolio snapshots used for performance context. |
| `INCLUDE_HISTORY_CONTEXT` | `true` | Enables SQLite-backed decision and portfolio history in prompts. |

## Trading Flow

1. Start the application.
2. Load configuration from environment variables.
3. Check whether `BOT_ENABLED` is true.
4. Check whether the US market is open.
5. If the market is closed, sleep and do not call OpenAI.
6. If the market is open, manage positions every 5 minutes when enabled and run the existing trading cycle every 15 minutes.
7. Collect broker/account/position/market data.
8. Call the AI decision function.
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

Real Alpaca paper order submission is isolated in `broker.py` and remains blocked unless `BOT_ENABLED=true`, `PAPER_TRADING=true`, `DRY_RUN=false`, market data includes a valid latest price, and the risk manager approves the decision.

Before every new BUY, the execution layer refreshes broker account equity, long positions, and open BUY orders. It rejects entries that would exceed `MAX_OPEN_POSITIONS`, `MAX_TOTAL_INVESTED_PERCENT`, or the existing per-symbol allocation cap. Pending BUY notional is used when available; otherwise remaining quantity is valued at a current price. If neither is available, the guard conservatively reserves one full `MAX_POSITION_ALLOCATION_PERCENT` allocation rather than ignoring the pending exposure.

These portfolio limits apply only to new BUY exposure. They never force-close existing positions, even when the portfolio is already above a configured limit. HOLD, OpenAI SELL, partial-profit exits, trailing-stop exits, reconciliation, and reporting remain unaffected, and all exits remain permitted by these BUY limits.

## Persistent Storage

`database.py` uses Python's built-in SQLite support to persist finalized AI decisions, executions, portfolio snapshots, market snapshots, generated watchlists, and daily reporting statistics. Local development defaults to `trading_bot.db` in the project folder. Railway should use `DATABASE_PATH=/data/trading_bot.db` so records survive deploys and restarts.

The database initializes automatically on startup and creates these tables for current and future analytics: `decisions`, `executions`, `portfolio_snapshots`, `market_snapshots`, `watchlists`, `daily_statistics`, and `position_management`. Additive migrations preserve existing rows. If SQLite is unavailable, the bot logs the failure class and continues without database writes.

When `INCLUDE_HISTORY_CONTEXT=true`, recent decisions, executions, and portfolio snapshots are loaded from SQLite and sent to OpenAI as context. This history is advisory only; Python validation, risk management, and execution gates remain authoritative.

## Dynamic Watchlist

Dynamic watchlists are disabled by default. When `DYNAMIC_WATCHLIST_ENABLED=true`, the bot scans `SCANNER_UNIVERSE`, ranks symbols by volume, gain/loss movement, relative volume, volatility, and momentum, then sends the final capped watchlist to OpenAI. The risk manager and broker safety gates still apply, so the scanner cannot bypass configured trading controls.

When `BROAD_MARKET_SCAN_ENABLED=true`, the scanner first pulls tradable US equity assets from Alpaca, filters out inactive, untradable, OTC, ETF-like, low-price, and low-volume candidates where possible, then fetches native Alpaca bars in batches to rank candidates. Only the final `WATCHLIST_SIZE` symbols and their indicators are sent to OpenAI. If broad scanning fails, the bot falls back to scanner v1; if that fails, it falls back to the static allowed symbols.

To test OpenAI with fake paper-trading context:

```bash
python main.py --test-openai
```

This loads both prompt files, sends mock account/position/watchlist/market data to OpenAI, validates the JSON response, runs the decision through the risk manager in `DRY_RUN` mode, and exits. It does not call Lumibot, does not use a real broker account, and does not place trades.

To test the execution path without placing an order:

```bash
python main.py --test-execution --dry-run
```

This builds a fake approved BUY decision and fake market snapshot, then runs the broker execution path with `DRY_RUN=true`. It confirms the execution guard blocks submission and does not call Alpaca or Lumibot.

To run one real trading cycle and exit:

```bash
python main.py --single-cycle
```

This initialises the normal settings, database, broker, risk manager, and strategy, runs exactly one cycle, then exits. It respects `DRY_RUN` and the existing risk/execution safety gates.

## Deterministic Position Management

When enabled, `position_manager.py` refreshes broker holdings and current Alpaca prices every five minutes without running the broad scanner or invoking OpenAI. At a 3% gain it sells half of the original position once. After that order is broker-confirmed as filled, it retains the post-sale high and exits the remaining broker-held quantity at an exact 2% pullback. OpenAI SELL orders remain valid, and both paths inspect broker-current holdings and covering open SELL orders before submission.

For whole-share positions, half is rounded down to a whole share (for example, 3 shares sells 1). Fractional positions round down to six decimal places. The sale is capped to current holdings, and if the result would be zero or consume the entire position, no partial sale is made; the full position enters trailing management instead. Legacy holdings record whether their original quantity was recovered from confirmed executions or adopted as a conservative current-quantity baseline.

Run one management pass, respecting the configured `DRY_RUN` value:

```bash
python main.py --run-position-management-once
```

Run the diagnostic form only while `DRY_RUN=true`:

```bash
python main.py --test-position-manager
```

To test only the watchlist scanner:

```bash
python main.py --test-scanner --scanner-max-symbols 100
```

This collects scanner data, logs the scanner counts and final selected symbols, then exits before OpenAI, risk checks, or order execution.

## Discord Daily Summaries

Daily summaries are generated after regular US market close and sent once per trading day. SQLite stores persistent daily statistics for trading cycles, AI decisions, risk outcomes, scanner status, order status, runtime, and errors. The existing local JSON notification state under `data/` still prevents duplicate sends after app restarts.

Add these variables locally and in Railway:

```env
DISCORD_WEBHOOK_URL=
DISCORD_DAILY_SUMMARY_ENABLED=false
```

Set `DISCORD_DAILY_SUMMARY_ENABLED=true` when you are ready to send real summaries. Do not put webhook URLs in Git or the README.

The summary includes starting balance, ending balance, daily profit/loss, completed trades, top gain/loss trade when available, open positions, AI decision counts, and rejected trades.
Long reports are split into multiple Discord messages when needed to stay below Discord's message size limit.

To test with mock data:

```bash
python main.py --send-test-summary
```

To preview the message without sending it:

```bash
python main.py --send-test-summary --dry-run
```

## Next Steps

- Continue paper-trading validation on Railway before disabling `DRY_RUN`.
- Review Discord summaries after several market sessions and tune reporting fields if needed.
- Keep broad scanner and OpenAI prompt changes separate from execution safety changes.
