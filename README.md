# OpenAI Trading Bot

An OpenAI-driven Python trading bot scaffold that uses Lumibot as the execution layer, starts with Alpaca Paper Trading, and keeps risk controls in Python.

Core principle:

```text
OpenAI suggests.
Python validates.
Lumibot executes.
```

## Strategy Goals (read this before changing exit logic)

This bot is deliberately tuned to trade like a **day trader, not a swing trader**. That
decision came out of a review of a week of production logs where the AI's own discretionary
SELL judgement was the only thing standing between a losing position and reality — one
position was left open and losing more than 7% for hours because nothing forced an earlier
exit, while a separate winning position was cut at little more than 1% gain purely on AI
whim. That inconsistency is the reason `position_manager.py` now owns exits deterministically
instead of leaving them to AI discretion.

The outcomes this bot is tuned for, in priority order:

1. **Small, frequent, consistent wins over rare big ones.** Target roughly $100-200 profit
   per trade, not a multi-day hold hoping for $1,000+. `PARTIAL_TARGET_PERCENT` (3%) and the
   per-position allocation cap are sized so a full win lands in that range on a typical
   account balance — don't casually resize one without checking the other.
2. **Cut losers fast and mechanically, not on AI discretion.** `STOP_LOSS_PERCENT` (2%) and
   the `TIME_STOP_HOURS`/`TIME_STOP_BAND_PERCENT` dead-zone check exist specifically so a
   losing or stagnant position can never again be left to drift for hours the way it did in
   the log review that motivated this. These checks run every 5-minute position-management
   cycle, independent of whatever the AI decides that cycle.
3. **No overnight risk, full daily capital recycling.** Every open position is force-closed
   before the close (`EOD_FLATTEN_BUFFER_MINUTES`) so capital is never sitting idle in a
   multi-day hold — it's freed up to go back into a fresh opportunity the next session.
4. **More entries per day, not bigger ones.** `TRADING_INTERVAL_MINUTES` was deliberately
   shortened (15 → 10) to give the AI more looks per day at new BUY candidates, on the theory
   that daily trade *volume* is the lever for this account, not position size or hold time.

If you're changing any of the constants in `position_manager.py` or `config.py`, ask which of
these four goals the change serves before touching it — see "Design Decisions & Deferred
Work" below for what was deliberately left alone and why.

## Project Structure

