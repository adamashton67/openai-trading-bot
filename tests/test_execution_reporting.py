"""Fill accounting and defensive SELL execution coverage (all broker calls mocked)."""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import date, datetime

import pytest

import database
from broker import BrokerClient, BrokerSnapshot
from notifications.notifier import DailySummaryNotifier
from storage import TradingJournal
from strategy import TradingStrategy


TRADING_DAY = date(2026, 7, 16)


def _filled_sell(order_id: str, fill: float, quantity: float, cost: float) -> dict:
    return {
        "executed": True,
        "symbol": "TTD",
        "action": "SELL",
        "quantity": quantity,
        "filled_quantity": quantity,
        "average_fill_price": fill,
        "cost_basis_per_share": cost,
        "broker_order_id": order_id,
        "broker_status": "filled",
    }


@pytest.mark.parametrize(
    ("fill", "cost", "expected"),
    [(110, 100, 50), (90, 100, -50)],
)
def test_completed_sell_persists_profit_or_loss(tmp_path, fill, cost, expected):
    path = tmp_path / "trading.db"
    database.init_database(path)

    database.insert_execution(
        _filled_sell("order-result", fill, 5, cost),
        timestamp=datetime(2026, 7, 16, 14, 30),
    )

    report = database.load_daily_report_data(TRADING_DAY)
    assert report["stats"]["realised_pl"] == pytest.approx(expected)
    assert report["executions"][0]["realised_pl"] == pytest.approx(expected)
    if expected > 0:
        assert report["stats"]["largest_win"] == pytest.approx(expected)
        assert report["stats"]["winning_trades"] == 1
    else:
        assert report["stats"]["largest_loss"] == pytest.approx(expected)
        assert report["stats"]["losing_trades"] == 1


def test_partial_sell_accounts_only_cumulative_filled_quantity(tmp_path):
    database.init_database(tmp_path / "trading.db")
    execution_id = database.insert_execution(
        {
            **_filled_sell("partial-1", 112, 10, 100),
            "filled_quantity": 3,
            "broker_status": "partially_filled",
        },
        timestamp=datetime(2026, 7, 16, 14, 30),
    )

    assert execution_id is not None
    assert database.load_daily_report_data(TRADING_DAY)["stats"]["realised_pl"] == pytest.approx(36)


def test_submitted_unfilled_sell_has_no_realised_pl(tmp_path):
    database.init_database(tmp_path / "trading.db")
    database.insert_execution(
        {
            "executed": True,
            "symbol": "TTD",
            "action": "SELL",
            "quantity": 5,
            "cost_basis_per_share": 100,
            "broker_order_id": "pending-1",
            "broker_status": "submitted",
        },
        timestamp=datetime(2026, 7, 16, 14, 30),
    )
    report = database.load_daily_report_data(TRADING_DAY)
    assert report["stats"]["realised_pl"] == 0
    assert report["executions"][0]["realised_pl"] is None


def test_duplicate_reconciliation_does_not_double_count_and_survives_restart(tmp_path):
    path = tmp_path / "trading.db"
    database.init_database(path)
    execution_id = database.insert_execution(
        {
            "executed": True,
            "symbol": "TTD",
            "action": "SELL",
            "quantity": 4,
            "cost_basis_per_share": 100,
            "broker_order_id": "pending-2",
            "broker_status": "accepted",
        },
        timestamp=datetime(2026, 7, 16, 14, 30),
    )
    fill = {"broker_status": "filled", "filled_quantity": 4, "average_fill_price": 105}
    assert database.reconcile_execution(execution_id, fill)
    assert database.reconcile_execution(execution_id, fill)
    assert database.load_daily_report_data(TRADING_DAY)["stats"]["realised_pl"] == pytest.approx(20)

    database.init_database(path)
    assert database.load_daily_report_data(TRADING_DAY)["stats"]["realised_pl"] == pytest.approx(20)


