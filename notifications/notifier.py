"""Daily trading summary generation and notification orchestration."""

from __future__ import annotations

import logging
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import database
from notifications.discord_notifier import DiscordNotifier
from storage import TradingJournal


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SummaryResult:
    """Result of attempting to generate or send a daily summary."""

    sent: bool
    skipped: bool
    message: str


class DailySummaryNotifier:
    """Builds and sends Discord daily trading summaries once per day."""

    def __init__(
        self,
        journal: TradingJournal,
        discord_notifier: DiscordNotifier | None,
        enabled: bool,
        dry_run: bool = False,
        paper_trading: bool = True,
        bot_dry_run: bool = True,
        bot_version: str = "local",
    ) -> None:
        self.journal = journal
        self.discord_notifier = discord_notifier
        self.enabled = enabled
        self.dry_run = dry_run
        self.paper_trading = paper_trading
        self.bot_dry_run = bot_dry_run
        self.bot_version = bot_version

    def send_daily_summary(
        self,
        trading_day: date,
        current_snapshot: Any | None = None,
        force: bool = False,
    ) -> SummaryResult:
        """Generate and send a daily summary if enabled and not already sent."""
        if not self.enabled and not force and not self.dry_run:
            logger.info("Discord daily summary disabled.")
            return SummaryResult(sent=False, skipped=True, message="")

        last_summary_date = self.journal.get_last_summary_date()
        if not force and last_summary_date == trading_day.isoformat():
            logger.info("Discord summary skipped because already sent for %s.", trading_day)
            return SummaryResult(sent=False, skipped=True, message="")

        if current_snapshot is not None:
            self.journal.record_balance_snapshot(
                trading_day=trading_day,
                account=current_snapshot.account,
                positions=current_snapshot.positions,
            )

        day_data = self.journal.load_day(trading_day)
        report_data = database.load_daily_report_data(trading_day)
        message = self.format_summary(trading_day, day_data, report_data)
        logger.info("Discord daily summary generated for %s.", trading_day)

        if self.dry_run:
            print(message)
            logger.info("Discord dry run enabled. Summary printed but not sent.")
            return SummaryResult(sent=False, skipped=False, message=message)

        if self.discord_notifier is None:
            logger.warning("Discord send skipped because notifier is not configured.")
            return SummaryResult(sent=False, skipped=True, message=message)

        sent = self._send_message_chunks(message)
        if sent:
            self.journal.set_last_summary_date(trading_day)
            database.archive_daily_statistics(trading_day)
        return SummaryResult(sent=sent, skipped=not sent, message=message)

    def send_test_summary(self) -> SummaryResult:
        """Send a mock summary for local webhook testing."""
        trading_day = date.today()
        message = self.format_summary(trading_day, self._mock_day_data())
        logger.info("Discord test summary generated.")

        if self.dry_run:
            print(message)
            logger.info("Discord dry run enabled. Test summary printed but not sent.")
            return SummaryResult(sent=False, skipped=False, message=message)

        if self.discord_notifier is None:
            logger.warning("Discord test send skipped because notifier is not configured.")
            return SummaryResult(sent=False, skipped=True, message=message)

        sent = self._send_message_chunks(message)
        return SummaryResult(sent=sent, skipped=not sent, message=message)

    def format_summary(
        self,
        trading_day: date,
        day_data: dict[str, Any],
        report_data: dict[str, Any] | None = None,
    ) -> str:
        """Create a Discord-friendly daily trading report."""
        report_data = report_data or {}
        stats = report_data.get("stats", {}) if isinstance(report_data, dict) else {}
        decisions = report_data.get("decisions", []) if isinstance(report_data, dict) else []
        executions = report_data.get("executions", []) if isinstance(report_data, dict) else []
        portfolio_snapshots = report_data.get("portfolio_snapshots", []) if isinstance(report_data, dict) else []

        balance_snapshots = day_data.get("balance_snapshots", [])
        trades = self._execution_rows_to_trades(executions) or day_data.get("trades", [])
        rejected_trades = day_data.get("rejected_trades", [])
        ai_decisions = decisions or day_data.get("ai_decisions", [])

        starting_balance = self._snapshot_value(portfolio_snapshots[0]) if portfolio_snapshots else (
            self._balance_value(balance_snapshots[0]) if balance_snapshots else None
        )
        latest_snapshot = portfolio_snapshots[-1] if portfolio_snapshots else None
        ending_balance = self._snapshot_value(latest_snapshot) if latest_snapshot else (
            self._balance_value(balance_snapshots[-1]) if balance_snapshots else None
        )
        daily_pl = self._daily_pl(starting_balance, ending_balance)
        daily_pl_percent = self._daily_pl_percent(starting_balance, daily_pl)
        latest_account = self._latest_account(latest_snapshot, balance_snapshots)
        open_positions = self._latest_positions_from_sources(latest_snapshot, balance_snapshots)
        decision_counts = self._decision_counts(ai_decisions)
        top_decision = self._top_ai_decision(ai_decisions)
        performance = self._performance_stats(trades, stats)
        scanner_symbols = self._scanner_top_symbols(stats, report_data.get("watchlist_rows", []))
        runtime = self._format_runtime(self._to_float(stats.get("runtime_seconds")))
        error_lines = self._format_error_lines(stats)
        lines = [
            "==========================",
            "**🤖 OpenAI Trading Bot**",
            "**Daily Summary**",
            "==========================",
            "",
            f"**Date:** {trading_day.isoformat()}",
            "",
            "**Account**",
            f"- Starting Equity: {self._format_money(starting_balance)}",
            f"- Current Equity: {self._format_money(ending_balance)}",
            f"- Daily P/L ($): {self._format_signed_money(daily_pl)}",
            f"- Daily P/L (%): {self._format_percent(daily_pl_percent)}",
            f"- Cash: {self._format_money(self._to_float(latest_account.get('cash')))}",
            f"- Buying Power: {self._format_money(self._to_float(latest_account.get('buying_power')))}",
            "",
            "**Open Positions**",
            self._format_positions(open_positions),
            "",
            "**Trades Today**",
            self._format_trades(trades),
            "",
            "**Trading Statistics**",
            f"- Trading Cycles: {self._stat_int(stats, 'cycle_count')}",
            f"- AI BUY decisions: {self._stat_int(stats, 'ai_buy_count', decision_counts['BUY'])}",
            f"- AI SELL decisions: {self._stat_int(stats, 'ai_sell_count', decision_counts['SELL'])}",
            f"- AI HOLD decisions: {self._stat_int(stats, 'ai_hold_count', decision_counts['HOLD'])}",
            f"- Risk approvals: {self._stat_int(stats, 'risk_approved_count')}",
            f"- Risk rejections: {self._stat_int(stats, 'risk_rejected_count', len(rejected_trades))}",
            f"- Orders submitted: {self._stat_int(stats, 'orders_submitted', len(trades))}",
            f"- Orders filled: {self._stat_int(stats, 'orders_filled')}",
            f"- Orders cancelled: {self._stat_int(stats, 'orders_cancelled')}",
            f"- Current open positions: {len(open_positions)}",
            "",
            "**Scanner**",
            f"- Scanner mode: {stats.get('scanner_mode') or 'N/A'}",
            f"- Symbols scanned: {self._stat_int(stats, 'symbols_scanned')}",
            f"- Final watchlist size: {self._stat_int(stats, 'final_watchlist_size')}",
            f"- Top 10 ranked symbols: {scanner_symbols}",
            "",
            "**Top AI Decision**",
            self._format_top_decision(top_decision),
            "",
            "**Performance**",
            f"- Win Rate: {self._format_percent(performance['win_rate'])}",
            f"- Average Winner: {self._format_signed_money(performance['average_winner'])}",
            f"- Average Loser: {self._format_signed_money(performance['average_loser'])}",
            f"- Largest Winner: {self._format_signed_money(performance['largest_winner'])}",
            f"- Largest Loser: {self._format_signed_money(performance['largest_loser'])}",
        ]
        if error_lines:
            lines.extend(["", "**⚠️ Errors**", *error_lines])
        lines.extend(
            [
                "",
                "**Footer**",
                f"- Mode: {'Paper' if self.paper_trading else 'Live'}",
                f"- DRY_RUN: {self.bot_dry_run}",
                f"- Bot Version: {self.bot_version}",
                f"- Runtime: {runtime}",
            ]
        )
        return "\n".join(lines)

    def _send_message_chunks(self, message: str) -> bool:
        if self.discord_notifier is None:
            return False
        chunks = self._split_message(message)
        return all(self.discord_notifier.send_message(chunk) for chunk in chunks)

    def _split_message(self, message: str, limit: int = 1900) -> list[str]:
        if len(message) <= limit:
            return [message]
        chunks = []
        current = []
        current_length = 0
        for line in message.splitlines():
            line_length = len(line) + 1
            if current and current_length + line_length > limit:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
            current.append(line)
            current_length += line_length
        if current:
            chunks.append("\n".join(current))
        return chunks

    def _balance_value(self, snapshot_event: dict[str, Any]) -> float | None:
        account = snapshot_event.get("account", {})
        return self._to_float(account.get("portfolio_value") or account.get("cash"))

    def _daily_pl(self, starting_balance: float | None, ending_balance: float | None) -> float | None:
        if starting_balance is None or ending_balance is None:
            return None
        return ending_balance - starting_balance

    def _daily_pl_percent(
        self,
        starting_balance: float | None,
        daily_pl: float | None,
    ) -> float | None:
        if starting_balance in (None, 0) or daily_pl is None:
            return None
        return daily_pl / starting_balance * 100

    def _top_trade(self, trades: list[dict[str, Any]], highest: bool) -> str:
        trades_with_pl = [
            trade for trade in trades if self._trade_pl(trade) is not None
        ]
        if not trades_with_pl:
            return "N/A"

        trade = max(trades_with_pl, key=self._trade_pl) if highest else min(trades_with_pl, key=self._trade_pl)
        symbol = self._trade_symbol(trade)
        return f"{symbol} {self._format_signed_money(self._trade_pl(trade))}"

    def _latest_positions(self, balance_snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not balance_snapshots:
            return []
        return balance_snapshots[-1].get("positions", [])

    def _latest_positions_from_sources(
        self,
        latest_snapshot: dict[str, Any] | None,
        balance_snapshots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if latest_snapshot:
            raw_snapshot = self._json_loads(latest_snapshot.get("raw_snapshot"))
            positions = raw_snapshot.get("positions") if isinstance(raw_snapshot, dict) else None
            if isinstance(positions, list):
                return positions
        return self._latest_positions(balance_snapshots)

    def _decision_counts(self, ai_decisions: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for event in ai_decisions:
            action = str(event.get("action") or event.get("decision", {}).get("action", "")).upper()
            if action in counts:
                counts[action] += 1
        return counts

    def _format_trades(self, trades: list[dict[str, Any]]) -> str:
        if not trades:
            return "No trades completed today."

        return "\n".join(self._format_trade(trade) for trade in trades)

    def _format_trade(self, trade: dict[str, Any]) -> str:
        result = trade.get("result", {})
        decision = trade.get("decision", {})
        timestamp = self._format_time(trade.get("timestamp"))
        action = str(result.get("action") or decision.get("action") or "").upper()
        symbol = self._trade_symbol(trade)
        quantity = result.get("quantity") or result.get("shares") or "?"
        price = self._to_float(result.get("fill_price") or result.get("price"))
        price_text = self._format_money(price)
        pl = self._trade_pl(trade)
        pl_text = f" | P/L {self._format_signed_money(pl)}" if pl is not None else ""
        return f"- {timestamp} {symbol} {action} {quantity} @ {price_text}{pl_text}"

    def _format_positions(self, positions: list[dict[str, Any]]) -> str:
        if not positions:
            return "No open positions."

        formatted = []
        for position in positions:
            symbol = position.get("symbol") or position.get("asset") or "UNKNOWN"
            quantity = position.get("quantity") or position.get("qty") or position.get("shares") or "?"
            average_entry = self._to_float(position.get("average_price") or position.get("avg_entry_price"))
            market_value = self._to_float(position.get("market_value"))
            current_price = self._position_current_price(quantity, market_value)
            unrealised = self._position_unrealised_pl(quantity, average_entry, market_value)
            formatted.append(
                "- "
                f"{symbol} | Qty {quantity} | Avg {self._format_money(average_entry)} | "
                f"Price {self._format_money(current_price)} | U/P/L {self._format_signed_money(unrealised)}"
            )
        return "\n".join(formatted)

    def _format_rejected_trades(self, rejected_trades: list[dict[str, Any]]) -> str:
        if not rejected_trades:
            return "0"

        lines = [str(len(rejected_trades))]
        for rejected in rejected_trades[:5]:
            decision = rejected.get("decision", {})
            symbol = decision.get("symbol", "UNKNOWN")
            action = decision.get("action", "UNKNOWN")
            reason = rejected.get("reason", "Rejected by risk manager.")
            lines.append(f"{action} {symbol}: {reason}")
        return "\n".join(lines)

    def _trade_pl(self, trade: dict[str, Any]) -> float | None:
        result = trade.get("result", {})
        return self._to_float(
            result.get("profit_loss")
            or result.get("pnl")
            or result.get("realized_pl")
            or result.get("realized_pnl")
        )

    def _execution_rows_to_trades(self, executions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        trades = []
        for row in executions:
            raw_response = self._json_loads(row.get("raw_response"))
            trades.append(
                {
                    "timestamp": row.get("timestamp"),
                    "decision": {"action": row.get("side"), "symbol": row.get("symbol")},
                    "result": {
                        **(raw_response if isinstance(raw_response, dict) else {}),
                        "action": row.get("side"),
                        "symbol": row.get("symbol"),
                        "quantity": row.get("quantity"),
                        "fill_price": row.get("fill_price"),
                    },
                }
            )
        return trades

    def _snapshot_value(self, snapshot: dict[str, Any] | None) -> float | None:
        if not snapshot:
            return None
        return self._to_float(snapshot.get("portfolio_value") or snapshot.get("equity"))

    def _latest_account(
        self,
        latest_snapshot: dict[str, Any] | None,
        balance_snapshots: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if latest_snapshot:
            return latest_snapshot
        if balance_snapshots:
            return balance_snapshots[-1].get("account", {})
        return {}

    def _top_ai_decision(self, ai_decisions: list[dict[str, Any]]) -> dict[str, Any] | None:
        normalized = []
        for event in ai_decisions:
            decision = event.get("decision", event)
            confidence = self._to_float(decision.get("confidence"))
            if confidence is not None:
                normalized.append(decision)
        if not normalized:
            return None
        return max(normalized, key=lambda item: self._to_float(item.get("confidence")) or 0)

    def _format_top_decision(self, decision: dict[str, Any] | None) -> str:
        if not decision:
            return "- N/A"
        reason = self._shorten(str(decision.get("reason") or "N/A"), 110)
        return (
            f"- Symbol: {decision.get('symbol', 'UNKNOWN')}\n"
            f"- Action: {decision.get('action', 'UNKNOWN')}\n"
            f"- Confidence: {self._format_decimal(self._to_float(decision.get('confidence')))}\n"
            f"- Reason: {reason}"
        )

    def _performance_stats(self, trades: list[dict[str, Any]], stats: dict[str, Any]) -> dict[str, float | None]:
        pl_values = [self._trade_pl(trade) for trade in trades]
        pl_values = [value for value in pl_values if value is not None]
        winners = [value for value in pl_values if value > 0]
        losers = [value for value in pl_values if value < 0]
        total_closed = len(winners) + len(losers)
        return {
            "win_rate": (len(winners) / total_closed * 100) if total_closed else None,
            "average_winner": (sum(winners) / len(winners)) if winners else None,
            "average_loser": (sum(losers) / len(losers)) if losers else None,
            "largest_winner": self._to_float(stats.get("largest_win")) or (max(winners) if winners else None),
            "largest_loser": self._to_float(stats.get("largest_loss")) or (min(losers) if losers else None),
        }

    def _format_error_lines(self, stats: dict[str, Any]) -> list[str]:
        fields = [
            ("Scanner failures", "scanner_failures"),
            ("API failures", "api_errors"),
            ("OpenAI failures", "openai_failures"),
            ("Order failures", "order_failures"),
        ]
        lines = []
        for label, field_name in fields:
            value = self._stat_int(stats, field_name)
            if value:
                lines.append(f"- {label}: {value}")
        return lines

    def _scanner_top_symbols(self, stats: dict[str, Any], watchlist_rows: list[dict[str, Any]]) -> str:
        symbols = self._json_loads(stats.get("top_ranked_symbols"))
        if isinstance(symbols, list) and symbols:
            return ", ".join(str(symbol) for symbol in symbols[:10])
        if watchlist_rows:
            return ", ".join(str(row.get("symbol")) for row in watchlist_rows[:10] if row.get("symbol"))
        return "N/A"

    def _position_current_price(self, quantity: Any, market_value: float | None) -> float | None:
        quantity_value = self._to_float(quantity)
        if quantity_value in (None, 0) or market_value is None:
            return None
        return abs(market_value / quantity_value)

    def _position_unrealised_pl(
        self,
        quantity: Any,
        average_entry: float | None,
        market_value: float | None,
    ) -> float | None:
        quantity_value = self._to_float(quantity)
        if quantity_value is None or average_entry is None or market_value is None:
            return None
        return market_value - (quantity_value * average_entry)

    def _trade_symbol(self, trade: dict[str, Any]) -> str:
        result = trade.get("result", {})
        decision = trade.get("decision", {})
        return str(result.get("symbol") or decision.get("symbol") or "UNKNOWN").upper()

    def _format_time(self, value: str | None) -> str:
        if not value:
            return "--:--"
        try:
            return datetime.fromisoformat(value).strftime("%H:%M")
        except ValueError:
            return "--:--"

    def _format_money(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"${value:,.2f}"

    def _format_signed_money(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        sign = "+" if value >= 0 else "-"
        return f"{sign}${abs(value):,.2f}"

    def _format_percent(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        sign = "+" if value >= 0 else "-"
        return f"{sign}{abs(value):.2f}%"

    def _format_decimal(self, value: float | None) -> str:
        if value is None:
            return "N/A"
        return f"{value:.2f}"

    def _format_runtime(self, seconds: float | None) -> str:
        if seconds is None:
            return "N/A"
        minutes = int(seconds // 60)
        remaining_seconds = int(seconds % 60)
        return f"{minutes}m {remaining_seconds}s"

    def _stat_int(self, stats: dict[str, Any], field_name: str, fallback: int = 0) -> int:
        value = stats.get(field_name, fallback)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return fallback

    def _shorten(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 3)].rstrip() + "..."

    def _json_loads(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except Exception:
            return None

    def _to_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _mock_day_data(self) -> dict[str, Any]:
        return {
            "balance_snapshots": [
                {
                    "timestamp": "2026-07-05T09:30:00",
                    "account": {"portfolio_value": 100000.00, "cash": 90000.00},
                    "positions": [],
                },
                {
                    "timestamp": "2026-07-05T16:01:00",
                    "account": {"portfolio_value": 100482.31, "cash": 78100.00},
                    "positions": [
                        {"symbol": "MSFT", "quantity": 2},
                        {"symbol": "SPY", "quantity": 1},
                    ],
                },
            ],
            "trades": [
                {
                    "timestamp": "2026-07-05T09:45:00",
                    "decision": {"action": "BUY", "symbol": "AAPL"},
                    "result": {
                        "executed": True,
                        "action": "BUY",
                        "symbol": "AAPL",
                        "quantity": 5,
                        "price": 214.33,
                        "profit_loss": 221.40,
                    },
                },
                {
                    "timestamp": "2026-07-05T13:00:00",
                    "decision": {"action": "BUY", "symbol": "AMD"},
                    "result": {
                        "executed": True,
                        "action": "BUY",
                        "symbol": "AMD",
                        "quantity": 3,
                        "price": 164.10,
                        "profit_loss": -42.18,
                    },
                },
            ],
            "ai_decisions": [
                {"decision": {"action": "BUY"}},
                {"decision": {"action": "BUY"}},
                {"decision": {"action": "SELL"}},
                {"decision": {"action": "HOLD"}},
                {"decision": {"action": "HOLD"}},
            ],
            "rejected_trades": [
                {
                    "decision": {"action": "BUY", "symbol": "NVDA"},
                    "reason": "Suggested allocation exceeded max allocation.",
                }
            ],
        }
