"""Pre-deployment readiness coverage for safety-critical bot behavior."""

import json
import logging
import sqlite3
import time
from datetime import date, datetime
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

import config
import context_history
import database
import main
from config import Settings, load_settings
from broker import BrokerClient, BrokerSnapshot
from logger_config import install_logging_compatibility_shim
from notifications.discord_notifier import DiscordNotifier
from notifications.notifier import DailySummaryNotifier
from openai_logic import AIDecision, AIDecisionError, OpenAIDecisionClient, TradingContext
from openai_test import build_mock_trading_context
from market_indicators import calculate_market_indicators
from risk_manager import RiskManager
from scheduler import MarketScheduler
from storage import TradingJournal
from strategy import TradingStrategy
from watchlist_scanner import DynamicWatchlistScanner
from watchlist_scanner import is_broad_scan_asset_candidate


def make_settings(**overrides):
    values = {
        "bot_enabled": True,
        "paper_trading": True,
        "dry_run": False,
        "trading_interval_minutes": 15,
        "market_timezone": "America/New_York",
        "openai_api_key": "test-key",
        "openai_model": "test-model",
        "alpaca_api_key": "test-alpaca-key",
        "alpaca_secret_key": "test-alpaca-secret",
        "alpaca_paper_base_url": "https://paper-api.alpaca.markets",
        "max_position_allocation_percent": 5,
        "min_confidence": 0.7,
        "allowed_symbols": ["AAPL", "MSFT"],
        "dynamic_watchlist_enabled": False,
        "broad_market_scan_enabled": False,
        "broad_market_max_symbols": 1000,
        "max_scanner_candidates_after_filters": 1000,
        "broad_scan_batch_size": 100,
        "broad_scan_max_requests_per_cycle": 20,
        "broad_scan_batch_timeout_seconds": 10,
        "min_stock_price": 5,
        "min_average_volume": 500000,
        "exclude_etfs": True,
        "watchlist_size": 20,
        "scanner_universe": [
            "SPY",
            "QQQ",
            "AAPL",
            "MSFT",
            "NVDA",
            "AMZN",
            "GOOGL",
            "META",
            "TSLA",
            "AMD",
            "NFLX",
            "AVGO",
            "INTC",
            "ORCL",
            "CRM",
            "ADBE",
            "PYPL",
            "UBER",
            "SHOP",
            "PLTR",
        ],
        "decision_history_limit": 20,
        "execution_history_limit": 20,
        "portfolio_history_limit": 20,
        "include_history_context": True,
        "prompts_dir": Path("prompts"),
        "data_dir": Path("data"),
        "discord_webhook_url": "",
        "discord_daily_summary_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_logger_accepts_lumibot_color_kwarg(caplog):
    install_logging_compatibility_shim()
    logger = logging.getLogger("lumibot.test")

    with caplog.at_level(logging.ERROR, logger="lumibot.test"):
        logger.error("partial fill update failed", color="red")

    assert "partial fill update failed" in caplog.text


def test_normal_logging_still_works(caplog):
    install_logging_compatibility_shim()
    logger = logging.getLogger("bot.test")

    with caplog.at_level(logging.INFO, logger="bot.test"):
        logger.info("normal log %s", "works")

    assert "normal log works" in caplog.text


def test_config_loading_uses_safe_defaults(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda dotenv_path=None: False)
    for key in [
        "BOT_ENABLED",
        "PAPER_TRADING",
        "DRY_RUN",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "DISCORD_WEBHOOK_URL",
        "DISCORD_DAILY_SUMMARY_ENABLED",
        "DYNAMIC_WATCHLIST_ENABLED",
        "BROAD_MARKET_SCAN_ENABLED",
        "BROAD_MARKET_MAX_SYMBOLS",
        "MAX_SCANNER_CANDIDATES_AFTER_FILTERS",
        "BROAD_SCAN_BATCH_SIZE",
        "BROAD_SCAN_MAX_REQUESTS_PER_CYCLE",
        "BROAD_SCAN_BATCH_TIMEOUT_SECONDS",
        "MIN_STOCK_PRICE",
        "MIN_AVERAGE_VOLUME",
        "EXCLUDE_ETFS",
        "WATCHLIST_SIZE",
        "SCANNER_UNIVERSE",
        "DECISION_HISTORY_LIMIT",
        "EXECUTION_HISTORY_LIMIT",
        "PORTFOLIO_HISTORY_LIMIT",
        "INCLUDE_HISTORY_CONTEXT",
    ]:
        monkeypatch.delenv(key, raising=False)

    settings = load_settings()

    assert settings.bot_enabled is False
    assert settings.paper_trading is True
    assert settings.dry_run is True
    assert settings.discord_daily_summary_enabled is False
    assert settings.openai_model == "gpt-5-mini"
    assert settings.dynamic_watchlist_enabled is False
    assert settings.broad_market_scan_enabled is False
    assert settings.broad_market_max_symbols == 1000
    assert settings.max_scanner_candidates_after_filters == 1000
    assert settings.broad_scan_batch_size == 100
    assert settings.broad_scan_max_requests_per_cycle == 20
    assert settings.broad_scan_batch_timeout_seconds == 10
    assert settings.min_stock_price == 5
    assert settings.min_average_volume == 500000
    assert settings.exclude_etfs is True
    assert settings.watchlist_size == 20
    assert "AAPL" in settings.scanner_universe
    assert settings.include_history_context is True
    assert settings.decision_history_limit == 20


def test_config_loading_reads_environment(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", lambda dotenv_path=None: False)
    monkeypatch.setenv("BOT_ENABLED", "true")
    monkeypatch.setenv("PAPER_TRADING", "true")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DISCORD_DAILY_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("ALLOWED_SYMBOLS", "aapl, msft")
    monkeypatch.setenv("DYNAMIC_WATCHLIST_ENABLED", "true")
    monkeypatch.setenv("BROAD_MARKET_SCAN_ENABLED", "true")
    monkeypatch.setenv("BROAD_MARKET_MAX_SYMBOLS", "50")
    monkeypatch.setenv("MAX_SCANNER_CANDIDATES_AFTER_FILTERS", "25")
    monkeypatch.setenv("BROAD_SCAN_BATCH_SIZE", "12")
    monkeypatch.setenv("BROAD_SCAN_MAX_REQUESTS_PER_CYCLE", "3")
    monkeypatch.setenv("BROAD_SCAN_BATCH_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("MIN_STOCK_PRICE", "10")
    monkeypatch.setenv("MIN_AVERAGE_VOLUME", "750000")
    monkeypatch.setenv("EXCLUDE_ETFS", "false")
    monkeypatch.setenv("WATCHLIST_SIZE", "3")
    monkeypatch.setenv("SCANNER_UNIVERSE", "spy, aapl")
    monkeypatch.setenv("DECISION_HISTORY_LIMIT", "7")
    monkeypatch.setenv("EXECUTION_HISTORY_LIMIT", "8")
    monkeypatch.setenv("PORTFOLIO_HISTORY_LIMIT", "9")
    monkeypatch.setenv("INCLUDE_HISTORY_CONTEXT", "false")

    settings = load_settings()

    assert settings.bot_enabled is True
    assert settings.paper_trading is True
    assert settings.dry_run is False
    assert settings.discord_daily_summary_enabled is True
    assert settings.allowed_symbols == ["AAPL", "MSFT"]
    assert settings.dynamic_watchlist_enabled is True
    assert settings.broad_market_scan_enabled is True
    assert settings.broad_market_max_symbols == 50
    assert settings.max_scanner_candidates_after_filters == 25
    assert settings.broad_scan_batch_size == 12
    assert settings.broad_scan_max_requests_per_cycle == 3
    assert settings.broad_scan_batch_timeout_seconds == 1.5
    assert settings.min_stock_price == 10
    assert settings.min_average_volume == 750000
    assert settings.exclude_etfs is False
    assert settings.watchlist_size == 3
    assert settings.scanner_universe == ["SPY", "AAPL"]
    assert settings.decision_history_limit == 7
    assert settings.execution_history_limit == 8
    assert settings.portfolio_history_limit == 9
    assert settings.include_history_context is False


def test_database_path_defaults_to_local_file(monkeypatch):
    monkeypatch.delenv("DATABASE_PATH", raising=False)

    assert database.get_database_path() == Path("trading_bot.db")


def test_database_path_override_works(monkeypatch, tmp_path):
    database_path = tmp_path / "trading_bot.db"
    monkeypatch.setenv("DATABASE_PATH", str(database_path))

    assert database.get_database_path() == database_path


def test_database_file_is_created(tmp_path):
    database_path = tmp_path / "nested" / "trading_bot.db"

    assert database.init_database(database_path) is True
    assert database_path.exists()


def test_database_initialises_all_tables(tmp_path):
    database_path = tmp_path / "trading_bot.db"

    assert database.init_database(database_path) is True

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    assert {
        "decisions",
        "executions",
        "portfolio_snapshots",
        "market_snapshots",
        "watchlists",
    }.issubset(table_names)


def test_database_decision_insert_succeeds(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)

    decision_id = database.insert_decision(
        decision={
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.8,
            "suggested_allocation_percent": 5,
            "reason": "Test decision.",
        },
        raw_response={"symbol": "AAPL", "action": "BUY"},
        approved=True,
        approval_reason="Approved.",
        executed=False,
        timestamp=datetime(2026, 7, 6, 10, 0),
    )

    assert decision_id == 1
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT symbol, action, confidence, allocation_percent, approved, executed "
            "FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()

    assert row == ("AAPL", "BUY", 0.8, 5.0, 1, 0)


def test_database_raw_response_is_stored_as_json_text(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)

    decision_id = database.insert_decision(
        decision={
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.3,
            "suggested_allocation_percent": 0,
            "reason": "No valid setup.",
        },
        raw_response='{"action":"HOLD","symbol":"SPY"}',
    )

    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            "SELECT raw_response FROM decisions WHERE id = ?",
            (decision_id,),
        ).fetchone()[0]

    assert json.loads(stored) == {"action": "HOLD", "symbol": "SPY"}


def test_database_market_snapshot_insert_succeeds(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)

    snapshot_id = database.insert_market_snapshot(
        symbol="AAPL",
        snapshot={
            "symbol": "AAPL",
            "current_price": 123.45,
            "volume": 100000,
            "RSI14": 55.2,
            "EMA20": 120.1,
            "EMA50": 118.4,
            "VWAP": 121.8,
            "5m_change_percent": 0.25,
            "relative_volume": 1.4,
        },
        timestamp=datetime(2026, 7, 6, 10, 0),
    )

    assert snapshot_id == 1
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT symbol, price, volume, rsi, ema20, ema50, vwap, raw_snapshot "
            "FROM market_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

    assert row[:7] == ("AAPL", 123.45, 100000.0, 55.2, 120.1, 118.4, 121.8)
    raw_snapshot = json.loads(row[7])
    assert raw_snapshot["current_price"] == 123.45
    assert raw_snapshot["5m_change_percent"] == 0.25
    assert raw_snapshot["relative_volume"] == 1.4


def test_database_portfolio_snapshot_insert_succeeds(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)

    snapshot_id = database.insert_portfolio_snapshot(
        snapshot={
            "account": {
                "cash": 25000,
                "buying_power": 30000,
                "equity": 100500,
                "portfolio_value": 100500,
            },
            "positions": [
                {"symbol": "AAPL", "quantity": 2},
                {"symbol": "MSFT", "quantity": 1},
            ],
            "market_data": {"prices": {"AAPL": {"last_price": 214.33}}},
        },
        timestamp=datetime(2026, 7, 6, 10, 0),
    )

    assert snapshot_id == 1
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT cash, buying_power, equity, portfolio_value, positions_count, raw_snapshot "
            "FROM portfolio_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

    assert row[:5] == (25000.0, 30000.0, 100500.0, 100500.0, 2)
    raw_snapshot = json.loads(row[5])
    assert raw_snapshot["account"]["cash"] == 25000
    assert raw_snapshot["positions"][0]["symbol"] == "AAPL"
    assert raw_snapshot["market_data"]["prices"]["AAPL"]["last_price"] == 214.33


def test_database_portfolio_snapshot_missing_fields_are_null(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)

    snapshot_id = database.insert_portfolio_snapshot(
        snapshot={
            "account": {"cash": None},
            "positions": None,
        },
        timestamp=datetime(2026, 7, 6, 10, 0),
    )

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT cash, buying_power, equity, portfolio_value, positions_count, raw_snapshot "
            "FROM portfolio_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()

    assert row[:5] == (None, None, None, None, None)
    assert json.loads(row[5])["account"]["cash"] is None


def test_database_watchlist_insert_succeeds(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)

    watchlist_id = database.insert_watchlist_symbol(
        trading_date="2026-07-06",
        symbol="aapl",
        reason_added="top volume, high relative volume",
        raw_metadata={"symbol": "AAPL", "score": 5, "reasons_added": ["top volume"]},
    )

    assert watchlist_id == 1
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT date, symbol, reason_added, raw_metadata FROM watchlists WHERE id = ?",
            (watchlist_id,),
        ).fetchone()

    assert row[:3] == ("2026-07-06", "AAPL", "top volume, high relative volume")
    assert json.loads(row[3])["score"] == 5


def test_history_context_loads_recent_decisions_in_descending_order(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    database.insert_decision(
        decision={"symbol": "AAPL", "action": "BUY", "confidence": 0.8, "suggested_allocation_percent": 1, "reason": "Older."},
        raw_response={"symbol": "AAPL"},
        approved=True,
        approval_reason="Approved.",
        executed=False,
        timestamp=datetime(2026, 7, 6, 10, 0),
    )
    database.insert_decision(
        decision={"symbol": "MSFT", "action": "HOLD", "confidence": 0.4, "suggested_allocation_percent": 0, "reason": "Newer."},
        raw_response={"symbol": "MSFT"},
        approved=False,
        approval_reason="Hold.",
        executed=False,
        timestamp=datetime(2026, 7, 6, 10, 15),
    )

    history = context_history.load_history_context(
        decision_limit=20,
        execution_limit=20,
        portfolio_limit=20,
        today=date(2026, 7, 6),
    )

    assert [decision["symbol"] for decision in history["recent_ai_decisions"]] == ["MSFT", "AAPL"]
    assert history["recent_ai_decisions"][0]["approved"] is False
    assert history["recent_ai_decisions"][1]["executed"] is False


def test_history_context_loads_recent_executions(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO executions (
                timestamp, symbol, side, quantity, fill_price, status, broker_order_id, raw_response
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("2026-07-06T10:30:00", "AAPL", "buy", 5, 214.33, "filled", "order-1", "{}"),
        )

    history = context_history.load_history_context(
        decision_limit=20,
        execution_limit=20,
        portfolio_limit=20,
        today=date(2026, 7, 6),
    )

    assert history["recent_executions"] == [
        {
            "timestamp": "2026-07-06T10:30:00",
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 5,
            "fill_price": 214.33,
            "status": "filled",
            "broker_order_id": "order-1",
        }
    ]


def test_history_portfolio_summary_calculates_latest_vs_previous(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    database.insert_portfolio_snapshot(
        snapshot={"account": {"cash": 25000, "buying_power": 25000, "portfolio_value": 100000}, "positions": []},
        timestamp=datetime(2026, 7, 6, 10, 0),
    )
    database.insert_portfolio_snapshot(
        snapshot={"account": {"cash": 26000, "buying_power": 26000, "portfolio_value": 101000}, "positions": [{"symbol": "AAPL"}]},
        timestamp=datetime(2026, 7, 6, 10, 15),
    )

    history = context_history.load_history_context(
        decision_limit=20,
        execution_limit=20,
        portfolio_limit=20,
        today=date(2026, 7, 6),
    )
    summary = history["portfolio_performance_summary"]

    assert summary["latest_portfolio_value"] == 101000
    assert summary["previous_portfolio_value"] == 100000
    assert summary["portfolio_change"] == 1000
    assert summary["portfolio_change_percent"] == 1
    assert summary["latest_cash"] == 26000
    assert summary["latest_buying_power"] == 26000
    assert summary["latest_positions_count"] == 1
    assert summary["approximate_current_exposure"] == 75000


def test_history_counts_buy_sell_hold_for_today(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    for action, timestamp in [
        ("BUY", datetime(2026, 7, 6, 10, 0)),
        ("SELL", datetime(2026, 7, 6, 10, 15)),
        ("HOLD", datetime(2026, 7, 6, 10, 30)),
        ("BUY", datetime(2026, 7, 5, 10, 0)),
    ]:
        database.insert_decision(
            decision={"symbol": "AAPL", "action": action, "confidence": 0.8, "suggested_allocation_percent": 1, "reason": action},
            raw_response={"action": action},
            timestamp=timestamp,
        )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO executions (timestamp, symbol, side, quantity, status) VALUES (?, ?, ?, ?, ?)",
            ("2026-07-06T11:00:00", "AAPL", "buy", 1, "filled"),
        )

    history = context_history.load_history_context(
        decision_limit=20,
        execution_limit=20,
        portfolio_limit=20,
        today=date(2026, 7, 6),
    )
    summary = history["portfolio_performance_summary"]

    assert summary["number_of_decisions_today"] == 3
    assert summary["buy_count_today"] == 1
    assert summary["sell_count_today"] == 1
    assert summary["hold_count_today"] == 1
    assert summary["executed_trade_count_today"] == 1


def test_history_context_failure_returns_empty(monkeypatch, tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    def failing_connect(path):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(database, "_connect", failing_connect)

    history = context_history.load_history_context(
        decision_limit=20,
        execution_limit=20,
        portfolio_limit=20,
        today=date(2026, 7, 6),
    )

    assert history == context_history.empty_history_context()


def test_database_initialisation_failure_does_not_crash(tmp_path):
    blocked_parent = tmp_path / "not_a_directory"
    blocked_parent.write_text("blocked", encoding="utf-8")

    assert database.init_database(blocked_parent / "trading_bot.db") is False


def test_database_decision_insert_failure_does_not_crash_trading_flow(monkeypatch, tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    def failing_connect(path):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(database, "_connect", failing_connect)

    strategy = TradingStrategy(
        settings=make_settings(dry_run=True),
        broker=types.SimpleNamespace(
            collect_snapshot=lambda: BrokerSnapshot(
                account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
                positions=[],
                market_data={"prices": {"AAPL": {"last_price": 100}}},
            )
        ),
        risk_manager=RiskManager(make_settings(dry_run=True)),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"AAPL","action":"BUY"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "AAPL",
                "action": "BUY",
                "confidence": 0.9,
                "suggested_allocation_percent": 1,
                "reason": "Test decision.",
            }
        ),
    )

    strategy.run_cycle()


def test_prompt_files_are_loaded_and_rendered_at_runtime(tmp_path):
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "system_prompt.md").write_text("System prompt", encoding="utf-8")
    (prompts_dir / "user_prompt_template.md").write_text(
        "Now: {{current_datetime}}\nWatchlist: {{watchlist}}",
        encoding="utf-8",
    )

    client = object.__new__(OpenAIDecisionClient)
    client.prompts_dir = prompts_dir
    context = TradingContext(
        current_datetime=datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        market_status="open",
        watchlist_symbols=["aapl"],
    )

    assert client._load_prompt("system_prompt.md") == "System prompt"
    rendered = client._render_user_prompt(context)
    assert "2026-07-06" in rendered
    assert "AAPL" in rendered


def test_user_prompt_includes_structured_market_intelligence():
    client = object.__new__(OpenAIDecisionClient)
    client.prompts_dir = Path("prompts")
    context = TradingContext(
        current_datetime=datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        market_status="open",
        current_positions=[{"symbol": "AAPL", "quantity": 3}],
        watchlist_symbols=["AAPL", "MSFT"],
        recent_price_data={
            "dynamic_watchlist": [
                {
                    "symbol": "AAPL",
                    "score": 7,
                    "reasons_added": ["top gainer", "high relative volume"],
                    "current_price": 214.33,
                    "day_change_percent": 1.4,
                    "volume": 123456,
                    "relative_volume": 1.23,
                    "volatility_metric": 2.2,
                }
            ],
            "market_intelligence": {
                "AAPL": {
                    "current_price": 214.33,
                    "5m_change_percent": None,
                    "15m_change_percent": 0.4,
                    "1h_change_percent": 1.2,
                    "day_change_percent": 0.8,
                    "5d_change_percent": 2.1,
                    "20d_change_percent": 4.3,
                    "volume": 123456,
                    "average_20d_volume": 100000,
                    "relative_volume": 1.23,
                    "EMA20": 210.5,
                    "EMA50": 205.2,
                    "RSI14": 58.4,
                    "VWAP": 212.1,
                }
            }
        },
        history_context={
            "recent_ai_decisions": [
                {
                    "timestamp": "2026-07-06T10:00:00",
                    "symbol": "AAPL",
                    "action": "HOLD",
                    "confidence": 0.3,
                    "reason": "Unclear conditions.",
                }
            ],
            "recent_executions": [],
            "portfolio_performance_summary": {
                "latest_portfolio_value": 100000,
                "portfolio_change": 0,
                "buy_count_today": 0,
                "hold_count_today": 3,
            },
        },
    )

    rendered = client._render_user_prompt(context)

    assert "Market Intelligence:" in rendered
    assert "Dynamic Watchlist:" in rendered
    assert "AAPL:" in rendered
    assert "MSFT:" in rendered
    assert '"score": 7' in rendered
    assert "high relative volume" in rendered
    assert '"current_position": {' in rendered
    assert '"quantity": 3' in rendered
    assert '"current_price": 214.33' in rendered
    assert '"5m_change_percent": null' in rendered
    assert '"RSI14": 58.4' in rendered
    assert "Historical Context:" in rendered
    assert '"recent_ai_decisions": [' in rendered
    assert '"hold_count_today": 3' in rendered


def test_user_prompt_instructs_null_indicators_are_unavailable():
    template = Path("prompts/user_prompt_template.md").read_text(encoding="utf-8")

    assert "Treat null as unavailable" in template
    assert "Use only these provided indicator values" in template
    assert "multiple supplied indicators support the decision" in template
    assert "Consider current portfolio exposure" in template
    assert "Dynamic Watchlist" in template
    assert "Do not recommend symbols outside this final watchlist" in template
    assert "Historical Context" in template
    assert "Do not repeat a previous BUY solely" in template
    assert "Use Historical Context as context only" in template


def test_empty_history_renders_cleanly():
    client = object.__new__(OpenAIDecisionClient)
    client.prompts_dir = Path("prompts")
    context = TradingContext(
        current_datetime=datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York")),
        market_status="open",
        watchlist_symbols=["AAPL"],
        history_context=context_history.empty_history_context(),
    )

    rendered = client._render_user_prompt(context)

    assert "Historical Context:" in rendered
    assert '"recent_ai_decisions": []' in rendered
    assert '"recent_executions": []' in rendered


def test_ai_response_validation_accepts_supported_decision():
    decision = AIDecision.model_validate(
        {
            "symbol": "aapl",
            "action": "buy",
            "confidence": 0.72,
            "suggested_allocation_percent": 5,
            "reason": "Test reason.",
        }
    )

    assert decision.symbol == "AAPL"
    assert decision.action == "BUY"


def test_hold_with_null_exit_percentages_validates():
    decision = AIDecision.model_validate(
        {
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.42,
            "suggested_allocation_percent": 0,
            "reason": "Insufficient data.",
            "stop_loss_percent": None,
            "take_profit_percent": None,
        }
    )

    assert decision.action == "HOLD"
    assert decision.stop_loss_percent is None
    assert decision.take_profit_percent is None


def test_hold_confidence_must_be_numeric():
    decision = AIDecision.model_validate(
        {
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.3,
            "suggested_allocation_percent": 0,
            "reason": "Insufficient data.",
            "stop_loss_percent": None,
            "take_profit_percent": None,
        }
    )

    assert decision.action == "HOLD"
    assert decision.confidence == 0.3


def test_null_confidence_is_rejected_safely():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"SPY","action":"HOLD","confidence":null,'
            '"suggested_allocation_percent":0,"reason":"Insufficient data.",'
            '"stop_loss_percent":null,"take_profit_percent":null}'
        )


def test_missing_reason_is_rejected_safely():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"SPY","action":"HOLD","confidence":0.3,'
            '"suggested_allocation_percent":0,'
            '"stop_loss_percent":null,"take_profit_percent":null}'
        )


def test_cash_symbol_is_rejected_safely():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"CASH","action":"HOLD","confidence":0.3,'
            '"suggested_allocation_percent":0,"reason":"Insufficient data.",'
            '"stop_loss_percent":null,"take_profit_percent":null}'
        )


