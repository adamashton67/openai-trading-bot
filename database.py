"""SQLite persistence for trading decisions and future analytics tables."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_database_path: Path | None = None
_database_available = False


def get_database_path() -> Path:
    """Return the configured SQLite path, defaulting to local development."""
    return Path(os.getenv("DATABASE_PATH", "trading_bot.db")).expanduser()


def init_database(path: str | Path | None = None) -> bool:
    """Initialise SQLite tables and mark database writes as available."""
    global _database_available, _database_path

    db_path = Path(path).expanduser() if path is not None else get_database_path()
    _database_path = db_path

    try:
        if db_path.parent != Path("."):
            db_path.parent.mkdir(parents=True, exist_ok=True)

        with _connect(db_path) as connection:
            _create_tables(connection)
    except Exception as exc:
        _database_available = False
        logger.error("Database initialisation failed safely: %s.", exc.__class__.__name__)
        return False

    _database_available = True
    logger.info("Database initialised at %s", db_path)
    return True


def insert_decision(
    decision: dict[str, Any],
    raw_response: Any,
    approved: bool | None = None,
    approval_reason: str | None = None,
    executed: bool | None = None,
    timestamp: datetime | None = None,
) -> int | None:
    """Persist a finalized AI decision without interrupting trading flow."""
    if not _ensure_database_available():
        return None

    try:
        with _connect(_database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO decisions (
                    timestamp,
                    symbol,
                    action,
                    confidence,
                    allocation_percent,
                    reason,
                    approved,
                    approval_reason,
                    executed,
                    raw_response
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (timestamp or datetime.now()).isoformat(),
                    decision.get("symbol"),
                    decision.get("action"),
                    _to_float(decision.get("confidence")),
                    _to_float(decision.get("suggested_allocation_percent")),
                    decision.get("reason"),
                    _to_int_bool(approved),
                    approval_reason,
                    _to_int_bool(executed),
                    _json_text(raw_response),
                ),
            )
            return int(cursor.lastrowid)
    except Exception as exc:
        logger.error("Database decision insert failed safely: %s.", exc.__class__.__name__)
        return None


def insert_execution(
    result: dict[str, Any],
    decision_id: int | None = None,
    timestamp: datetime | None = None,
) -> int | None:
    """Persist an execution result without interrupting the bot."""
    if not _ensure_database_available():
        return None

    try:
        with _connect(_database_path) as connection:
            broker_order_id = _text_or_none(result.get("broker_order_id"))
            if broker_order_id and not result.get("duplicate_prevented"):
                existing = connection.execute(
                    "SELECT id FROM executions WHERE broker_order_id = ? ORDER BY id LIMIT 1",
                    (broker_order_id,),
                ).fetchone()
                if existing:
                    execution_id = int(existing[0])
                    _update_execution_row(connection, execution_id, result)
                    _record_execution_pl(connection, execution_id)
                    existing_date = connection.execute(
                        "SELECT substr(timestamp, 1, 10) FROM executions WHERE id = ?",
                        (execution_id,),
                    ).fetchone()[0]
                    _refresh_daily_order_status_stats(connection, existing_date)
                    return execution_id

            cursor = connection.execute(
                """
                INSERT INTO executions (
                    decision_id,
                    timestamp,
                    symbol,
                    side,
                    quantity,
                    submitted_price,
                    fill_price,
                    filled_quantity,
                    average_fill_price,
                    status,
                    broker_order_id,
                    broker_status,
                    cost_basis_per_share,
                    error_reason,
                    reconciled_at,
                    duplicate_prevented,
                    exit_source,
                    exit_reason,
                    raw_response
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    (timestamp or datetime.now()).isoformat(),
                    str(result.get("symbol") or "").upper() or None,
                    str(result.get("action") or result.get("side") or "").upper() or None,
                    _to_float(result.get("quantity")),
                    _to_float(result.get("submitted_price") or result.get("price")),
                    _to_float(result.get("fill_price") or result.get("average_fill_price")),
                    _to_float(result.get("filled_quantity")),
                    _to_float(result.get("average_fill_price") or result.get("fill_price")),
                    result.get("raw_status")
                    or result.get("broker_status")
                    or result.get("status")
                    or result.get("reason"),
                    broker_order_id,
                    result.get("broker_status") or result.get("raw_status") or result.get("status"),
                    _to_float(result.get("cost_basis_per_share")),
                    _text_or_none(result.get("error_reason")),
                    result.get("reconciled_at"),
                    1 if result.get("duplicate_prevented") else 0,
                    _text_or_none(result.get("exit_source")),
                    _text_or_none(result.get("exit_reason")),
                    _json_text(result),
                ),
            )
            execution_id = int(cursor.lastrowid)
            _record_execution_pl(connection, execution_id)
            _refresh_daily_order_status_stats(
                connection, (timestamp or datetime.now()).date().isoformat()
            )
            return execution_id
    except Exception as exc:
        logger.error("Database execution insert failed safely: %s.", exc.__class__.__name__)
        return None


