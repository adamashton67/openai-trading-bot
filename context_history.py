"""Load recent SQLite history for OpenAI trading context."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import database


logger = logging.getLogger(__name__)


def load_history_context(
    decision_limit: int,
    execution_limit: int,
    portfolio_limit: int,
    today: date | None = None,
) -> dict[str, Any]:
    """Return recent decision, execution, and portfolio context."""
    try:
        if not database._ensure_database_available():
            return empty_history_context()

        with database._connect(database._database_path) as connection:
            decisions = _load_recent_decisions(connection, decision_limit)
            executions = _load_recent_executions(connection, execution_limit)
            portfolio_snapshots = _load_recent_portfolio_snapshots(connection, portfolio_limit)

        return {
            "recent_ai_decisions": decisions,
            "recent_executions": executions,
            "portfolio_performance_summary": _portfolio_summary(
                decisions=decisions,
                executions=executions,
                portfolio_snapshots=portfolio_snapshots,
                today=today,
            ),
        }
    except Exception as exc:
        logger.warning("History context load failed safely: %s.", exc.__class__.__name__)
        return empty_history_context()


def empty_history_context() -> dict[str, Any]:
    """Return an empty but structured history context."""
    return {
        "recent_ai_decisions": [],
        "recent_executions": [],
        "portfolio_performance_summary": {},
    }


def _load_recent_decisions(connection: Any, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            timestamp,
            symbol,
            action,
            confidence,
            allocation_percent,
            approved,
            approval_reason,
            executed,
            reason
        FROM decisions
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "timestamp": row[0],
            "symbol": row[1],
            "action": row[2],
            "confidence": row[3],
            "allocation_percent": row[4],
            "approved": _bool_or_none(row[5]),
            "approval_reason": row[6],
            "executed": _bool_or_none(row[7]),
            "reason": row[8],
        }
        for row in rows
    ]


def _load_recent_executions(connection: Any, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            timestamp,
            symbol,
            side,
            quantity,
            fill_price,
            status,
            broker_order_id
        FROM executions
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "timestamp": row[0],
            "symbol": row[1],
            "side": row[2],
            "quantity": row[3],
            "fill_price": row[4],
            "status": row[5],
            "broker_order_id": row[6],
        }
        for row in rows
    ]


def _load_recent_portfolio_snapshots(connection: Any, limit: int) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT
            timestamp,
            cash,
            buying_power,
            equity,
            portfolio_value,
            positions_count,
            raw_snapshot
        FROM portfolio_snapshots
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        {
            "timestamp": row[0],
            "cash": row[1],
            "buying_power": row[2],
            "equity": row[3],
            "portfolio_value": row[4],
            "positions_count": row[5],
            "raw_snapshot": row[6],
        }
        for row in rows
    ]


def _portfolio_summary(
    decisions: list[dict[str, Any]],
    executions: list[dict[str, Any]],
    portfolio_snapshots: list[dict[str, Any]],
    today: date | None,
) -> dict[str, Any]:
    latest = portfolio_snapshots[0] if portfolio_snapshots else {}
    previous = portfolio_snapshots[1] if len(portfolio_snapshots) > 1 else {}
    latest_value = _to_float(latest.get("portfolio_value"))
    previous_value = _to_float(previous.get("portfolio_value"))
    change = None
    change_percent = None
    if latest_value is not None and previous_value not in (None, 0):
        change = latest_value - previous_value
        change_percent = (change / previous_value) * 100

    today_prefix = today.isoformat() if today is not None else None
    decisions_today = [
        decision
        for decision in decisions
        if _is_today(decision.get("timestamp"), today_prefix)
    ]
    executions_today = [
        execution
        for execution in executions
        if _is_today(execution.get("timestamp"), today_prefix)
    ]

    return {
        "latest_portfolio_value": latest_value,
        "previous_portfolio_value": previous_value,
        "portfolio_change": change,
        "portfolio_change_percent": change_percent,
        "latest_cash": _to_float(latest.get("cash")),
        "latest_buying_power": _to_float(latest.get("buying_power")),
        "latest_positions_count": latest.get("positions_count"),
        "approximate_current_exposure": _approximate_exposure(latest),
        "number_of_decisions_today": len(decisions_today),
        "buy_count_today": _decision_count(decisions_today, "BUY"),
        "sell_count_today": _decision_count(decisions_today, "SELL"),
        "hold_count_today": _decision_count(decisions_today, "HOLD"),
        "executed_trade_count_today": len(executions_today),
    }


def _approximate_exposure(snapshot: dict[str, Any]) -> float | None:
    portfolio_value = _to_float(snapshot.get("portfolio_value"))
    cash = _to_float(snapshot.get("cash"))
    if portfolio_value is None or cash is None:
        return None
    return portfolio_value - cash


def _decision_count(decisions: list[dict[str, Any]], action: str) -> int:
    return sum(1 for decision in decisions if str(decision.get("action", "")).upper() == action)


def _is_today(timestamp: str | None, today_prefix: str | None) -> bool:
    if not timestamp or today_prefix is None:
        return False
    return timestamp.startswith(today_prefix)


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