def test_valid_hold_uses_watchlist_symbol_with_reason():
    decision = AIDecision.model_validate(
        {
            "symbol": "SPY",
            "action": "HOLD",
            "confidence": 0.3,
            "suggested_allocation_percent": 0,
            "reason": "Insufficient signal strength across the watchlist.",
            "stop_loss_percent": None,
            "take_profit_percent": None,
        }
    )

    assert decision.symbol == "SPY"
    assert decision.action == "HOLD"
    assert decision.reason


def test_hold_with_zero_exit_percentages_is_normalized_to_null():
    client = object.__new__(OpenAIDecisionClient)

    decision = client._parse_decision(
        '{"symbol":"SPY","action":"HOLD","confidence":0.42,'
        '"suggested_allocation_percent":0,"reason":"Insufficient data.",'
        '"stop_loss_percent":0,"take_profit_percent":0}'
    )

    assert decision.action == "HOLD"
    assert decision.stop_loss_percent is None
    assert decision.take_profit_percent is None


def test_buy_with_positive_exit_percentages_validates():
    decision = AIDecision.model_validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.81,
            "suggested_allocation_percent": 5,
            "reason": "Test setup.",
            "stop_loss_percent": 3,
            "take_profit_percent": 6,
        }
    )

    assert decision.action == "BUY"
    assert decision.stop_loss_percent == 3
    assert decision.take_profit_percent == 6