def insert_market_snapshot(
    symbol: str,
    snapshot: dict[str, Any],
    timestamp: datetime | None = None,
) -> int | None:
    """Persist one market indicator snapshot without interrupting the bot."""
    if not _ensure_database_available():
        return None

    try:
        with _connect(_database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO market_snapshots (
                    timestamp,
                    symbol,
                    price,
                    volume,
                    rsi,
                    ema20,
                    ema50,
                    vwap,
                    raw_snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (timestamp or datetime.now()).isoformat(),
                    symbol.upper(),
                    _to_float(snapshot.get("current_price")),
                    _to_float(snapshot.get("volume")),
                    _to_float(snapshot.get("RSI14")),
                    _to_float(snapshot.get("EMA20")),
                    _to_float(snapshot.get("EMA50")),
                    _to_float(snapshot.get("VWAP")),
                    _json_text(snapshot),
                ),
            )
            return int(cursor.lastrowid)
    except Exception as exc:
        logger.error("Database market snapshot insert failed safely: %s.", exc.__class__.__name__)
        return None


def insert_portfolio_snapshot(
    snapshot: dict[str, Any],
    timestamp: datetime | None = None,
) -> int | None:
    """Persist one portfolio/account snapshot without interrupting the bot."""
    if not _ensure_database_available():
        return None

    account = snapshot.get("account") if isinstance(snapshot, dict) else {}
    positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
    if not isinstance(account, dict):
        account = {}

    try:
        with _connect(_database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO portfolio_snapshots (
                    timestamp,
                    cash,
                    buying_power,
                    equity,
                    portfolio_value,
                    positions_count,
                    raw_snapshot
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (timestamp or datetime.now()).isoformat(),
                    _to_float(account.get("cash")),
                    _to_float(account.get("buying_power")),
                    _to_float(account.get("equity")),
                    _to_float(account.get("portfolio_value")),
                    len(positions) if isinstance(positions, list) else None,
                    _json_text(snapshot),
                ),
            )
            return int(cursor.lastrowid)
    except Exception as exc:
        logger.error("Database portfolio snapshot insert failed safely: %s.", exc.__class__.__name__)
        return None


