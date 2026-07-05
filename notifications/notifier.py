"""Daily trading summary generation and notification orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

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
    ) -> None:
        self.journal = journal
        self.discord_notifier = discord_notifier
        self.enabled = enabled
        self.dry_run = dry_run

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
        message = self.format_summary(trading_day, day_data)
        logger.info("Discord daily summary generated for %s.", trading_day)

        if self.dry_run:
            print(message)
            logger.info("Discord dry run enabled. Summary printed but not sent.")
            return SummaryResult(sent=False, skipped=False, message=message)

        if self.discord_notifier is None:
            logger.warning("Discord send skipped because notifier is not configured.")
            return SummaryResult(sent=False, skipped=True, message=message)

        sent = self.discord_notifier.send_message(message)
        if sent:
            self.journal.set_last_summary_date(trading_day)
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

        sent = self.discord_notifier.send_message(message)
        return SummaryResult(sent=sent, skipped=not sent, message=message)

    def format_summary(self, trading_day: date, day_data: dict[str, Any]) -> str:
        """Create a compact Discord-friendly daily summary."""
        balance_snapshots = day_data.get("balance_snapshots", [])
        trades = day_data.get("trades", [])
        rejected_trades = day_data.get("rejected_trades", [])
        ai_decisions = day_data.get("ai_decisions", [])

        starting_balance = self._balance_value(balance_snapshots[0]) if balance_snapshots else None
        ending_balance = self._balance_value(balance_snapshots[-1]) if balance_snapshots else None
        daily_pl = self._daily_pl(starting_balance, ending_balance)
        daily_pl_percent = self._daily_pl_percent(starting_balance, daily_pl)

        top_gain = self._top_trade(trades, highest=True)
        top_loss = self._top_trade(trades, highest=False)
        open_positions = self._latest_positions(balance_snapshots)
        decision_counts = self._decision_counts(ai_decisions)

        lines = [
            f"**Daily Trading Summary - {trading_day.isoformat()}**",
            "",
            "**Starting Balance:**",
            self._format_money(starting_balance),
            "",
            "**Ending Balance:**",
            self._format_money(ending_balance),
            "",
            "**Daily P/L:**",
            f"{self._format_signed_money(daily_pl)} ({self._format_percent(daily_pl_percent)})",
            "",
            "**Trades Completed:**",
            str(len(trades)),
            "",
            "**Top Gain:**",
            top_gain,
            "",
            "**Top Loss:**",
            top_loss,
            "",
            "**All Trades:**",
            self._format_trades(trades),
            "",
            "**Open Positions:**",
            self._format_positions(open_positions),
            "",
            "**AI Decisions:**",
            (
                f"BUY: {decision_counts['BUY']}\n"
                f"SELL: {decision_counts['SELL']}\n"
                f"HOLD: {decision_counts['HOLD']}"
            ),
            "",
            "**Rejected Trades:**",
            self._format_rejected_trades(rejected_trades),
        ]
        return "\n".join(lines)

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

    def _decision_counts(self, ai_decisions: list[dict[str, Any]]) -> dict[str, int]:
        counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for event in ai_decisions:
            action = str(event.get("decision", {}).get("action", "")).upper()
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
        price = self._to_float(result.get("price") or result.get("fill_price"))
        price_text = self._format_money(price)
        return f"{timestamp} {action} {symbol} {quantity} shares @ {price_text}"

    def _format_positions(self, positions: list[dict[str, Any]]) -> str:
        if not positions:
            return "No open positions."

        formatted = []
        for position in positions:
            symbol = position.get("symbol") or position.get("asset") or "UNKNOWN"
            quantity = position.get("quantity") or position.get("qty") or position.get("shares") or "?"
            share_label = "share" if str(quantity) == "1" else "shares"
            formatted.append(f"{symbol} {quantity} {share_label}")
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