def test_invalid_ai_json_is_rejected():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision("not json")


def test_invalid_ai_response_schema_is_rejected():
    client = object.__new__(OpenAIDecisionClient)

    with pytest.raises(AIDecisionError):
        client._parse_decision(
            '{"symbol":"AAPL","action":"WAIT","confidence":1.2,'
            '"suggested_allocation_percent":5,"reason":"Invalid."}'
        )


def test_gpt_5_family_request_omits_temperature():
    client = object.__new__(OpenAIDecisionClient)
    client.model = "gpt-5-mini"

    request = client._build_chat_completion_request("system", "user")

    assert request["model"] == "gpt-5-mini"
    assert "temperature" not in request
    assert request["response_format"] == {"type": "json_object"}


def test_gpt_4o_family_request_includes_temperature():
    client = object.__new__(OpenAIDecisionClient)
    client.model = "gpt-4o-mini"

    request = client._build_chat_completion_request("system", "user")

    assert request["model"] == "gpt-4o-mini"
    assert request["temperature"] == 0.2
    assert request["response_format"] == {"type": "json_object"}


def test_test_openai_route_exists():
    parser_args = ["main.py", "--test-openai"]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("sys.argv", parser_args)
        args = main.parse_args()

    assert args.test_openai is True


def test_test_execution_route_exists():
    parser_args = ["main.py", "--test-execution", "--dry-run"]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("sys.argv", parser_args)
        args = main.parse_args()

    assert args.test_execution is True
    assert args.dry_run is True


def test_single_cycle_route_exists():
    parser_args = ["main.py", "--single-cycle"]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("sys.argv", parser_args)
        args = main.parse_args()

    assert args.single_cycle is True


def test_test_scanner_route_and_cap_exist():
    parser_args = ["main.py", "--test-scanner", "--scanner-max-symbols", "100"]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("sys.argv", parser_args)
        args = main.parse_args()

    assert args.test_scanner is True
    assert args.scanner_max_symbols == 100


def test_mock_openai_context_generation_uses_safe_fake_data():
    context = build_mock_trading_context(
        make_settings(allowed_symbols=["AAPL", "MSFT", "SPY"])
    )

    assert context.market_status == "TEST_MODE_MARKET_OPEN"
    assert context.portfolio_value == 100000
    assert context.account_cash == 25000
    assert context.buying_power == 25000
    assert context.watchlist_symbols == ["AAPL", "MSFT", "SPY"]
    assert context.risk_rules["dry_run_enabled"] is True
    assert context.current_positions[0]["symbol"] == "MSFT"
    assert "AAPL" in context.recent_price_data


def test_test_openai_command_exits_without_broker_execution(monkeypatch, tmp_path):
    called = {"openai_test": False}

    fake_openai_test = types.ModuleType("openai_test")

    def fake_run(settings):
        called["openai_test"] = True
        return 0

    fake_openai_test.run_openai_integration_test = fake_run
    monkeypatch.setitem(sys.modules, "openai_test", fake_openai_test)
    monkeypatch.setitem(sys.modules, "broker", None)
    monkeypatch.setattr(sys, "argv", ["main.py", "--test-openai"])
    monkeypatch.setattr(config, "load_dotenv", lambda dotenv_path=None: False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "trading_bot.db"))

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0
    assert called["openai_test"] is True


def test_single_cycle_command_runs_one_cycle(monkeypatch, tmp_path):
    calls = {
        "broker_connected": 0,
        "strategy_cycles": 0,
        "risk_manager_created": 0,
        "scheduler_created": 0,
    }

    fake_broker_module = types.ModuleType("broker")

    class FakeBrokerClient:
        def __init__(self, settings):
            self.settings = settings

        def connect(self):
            calls["broker_connected"] += 1

    fake_broker_module.BrokerClient = FakeBrokerClient
    fake_risk_module = types.ModuleType("risk_manager")

    class FakeRiskManager:
        def __init__(self, settings):
            calls["risk_manager_created"] += 1

    fake_risk_module.RiskManager = FakeRiskManager
    fake_scheduler_module = types.ModuleType("scheduler")

    class FakeMarketScheduler:
        def __init__(self, settings):
            calls["scheduler_created"] += 1

    fake_scheduler_module.MarketScheduler = FakeMarketScheduler
    fake_strategy_module = types.ModuleType("strategy")

    class FakeTradingStrategy:
        def __init__(self, settings, broker, risk_manager, journal=None):
            pass

        def run_cycle(self):
            calls["strategy_cycles"] += 1

    fake_strategy_module.TradingStrategy = FakeTradingStrategy
    settings = make_settings(data_dir=tmp_path, dry_run=True)

    monkeypatch.setitem(sys.modules, "broker", fake_broker_module)
    monkeypatch.setitem(sys.modules, "risk_manager", fake_risk_module)
    monkeypatch.setitem(sys.modules, "scheduler", fake_scheduler_module)
    monkeypatch.setitem(sys.modules, "strategy", fake_strategy_module)
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "init_database", lambda: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "--single-cycle"])

    main.main()

    assert calls["broker_connected"] == 1
    assert calls["risk_manager_created"] == 1
    assert calls["scheduler_created"] == 1
    assert calls["strategy_cycles"] == 1


def test_test_scanner_command_does_not_construct_risk_or_strategy(monkeypatch, tmp_path):
    calls = {
        "broker_connected": 0,
        "scanner_ran": 0,
        "risk_manager_created": 0,
        "strategy_created": 0,
    }

    fake_broker_module = types.ModuleType("broker")

    class FakeBrokerClient:
        def __init__(self, settings):
            self.settings = settings

        def connect(self):
            calls["broker_connected"] += 1

    fake_broker_module.BrokerClient = FakeBrokerClient
    fake_scanner_module = types.ModuleType("scanner_test")

    def fake_run_scanner_test(settings, broker):
        calls["scanner_ran"] += 1
        assert settings.dynamic_watchlist_enabled is True
        assert settings.max_scanner_candidates_after_filters == 100
        assert settings.broad_market_max_symbols == 100
        return 0

    fake_scanner_module.run_scanner_test = fake_run_scanner_test
    fake_risk_module = types.ModuleType("risk_manager")

    class FakeRiskManager:
        def __init__(self, settings):
            calls["risk_manager_created"] += 1

    fake_risk_module.RiskManager = FakeRiskManager
    fake_strategy_module = types.ModuleType("strategy")

    class FakeTradingStrategy:
        def __init__(self, *args, **kwargs):
            calls["strategy_created"] += 1

    fake_strategy_module.TradingStrategy = FakeTradingStrategy

    settings = make_settings(
        data_dir=tmp_path,
        dynamic_watchlist_enabled=False,
        broad_market_max_symbols=1000,
        max_scanner_candidates_after_filters=1000,
    )

    monkeypatch.setitem(sys.modules, "broker", fake_broker_module)
    monkeypatch.setitem(sys.modules, "scanner_test", fake_scanner_module)
    monkeypatch.setitem(sys.modules, "risk_manager", fake_risk_module)
    monkeypatch.setitem(sys.modules, "strategy", fake_strategy_module)
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(main, "init_database", lambda: None)
    monkeypatch.setattr(sys, "argv", ["main.py", "--test-scanner", "--scanner-max-symbols", "100"])

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == 0
    assert calls["broker_connected"] == 1
    assert calls["scanner_ran"] == 1
    assert calls["risk_manager_created"] == 0
    assert calls["strategy_created"] == 0


def test_risk_manager_rejects_when_bot_disabled():
    manager = RiskManager(make_settings(bot_enabled=False))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "BOT_ENABLED" in reason


def test_risk_manager_rejects_dry_run_mode():
    manager = RiskManager(make_settings(dry_run=True))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "DRY_RUN" in reason


def test_risk_manager_rejects_when_paper_trading_disabled():
    manager = RiskManager(make_settings(paper_trading=False))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "Live trading is disabled" in reason


def test_risk_manager_rejects_unsupported_symbols():
    manager = RiskManager(make_settings(allowed_symbols=["AAPL"]))

    approved, reason = manager.validate(
        {
            "symbol": "TSLA",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
        }
    )

    assert approved is False
    assert "ALLOWED_SYMBOLS" in reason