def insert_watchlist_symbol(
    trading_date: str,
    symbol: str,
    reason_added: str,
    raw_metadata: Any,
) -> int | None:
    """Persist one generated watchlist symbol without interrupting the bot."""
    if not _ensure_database_available():
        return None

    try:
        with _connect(_database_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO watchlists (
                    date,
                    symbol,
                    reason_added,
                    raw_metadata
                )
                VALUES (?, ?, ?, ?)
                """,
                (
                    trading_date,
                    symbol.upper(),
                    reason_added,
                    _json_text(raw_metadata),
                ),
            )
            return int(cursor.lastrowid)
    except Exception as exc:
        logger.error("Database watchlist insert failed safely: %s.", exc.__class__.__name__)
        return None


DAILY_STAT_COUNTERS = {
    "cycle_count",
    "ai_buy_count",
    "ai_sell_count",
    "ai_hold_count",
    "risk_approved_count",
    "risk_rejected_count",
    "orders_submitted",
    "orders_filled",
    "orders_cancelled",
    "order_failures",
    "scanner_runs",
    "scanner_failures",
    "openai_requests",
    "openai_failures",
    "api_errors",
    "realised_pl",
    "runtime_seconds",
}


def increment_daily_stat(
    trading_date: date | str,
    field_name: str,
    amount: int | float = 1,
) -> None:
    """Increment one persisted daily statistic without interrupting the bot."""
    if field_name not in DAILY_STAT_COUNTERS:
        logger.error("Database daily stat update skipped for unsupported field: %s.", field_name)
        return
    if not _ensure_database_available():
        return

    try:
        date_text = _date_text(trading_date)
        with _connect(_database_path) as connection:
            _ensure_daily_stats_row(connection, date_text)
            connection.execute(
                f"""
                UPDATE daily_statistics
                SET {field_name} = COALESCE({field_name}, 0) + ?,
                    updated_at = ?
                WHERE date = ?
                """,
                (amount, datetime.now().isoformat(), date_text),
            )
    except Exception as exc:
        logger.error("Database daily stat update failed safely: %s.", exc.__class__.__name__)


def update_daily_scanner_stats(
    trading_date: date | str,
    market_data: dict[str, Any],
) -> None:
    """Persist scanner rollup fields for the trading report."""
    if not _ensure_database_available():
        return

    try:
        date_text = _date_text(trading_date)
        symbols = market_data.get("symbols", []) if isinstance(market_data, dict) else []
        dynamic_watchlist = market_data.get("dynamic_watchlist", []) if isinstance(market_data, dict) else []
        top_symbols = []
        if isinstance(dynamic_watchlist, list):
            top_symbols = [
                str(candidate.get("symbol", "")).upper()
                for candidate in dynamic_watchlist[:10]
                if isinstance(candidate, dict) and candidate.get("symbol")
            ]

        scanner_failed = bool(market_data.get("broad_scan_failed")) or market_data.get("scanner_status") in {
            "fallback_static",
            "no_candidates",
        }

        with _connect(_database_path) as connection:
            _ensure_daily_stats_row(connection, date_text)
            connection.execute(
                """
                UPDATE daily_statistics
                SET scanner_runs = scanner_runs + ?,
                    scanner_failures = scanner_failures + ?,
                    scanner_mode = ?,
                    symbols_scanned = ?,
                    final_watchlist_size = ?,
                    top_ranked_symbols = ?,
                    updated_at = ?
                WHERE date = ?
                """,
                (
                    1 if market_data.get("dynamic_watchlist_enabled") else 0,
                    1 if scanner_failed else 0,
                    market_data.get("scanner_mode"),
                    len(market_data.get("scanner_universe", []) or symbols)
                    if isinstance(market_data.get("scanner_universe", []) or symbols, list)
                    else None,
                    len(symbols) if isinstance(symbols, list) else None,
                    _json_text(top_symbols),
                    datetime.now().isoformat(),
                    date_text,
                ),
            )
    except Exception as exc:
        logger.error("Database scanner stat update failed safely: %s.", exc.__class__.__name__)


def record_daily_execution_result(
    trading_date: date | str,
    result: dict[str, Any],
) -> None:
    """Update daily execution counters from a broker result."""
    if not _ensure_database_available():
        return

    try:
        date_text = _date_text(trading_date)
        executed = bool(result.get("executed"))
        action = str(result.get("action") or "").upper()
        with _connect(_database_path) as connection:
            _ensure_daily_stats_row(connection, date_text)
            connection.execute(
                """
                UPDATE daily_statistics
                SET orders_submitted = orders_submitted + ?,
                    orders_filled = orders_filled + ?,
                    orders_cancelled = orders_cancelled + ?,
                    order_failures = order_failures + ?,
                    updated_at = ?
                WHERE date = ?
                """,
                (
                    1 if executed else 0,
                    0,
                    0,
                    1 if action in {"BUY", "SELL"} and not executed else 0,
                    datetime.now().isoformat(),
                    date_text,
                ),
            )
    except Exception as exc:
        logger.error("Database execution stat update failed safely: %s.", exc.__class__.__name__)


def load_daily_report_data(trading_date: date | str) -> dict[str, Any]:
    """Load persisted daily statistics and report inputs from SQLite."""
    if not _ensure_database_available():
        return {}

    try:
        date_text = _date_text(trading_date)
        with _connect(_database_path) as connection:
            connection.row_factory = sqlite3.Row
            _ensure_daily_stats_row(connection, date_text)
            stats = dict(
                connection.execute(
                    "SELECT * FROM daily_statistics WHERE date = ?",
                    (date_text,),
                ).fetchone()
            )
            decisions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT timestamp, symbol, action, confidence, allocation_percent,
                           reason, approved, approval_reason, executed
                    FROM decisions
                    WHERE substr(timestamp, 1, 10) = ?
                    ORDER BY timestamp ASC, id ASC
                    """,
                    (date_text,),
                ).fetchall()
            ]
            executions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT timestamp, symbol, side, quantity, submitted_price,
                           fill_price, filled_quantity, average_fill_price,
                           status, broker_status, broker_order_id, realised_pl,
                           cost_basis_per_share, error_reason, reconciliation_status,
                           reconciliation_attempts, reconciliation_error,
                           reconciliation_last_attempt_at, exit_source, exit_reason,
                           raw_response
                    FROM executions
                    WHERE substr(timestamp, 1, 10) = ?
                    ORDER BY timestamp ASC, id ASC
                    """,
                    (date_text,),
                ).fetchall()
            ]
            snapshots = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT timestamp, cash, buying_power, equity, portfolio_value,
                           positions_count, raw_snapshot
                    FROM portfolio_snapshots
                    WHERE substr(timestamp, 1, 10) = ?
                    ORDER BY timestamp ASC, id ASC
                    """,
                    (date_text,),
                ).fetchall()
            ]
            watchlist_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT symbol, reason_added, raw_metadata
                    FROM watchlists
                    WHERE date = ?
                    ORDER BY id DESC
                    LIMIT 10
                    """,
                    (date_text,),
                ).fetchall()
            ]
            managed_positions = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT symbol, opened_at, original_quantity, current_quantity,
                           cost_basis_per_share, partial_profit_taken,
                           partial_profit_filled_quantity, partial_profit_fill_price,
                           trailing_high_price, trailing_stop_price,
                           trailing_stop_activated, status, closed_at, last_checked_at
                    FROM position_management
                    WHERE lower(COALESCE(status, '')) != 'closed'
                    ORDER BY symbol
                    """
                ).fetchall()
            ]
            exit_source_summary = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT COALESCE(exit_source, 'manual/reconciliation') AS exit_source,
                           COUNT(*) AS execution_count,
                           SUM(CASE WHEN lower(COALESCE(broker_status, status, '')) = 'filled'
                                    THEN 1 ELSE 0 END) AS filled_count,
                           COALESCE(SUM(realised_pl), 0) AS realised_pl
                    FROM executions
                    WHERE substr(timestamp, 1, 10) = ?
                      AND upper(COALESCE(side, '')) = 'SELL'
                    GROUP BY COALESCE(exit_source, 'manual/reconciliation')
                    ORDER BY exit_source
                    """,
                    (date_text,),
                ).fetchall()
            ]
        return {
            "stats": stats,
            "decisions": decisions,
            "executions": executions,
            "portfolio_snapshots": snapshots,
            "watchlist_rows": watchlist_rows,
            "position_management": managed_positions,
            "exit_source_summary": exit_source_summary,
        }
    except Exception as exc:
        logger.error("Database daily report load failed safely: %s.", exc.__class__.__name__)
        return {}


def archive_daily_statistics(trading_date: date | str) -> None:
    """Mark a daily statistics row as archived after a successful report send."""
    if not _ensure_database_available():
        return

    try:
        date_text = _date_text(trading_date)
        with _connect(_database_path) as connection:
            _ensure_daily_stats_row(connection, date_text)
            connection.execute(
                """
                UPDATE daily_statistics
                SET archived_at = ?, updated_at = ?
                WHERE date = ?
                """,
                (datetime.now().isoformat(), datetime.now().isoformat(), date_text),
            )
    except Exception as exc:
        logger.error("Database daily statistics archive failed safely: %s.", exc.__class__.__name__)


def load_reconcilable_executions() -> list[dict[str, Any]]:
    """Return broker-backed executions whose final fill state should be refreshed."""
    if not _ensure_database_available():
        return []
    try:
        with _connect(_database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM executions
                WHERE broker_order_id IS NOT NULL
                  AND COALESCE(duplicate_prevented, 0) = 0
                  AND lower(COALESCE(reconciliation_status, 'pending')) NOT IN
                      ('historical_unavailable', 'reconciliation_unavailable', 'reconciled')
                  AND lower(COALESCE(broker_status, status, '')) NOT IN
                      ('filled', 'cancelled', 'canceled', 'rejected', 'expired')
                ORDER BY id
                """
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception as exc:
        logger.error("Database execution reconciliation load failed safely: %s.", exc.__class__.__name__)
        return []


