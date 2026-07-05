"""Simple JSON persistence for trading journal and notification state."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


class TradingJournal:
    """Stores daily balances, decisions, rejected trades, and trade results."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.journal_dir = data_dir / "journal"
        self.state_path = data_dir / "notification_state.json"
        self.journal_dir.mkdir(parents=True, exist_ok=True)

    def record_balance_snapshot(
        self,
        trading_day: date,
        account: dict[str, Any],
        positions: list[dict[str, Any]],
        timestamp: datetime | None = None,
    ) -> None:
        """Persist an account and position snapshot for summary balances."""
        self._append_event(
            trading_day,
            "balance_snapshots",
            {
                "timestamp": self._timestamp(timestamp),
                "account": account,
                "positions": positions,
            },
        )

    def record_ai_decision(
        self,
        trading_day: date,
        decision: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> None:
        """Persist an AI decision for daily decision counts."""
        self._append_event(
            trading_day,
            "ai_decisions",
            {
                "timestamp": self._timestamp(timestamp),
                "decision": decision,
            },
        )

    def record_rejected_trade(
        self,
        trading_day: date,
        decision: dict[str, Any],
        reason: str,
        timestamp: datetime | None = None,
    ) -> None:
        """Persist a rejected non-HOLD trade suggestion."""
        self._append_event(
            trading_day,
            "rejected_trades",
            {
                "timestamp": self._timestamp(timestamp),
                "decision": decision,
                "reason": reason,
            },
        )

    def record_trade_result(
        self,
        trading_day: date,
        decision: dict[str, Any],
        result: dict[str, Any],
        timestamp: datetime | None = None,
    ) -> None:
        """Persist trade execution results when broker execution is attempted."""
        if not result.get("executed"):
            return

        self._append_event(
            trading_day,
            "trades",
            {
                "timestamp": self._timestamp(timestamp),
                "decision": decision,
                "result": result,
            },
        )

    def load_day(self, trading_day: date) -> dict[str, Any]:
        """Load one trading day's journal data."""
        return self._load_json(self._journal_path(trading_day), self._empty_day())

    def get_last_summary_date(self) -> str | None:
        """Return the most recent trading day that received a summary."""
        state = self._load_json(self.state_path, {})
        value = state.get("last_summary_date")
        return str(value) if value else None

    def set_last_summary_date(self, trading_day: date) -> None:
        """Record that a daily summary has been sent for a trading day."""
        self._write_json(
            self.state_path,
            {
                "last_summary_date": trading_day.isoformat(),
                "updated_at": datetime.now().isoformat(),
            },
        )

    def _append_event(
        self,
        trading_day: date,
        event_name: str,
        event: dict[str, Any],
    ) -> None:
        day_data = self.load_day(trading_day)
        day_data.setdefault(event_name, []).append(event)
        self._write_json(self._journal_path(trading_day), day_data)

    def _journal_path(self, trading_day: date) -> Path:
        return self.journal_dir / f"{trading_day.isoformat()}.json"

    def _empty_day(self) -> dict[str, Any]:
        return {
            "balance_snapshots": [],
            "ai_decisions": [],
            "rejected_trades": [],
            "trades": [],
        }

    def _load_json(self, path: Path, default: dict[str, Any]) -> dict[str, Any]:
        if not path.exists():
            return default.copy()

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load persistence file %s: %s", path, exc)
            return default.copy()

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _timestamp(self, timestamp: datetime | None) -> str:
        return (timestamp or datetime.now()).isoformat()