def test_static_mode_rejects_symbols_outside_allowed_symbols():
    manager = RiskManager(make_settings(dynamic_watchlist_enabled=False, allowed_symbols=["AAPL"]))

    approved, reason = manager.validate(
        {
            "symbol": "PLTR",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
            "cycle_allowed_symbols": ["PLTR"],
        }
    )

    assert approved is False
    assert "ALLOWED_SYMBOLS" in reason


def test_dynamic_mode_allows_symbols_in_final_watchlist():
    manager = RiskManager(
        make_settings(
            dynamic_watchlist_enabled=True,
            allowed_symbols=["AAPL"],
        )
    )

    approved, reason = manager.validate(
        {
            "symbol": "PLTR",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
            "cycle_allowed_symbols": ["PLTR", "AAPL"],
        }
    )

    assert approved is True
    assert "Approved" in reason


def test_dynamic_mode_rejects_symbols_outside_final_watchlist():
    manager = RiskManager(
        make_settings(
            dynamic_watchlist_enabled=True,
            allowed_symbols=["AAPL", "PLTR"],
        )
    )

    approved, reason = manager.validate(
        {
            "symbol": "MSFT",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 1,
            "cycle_allowed_symbols": ["PLTR"],
        }
    )

    assert approved is False
    assert "final watchlist" in reason


def test_risk_manager_rejects_allocation_above_max_limit():
    manager = RiskManager(make_settings(max_position_allocation_percent=5))

    approved, reason = manager.validate(
        {
            "symbol": "AAPL",
            "action": "BUY",
            "confidence": 0.9,
            "suggested_allocation_percent": 10,
        }
    )

    assert approved is False
    assert "exceeds maximum" in reason


def broker_with_snapshot(settings, price=100, fake_broker=None, execution_strategy_factory=None):
    broker = BrokerClient(
        settings,
        broker_factory=lambda config: fake_broker,
        order_factory=lambda **kwargs: types.SimpleNamespace(**kwargs),
        execution_strategy_factory=execution_strategy_factory,
    )
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000, "cash": 25000, "buying_power": 25000},
        positions=[],
        market_data={"prices": {"AAPL": {"last_price": price}, "MSFT": {"last_price": price}, "PLTR": {"last_price": price}}},
    )
    return broker


def approved_decision(**overrides):
    decision = {
        "symbol": "AAPL",
        "action": "BUY",
        "confidence": 0.9,
        "suggested_allocation_percent": 5,
        "reason": "Approved test decision.",
    }
    decision.update(overrides)
    return decision


def make_mock_bars(length=80, start_price=100, volume=1000):
    rows = []
    for index in range(length):
        close = start_price + index
        rows.append(
            {
                "open": close - 0.5,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": volume + index,
            }
        )
    return pd.DataFrame(rows)


def test_market_indicators_calculate_from_mocked_data():
    minute_bars = make_mock_bars(length=80, start_price=100, volume=1000)
    daily_bars = make_mock_bars(length=60, start_price=90, volume=10000)

    indicators = calculate_market_indicators("AAPL", minute_bars, daily_bars)

    assert indicators["symbol"] == "AAPL"
    assert indicators["current_price"] == 179
    assert indicators["5m_change_percent"] == pytest.approx(((179 - 174) / 174) * 100)
    assert indicators["15m_change_percent"] == pytest.approx(((179 - 164) / 164) * 100)
    assert indicators["1h_change_percent"] == pytest.approx(((179 - 119) / 119) * 100)
    assert indicators["5d_change_percent"] == pytest.approx(((149 - 144) / 144) * 100)
    assert indicators["20d_change_percent"] == pytest.approx(((149 - 129) / 129) * 100)
    assert indicators["volume"] == pytest.approx(minute_bars["volume"].sum())
    assert indicators["average_20d_volume"] == pytest.approx(daily_bars["volume"].tail(20).mean())
    assert indicators["relative_volume"] == pytest.approx(
        minute_bars["volume"].sum() / daily_bars["volume"].tail(20).mean()
    )


def test_market_indicators_calculate_ema_rsi_and_vwap():
    minute_bars = make_mock_bars(length=80, start_price=100, volume=1000)
    daily_bars = make_mock_bars(length=60, start_price=90, volume=10000)

    indicators = calculate_market_indicators("AAPL", minute_bars, daily_bars)
    typical_price = (minute_bars["high"] + minute_bars["low"] + minute_bars["close"]) / 3
    expected_vwap = (typical_price * minute_bars["volume"]).sum() / minute_bars["volume"].sum()

    assert indicators["EMA20"] == pytest.approx(
        minute_bars["close"].ewm(span=20, adjust=False).mean().iloc[-1]
    )
    assert indicators["EMA50"] == pytest.approx(
        minute_bars["close"].ewm(span=50, adjust=False).mean().iloc[-1]
    )
    assert indicators["RSI14"] == 100
    assert indicators["VWAP"] == pytest.approx(expected_vwap)


def test_missing_bars_do_not_crash_indicator_calculation():
    indicators = calculate_market_indicators("AAPL", None, None)

    assert indicators["symbol"] == "AAPL"
    assert indicators["current_price"] is None
    assert indicators["RSI14"] is None


def test_dynamic_scanner_creates_ranked_watchlist():
    scanner = DynamicWatchlistScanner(watchlist_size=3)
    ranked = scanner.rank(
        ["AAPL", "MSFT", "TSLA"],
        {
            "AAPL": {
                "current_price": 200,
                "day_change_percent": 2.1,
                "volume": 5000000,
                "relative_volume": 2.0,
                "1h_change_percent": 2.4,
            },
            "MSFT": {
                "current_price": 400,
                "day_change_percent": -1.5,
                "volume": 3000000,
                "relative_volume": 1.1,
                "1h_change_percent": 0.5,
            },
            "TSLA": {
                "current_price": 250,
                "day_change_percent": 0.2,
                "volume": 1000000,
                "relative_volume": 1.0,
                "1h_change_percent": 0.2,
            },
        },
    )

    assert [candidate.symbol for candidate in ranked] == ["AAPL", "MSFT", "TSLA"]
    assert ranked[0].score == 9
    assert "high relative volume" in ranked[0].reasons_added
    assert "high volatility" in ranked[0].reasons_added


def test_dynamic_scanner_merges_duplicate_symbols():
    scanner = DynamicWatchlistScanner(watchlist_size=10)
    ranked = scanner.rank(
        ["aapl", "AAPL", "msft"],
        {
            "AAPL": {"volume": 1000, "day_change_percent": 1.2, "relative_volume": 1.6},
            "MSFT": {"volume": 900, "day_change_percent": 0.1, "relative_volume": 1.0},
        },
    )

    assert [candidate.symbol for candidate in ranked].count("AAPL") == 1
    assert [candidate.symbol for candidate in ranked] == ["AAPL", "MSFT"]


def test_dynamic_scanner_caps_final_watchlist_size():
    scanner = DynamicWatchlistScanner(watchlist_size=2)
    ranked = scanner.rank(
        ["AAPL", "MSFT", "NVDA"],
        {
            "AAPL": {"volume": 1000, "day_change_percent": 1.1, "relative_volume": 1.6},
            "MSFT": {"volume": 900, "day_change_percent": 1.1, "relative_volume": 1.6},
            "NVDA": {"volume": 800, "day_change_percent": 1.1, "relative_volume": 1.6},
        },
    )

    assert len(ranked) == 2


def test_dynamic_scanner_scores_momentum_indicators():
    scanner = DynamicWatchlistScanner(watchlist_size=2)
    ranked = scanner.rank(
        ["AAPL", "MSFT"],
        {
            "AAPL": {
                "volume": 1000,
                "day_change_percent": 0.1,
                "relative_volume": 1.0,
                "5d_change_percent": 4,
                "20d_change_percent": 6,
            },
            "MSFT": {
                "volume": 900,
                "day_change_percent": 0.1,
                "relative_volume": 1.0,
            },
        },
    )

    assert ranked[0].symbol == "AAPL"
    assert "strong 5d momentum" in ranked[0].reasons_added
    assert "strong 20d momentum" in ranked[0].reasons_added


def test_broad_asset_filter_excludes_inactive_untradable_otc_and_etf():
    good_asset = types.SimpleNamespace(
        symbol="AAPL",
        status="active",
        tradable=True,
        asset_class="us_equity",
        exchange="NASDAQ",
    )
    bad_assets = [
        types.SimpleNamespace(symbol="HALT", status="inactive", tradable=True, asset_class="us_equity", exchange="NYSE"),
        types.SimpleNamespace(symbol="LOCK", status="active", tradable=False, asset_class="us_equity", exchange="NYSE"),
        types.SimpleNamespace(symbol="OTC1", status="active", tradable=True, asset_class="us_equity", exchange="OTC"),
        types.SimpleNamespace(symbol="ETF1", status="active", tradable=True, asset_class="us_equity", exchange="ARCA", asset_type="etf"),
    ]

    assert is_broad_scan_asset_candidate(good_asset) is True
    assert all(not is_broad_scan_asset_candidate(asset) for asset in bad_assets)


def test_broad_asset_filter_excludes_noisy_symbols_and_instruments():
    bad_assets = [
        types.SimpleNamespace(symbol="BRK.B", status="active", tradable=True, asset_class="us_equity", exchange="NYSE"),
        types.SimpleNamespace(symbol="ABC/WS", status="active", tradable=True, asset_class="us_equity", exchange="NYSE"),
        types.SimpleNamespace(symbol="LONGER", status="active", tradable=True, asset_class="us_equity", exchange="NYSE"),
        types.SimpleNamespace(symbol="FDRV", status="active", tradable=True, asset_class="us_equity", exchange="ARCA", name="Fidelity Electric Vehicles ETF"),
        types.SimpleNamespace(symbol="ABCU", status="active", tradable=True, asset_class="us_equity", exchange="NYSE", name="Example Acquisition Units"),
        types.SimpleNamespace(symbol="ABCR", status="active", tradable=True, asset_class="us_equity", exchange="NYSE", name="Example Rights"),
        types.SimpleNamespace(symbol="PREF", status="active", tradable=True, asset_class="us_equity", exchange="NYSE", name="Example Preferred Shares"),
    ]
    good_asset = types.SimpleNamespace(
        symbol="PLTR",
        status="active",
        tradable=True,
        asset_class="us_equity",
        exchange="NYSE",
    )

    assert all(not is_broad_scan_asset_candidate(asset) for asset in bad_assets)
    assert is_broad_scan_asset_candidate(good_asset) is True


def test_broad_asset_filter_allows_long_symbol_when_explicitly_allowed():
    asset = types.SimpleNamespace(
        symbol="LONGER",
        status="active",
        tradable=True,
        asset_class="us_equity",
        exchange="NYSE",
    )

    assert is_broad_scan_asset_candidate(asset) is False
    assert is_broad_scan_asset_candidate(asset, explicit_allowed_symbols=["LONGER"]) is True


def test_broad_asset_filter_keeps_realistic_alpaca_field_shapes():
    enum_like_asset = types.SimpleNamespace(
        symbol="PLTR",
        status=types.SimpleNamespace(value="active"),
        tradable=True,
        asset_class=types.SimpleNamespace(value="us_equity"),
        exchange=types.SimpleNamespace(value="NASDAQ"),
    )
    dotted_enum_asset = types.SimpleNamespace(
        symbol="NVDA",
        status="AssetStatus.ACTIVE",
        tradable=True,
        asset_class="AssetClass.US_EQUITY",
        exchange="AssetExchange.NASDAQ",
    )

    assert is_broad_scan_asset_candidate(enum_like_asset) is True
    assert is_broad_scan_asset_candidate(dotted_enum_asset) is True