def test_historical_repair_repairs_supported_row_and_skips_incomplete_row(tmp_path):
    path = tmp_path / "trading.db"
    database.init_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO executions
                (timestamp, symbol, side, quantity, filled_quantity, average_fill_price,
                 cost_basis_per_share, status, broker_status, broker_order_id, raw_response)
            VALUES (?, 'TTD', 'SELL', 2, 2, 120, 100, 'filled', 'filled', 'old-good', '{}')
            """,
            ("2026-07-16T10:00:00",),
        )
        connection.execute(
            """
            INSERT INTO executions
                (timestamp, symbol, side, quantity, status, broker_status, broker_order_id, raw_response)
            VALUES (?, 'TTD', 'SELL', 2, 'filled', 'filled', 'old-incomplete', '{}')
            """,
            ("2026-07-16T11:00:00",),
        )

    result = database.repair_historical_realised_pl()
    assert result == {"repaired": 1, "skipped": 0, "insufficient_data": 1}
    assert database.load_daily_report_data(TRADING_DAY)["stats"]["realised_pl"] == pytest.approx(40)


def test_legacy_schema_migration_is_idempotent_and_preserves_rows(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE executions (
                id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, side TEXT,
                quantity INTEGER, fill_price REAL, status TEXT, broker_order_id TEXT,
                raw_response TEXT
            );
            INSERT INTO executions VALUES (1, '2026-07-15T10:00:00', 'TTD', 'SELL', 1, NULL,
                                            'error', NULL, '{}');
            CREATE TABLE daily_statistics (date TEXT PRIMARY KEY, realised_pl REAL DEFAULT 0,
                                           updated_at TEXT);
            INSERT INTO daily_statistics(date, realised_pl) VALUES ('2026-07-15', 0);
            """
        )
    assert database.init_database(path)
    assert database.init_database(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        row_count = connection.execute("SELECT COUNT(*) FROM executions").fetchone()[0]
    assert {"filled_quantity", "average_fill_price", "realised_pl", "error_reason"} <= columns
    assert row_count == 1


class _FakeApi:
    def __init__(self, quantity=5, average_price=100, orders=None):
        self.quantity = quantity
        self.average_price = average_price
        self.orders = orders or []

    def get_all_positions(self):
        if self.quantity <= 0:
            return []
        return [types.SimpleNamespace(symbol="TTD", qty=str(self.quantity), avg_entry_price=str(self.average_price))]

    def get_orders(self, status="open"):
        return self.orders


class _FakeBroker:
    def __init__(self, api, rejection=None):
        self.api = api
        self.rejection = rejection
        self.submitted_orders = []

    def submit_order(self, order):
        self.submitted_orders.append(order)
        if self.rejection:
            raise ValueError(self.rejection)
        return types.SimpleNamespace(identifier="alpaca-1", status="accepted")


def _settings():
    return types.SimpleNamespace(
        bot_enabled=True,
        paper_trading=True,
        dry_run=False,
        dynamic_watchlist_enabled=False,
        allowed_symbols=["TTD"],
        max_position_allocation_percent=5,
        alpaca_api_key="safe-key",
        alpaca_secret_key="safe-secret",
    )


def _client(api, rejection=None, allocation=5):
    fake = _FakeBroker(api, rejection=rejection)
    client = BrokerClient(
        _settings(),
        order_factory=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    client._broker = fake
    client._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": 1000},
        positions=[{"symbol": "TTD", "quantity": api.quantity, "average_price": api.average_price}],
        market_data={"prices": {"TTD": {"last_price": 10}}},
    )
    decision = {
        "symbol": "TTD", "action": "SELL", "confidence": 0.9,
        "suggested_allocation_percent": allocation, "reason": "test",
    }
    return client, fake, decision


def test_sell_without_current_broker_position_is_rejected_before_submission():
    client, fake, decision = _client(_FakeApi(quantity=0))
    result = client.execute_order(decision)
    assert result["executed"] is False
    assert result["currently_held_quantity"] == 0
    assert fake.submitted_orders == []


def test_sell_quantity_is_capped_to_current_holdings():
    client, fake, decision = _client(_FakeApi(quantity=2))
    result = client.execute_order(decision)
    assert result["requested_quantity"] == 5
    assert result["quantity"] == 2
    assert fake.submitted_orders[0].quantity == 2


def test_covering_open_sell_prevents_duplicate_submission():
    open_sell = types.SimpleNamespace(
        id="open-1", symbol="TTD", side="sell", status="accepted", qty="5", filled_qty="0"
    )
    client, fake, decision = _client(_FakeApi(quantity=5, orders=[open_sell]))
    result = client.execute_order(decision)
    assert result["executed"] is False
    assert result["broker_order_id"] == "open-1"
    assert fake.submitted_orders == []


def test_alpaca_rejection_reason_and_context_are_persisted(tmp_path):
    path = tmp_path / "trading.db"
    database.init_database(path)
    client, _, decision = _client(_FakeApi(quantity=5), rejection="insufficient qty available")
    result = client.execute_order(decision)
    execution_id = database.insert_execution(result, timestamp=datetime(2026, 7, 16, 14, 30))
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT symbol, side, quantity, error_reason, raw_response FROM executions WHERE id = ?",
            (execution_id,),
        ).fetchone()
    raw = json.loads(row[4])
    assert row[:4] == ("TTD", "SELL", 5.0, "insufficient qty available")
    assert raw["currently_held_quantity"] == 5
    assert raw["exception_class"] == "ValueError"


