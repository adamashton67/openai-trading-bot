"""SQLite persistence for trading decisions and future analytics tables."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
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
            quantity INTEGER,
            fill_price REAL,
            status TEXT,
            broker_order_id TEXT,
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
        """
    )


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), sort_keys=True)
        except json.JSONDecodeError:
            return json.dumps({"raw_response": value}, sort_keys=True)

    return json.dumps(value, sort_keys=True)


def _to_int_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