class MockLumibotBroker:
    def __init__(self, should_raise=False):
        self.submitted_orders = []
        self.should_raise = should_raise

    def submit_order(self, order):
        self.submitted_orders.append(order)
        if self.should_raise:
            raise RuntimeError("broker failed")
        return types.SimpleNamespace(identifier="paper-123", status="submitted")


class MockExecutionStrategy:
    def __init__(self, broker, name):
        self.broker = broker
        self.name = name
        self.created_orders = []
        self.submitted_orders = []

    def create_order(self, symbol, action, quantity):
        order = types.SimpleNamespace(
            symbol=symbol,
            action=action,
            quantity=quantity,
            strategy=self.name,
            order_type="market",
        )
        self.created_orders.append(order)
        return order

    def submit_order(self, order):
        self.submitted_orders.append(order)
        return self.broker.submit_order(order)


class MockAlpacaApi:
    def __init__(self, assets=None, raise_assets=False):
        self.assets = assets or [
            types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="SPY", status="active", tradable=True, asset_class="us_equity", exchange="ARCA"),
            types.SimpleNamespace(symbol="TSLA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        ]
        self.raise_assets = raise_assets

    def get_account(self):
        return types.SimpleNamespace(
            cash="25000",
            buying_power="25000",
            portfolio_value="100000",
        )

    def get_all_positions(self):
        return [
            types.SimpleNamespace(
                symbol="MSFT",
                qty="2",
                market_value="869.60",
                avg_entry_price="431.21",
            )
        ]

    def get_all_assets(self):
        if self.raise_assets:
            raise RuntimeError("asset list unavailable")
        return self.assets


class MockDataSource:
    def __init__(self, failing_symbols=None, daily_volumes=None, start_prices=None):
        self.failing_symbols = set(failing_symbols or [])
        self.daily_volumes = daily_volumes or {}
        self.start_prices = start_prices or {}

    def get_historical_prices(
        self,
        asset,
        length,
        timestep="",
        include_after_hours=False,
    ):
        if asset.symbol in self.failing_symbols:
            raise RuntimeError("bar data unavailable")
        base_volume = {
            "AAPL": 3000,
            "MSFT": 2000,
            "SPY": 1500,
            "TSLA": 2500,
        }.get(asset.symbol, 1000)
        start_price = {
            "AAPL": 100,
            "MSFT": 120,
            "SPY": 90,
            "TSLA": 80,
        }.get(asset.symbol, 70)
        start_price = self.start_prices.get(asset.symbol, start_price)
        if timestep == "minute":
            return types.SimpleNamespace(df=make_mock_bars(length=length, start_price=start_price, volume=base_volume))
        daily_volume = self.daily_volumes.get(asset.symbol, base_volume * 10)
        return types.SimpleNamespace(df=make_mock_bars(length=length, start_price=start_price, volume=daily_volume))


class MockDataBroker:
    def __init__(
        self,
        failing_symbols=None,
        assets=None,
        raise_assets=False,
        daily_volumes=None,
        start_prices=None,
        rate_limit_batches=False,
        fail_batches=False,
        batch_delay_seconds=0,
    ):
        self.api = MockAlpacaApi(assets=assets, raise_assets=raise_assets)
        self.data_source = MockDataSource(
            failing_symbols=failing_symbols,
            daily_volumes=daily_volumes,
            start_prices=start_prices,
        )
        self.last_price_calls = []
        self.last_prices_batches = []
        self.rate_limit_batches = rate_limit_batches
        self.fail_batches = fail_batches
        self.batch_delay_seconds = batch_delay_seconds

    def get_last_price(self, asset):
        self.last_price_calls.append(asset.symbol)
        prices = {
            "AAPL": 214.33,
            "MSFT": 434.80,
            "SPY": 550.25,
            "TSLA": 250.25,
            "NVDA": 180.10,
            "AMD": 160.50,
            "PLTR": 24.50,
            "LOWP": 3.50,
            "LOWV": 85.25,
        }
        return prices.get(asset.symbol)

    def get_last_prices(self, symbols):
        self.last_prices_batches.append(list(symbols))
        if self.batch_delay_seconds:
            time.sleep(self.batch_delay_seconds)
        if self.rate_limit_batches:
            raise RuntimeError("429 Client Error: Too Many Requests")
        if self.fail_batches:
            raise RuntimeError("batch failed")
        prices = {
            "AAPL": 214.33,
            "MSFT": 434.80,
            "SPY": 550.25,
            "TSLA": 250.25,
            "NVDA": 180.10,
            "AMD": 160.50,
            "PLTR": 24.50,
            "LOWP": 3.50,
            "LOWV": 85.25,
        }
        return {symbol: prices.get(symbol) for symbol in symbols if prices.get(symbol) is not None}


def test_dry_run_still_allows_broker_data_collection():
    broker = BrokerClient(
        make_settings(dry_run=True, allowed_symbols=["AAPL", "MSFT", "SPY"]),
        broker_factory=lambda config: MockDataBroker(),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.account["cash"] == 25000
    assert snapshot.account["buying_power"] == 25000
    assert snapshot.account["portfolio_value"] == 100000
    assert snapshot.positions == [
        {
            "symbol": "MSFT",
            "quantity": 2,
            "market_value": 869.6,
            "average_price": 431.21,
        }
    ]
    assert snapshot.market_data["prices"]["AAPL"]["last_price"] == 214.33
    assert snapshot.market_data["prices"]["MSFT"]["last_price"] == 434.8
    assert snapshot.market_data["prices"]["SPY"]["last_price"] == 550.25
    assert snapshot.market_data["market_intelligence"]["AAPL"]["RSI14"] == 100
    assert snapshot.market_data["market_intelligence"]["MSFT"]["EMA20"] is not None
    assert snapshot.market_data["market_intelligence"]["SPY"]["VWAP"] is not None


def test_failed_symbol_does_not_crash_whole_market_collection():
    broker = BrokerClient(
        make_settings(dry_run=True, allowed_symbols=["AAPL", "MSFT", "SPY"]),
        broker_factory=lambda config: MockDataBroker(failing_symbols={"MSFT"}),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert "AAPL" in snapshot.market_data["market_intelligence"]
    assert "SPY" in snapshot.market_data["market_intelligence"]
    assert "MSFT" not in snapshot.market_data["market_intelligence"]


def test_dynamic_watchlist_enabled_builds_capped_final_watchlist():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            watchlist_size=2,
            scanner_universe=["AAPL", "MSFT", "SPY"],
            allowed_symbols=["AAPL", "MSFT", "SPY"],
        ),
        broker_factory=lambda config: MockDataBroker(),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["dynamic_watchlist_enabled"] is True
    assert len(snapshot.market_data["symbols"]) == 2
    assert len(snapshot.market_data["dynamic_watchlist"]) == 2
    assert set(snapshot.market_data["symbols"]) == set(snapshot.market_data["market_intelligence"])
    assert snapshot.market_data["dynamic_watchlist"][0]["score"] >= snapshot.market_data["dynamic_watchlist"][1]["score"]


def test_broad_market_scan_enabled_builds_final_watchlist():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_market_max_symbols=3,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "NVDA", "AMD"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=[
                types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="NVDA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="AMD", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            ],
            daily_volumes={"AAPL": 900000, "MSFT": 800000, "NVDA": 700000, "AMD": 600000},
        ),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "broad_generated"
    assert snapshot.market_data["scanner_mode"] == "broad_market"
    assert snapshot.market_data["broad_candidate_count"] == 3
    assert len(snapshot.market_data["symbols"]) == 2
    assert set(snapshot.market_data["symbols"]) == set(snapshot.market_data["market_intelligence"])
    assert len(snapshot.market_data["dynamic_watchlist"]) == 2


def test_broad_market_scan_logs_major_stages(caplog):
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_market_max_symbols=3,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "NVDA"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=[
                types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="NVDA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            ],
            daily_volumes={"AAPL": 900000, "MSFT": 800000, "NVDA": 700000},
        ),
    )

    with caplog.at_level(logging.INFO, logger="broker"):
        broker.connect()
        broker.collect_snapshot()

    assert "Broad scanner: starting broad market scan." in caplog.text
    assert "Broad scanner: fetching tradable assets." in caplog.text
    assert "Broad scanner: fetched 3 assets." in caplog.text
    assert "Broad scanner: filtering tradable US equities." in caplog.text
    assert "Broad scanner: 3 symbols remain after asset filters." in caplog.text
    assert "Broad scanner: collecting market data." in caplog.text
    assert "Broad scanner: applying liquidity filters." in caplog.text
    assert "Broad scanner: 3 symbols remain after price filters." in caplog.text
    assert "Broad scanner: requesting batch 1 with 3 symbols." in caplog.text
    assert "Broad scanner: completed batch 1 with 3 prices." in caplog.text
    assert "Broad scanner: capped 3 symbols for indicator calculation." in caplog.text
    assert "Broad scanner: 3 symbols remain after liquidity filters." in caplog.text
    assert "Broad scanner: beginning ranking." in caplog.text
    assert "Broad scanner: final watchlist size is 2." in caplog.text
    assert "Broad scanner: top selected symbols:" in caplog.text


def test_broad_scan_context_only_contains_final_watchlist():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_market_max_symbols=4,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "NVDA", "AMD"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=[
                types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="NVDA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="AMD", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            ],
            daily_volumes={"AAPL": 900000, "MSFT": 800000, "NVDA": 700000, "AMD": 600000},
        ),
    )
    broker.connect()
    snapshot = broker.collect_snapshot()
    strategy = TradingStrategy(
        settings=make_settings(dynamic_watchlist_enabled=True),
        broker=broker,
        risk_manager=RiskManager(make_settings()),
    )

    context = strategy._build_ai_context(snapshot)

    assert len(context.watchlist_symbols) == 2
    assert set(context.recent_price_data["market_intelligence"]) == set(context.watchlist_symbols)
    assert set(context.recent_price_data["prices"]) == set(context.watchlist_symbols)


def test_broad_market_scan_disabled_uses_configured_scanner_v1():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=False,
            watchlist_size=2,
            scanner_universe=["AAPL", "MSFT", "SPY"],
            allowed_symbols=["AAPL", "MSFT", "SPY"],
        ),
        broker_factory=lambda config: MockDataBroker(),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "generated"
    assert snapshot.market_data["scanner_mode"] == "configured_universe"