def test_lumibot_missing_subscriber_is_not_treated_as_alpaca_failure():
    open_sell = types.SimpleNamespace(
        id="alpaca-recovered", symbol="TTD", side="sell", status="accepted", qty="5", filled_qty="0"
    )
    api = _FakeApi(quantity=5)
    calls = {"count": 0}

    def sequenced_orders(status="open"):
        calls["count"] += 1
        return [] if calls["count"] == 1 else [open_sell]

    api.get_orders = sequenced_orders
    client, _, decision = _client(
        api,
        rejection="Subscriber openai_trading_bot_executor not found",
    )
    result = client.execute_order(decision)
    assert result["executed"] is True
    assert result["broker_order_id"] == "alpaca-recovered"
    assert result["broker_status"] == "accepted"


def test_ttd_style_failed_sell_does_not_crash_trading_cycle(tmp_path):
    path = tmp_path / "trading.db"
    database.init_database(path)

    class CycleBroker:
        def collect_snapshot(self):
            return BrokerSnapshot(
                account={"portfolio_value": 1000, "cash": 500, "buying_power": 500},
                positions=[{"symbol": "TTD", "quantity": 5, "average_price": 100}],
                market_data={"symbols": ["TTD"], "prices": {"TTD": {"last_price": 100}}},
            )

        def execute_order(self, decision):
            return {
                "executed": False,
                "symbol": "TTD",
                "action": "SELL",
                "quantity": 5,
                "currently_held_quantity": 5,
                "raw_status": "rejected",
                "broker_status": "rejected",
                "error_reason": "insufficient qty available",
                "reason": "Alpaca rejected the order.",
            }

    settings = types.SimpleNamespace(
        market_timezone="America/New_York",
        include_history_context=False,
        dynamic_watchlist_enabled=False,
        allowed_symbols=["TTD"],
        paper_trading=True,
        min_confidence=0.7,
        max_position_allocation_percent=5,
    )
    strategy = TradingStrategy(
        settings=settings,
        broker=CycleBroker(),
        risk_manager=types.SimpleNamespace(validate=lambda decision: (True, "approved")),
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"TTD","action":"SELL"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: {
                "symbol": "TTD", "action": "SELL", "confidence": 0.9,
                "suggested_allocation_percent": 5, "reason": "test",
            }
        ),
    )

    strategy.run_cycle()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT symbol, side, broker_status, error_reason FROM executions"
        ).fetchone()
    assert row == ("TTD", "SELL", "rejected", "insufficient qty available")


def test_discord_summary_uses_persisted_realised_pl(tmp_path):
    database.init_database(tmp_path / "trading.db")
    database.insert_execution(
        _filled_sell("summary-1", 110, 2, 100),
        timestamp=datetime(2026, 7, 16, 14, 30),
    )
    journal = TradingJournal(tmp_path / "data")
    notifier = DailySummaryNotifier(journal, None, enabled=False)
    message = notifier.format_summary(
        TRADING_DAY,
        journal.load_day(TRADING_DAY),
        database.load_daily_report_data(TRADING_DAY),
    )
    assert "- Realised P/L: +$20.00" in message