```text
.
├── main.py                        # Long-running entry point, CLI flags, main loop
├── config.py                      # Settings dataclass loaded from environment variables
├── scheduler.py                   # Market-hours checks, NYSE calendar, cycle cadence, file lock
├── broker.py                      # Alpaca/Lumibot execution, portfolio guards, order reconciliation
├── strategy.py                    # AI-driven trading cycle: build context, get decision, risk-check, execute
├── risk_manager.py                # Python-owned guardrails applied to every AI suggestion
├── position_manager.py            # Deterministic exits: partial-profit, trailing, stop-loss, time-stop, EOD flatten
├── openai_logic.py                # Prompt loading, OpenAI call, JSON parsing/validation (AIDecision)
├── market_indicators.py           # Technical indicator calculations for scanner/prompt data
├── watchlist_scanner.py           # Dynamic/broad-market watchlist scanning and ranking
├── context_history.py             # Loads recent SQLite history for the OpenAI prompt
├── database.py                    # SQLite persistence: decisions, executions, snapshots, position_management
├── storage.py                     # Trading journal (local JSON) used by Discord summaries
├── logger_config.py               # Logging setup
├── notifications/
│   ├── notifier.py                # Daily summary building/dedupe
│   └── discord_notifier.py        # Discord webhook delivery
├── tests/                         # pytest suite (see below)
├── prompts/
│   ├── system_prompt.md           # AI system prompt: constraints, JSON schema, decision rules
│   └── user_prompt_template.md
├── execution_test.py / openai_test.py / scanner_test.py   # Backing modules for --test-* CLI flags
├── data/, logs/                    # Runtime output (gitignored) — journal, lock file, log rotation
├── requirements.txt / requirements-dev.txt
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
| `TRADING_INTERVAL_MINUTES` | `10` | How often to run a cycle during regular US market hours. |
| `POSITION_MANAGEMENT_ENABLED` | `false` | Enables broker-only deterministic management of existing positions. |
| `POSITION_MANAGEMENT_INTERVAL_MINUTES` | `5` | How often to manage open positions without scanning or calling OpenAI. |
| `MARKET_TIMEZONE` | `America/New_York` | Timezone used for market checks. |
| `OPENAI_API_KEY` | empty | OpenAI API key. |
| `OPENAI_MODEL` | `gpt-5-mini` | Model used for each AI trading decision. |
| `ALPACA_API_KEY` | empty | Alpaca API key. |
| `ALPACA_SECRET_KEY` | empty | Alpaca secret key. |
| `MAX_POSITION_ALLOCATION_PERCENT` | `5` | Starter risk limit per suggested trade. |
| `MAX_OPEN_POSITIONS` | `10` | Maximum distinct held or pending-entry symbols after a new BUY. |
| `MAX_TOTAL_INVESTED_PERCENT` | `60` | Maximum portfolio percentage invested after held positions, pending BUYs, and a new BUY. |
| `MAX_MARKET_DATA_AGE_SECONDS` | `180` | Maximum age of intraday data allowed for a new BUY. |
| `MIN_CONFIDENCE` | `0.70` | Minimum AI confidence before a trade can pass risk checks. |
| `ALLOWED_SYMBOLS` | sample symbols | Optional comma-separated symbol allowlist. |
| `DYNAMIC_WATCHLIST_ENABLED` | `false` | Enables the scanner-built analysis watchlist during each trading cycle. |
| `BROAD_MARKET_SCAN_ENABLED` | `false` | Uses Alpaca tradable US equity assets for a broader scanner before selecting the final watchlist. |
| `BROAD_MARKET_MAX_SYMBOLS` | `1000` | Maximum liquid broad-scan candidates evaluated before final ranking. |
| `MAX_SCANNER_CANDIDATES_AFTER_FILTERS` | `1000` | Maximum highest-liquidity broad-scan candidates sent into intraday indicator calculation. |
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
6. If the market is open, manage positions every 5 minutes when enabled and run the existing trading cycle every 10 minutes.
7. Collect broker/account/position/market data.
8. Call the AI decision function.
9. Pass the AI decision through the risk manager.
10. If approved, execute through Lumibot.
11. Log the result.
12. Continue until stopped.

These are two independent, differently-timed loops driven by `main.py` and `scheduler.py`:

Both schedules are anchored to their planned start times. Scanner, model, or broker runtime no longer gets added to the configured interval, so a 10-minute trading cycle remains on a 10-minute cadence even when one cycle takes time to complete.

- **The AI trading cycle** (steps 7-11 above, every `TRADING_INTERVAL_MINUTES`) only ever
  proposes new BUYs, discretionary SELLs, or HOLDs for one symbol per cycle — see "AI Decision
  Layer" below for why that's one decision at a time, not a batch.
- **Deterministic position management** (`position_manager.py`, every
  `POSITION_MANAGEMENT_INTERVAL_MINUTES`) runs independently of the AI entirely and owns every
  exit for existing positions. See "Deterministic Position Management" for the exact order it
  checks things in — EOD flatten first, then trailing, then stop-loss/time-stop, then the
  partial-profit target — which matters if you're reading or extending `_manage_symbol`.

## AI Decision Layer

`openai_logic.py` owns prompt loading, OpenAI API calls, JSON parsing, and Pydantic validation.

`strategy.py` calls it like this during a trading cycle:

```python
context = self._build_ai_context(snapshot)
decision = self._get_ai_client().get_decision(context)
risk_manager_input = decision.to_risk_manager_dict()
```

The AI layer never executes trades and never bypasses the risk manager.

`AIDecision` in `openai_logic.py` is a single object — one symbol, one action, per trading
cycle. The AI sees full context on every position and every watchlist symbol each cycle (see
`_build_ai_context` / `_format_market_intelligence` in `strategy.py`), but can only act on one
of them. This was evaluated and deliberately deferred rather than changed — see "Design
Decisions & Deferred Work" below for why.

Real Alpaca paper order submission is isolated in `broker.py` and remains blocked unless `BOT_ENABLED=true`, `PAPER_TRADING=true`, `DRY_RUN=false`, market data includes a valid latest price, and the risk manager approves the decision.

Before every new BUY, the execution layer refreshes broker account equity, long positions, open BUY orders, and the latest execution price. The suggested allocation is treated as the target final position size, so an existing position is topped up only by the remaining amount instead of receiving another full allocation. It rejects entries that would exceed `MAX_OPEN_POSITIONS`, `MAX_TOTAL_INVESTED_PERCENT`, the per-symbol allocation cap, or `MAX_MARKET_DATA_AGE_SECONDS`. Pending BUY notional is used when available; otherwise remaining quantity is valued at a current price. If neither is available, the guard conservatively reserves one full `MAX_POSITION_ALLOCATION_PERCENT` allocation rather than ignoring the pending exposure.

These portfolio limits apply only to new BUY exposure. They never force-close existing positions, even when the portfolio is already above a configured limit. HOLD, OpenAI SELL, partial-profit exits, trailing-stop exits, reconciliation, and reporting remain unaffected, and all exits remain permitted by these BUY limits.

## Persistent Storage

`database.py` uses Python's built-in SQLite support to persist finalized AI decisions, executions, portfolio snapshots, market snapshots, generated watchlists, and daily reporting statistics. Local development defaults to `trading_bot.db` in the project folder. Railway should use `DATABASE_PATH=/data/trading_bot.db` so records survive deploys and restarts.

The database initializes automatically on startup and creates these tables for current and future analytics: `decisions`, `executions`, `portfolio_snapshots`, `market_snapshots`, `watchlists`, `daily_statistics`, and `position_management`. Additive migrations preserve existing rows. If SQLite is unavailable, the bot logs the failure class and continues without database writes.

When `INCLUDE_HISTORY_CONTEXT=true`, recent decisions, executions, and portfolio snapshots are loaded from SQLite and sent to OpenAI as context. This history is advisory only; Python validation, risk management, and execution gates remain authoritative.

## Dynamic Watchlist

Dynamic watchlists are disabled by default. When `DYNAMIC_WATCHLIST_ENABLED=true`, the bot scans `SCANNER_UNIVERSE`, ranks symbols by volume, gain/loss movement, relative volume, volatility, and momentum, then sends the final capped watchlist to OpenAI. Intraday metrics use the latest regular-market session only, relative volume is adjusted for elapsed session time, and stale intraday data cannot produce a new BUY. The risk manager and broker safety gates still apply, so the scanner cannot bypass configured trading controls.

When `BROAD_MARKET_SCAN_ENABLED=true`, the scanner first pulls tradable US equity assets from Alpaca and filters out inactive, untradable, OTC, and ETF-like assets. It fetches completed daily bars for the full filtered universe, applies price and volume requirements, keeps the highest-liquidity candidates, and refreshes their current intraday bars before ranking. Assets and completed daily bars are cached for the trading day, while intraday data is refreshed every cycle. Only the final `WATCHLIST_SIZE` symbols and their indicators are sent to OpenAI. The AI also receives an explicit list of symbols that still have portfolio capacity for at least one whole share, preventing repeated BUY suggestions for positions already at their target. If broad scanning fails, the bot falls back to scanner v1; if that fails, it falls back to the static allowed symbols.

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

When enabled, `position_manager.py` refreshes broker holdings and current Alpaca prices every five minutes without running the broad scanner or invoking OpenAI. `PositionManager` takes a `MarketScheduler` instance (constructed once in `main.py` and shared with the AI trading cycle) so it can read today's actual NYSE close from the market calendar for the EOD-flatten check below, rather than duplicating calendar logic. At a 3% gain it sells half of the original position once. After that order is broker-confirmed as filled, it retains the post-sale high and exits the remaining broker-held quantity at an exact 2% pullback. OpenAI SELL orders remain valid, and both paths inspect broker-current holdings and covering open SELL orders before submission.

For whole-share positions, half is rounded down to a whole share (for example, 3 shares sells 1). Fractional positions round down to six decimal places. The sale is capped to current holdings, and if the result would be zero or consume the entire position, no partial sale is made; the full position enters trailing management instead. Legacy holdings record whether their original quantity was recovered from confirmed executions or adopted as a conservative current-quantity baseline.

Before a position ever reaches the 3% partial-profit target, three additional deterministic exits protect against losers and stagnant capital:

- **Hard stop-loss (`STOP_LOSS_PERCENT`, 2%)**: closes the entire position immediately if it is down 2% or more, independent of the AI's discretionary SELL judgement.
- **Time-stop / dead-zone (`TIME_STOP_HOURS`, 2 hours; `TIME_STOP_BAND_PERCENT`, ±1%)**: closes the entire position if it has been open 2 hours or more and is sitting within ±1% of cost basis, so capital is not left idle in a trade that is not clearly heading toward the stop-loss or the profit target.
- **EOD flatten (`EOD_FLATTEN_BUFFER_MINUTES`, 30 minutes)**: force-closes every open position 30 minutes before today's actual NYSE close (using the market calendar, so early-close days are respected), regardless of gain, loss, or trailing state. This check runs first and overrides every other exit path.

Once the partial profit is taken and trailing management activates, the 2% trailing stop owns loss control for the remaining shares; the stop-loss and time-stop checks no longer apply. Each exit path records its own `status` (`stop_loss_submitted`, `time_stop_submitted`, `eod_flatten_submitted`) and `exit_source`/`exit_reason` pair, consistent with the existing `partial_profit` and `trailing_stop` conventions.

**Exit check order inside `_manage_symbol` (top to bottom, first match wins):**

1. EOD flatten — overrides everything below, regardless of state.
2. Trailing stop — only if `trailing_stop_activated` (i.e. the partial profit already filled).
3. Stop-loss / time-stop — only reachable pre-partial-profit (gain below `PARTIAL_TARGET_PERCENT`).
4. Partial-profit target — the original 3% rule, only once nothing above has fired.

**Every exit path guards against resubmitting while its own order is still pending
reconciliation.** Trailing, stop-loss/time-stop, and EOD flatten each check
`state.get("final_exit_order_id")` and return immediately if it's already set, instead of
calling the broker again on the next 5-minute cycle before the previous order has filled. This
was added after the original stop-loss/time-stop implementation was found to be missing this
guard (broker-side `_covering_open_sell_order` duplicate-prevention in `broker.py` stopped it
from ever placing a real duplicate order, but it was still calling the broker and logging
"duplicate prevented" every cycle until the order filled — fixed for consistency with the
other two paths).

**EOD flatten cancels a still-open partial-profit order before flattening, if it can.** If the
flatten deadline arrives while a partial-profit SELL is submitted but not yet filled
(`partial_profit_order_id` set, `partial_profit_taken` still false), a full-quantity flatten
sell submitted alongside it would only be able to sell the shares not already reserved by that
order — `broker._covering_open_sell_order` only treats an existing order as "covering" a new
request when its remaining quantity is greater than or equal to the new request, so a
half-size pending order does not shield a full-quantity flatten from a broker-side rejection.
`_manage_eod_flatten` calls `BrokerClient.cancel_position_management_order(order_id)` first in
this situation (a thin wrapper around Alpaca's `cancel_order_by_id`) and then submits the
flatten sell regardless of whether the cancel call reports success — if cancellation worked,
the flatten now succeeds in the same cycle; if it didn't (or the broker doesn't expose
cancellation), the flatten sell falls back to the same safe broker-side rejection it would
have hit anyway, and retries on the next 5-minute cycle.

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

## Design Decisions & Deferred Work

These were deliberately evaluated and left alone. If you're picking this project back up,
read this before "fixing" any of them — they're intentional, not oversights.

- **Position limits (`MAX_OPEN_POSITIONS`, `MAX_TOTAL_INVESTED_PERCENT`,
  `MAX_POSITION_ALLOCATION_PERCENT`) were left unchanged** when the exit logic above was
  added. A week of production logs showed the busiest day already produced ~16 trades within
  the existing caps, while the quietest days coincided with capital being stuck in a single
  stagnant loser for days rather than the caps themselves being the bottleneck. The
  stop-loss/time-stop/EOD-flatten changes were made first specifically to test whether faster
  capital recycling increases daily trade count on its own before touching the caps. If you're
  considering raising them, check daily trade counts since these exits shipped first — you may
  not need to.
- **Multi-symbol AI decisions per cycle were considered and deferred.** The AI already sees
  every position and every watchlist symbol's indicators each cycle; it just can't act on more
  than one per cycle. Batching multiple decisions into one AI response was evaluated as a way
  to open more new positions per day, but was set aside because it needs real design work
  first: per-decision JSON validation (so one malformed decision in a batch doesn't cost the
  whole cycle instead of just one, the way a single-decision failure does today), and
  cumulative allocation/position-count validation across the batch in `risk_manager.py` and
  `broker.py` (today's portfolio checks assume one decision at a time). There's also a real
  correlation risk: decisions made in the same cycle share the same market snapshot, so a
  batch of BUYs is more likely to be correlated than BUYs staggered across separate cycles.
  `TRADING_INTERVAL_MINUTES` was shortened instead as the lower-risk lever for more entries
  per day. Revisit batching only as a separate, carefully-scoped change.
- **`TRADING_INTERVAL_MINUTES` was shortened from 15 to 10 minutes** to increase how often the
  AI evaluates new BUY candidates per day, on the theory that entry frequency — not position
  size or hold time — is the lever for this account's $100-200-per-trade goal. If this is
  overridden as an explicit environment variable on Railway (rather than left to the code
  default), remember to update it there too; changing the default in `config.py` alone won't
  affect an environment where it's set explicitly.

## Operations Notes

Lumibot logs a `LUMIBOT_TELEMETRY` line on every cycle with process memory, thread count, and
file-descriptor usage — useful for right-sizing Railway resources without guessing. Railway's
seven-day metrics on 2026-08-03 showed average memory use of roughly 0.33 GB (0.62 GB maximum)
and average CPU use of roughly 0.0022 vCPU. The broad market scanner and indicator calculations
run sequentially, batched via `BROAD_SCAN_DATA_BATCH_SIZE`, so this workload does not need many
vCPUs. Railway bills per-second on actual vCPU-seconds and GB-seconds consumed, not on the
resource limit/cap configured for the service — the cap is a safety ceiling against runaway
usage, not a cost lever.

During a normal market session the production workload is:

- Up to roughly 39 AI trading cycles at the 10-minute cadence. Each cycle reconciles prior
  executions, builds the final watchlist and market indicators, requests one OpenAI decision,
  applies Python risk controls, optionally submits one order, and persists the result.
- Up to roughly 78 deterministic position-management passes at the 5-minute cadence. These
  refresh only current holdings and prices; they do not run the scanner or call OpenAI.
- One closing account/position snapshot and one Discord daily report after the market closes.
  Once the report is recorded as sent in persistent SQLite state, later closed-market checks
  and Railway restarts do not rebuild the scanner or resend the report.
- No OpenAI calls on weekends, market holidays, or while the regular market is closed.

Each successful OpenAI response logs input, cached-input, output, reasoning-output, and total
token counts. Use those counts with the current OpenAI model pricing rather than estimating
API spend from request count alone.

## Next Steps

- Keep Railway in Alpaca paper-trading mode while validating real paper-order execution with
  `DRY_RUN=false`; do not consider live brokerage execution until several sessions are stable.
- Watch daily trade counts for a few sessions now that stop-loss/time-stop/EOD-flatten and the
  10-minute cycle are live, before deciding whether position limits need to change (see
  "Design Decisions & Deferred Work" above).
- Review Discord summaries after several market sessions and tune reporting fields if needed.
- Keep broad scanner and OpenAI prompt changes separate from execution safety changes.