def test_broad_market_scan_filters_assets_price_and_volume():
    assets = [
        types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        types.SimpleNamespace(symbol="LOWP", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        types.SimpleNamespace(symbol="LOWV", status="active", tradable=True, asset_class="us_equity", exchange="NYSE"),
        types.SimpleNamespace(symbol="ETF1", status="active", tradable=True, asset_class="us_equity", exchange="ARCA", name="Example ETF"),
        types.SimpleNamespace(symbol="OTC1", status="active", tradable=True, asset_class="us_equity", exchange="OTC"),
        types.SimpleNamespace(symbol="HALT", status="inactive", tradable=True, asset_class="us_equity", exchange="NYSE"),
    ]
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            min_stock_price=5,
            min_average_volume=500000,
            watchlist_size=5,
            allowed_symbols=["AAPL", "LOWP", "LOWV"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=assets,
            daily_volumes={"AAPL": 900000, "LOWP": 900000, "LOWV": 1000},
            start_prices={"LOWP": -118},
        ),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "broad_generated"
    assert snapshot.market_data["symbols"] == ["AAPL"]
    assert snapshot.market_data["broad_candidate_count"] == 1


def test_broad_market_scan_keeps_realistic_active_tradable_equities():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["PLTR", "NVDA"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=[
                types.SimpleNamespace(
                    symbol="PLTR",
                    status=types.SimpleNamespace(value="active"),
                    tradable=True,
                    asset_class=types.SimpleNamespace(value="us_equity"),
                    exchange=types.SimpleNamespace(value="NASDAQ"),
                ),
                types.SimpleNamespace(
                    symbol="NVDA",
                    status="AssetStatus.ACTIVE",
                    tradable=True,
                    asset_class="AssetClass.US_EQUITY",
                    exchange="AssetExchange.NASDAQ",
                ),
            ],
            daily_volumes={"PLTR": 900000, "NVDA": 800000},
        ),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "broad_generated"
    assert set(snapshot.market_data["symbols"]) == {"PLTR", "NVDA"}
    assert snapshot.market_data["broad_candidate_count"] == 2


def test_broad_market_scan_applies_candidate_cap_before_indicators():
    assets = [
        types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ", average_volume=900000),
        types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ", average_volume=800000),
        types.SimpleNamespace(symbol="NVDA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ", average_volume=700000),
        types.SimpleNamespace(symbol="AMD", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ", average_volume=600000),
    ]
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_market_max_symbols=10,
            max_scanner_candidates_after_filters=2,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "NVDA", "AMD"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=assets,
            daily_volumes={"AAPL": 900000, "MSFT": 800000, "NVDA": 700000, "AMD": 600000},
        ),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["broad_price_filtered_count"] == 4
    assert snapshot.market_data["broad_capped_candidate_count"] == 2
    assert snapshot.market_data["broad_candidate_count"] == 2
    assert len(snapshot.market_data["symbols"]) == 2


def test_broad_market_scan_uses_batched_prices_instead_of_per_symbol_latest_price():
    fake_broker = MockDataBroker(
        assets=[
            types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="NVDA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        ],
        daily_volumes={"AAPL": 900000, "MSFT": 800000, "NVDA": 700000},
    )
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_scan_batch_size=2,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "NVDA"],
        ),
        broker_factory=lambda config: fake_broker,
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "broad_generated"
    assert fake_broker.last_prices_batches == [["AAPL", "MSFT"], ["NVDA"]]
    assert fake_broker.last_price_calls == []
    assert snapshot.market_data["broad_batch_request_count"] == 2


def test_broad_market_scan_respects_max_batch_request_cap():
    fake_broker = MockDataBroker(
        assets=[
            types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="NVDA", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="AMD", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        ],
        daily_volumes={"AAPL": 900000, "MSFT": 800000, "NVDA": 700000, "AMD": 600000},
    )
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_scan_batch_size=1,
            broad_scan_max_requests_per_cycle=2,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "NVDA", "AMD"],
        ),
        broker_factory=lambda config: fake_broker,
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert fake_broker.last_prices_batches == [["AAPL"], ["MSFT"]]
    assert snapshot.market_data["broad_batch_request_count"] == 2
    assert snapshot.market_data["broad_price_filtered_count"] == 2
    assert set(snapshot.market_data["symbols"]).issubset({"AAPL", "MSFT"})


def test_broad_market_scan_rate_limit_falls_back_to_scanner_v1(caplog):
    fake_broker = MockDataBroker(
        assets=[
            types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        ],
        rate_limit_batches=True,
    )
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["AAPL", "MSFT"],
            watchlist_size=2,
        ),
        broker_factory=lambda config: fake_broker,
    )

    with caplog.at_level(logging.WARNING, logger="broker"):
        broker.connect()
        snapshot = broker.collect_snapshot()

    assert snapshot.market_data["broad_rate_limit_fallback"] is True
    assert snapshot.market_data["scanner_status"] == "generated"
    assert snapshot.market_data["scanner_mode"] == "configured_universe"
    assert "rate limit fallback triggered" in caplog.text


def test_broad_market_scan_batch_timeout_falls_back_without_blocking(caplog):
    fake_broker = MockDataBroker(
        assets=[
            types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        ],
        batch_delay_seconds=0.05,
    )
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_scan_batch_size=2,
            broad_scan_batch_timeout_seconds=0.01,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["AAPL", "MSFT"],
            watchlist_size=2,
        ),
        broker_factory=lambda config: fake_broker,
    )

    with caplog.at_level(logging.INFO, logger="broker"):
        broker.connect()
        snapshot = broker.collect_snapshot()

    assert fake_broker.last_prices_batches == [["AAPL", "MSFT"]]
    assert snapshot.market_data["broad_batch_timeout_count"] == 1
    assert snapshot.market_data["broad_scan_failed"] is True
    assert snapshot.market_data["scanner_status"] == "generated"
    assert snapshot.market_data["scanner_mode"] == "configured_universe"
    assert "Broad scanner: requesting batch 1 with 2 symbols." in caplog.text
    assert "Broad scanner: batch 1 timed out after 0.01 seconds." in caplog.text


def test_broad_market_scan_batch_failure_skips_batch_and_falls_back(caplog):
    fake_broker = MockDataBroker(
        assets=[
            types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
            types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
        ],
        fail_batches=True,
    )
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            broad_scan_batch_size=2,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["AAPL", "MSFT"],
            watchlist_size=2,
        ),
        broker_factory=lambda config: fake_broker,
    )

    with caplog.at_level(logging.INFO, logger="broker"):
        broker.connect()
        snapshot = broker.collect_snapshot()

    assert fake_broker.last_prices_batches == [["AAPL", "MSFT"]]
    assert snapshot.market_data["broad_scan_failed"] is True
    assert snapshot.market_data["scanner_status"] == "generated"
    assert "Broad scanner: skipped one price batch safely: RuntimeError." in caplog.text


def test_broad_market_scan_aggregates_insufficient_data_logs(caplog):
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            min_average_volume=10000,
            watchlist_size=2,
            allowed_symbols=["AAPL", "MSFT", "SPY"],
        ),
        broker_factory=lambda config: MockDataBroker(
            assets=[
                types.SimpleNamespace(symbol="AAPL", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="MSFT", status="active", tradable=True, asset_class="us_equity", exchange="NASDAQ"),
                types.SimpleNamespace(symbol="SPY", status="active", tradable=True, asset_class="us_equity", exchange="ARCA"),
            ],
            failing_symbols={"MSFT"},
            daily_volumes={"AAPL": 900000, "MSFT": 800000, "SPY": 700000},
        ),
    )

    with caplog.at_level(logging.INFO, logger="broker"):
        broker.connect()
        snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "broad_generated"
    assert "Broad scanner: skipped 1 symbols due to insufficient data." in caplog.text
    assert "Could not calculate market indicators for MSFT" not in caplog.text


def test_broad_market_scan_failure_falls_back_to_scanner_v1():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["AAPL", "MSFT"],
            watchlist_size=2,
        ),
        broker_factory=lambda config: MockDataBroker(raise_assets=True),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["broad_scan_failed"] is True
    assert snapshot.market_data["scanner_status"] == "generated"
    assert snapshot.market_data["scanner_mode"] == "configured_universe"
    assert set(snapshot.market_data["symbols"]).issubset({"AAPL", "MSFT"})


def test_broad_market_scan_failure_logs_stage_class_and_message(caplog):
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["AAPL", "MSFT"],
            watchlist_size=2,
        ),
        broker_factory=lambda config: MockDataBroker(raise_assets=True),
    )

    with caplog.at_level(logging.WARNING, logger="broker"):
        broker.connect()
        snapshot = broker.collect_snapshot()

    assert snapshot.market_data["broad_scan_failed"] is True
    assert snapshot.market_data["scanner_status"] == "generated"
    assert "Broad scanner failed during fetching tradable assets." in caplog.text
    assert "Exception: RuntimeError." in caplog.text
    assert "Message: asset list unavailable." in caplog.text
    assert "Broad market scanner failed safely during fetching tradable assets." in caplog.text
    assert "Broad scanner exception class: RuntimeError." in caplog.text
    assert "Broad scanner exception message: asset list unavailable." in caplog.text


def test_broad_and_configured_scanner_failure_falls_back_to_static(monkeypatch):
    def failing_rank(self, universe, market_intelligence):
        raise RuntimeError("scanner failed")

    monkeypatch.setattr("broker.DynamicWatchlistScanner.rank", failing_rank)
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            broad_market_scan_enabled=True,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["SPY"],
            watchlist_size=2,
        ),
        broker_factory=lambda config: MockDataBroker(raise_assets=True),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "fallback_static"
    assert snapshot.market_data["scanner_mode"] == "static"
    assert snapshot.market_data["symbols"] == ["SPY"]


def test_dynamic_scanner_failure_falls_back_to_static_watchlist(monkeypatch):
    def failing_rank(self, universe, market_intelligence):
        raise RuntimeError("scanner failed")

    monkeypatch.setattr("broker.DynamicWatchlistScanner.rank", failing_rank)
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            watchlist_size=2,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["SPY"],
        ),
        broker_factory=lambda config: MockDataBroker(failing_symbols={"AAPL", "MSFT"}),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["symbols"] == ["SPY"]
    assert snapshot.market_data["scanner_status"] == "fallback_static"
    assert snapshot.market_data["dynamic_watchlist"] == []
    assert "SPY" in snapshot.market_data["market_intelligence"]


def test_dynamic_scanner_no_candidates_produces_hold_status():
    broker = BrokerClient(
        make_settings(
            dynamic_watchlist_enabled=True,
            watchlist_size=2,
            scanner_universe=["AAPL", "MSFT"],
            allowed_symbols=["SPY"],
        ),
        broker_factory=lambda config: MockDataBroker(failing_symbols={"AAPL", "MSFT"}),
    )

    broker.connect()
    snapshot = broker.collect_snapshot()

    assert snapshot.market_data["scanner_status"] == "no_candidates"
    assert snapshot.market_data["symbols"] == ["SPY"]
    assert snapshot.market_data["dynamic_watchlist"] == []


def test_alpaca_config_uses_paper_without_deprecated_endpoint():
    broker = BrokerClient(make_settings())

    alpaca_config = broker._alpaca_config()

    assert alpaca_config["PAPER"] is True
    assert "ENDPOINT" not in alpaca_config
    assert alpaca_config["API_KEY"] == "test-alpaca-key"
    assert alpaca_config["API_SECRET"] == "test-alpaca-secret"


def test_strategy_context_includes_populated_broker_data():
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[{"symbol": "MSFT", "quantity": 2}],
        market_data={
            "prices": {"AAPL": {"last_price": 214.33}},
            "market_intelligence": {
                "AAPL": {
                    "current_price": 214.33,
                    "RSI14": 55.2,
                    "EMA20": 210.0,
                    "EMA50": 205.0,
                    "VWAP": 212.0,
                }
            },
        },
    )
    strategy = TradingStrategy(
        settings=make_settings(allowed_symbols=["AAPL", "MSFT"]),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )

    context = strategy._build_ai_context(snapshot)

    assert context.account_cash == 25000
    assert context.buying_power == 25000
    assert context.portfolio_value == 100000
    assert context.current_positions == [{"symbol": "MSFT", "quantity": 2}]
    assert context.recent_price_data["prices"]["AAPL"]["last_price"] == 214.33
    assert context.recent_price_data["market_intelligence"]["AAPL"]["RSI14"] == 55.2