def reconcile_execution(execution_id: int, broker_result: dict[str, Any]) -> bool:
    """Apply cumulative broker fill data and account P/L idempotently."""
    if not _ensure_database_available():
        return False
    try:
        with _connect(_database_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if not exists:
                return False
            payload = dict(broker_result)
            payload["reconciled_at"] = datetime.now().isoformat()
            broker_status = str(
                payload.get("broker_status") or payload.get("status") or ""
            ).lower()
            terminal_statuses = {
                "filled",
                "cancelled",
                "canceled",
                "rejected",
                "expired",
            }
            payload["reconciliation_status"] = (
                "reconciled" if broker_status in terminal_statuses else "pending"
            )
            _update_execution_row(connection, execution_id, payload)
            connection.execute(
                """
                UPDATE executions
                SET reconciliation_attempts = COALESCE(reconciliation_attempts, 0) + 1,
                    reconciliation_error = NULL,
                    reconciliation_last_attempt_at = ?
                WHERE id = ?
                """,
                (payload["reconciled_at"], execution_id),
            )
            _record_execution_pl(connection, execution_id)
            execution_date = connection.execute(
                "SELECT substr(timestamp, 1, 10) FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()[0]
            _refresh_daily_order_status_stats(connection, execution_date)
        return True
    except Exception as exc:
        logger.error("Database execution reconciliation failed safely: %s.", exc.__class__.__name__)
        return False


def record_reconciliation_unavailable(
    execution_id: int,
    reason: str,
    *,
    terminal: bool,
) -> bool:
    """Record a lookup failure separately without altering the original execution result."""
    if not _ensure_database_available():
        return False
    try:
        now = datetime.now().isoformat()
        with _connect(_database_path) as connection:
            row = connection.execute(
                "SELECT reconciliation_attempts FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if row is None:
                return False
            attempts = int(row[0] or 0) + 1
            status = "historical_unavailable" if terminal else "retry_pending"
            if not terminal and attempts >= 3:
                status = "reconciliation_unavailable"
            connection.execute(
                """
                UPDATE executions
                SET reconciliation_status = ?, reconciliation_attempts = ?,
                    reconciliation_error = ?, reconciliation_last_attempt_at = ?
                WHERE id = ?
                """,
                (status, attempts, str(reason)[:500], now, execution_id),
            )
        return True
    except Exception as exc:
        logger.error(
            "Database reconciliation failure update failed safely: %s.",
            exc.__class__.__name__,
        )
        return False


def repair_historical_realised_pl() -> dict[str, int]:
    """Repair SELL P/L only where completed fill and cost-basis evidence exists."""
    counts = {"repaired": 0, "skipped": 0, "insufficient_data": 0}
    if not _ensure_database_available():
        return counts
    try:
        with _connect(_database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM executions WHERE upper(COALESCE(side, '')) = 'SELL' ORDER BY id"
            ).fetchall()
            for row in rows:
                before = row["realised_pl"]
                outcome = _record_execution_pl(connection, int(row["id"]))
                if outcome == "insufficient_data":
                    counts["insufficient_data"] += 1
                elif outcome == "recorded" and before is None:
                    counts["repaired"] += 1
                else:
                    counts["skipped"] += 1
    except Exception as exc:
        logger.error("Historical realised P/L repair failed safely: %s.", exc.__class__.__name__)
    logger.info(
        "Historical realised P/L repair: repaired=%s skipped=%s insufficient_data=%s.",
        counts["repaired"], counts["skipped"], counts["insufficient_data"],
    )
    return counts


POSITION_MANAGEMENT_FIELDS = {
    "opened_at",
    "original_quantity",
    "current_quantity",
    "cost_basis_per_share",
    "partial_profit_target_percent",
    "partial_profit_trigger_price",
    "partial_profit_taken",
    "partial_profit_quantity",
    "partial_profit_order_id",
    "partial_profit_filled_quantity",
    "partial_profit_fill_price",
    "partial_profit_taken_at",
    "trailing_stop_percent",
    "trailing_high_price",
    "trailing_stop_price",
    "trailing_stop_activated",
    "final_exit_order_id",
    "final_exit_filled_quantity",
    "closed_at",
    "status",
    "last_checked_at",
    "raw_metadata",
}


def load_active_position_management(symbol: str | None = None) -> list[dict[str, Any]]:
    """Load active deterministic position-management records."""
    if not _ensure_database_available():
        return []
    try:
        with _connect(_database_path) as connection:
            connection.row_factory = sqlite3.Row
            query = "SELECT * FROM position_management WHERE lower(status) != 'closed'"
            parameters: tuple[Any, ...] = ()
            if symbol:
                query += " AND symbol = ?"
                parameters = (symbol.upper(),)
            query += " ORDER BY id"
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]
    except Exception as exc:
        logger.error("Position-management state load failed safely: %s.", exc.__class__.__name__)
        return []


def create_position_management(record: dict[str, Any]) -> dict[str, Any] | None:
    """Create one active state row, returning the existing row on a race."""
    if not _ensure_database_available():
        return None
    symbol = str(record.get("symbol") or "").upper()
    if not symbol:
        return None
    values = {
        key: (_json_text(value) if key == "raw_metadata" else value)
        for key, value in record.items()
        if key in POSITION_MANAGEMENT_FIELDS
    }
    values.setdefault("opened_at", datetime.now().isoformat())
    values.setdefault("last_checked_at", datetime.now().isoformat())
    values.setdefault("status", "open")
    try:
        with _connect(_database_path) as connection:
            columns = ["symbol", *values.keys()]
            placeholders = ", ".join("?" for _ in columns)
            connection.execute(
                f"INSERT INTO position_management ({', '.join(columns)}) VALUES ({placeholders})",
                [symbol, *values.values()],
            )
        rows = load_active_position_management(symbol)
        return rows[-1] if rows else None
    except sqlite3.IntegrityError:
        rows = load_active_position_management(symbol)
        return rows[-1] if rows else None
    except Exception as exc:
        logger.error("Position-management state insert failed safely: %s.", exc.__class__.__name__)
        return None


def update_position_management(symbol: str, **changes: Any) -> bool:
    """Update the active state row for a symbol without destructive replacement."""
    if not _ensure_database_available():
        return False
    values = {
        key: (_json_text(value) if key == "raw_metadata" else value)
        for key, value in changes.items()
        if key in POSITION_MANAGEMENT_FIELDS
    }
    if not values:
        return True
    try:
        with _connect(_database_path) as connection:
            assignments = ", ".join(f"{key} = ?" for key in values)
            cursor = connection.execute(
                f"UPDATE position_management SET {assignments} "
                "WHERE symbol = ? AND lower(status) != 'closed'",
                [*values.values(), symbol.upper()],
            )
            return cursor.rowcount > 0
    except Exception as exc:
        logger.error("Position-management state update failed safely: %s.", exc.__class__.__name__)
        return False


def close_position_management(symbol: str, *, closed_at: datetime | None = None) -> bool:
    """Close stale state after broker holdings reach zero."""
    timestamp = (closed_at or datetime.now()).isoformat()
    return update_position_management(
        symbol,
        current_quantity=0,
        closed_at=timestamp,
        status="closed",
        last_checked_at=timestamp,
    )


def load_execution_by_order_id(order_id: str | None) -> dict[str, Any] | None:
    """Load a persisted execution used to drive management-order state."""
    if not order_id or not _ensure_database_available():
        return None
    try:
        with _connect(_database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM executions WHERE broker_order_id = ? ORDER BY id DESC LIMIT 1",
                (str(order_id),),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        logger.error("Execution lookup failed safely: %s.", exc.__class__.__name__)
        return None


def infer_original_position_quantity(symbol: str, current_quantity: float) -> tuple[float, dict[str, Any]]:
    """Conservatively recover pre-exit quantity from broker-confirmed SELL fills.

    Legacy rows may predate this bot's execution ledger. In that case the current
    broker quantity is an explicit management baseline, not asserted as the true
    historical original quantity.
    """
    metadata: dict[str, Any] = {"adopted_legacy_position": True}
    if not _ensure_database_available():
        metadata["original_quantity_source"] = "broker_current_conservative_baseline"
        return current_quantity, metadata
    try:
        with _connect(_database_path) as connection:
            latest_buy = connection.execute(
                """
                SELECT MAX(timestamp)
                FROM executions
                WHERE symbol = ? AND upper(COALESCE(side, '')) = 'BUY'
                  AND lower(COALESCE(broker_status, status, '')) = 'filled'
                """,
                (symbol.upper(),),
            ).fetchone()[0]
            if not latest_buy:
                metadata["original_quantity_source"] = "broker_current_conservative_baseline"
                metadata["historical_original_unknown"] = True
                return current_quantity, metadata
            sold = connection.execute(
                """
                SELECT COALESCE(SUM(filled_quantity), 0)
                FROM executions
                WHERE symbol = ? AND upper(COALESCE(side, '')) = 'SELL'
                  AND timestamp >= ?
                  AND lower(COALESCE(broker_status, status, '')) IN
                      ('filled', 'partially_filled', 'partial_fill')
                """,
                (symbol.upper(), latest_buy),
            ).fetchone()[0]
        sold_quantity = float(sold or 0)
        if sold_quantity > 0:
            metadata["original_quantity_source"] = "broker_current_plus_confirmed_sell_fills"
            metadata["recovered_prior_sell_quantity"] = sold_quantity
            return current_quantity + sold_quantity, metadata
    except Exception as exc:
        logger.warning("Legacy quantity inference failed safely for %s: %s.", symbol, exc.__class__.__name__)
    metadata["original_quantity_source"] = "broker_current_conservative_baseline"
    metadata["historical_original_unknown"] = True
    return current_quantity, metadata


def _ensure_database_available() -> bool:
    if _database_available and _database_path is not None:
        return True

    if _database_path is None:
        return init_database()

    return False


def _connect(path: Path | None) -> sqlite3.Connection:
    if path is None:
        raise sqlite3.OperationalError("database path is unavailable")

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            symbol TEXT,
            action TEXT,
            confidence REAL,
            allocation_percent REAL,
            reason TEXT,
            approved INTEGER,
            approval_reason TEXT,
            executed INTEGER,
            raw_response TEXT
        );

        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER,
            timestamp TEXT,
            symbol TEXT,
            side TEXT,
            quantity REAL,
            submitted_price REAL,
            fill_price REAL,
            filled_quantity REAL,
            average_fill_price REAL,
            status TEXT,
            broker_order_id TEXT,
            broker_status TEXT,
            cost_basis_per_share REAL,
            realised_pl REAL,
            error_reason TEXT,
            reconciled_at TEXT,
            reconciliation_status TEXT DEFAULT 'pending',
            reconciliation_attempts INTEGER DEFAULT 0,
            reconciliation_error TEXT,
            reconciliation_last_attempt_at TEXT,
            realised_pl_recorded INTEGER DEFAULT 0,
            accounted_filled_quantity REAL DEFAULT 0,
            duplicate_prevented INTEGER DEFAULT 0,
            raw_response TEXT,
            FOREIGN KEY(decision_id) REFERENCES decisions(id)
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            cash REAL,
            buying_power REAL,
            equity REAL,
            portfolio_value REAL,
            positions_count INTEGER,
            raw_snapshot TEXT
        );

        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            price REAL,
            volume REAL,
            rsi REAL,
            ema20 REAL,
            ema50 REAL,
            vwap REAL,
            raw_snapshot TEXT
        );

        CREATE TABLE IF NOT EXISTS watchlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            symbol TEXT,
            reason_added TEXT,
            raw_metadata TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_statistics (
            date TEXT PRIMARY KEY,
            cycle_count INTEGER DEFAULT 0,
            ai_buy_count INTEGER DEFAULT 0,
            ai_sell_count INTEGER DEFAULT 0,
            ai_hold_count INTEGER DEFAULT 0,
            risk_approved_count INTEGER DEFAULT 0,
            risk_rejected_count INTEGER DEFAULT 0,
            orders_submitted INTEGER DEFAULT 0,
            orders_filled INTEGER DEFAULT 0,
            orders_cancelled INTEGER DEFAULT 0,
            order_failures INTEGER DEFAULT 0,
            scanner_runs INTEGER DEFAULT 0,
            scanner_failures INTEGER DEFAULT 0,
            openai_requests INTEGER DEFAULT 0,
            openai_failures INTEGER DEFAULT 0,
            api_errors INTEGER DEFAULT 0,
            realised_pl REAL DEFAULT 0,
            largest_win REAL,
            largest_loss REAL,
            winning_trades INTEGER DEFAULT 0,
            losing_trades INTEGER DEFAULT 0,
            runtime_seconds REAL DEFAULT 0,
            scanner_mode TEXT,
            symbols_scanned INTEGER,
            final_watchlist_size INTEGER,
            top_ranked_symbols TEXT,
            archived_at TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS position_management (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            original_quantity REAL,
            current_quantity REAL NOT NULL,
            cost_basis_per_share REAL NOT NULL,
            partial_profit_target_percent REAL NOT NULL DEFAULT 3.0,
            partial_profit_trigger_price REAL NOT NULL,
            partial_profit_taken INTEGER NOT NULL DEFAULT 0,
            partial_profit_quantity REAL,
            partial_profit_order_id TEXT,
            partial_profit_filled_quantity REAL DEFAULT 0,
            partial_profit_fill_price REAL,
            partial_profit_taken_at TEXT,
            trailing_stop_percent REAL NOT NULL DEFAULT 2.0,
            trailing_high_price REAL,
            trailing_stop_price REAL,
            trailing_stop_activated INTEGER NOT NULL DEFAULT 0,
            final_exit_order_id TEXT,
            final_exit_filled_quantity REAL DEFAULT 0,
            closed_at TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            last_checked_at TEXT,
            raw_metadata TEXT
        );
        """
    )
    _migrate_schema(connection)


def _migrate_schema(connection: sqlite3.Connection) -> None:
    """Add reporting columns without recreating or discarding Railway SQLite data."""
    execution_columns = {
        "submitted_price": "REAL",
        "filled_quantity": "REAL",
        "average_fill_price": "REAL",
        "broker_status": "TEXT",
        "cost_basis_per_share": "REAL",
        "realised_pl": "REAL",
        "error_reason": "TEXT",
        "reconciled_at": "TEXT",
        "reconciliation_status": "TEXT DEFAULT 'pending'",
        "reconciliation_attempts": "INTEGER DEFAULT 0",
        "reconciliation_error": "TEXT",
        "reconciliation_last_attempt_at": "TEXT",
        "realised_pl_recorded": "INTEGER DEFAULT 0",
        "accounted_filled_quantity": "REAL DEFAULT 0",
        "duplicate_prevented": "INTEGER DEFAULT 0",
        "exit_source": "TEXT",
        "exit_reason": "TEXT",
    }
    daily_columns = {
        "winning_trades": "INTEGER DEFAULT 0",
        "losing_trades": "INTEGER DEFAULT 0",
    }
    position_management_columns = {
        "opened_at": "TEXT",
        "original_quantity": "REAL",
        "current_quantity": "REAL",
        "cost_basis_per_share": "REAL",
        "partial_profit_target_percent": "REAL DEFAULT 3.0",
        "partial_profit_trigger_price": "REAL",
        "partial_profit_taken": "INTEGER DEFAULT 0",
        "partial_profit_quantity": "REAL",
        "partial_profit_order_id": "TEXT",
        "partial_profit_filled_quantity": "REAL DEFAULT 0",
        "partial_profit_fill_price": "REAL",
        "partial_profit_taken_at": "TEXT",
        "trailing_stop_percent": "REAL DEFAULT 2.0",
        "trailing_high_price": "REAL",
        "trailing_stop_price": "REAL",
        "trailing_stop_activated": "INTEGER DEFAULT 0",
        "final_exit_order_id": "TEXT",
        "final_exit_filled_quantity": "REAL DEFAULT 0",
        "closed_at": "TEXT",
        "status": "TEXT DEFAULT 'open'",
        "last_checked_at": "TEXT",
        "raw_metadata": "TEXT",
    }
    _add_missing_columns(connection, "executions", execution_columns)
    _add_missing_columns(connection, "daily_statistics", daily_columns)
    _add_missing_columns(connection, "position_management", position_management_columns)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_broker_order_id ON executions(broker_order_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_executions_exit_source ON executions(exit_source)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_position_management_active_symbol "
        "ON position_management(symbol) WHERE lower(status) != 'closed'"
    )


def _add_missing_columns(
    connection: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    existing = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    for column_name, definition in columns.items():
        if column_name not in existing:
            connection.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
            )


def _update_execution_row(
    connection: sqlite3.Connection,
    execution_id: int,
    result: dict[str, Any],
) -> None:
    status = result.get("broker_status") or result.get("raw_status") or result.get("status")
    average_fill_price = _to_float(
        result.get("average_fill_price") or result.get("filled_avg_price") or result.get("fill_price")
    )
    values = {
        "filled_quantity": _to_float(result.get("filled_quantity") or result.get("filled_qty")),
        "average_fill_price": average_fill_price,
        "fill_price": average_fill_price,
        "broker_status": _text_or_none(status),
        "status": _text_or_none(status),
        "cost_basis_per_share": _to_float(result.get("cost_basis_per_share")),
        "error_reason": _text_or_none(result.get("error_reason")),
        "reconciled_at": result.get("reconciled_at"),
        "reconciliation_status": _text_or_none(result.get("reconciliation_status")),
    }
    assignments = []
    parameters: list[Any] = []
    for column_name, value in values.items():
        if value is not None:
            assignments.append(f"{column_name} = ?")
            parameters.append(value)
    if result:
        current_raw = connection.execute(
            "SELECT raw_response FROM executions WHERE id = ?", (execution_id,)
        ).fetchone()
        merged_result = _json_dict(current_raw[0] if current_raw else None)
        merged_result.update(result)
        assignments.append("raw_response = ?")
        parameters.append(_json_text(merged_result))
    if not assignments:
        return
    parameters.append(execution_id)
    connection.execute(
        f"UPDATE executions SET {', '.join(assignments)} WHERE id = ?",
        parameters,
    )


def _record_execution_pl(connection: sqlite3.Connection, execution_id: int) -> str:
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if (
        row is None
        or str(row["side"] or "").upper() != "SELL"
        or bool(row["duplicate_prevented"])
    ):
        return "skipped"

    status = str(row["broker_status"] or row["status"] or "").lower().replace(" ", "_")
    if status not in {"filled", "partially_filled", "partial_fill"}:
        return "skipped"

    raw = _json_dict(row["raw_response"])
    filled_quantity = _to_float(row["filled_quantity"])
    if filled_quantity is None and status == "filled":
        filled_quantity = _to_float(raw.get("filled_quantity") or raw.get("filled_qty") or row["quantity"])
    fill_price = _to_float(row["average_fill_price"] or row["fill_price"])
    cost_basis = _to_float(row["cost_basis_per_share"] or raw.get("cost_basis_per_share"))
    reported_pl = _first_float(
        raw.get("realised_pl"), raw.get("realized_pl"), raw.get("profit_loss"), raw.get("realized_pnl")
    )

    if filled_quantity is not None and filled_quantity > 0 and fill_price is not None and cost_basis is not None:
        realised_pl = (fill_price - cost_basis) * filled_quantity
    elif reported_pl is not None and filled_quantity is not None and filled_quantity > 0 and fill_price is not None:
        # Compatibility for already broker-confirmed records that explicitly persisted P/L.
        realised_pl = reported_pl
    else:
        return "insufficient_data"

    connection.execute(
        """
        UPDATE executions
        SET filled_quantity = COALESCE(filled_quantity, ?),
            average_fill_price = COALESCE(average_fill_price, ?),
            fill_price = COALESCE(fill_price, ?),
            realised_pl = ?, realised_pl_recorded = 1,
            accounted_filled_quantity = ?
        WHERE id = ?
        """,
        (filled_quantity, fill_price, fill_price, realised_pl, filled_quantity, execution_id),
    )
    _refresh_daily_realised_stats(connection, str(row["timestamp"] or "")[:10])
    return "recorded"


def _refresh_daily_realised_stats(connection: sqlite3.Connection, date_text: str) -> None:
    if not date_text:
        return
    _ensure_daily_stats_row(connection, date_text)
    aggregate = connection.execute(
        """
        SELECT COALESCE(SUM(realised_pl), 0),
               MAX(CASE WHEN realised_pl > 0 THEN realised_pl END),
               MIN(CASE WHEN realised_pl < 0 THEN realised_pl END),
               SUM(CASE WHEN realised_pl > 0 THEN 1 ELSE 0 END),
               SUM(CASE WHEN realised_pl < 0 THEN 1 ELSE 0 END)
        FROM executions
        WHERE substr(timestamp, 1, 10) = ? AND realised_pl_recorded = 1
        """,
        (date_text,),
    ).fetchone()
    connection.execute(
        """
        UPDATE daily_statistics
        SET realised_pl = ?, largest_win = ?, largest_loss = ?,
            winning_trades = ?, losing_trades = ?, updated_at = ?
        WHERE date = ?
        """,
        (*aggregate, datetime.now().isoformat(), date_text),
    )


def _refresh_daily_order_status_stats(connection: sqlite3.Connection, date_text: str) -> None:
    if not date_text:
        return
    _ensure_daily_stats_row(connection, date_text)
    aggregate = connection.execute(
        """
        SELECT SUM(CASE WHEN lower(COALESCE(broker_status, status, '')) IN
                                  ('filled', 'partially_filled', 'partial_fill') THEN 1 ELSE 0 END),
               SUM(CASE WHEN lower(COALESCE(broker_status, status, '')) IN
                                  ('cancelled', 'canceled') THEN 1 ELSE 0 END)
        FROM executions WHERE substr(timestamp, 1, 10) = ?
        """,
        (date_text,),
    ).fetchone()
    connection.execute(
        """
        UPDATE daily_statistics
        SET orders_filled = ?, orders_cancelled = ?, updated_at = ?
        WHERE date = ?
        """,
        (int(aggregate[0] or 0), int(aggregate[1] or 0), datetime.now().isoformat(), date_text),
    )


def _ensure_daily_stats_row(connection: sqlite3.Connection, date_text: str) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO daily_statistics (date, updated_at)
        VALUES (?, ?)
        """,
        (date_text, datetime.now().isoformat()),
    )


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except json.JSONDecodeError:
            return json.dumps({"raw_response": value}, sort_keys=True)

    return json.dumps(value, sort_keys=True)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _first_float(*values: Any) -> float | None:
    for value in values:
        converted = _to_float(value)
        if converted is not None:
            return converted
    return None


def _to_int_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_text(value: date | str) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)