def test_strategy_history_context_can_be_disabled():
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[],
        market_data={},
    )
    strategy = TradingStrategy(
        settings=make_settings(include_history_context=False),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )

    history = strategy._load_history_context(datetime(2026, 7, 6, 10, 0))
    context = strategy._build_ai_context(snapshot)

    assert history == context_history.empty_history_context()
    assert context.history_context == {}


def test_strategy_history_database_failure_does_not_crash_context_build(monkeypatch, tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    def failing_loader(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(context_history, "load_history_context", failing_loader)
    strategy = TradingStrategy(
        settings=make_settings(include_history_context=True),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )

    history = strategy._load_history_context(datetime(2026, 7, 6, 10, 0))

    assert history == context_history.empty_history_context()


def test_strategy_writes_one_market_snapshot_per_symbol_per_cycle(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[],
        market_data={
            "market_intelligence": {
                "AAPL": {
                    "current_price": 214.33,
                    "volume": 1000,
                    "RSI14": 55.2,
                    "EMA20": 210.0,
                    "EMA50": 205.0,
                    "VWAP": 212.0,
                },
                "aapl": {
                    "current_price": 215.0,
                    "volume": 1100,
                    "RSI14": 56.0,
                    "EMA20": 211.0,
                    "EMA50": 206.0,
                    "VWAP": 213.0,
                },
                "MSFT": {
                    "current_price": 434.8,
                    "volume": 2000,
                    "RSI14": 48.1,
                    "EMA20": 430.0,
                    "EMA50": 426.0,
                    "VWAP": 432.0,
                },
            }
        },
    )
    strategy = TradingStrategy(
        settings=make_settings(allowed_symbols=["AAPL", "MSFT"]),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )

    strategy._record_market_snapshots(snapshot, datetime(2026, 7, 6, 10, 0))

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT symbol, raw_snapshot FROM market_snapshots ORDER BY symbol"
        ).fetchall()

    assert [row[0] for row in rows] == ["AAPL", "MSFT"]
    assert json.loads(rows[0][1])["current_price"] == 214.33
    assert json.loads(rows[1][1])["RSI14"] == 48.1


def test_strategy_persists_dynamic_watchlist_rows(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[],
        market_data={
            "symbols": ["AAPL", "MSFT"],
            "dynamic_watchlist": [
                {
                    "symbol": "AAPL",
                    "score": 7,
                    "reasons_added": ["top gainer", "high relative volume"],
                    "current_price": 214.33,
                },
                {
                    "symbol": "MSFT",
                    "score": 4,
                    "reasons_added": ["top volume"],
                    "current_price": 434.8,
                },
            ],
        },
    )
    strategy = TradingStrategy(
        settings=make_settings(dynamic_watchlist_enabled=True),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )

    strategy._record_dynamic_watchlist(snapshot, datetime(2026, 7, 6, 10, 0))

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT date, symbol, reason_added, raw_metadata FROM watchlists ORDER BY symbol"
        ).fetchall()

    assert len(rows) == 2
    assert rows[0][:3] == ("2026-07-06", "AAPL", "top gainer, high relative volume")
    assert json.loads(rows[0][3])["score"] == 7
    assert rows[1][1] == "MSFT"


def test_strategy_writes_one_portfolio_snapshot_per_cycle(tmp_path):
    database_path = tmp_path / "trading_bot.db"
    database.init_database(database_path)
    snapshot = BrokerSnapshot(
        account={
            "cash": 25000,
            "buying_power": 30000,
            "equity": 100500,
            "portfolio_value": 100500,
        },
        positions=[{"symbol": "AAPL", "quantity": 2}],
        market_data={"prices": {"AAPL": {"last_price": 214.33}}},
    )
    strategy = TradingStrategy(
        settings=make_settings(allowed_symbols=["AAPL"]),
        broker=types.SimpleNamespace(collect_snapshot=lambda: snapshot),
        risk_manager=RiskManager(make_settings(dry_run=True)),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"AAPL","action":"HOLD"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "AAPL",
                "action": "HOLD",
                "confidence": 0.8,
                "suggested_allocation_percent": 0,
                "reason": "Test hold.",
            }
        ),
    )

    strategy.run_cycle()

    with sqlite3.connect(database_path) as connection:
        portfolio_count = connection.execute(
            "SELECT COUNT(*) FROM portfolio_snapshots"
        ).fetchone()[0]
        raw_snapshot = connection.execute(
            "SELECT raw_snapshot FROM portfolio_snapshots"
        ).fetchone()[0]

    assert portfolio_count == 1
    assert json.loads(raw_snapshot)["account"]["portfolio_value"] == 100500


def test_portfolio_snapshot_insert_failure_does_not_crash_trading_cycle(monkeypatch, tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    def failing_insert(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(database, "insert_portfolio_snapshot", failing_insert)
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[],
        market_data={},
    )
    strategy = TradingStrategy(
        settings=make_settings(allowed_symbols=["AAPL"]),
        broker=types.SimpleNamespace(collect_snapshot=lambda: snapshot),
        risk_manager=RiskManager(make_settings(dry_run=True)),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"AAPL","action":"HOLD"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "AAPL",
                "action": "HOLD",
                "confidence": 0.8,
                "suggested_allocation_percent": 0,
                "reason": "Test hold.",
            }
        ),
    )

    strategy.run_cycle()


def test_market_snapshot_insert_failure_does_not_crash_trading_cycle(monkeypatch, tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    def failing_insert(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(database, "insert_market_snapshot", failing_insert)
    snapshot = BrokerSnapshot(
        account={"cash": 25000, "buying_power": 25000, "portfolio_value": 100000},
        positions=[],
        market_data={
            "market_intelligence": {
                "AAPL": {
                    "current_price": 214.33,
                    "volume": 1000,
                    "RSI14": 55.2,
                    "EMA20": 210.0,
                    "EMA50": 205.0,
                    "VWAP": 212.0,
                }
            }
        },
    )
    strategy = TradingStrategy(
        settings=make_settings(allowed_symbols=["AAPL"]),
        broker=types.SimpleNamespace(collect_snapshot=lambda: snapshot),
        risk_manager=RiskManager(make_settings(dry_run=True)),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"AAPL","action":"HOLD"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "AAPL",
                "action": "HOLD",
                "confidence": 0.8,
                "suggested_allocation_percent": 0,
                "reason": "Test hold.",
            }
        ),
    )

    strategy.run_cycle()


def test_broker_dry_run_blocks_execution():
    fake_broker = MockLumibotBroker()
    strategy_created = {"value": False}

    def strategy_factory(broker, name):
        strategy_created["value"] = True
        return MockExecutionStrategy(broker, name)

    broker = broker_with_snapshot(
        make_settings(dry_run=True),
        fake_broker=fake_broker,
        execution_strategy_factory=strategy_factory,
    )

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "DRY_RUN" in result["reason"]
    assert fake_broker.submitted_orders == []
    assert strategy_created["value"] is False


def test_broker_paper_trading_false_blocks_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(paper_trading=False), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "PAPER_TRADING" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_bot_disabled_blocks_execution():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(bot_enabled=False), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "BOT_ENABLED" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_strategy_rejects_buy_outside_final_watchlist_before_execution(tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    class StubBroker:
        def __init__(self):
            self.execute_called = False

        def collect_snapshot(self):
            return BrokerSnapshot(
                account={"cash": 100000, "buying_power": 100000, "portfolio_value": 100000},
                positions=[],
                market_data={
                    "symbols": ["AAPL"],
                    "dynamic_watchlist": [{"symbol": "AAPL", "score": 5, "reasons_added": ["top volume"]}],
                    "market_intelligence": {
                        "AAPL": {
                            "current_price": 214.33,
                            "volume": 1000,
                            "RSI14": 55,
                            "EMA20": 210,
                            "EMA50": 205,
                            "VWAP": 212,
                        }
                    },
                },
            )

        def execute_order(self, approved_decision):
            self.execute_called = True
            return {"executed": True}

    broker = StubBroker()
    strategy = TradingStrategy(
        settings=make_settings(
            dry_run=False,
            allowed_symbols=["AAPL", "MSFT"],
            dynamic_watchlist_enabled=True,
        ),
        broker=broker,
        risk_manager=RiskManager(
            make_settings(
                dry_run=False,
                allowed_symbols=["AAPL", "MSFT"],
                dynamic_watchlist_enabled=True,
            )
        ),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"MSFT","action":"BUY"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "MSFT",
                "action": "BUY",
                "confidence": 0.95,
                "suggested_allocation_percent": 1,
                "reason": "Outside final watchlist.",
            }
        ),
    )

    strategy.run_cycle()

    assert broker.execute_called is False


def test_strategy_allows_dynamic_symbol_in_final_watchlist(tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    class StubBroker:
        def __init__(self):
            self.executed_decision = None

        def collect_snapshot(self):
            return BrokerSnapshot(
                account={"cash": 100000, "buying_power": 100000, "portfolio_value": 100000},
                positions=[],
                market_data={
                    "symbols": ["PLTR"],
                    "dynamic_watchlist": [{"symbol": "PLTR", "score": 8, "reasons_added": ["high relative volume"]}],
                    "market_intelligence": {
                        "PLTR": {
                            "current_price": 24.5,
                            "volume": 1000,
                            "RSI14": 55,
                            "EMA20": 24,
                            "EMA50": 23,
                            "VWAP": 24.2,
                        }
                    },
                },
            )

        def execute_order(self, approved_decision):
            self.executed_decision = approved_decision
            return {"executed": False, "reason": "test execution skipped"}

    broker = StubBroker()
    settings = make_settings(
        dry_run=False,
        allowed_symbols=["AAPL", "MSFT"],
        dynamic_watchlist_enabled=True,
    )
    strategy = TradingStrategy(
        settings=settings,
        broker=broker,
        risk_manager=RiskManager(settings),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"PLTR","action":"BUY"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "PLTR",
                "action": "BUY",
                "confidence": 0.95,
                "suggested_allocation_percent": 1,
                "reason": "In final generated watchlist.",
            }
        ),
    )

    strategy.run_cycle()

    assert broker.executed_decision is not None
    assert broker.executed_decision["symbol"] == "PLTR"
    assert broker.executed_decision["cycle_allowed_symbols"] == ["PLTR"]


def test_no_scanner_candidates_returns_hold_without_openai_call():
    snapshot = BrokerSnapshot(
        account={"cash": 100000, "buying_power": 100000, "portfolio_value": 100000},
        positions=[],
        market_data={"symbols": ["SPY"], "scanner_status": "no_candidates"},
    )
    strategy = TradingStrategy(
        settings=make_settings(dynamic_watchlist_enabled=True, allowed_symbols=["SPY"]),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )
    strategy.ai_client = types.SimpleNamespace(
        get_decision=lambda context: pytest.fail("OpenAI should not be called")
    )

    decision = strategy.get_ai_decision(snapshot)

    assert decision["action"] == "HOLD"
    assert decision["reason"] == "Dynamic scanner returned no candidates."


def test_broker_connect_failure_is_caught_safely():
    def failing_broker_factory(config):
        raise RuntimeError("bad credentials")

    broker = BrokerClient(make_settings(), broker_factory=failing_broker_factory)

    broker.connect()

    assert broker._broker_available is False
    assert broker._broker is None


def test_broker_unavailable_blocks_execution_after_failed_connect():
    def failing_broker_factory(config):
        raise RuntimeError("bad credentials")

    broker = broker_with_snapshot(make_settings(), fake_broker=None)
    broker._broker_factory = failing_broker_factory
    broker.connect()

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert result["reason"] == "Broker unavailable"


def test_lazy_broker_initialisation_failure_returns_broker_unavailable():
    def failing_broker_factory(config):
        raise RuntimeError("network down")

    broker = broker_with_snapshot(make_settings(), fake_broker=None)
    broker._broker_factory = failing_broker_factory

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert result["reason"] == "Broker unavailable"


def test_broker_hold_does_not_call_lumibot():
    fake_broker = MockLumibotBroker()
    strategy_created = {"value": False}

    def strategy_factory(broker, name):
        strategy_created["value"] = True
        return MockExecutionStrategy(broker, name)

    broker = broker_with_snapshot(
        make_settings(),
        fake_broker=fake_broker,
        execution_strategy_factory=strategy_factory,
    )

    result = broker.execute_order(approved_decision(action="HOLD", suggested_allocation_percent=0))

    assert result["executed"] is False
    assert result["reason"] == "HOLD decision. No order placed."
    assert fake_broker.submitted_orders == []
    assert strategy_created["value"] is False


def test_broker_unsupported_symbol_does_not_call_lumibot():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(allowed_symbols=["AAPL"]), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(symbol="TSLA"))

    assert result["executed"] is False
    assert "ALLOWED_SYMBOLS" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_dynamic_mode_allows_symbol_in_final_watchlist():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(
        make_settings(
            allowed_symbols=["AAPL"],
            dynamic_watchlist_enabled=True,
        ),
        fake_broker=fake_broker,
    )

    result = broker.execute_order(
        approved_decision(
            symbol="PLTR",
            suggested_allocation_percent=1,
            cycle_allowed_symbols=["PLTR"],
        )
    )

    assert result["executed"] is True
    assert fake_broker.submitted_orders


def test_broker_dynamic_mode_rejects_symbol_outside_final_watchlist():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(
        make_settings(
            allowed_symbols=["AAPL", "PLTR"],
            dynamic_watchlist_enabled=True,
        ),
        fake_broker=fake_broker,
    )

    result = broker.execute_order(
        approved_decision(
            symbol="PLTR",
            suggested_allocation_percent=1,
            cycle_allowed_symbols=["AAPL"],
        )
    )

    assert result["executed"] is False
    assert "final watchlist" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_allocation_above_max_does_not_call_lumibot():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(max_position_allocation_percent=5), fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(suggested_allocation_percent=10))

    assert result["executed"] is False
    assert "exceeds maximum" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_rejects_buy_that_would_exceed_position_allocation():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(
        make_settings(max_position_allocation_percent=5),
        price=100,
        fake_broker=fake_broker,
    )
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000, "cash": 25000, "buying_power": 25000},
        positions=[{"symbol": "AAPL", "quantity": 30, "market_value": 3000}],
        market_data={"prices": {"AAPL": {"last_price": 100}}},
    )

    result = broker.execute_order(approved_decision(suggested_allocation_percent=3))

    assert result["executed"] is False
    assert "Projected AAPL allocation exceeds maximum" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_allows_buy_within_remaining_position_allocation():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(
        make_settings(max_position_allocation_percent=5),
        price=100,
        fake_broker=fake_broker,
    )
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000, "cash": 25000, "buying_power": 25000},
        positions=[{"symbol": "AAPL", "quantity": 20, "market_value": 2000}],
        market_data={"prices": {"AAPL": {"last_price": 100}}},
    )

    result = broker.execute_order(approved_decision(suggested_allocation_percent=3))

    assert result["executed"] is True
    assert result["quantity"] == 30
    assert fake_broker.submitted_orders[0].symbol == "AAPL"


def test_broker_rejects_sell_without_existing_position():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(action="SELL"))

    assert result["executed"] is False
    assert "No existing AAPL position to sell" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_allows_sell_with_existing_position():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000, "cash": 25000, "buying_power": 25000},
        positions=[{"symbol": "AAPL", "quantity": 50, "market_value": 5000}],
        market_data={"prices": {"AAPL": {"last_price": 100}}},
    )

    result = broker.execute_order(approved_decision(action="SELL"))

    assert result["executed"] is True
    assert fake_broker.submitted_orders[0].action == "SELL"


def test_dry_run_blocks_before_position_guard():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(dry_run=True), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(action="SELL"))

    assert result["executed"] is False
    assert "DRY_RUN" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_quantity_calculation_works():
    broker = broker_with_snapshot(make_settings(), price=100)

    quantity = broker._calculate_quantity(approved_decision(suggested_allocation_percent=5), 100)

    assert quantity == 50


def test_broker_zero_quantity_is_rejected():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=1000000, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(suggested_allocation_percent=1))

    assert result["executed"] is False
    assert "quantity is 0" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_missing_price_is_rejected():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), fake_broker=fake_broker)
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000},
        positions=[],
        market_data={"prices": {}},
    )

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert "price" in result["reason"]
    assert fake_broker.submitted_orders == []


def test_broker_buy_calls_mocked_lumibot_execution():
    fake_broker = MockLumibotBroker()
    created_strategies = []

    def strategy_factory(broker, name):
        strategy = MockExecutionStrategy(broker, name)
        created_strategies.append(strategy)
        return strategy

    broker = broker_with_snapshot(
        make_settings(),
        price=100,
        fake_broker=fake_broker,
        execution_strategy_factory=strategy_factory,
    )

    result = broker.execute_order(approved_decision(action="BUY"))

    assert result["executed"] is True
    assert result["broker_order_id"] == "paper-123"
    assert result["quantity"] == 50
    assert created_strategies[0].created_orders[0].action == "BUY"
    assert created_strategies[0].submitted_orders[0].strategy == created_strategies[0].name
    assert fake_broker.submitted_orders[0].action == "BUY"
    assert fake_broker.submitted_orders[0].strategy is not None


def test_broker_sell_calls_mocked_lumibot_execution():
    fake_broker = MockLumibotBroker()
    created_strategies = []

    def strategy_factory(broker, name):
        strategy = MockExecutionStrategy(broker, name)
        created_strategies.append(strategy)
        return strategy

    broker = broker_with_snapshot(
        make_settings(),
        price=100,
        fake_broker=fake_broker,
        execution_strategy_factory=strategy_factory,
    )
    broker._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 100000, "cash": 25000, "buying_power": 25000},
        positions=[{"symbol": "AAPL", "quantity": 50, "market_value": 5000}],
        market_data={"prices": {"AAPL": {"last_price": 100}}},
    )

    result = broker.execute_order(approved_decision(action="SELL"))

    assert result["executed"] is True
    assert result["broker_order_id"] == "paper-123"
    assert created_strategies[0].created_orders[0].action == "SELL"
    assert created_strategies[0].submitted_orders[0].strategy == created_strategies[0].name
    assert fake_broker.submitted_orders[0].action == "SELL"
    assert fake_broker.submitted_orders[0].strategy is not None


def test_execution_order_is_not_strategy_none_for_fill_event_routing():
    fake_broker = MockLumibotBroker()
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision(action="BUY"))

    assert result["executed"] is True
    assert fake_broker.submitted_orders[0].strategy == "openai_trading_bot_executor"


def test_broker_lumibot_exception_is_caught_safely():
    fake_broker = MockLumibotBroker(should_raise=True)
    broker = broker_with_snapshot(make_settings(), price=100, fake_broker=fake_broker)

    result = broker.execute_order(approved_decision())

    assert result["executed"] is False
    assert result["reason"] == "Broker order submission failed."
    assert result["raw_status"] == "RuntimeError"


def test_invalid_or_missing_openai_decision_falls_back_to_hold():
    class FailingAIClient:
        def get_decision(self, context):
            raise AIDecisionError("missing response")

    strategy = TradingStrategy(
        settings=make_settings(),
        broker=None,
        risk_manager=RiskManager(make_settings()),
    )
    strategy.ai_client = FailingAIClient()
    snapshot = BrokerSnapshot(
        account={"cash": 1000, "buying_power": 1000, "portfolio_value": 1000},
        positions=[],
        market_data={},
    )

    decision = strategy.get_ai_decision(snapshot)

    assert decision["action"] == "HOLD"
    assert decision["suggested_allocation_percent"] == 0


def test_rejected_ai_decision_does_not_reach_broker_execution(tmp_path):
    database.init_database(tmp_path / "trading_bot.db")

    class StubBroker:
        def __init__(self):
            self.execute_called = False

        def collect_snapshot(self):
            return BrokerSnapshot(
                account={"cash": 1000, "buying_power": 1000, "portfolio_value": 1000},
                positions=[],
                market_data={},
            )

        def execute_order(self, approved_decision):
            self.execute_called = True
            return {"executed": True}

    class HoldStrategy(TradingStrategy):
        def get_ai_decision(self, snapshot):
            return {
                "symbol": "CASH",
                "action": "HOLD",
                "confidence": 0,
                "suggested_allocation_percent": 0,
                "reason": "No trade.",
            }

    broker = StubBroker()
    strategy = HoldStrategy(
        settings=make_settings(),
        broker=broker,
        risk_manager=RiskManager(make_settings()),
    )

    strategy.run_cycle()

    assert broker.execute_called is False


def test_scheduler_identifies_closed_market_so_openai_can_be_skipped():
    scheduler = MarketScheduler(make_settings())
    sunday = datetime(2026, 7, 5, 12, 0, tzinfo=ZoneInfo("America/New_York"))

    assert scheduler.is_market_open(sunday) is False


def test_scheduler_identifies_open_market_window():
    scheduler = MarketScheduler(make_settings())
    monday_open = datetime(2026, 7, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    assert scheduler.is_market_open(monday_open) is True


def test_discord_summary_formatting_includes_required_sections(tmp_path):
    notifier = DailySummaryNotifier(
        journal=TradingJournal(tmp_path),
        discord_notifier=None,
        enabled=False,
        dry_run=True,
    )

    message = notifier.format_summary(date(2026, 7, 6), notifier._mock_day_data())

    assert "Daily Trading Summary - 2026-07-06" in message
    assert "Starting Balance" in message
    assert "Trades Completed" in message
    assert "AI Decisions" in message
    assert "Rejected Trades" in message


def test_discord_send_failure_returns_false_without_raising(monkeypatch):
    class FailingRequests:
        @staticmethod
        def post(*args, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setitem(__import__("sys").modules, "requests", FailingRequests)
    notifier = DiscordNotifier("https://discord.com/api/webhooks/test")

    assert notifier.send_message("test") is False


class FakeDiscordNotifier:
    def __init__(self):
        self.messages = []

    def send_message(self, content: str) -> bool:
        self.messages.append(content)
        return True


def test_duplicate_daily_summary_is_prevented(tmp_path):
    fake_discord = FakeDiscordNotifier()
    notifier = DailySummaryNotifier(
        journal=TradingJournal(tmp_path),
        discord_notifier=fake_discord,
        enabled=True,
    )
    trading_day = date(2026, 7, 6)

    first = notifier.send_daily_summary(trading_day)
    second = notifier.send_daily_summary(trading_day)

    assert first.sent is True
    assert second.skipped is True
    assert len(fake_discord.messages) == 1
