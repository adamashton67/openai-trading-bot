"""Offline coverage for deterministic five-minute position management."""

from __future__ import annotations

import json
import sqlite3
import types
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

import database
from position_manager import PositionManager
from scheduler import MarketScheduler


@pytest.fixture(autouse=True)
def isolated_database(tmp_path):
    database.init_database(tmp_path / "positions.db")


def settings(**overrides):
    values = {
        "market_timezone": "America/New_York",
        "trading_interval_seconds": 900,
        "position_management_interval_seconds": 300,
        "position_management_enabled": True,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


class FakeScheduler:
    """Stand-in MarketScheduler with a market close a fixed offset from `now`.

    Defaults to a close far enough in the future that ordinary tests never
    cross the EOD-flatten threshold; EOD-flatten tests pass a small
    `close_offset_minutes` to place `now` on either side of the deadline.
    """

    def __init__(self, close_offset_minutes=360, no_session=False):
        self.close_offset_minutes = close_offset_minutes
        self.no_session = no_session

    def market_hours(self, now):
        if self.no_session:
            return None
        market_open = now - timedelta(hours=1)
        market_close = now + timedelta(minutes=self.close_offset_minutes)
        return market_open, market_close


def make_manager(broker, scheduler=None):
    return PositionManager(settings(), broker, scheduler or FakeScheduler())


class FakeBroker:
    def __init__(self, positions=None, prices=None, *, dry_run=False, cancel_succeeds=True):
        self.positions = positions or []
        self.prices = prices or {}
        self.dry_run = dry_run
        self.submissions = []
        self.reconciliations = 0
        self.next_status = "accepted"
        self.price_errors = set()
        self.cancel_succeeds = cancel_succeeds
        self.cancelled_order_ids = []

    def reconcile_executions(self):
        self.reconciliations += 1

    def refresh_open_positions(self):
        return [dict(position) for position in self.positions]

    def get_current_price(self, symbol):
        if symbol in self.price_errors:
            raise RuntimeError("expected data failure")
        return self.prices.get(symbol)

    def cancel_position_management_order(self, order_id):
        self.cancelled_order_ids.append(order_id)
        return self.cancel_succeeds

    def execute_position_management_sell(
        self, symbol, quantity, *, observed_price, cost_basis_per_share, exit_source, exit_reason
    ):
        if self.dry_run:
            return {
                "executed": False,
                "symbol": symbol,
                "action": "SELL",
                "quantity": quantity,
                "reason": "DRY_RUN is true. No order placed.",
                "exit_source": exit_source,
                "exit_reason": exit_reason,
            }
        order_id = f"order-{len(self.submissions) + 1}"
        result = {
            "executed": True,
            "symbol": symbol,
            "action": "SELL",
            "quantity": quantity,
            "broker_order_id": order_id,
            "broker_status": self.next_status,
            "submitted_price": observed_price,
            "cost_basis_per_share": cost_basis_per_share,
            "exit_source": exit_source,
            "exit_reason": exit_reason,
        }
        if self.next_status == "filled":
            result.update(filled_quantity=quantity, average_fill_price=observed_price)
        self.submissions.append(result)
        return result


def position(symbol="AAPL", quantity=10, cost=100):
    return {"symbol": symbol, "quantity": quantity, "average_price": cost}


def state(symbol="AAPL"):
    rows = database.load_active_position_management(symbol)
    return rows[-1] if rows else None


def age_position(symbol, hours):
    past = datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=hours)
    database.update_position_management(symbol, opened_at=past.isoformat())


def test_position_run_never_calls_openai_or_broad_scanner():
    broker = FakeBroker([position()], {"AAPL": 102})
    broker.get_ai_decision = lambda: pytest.fail("OpenAI must not be called")
    broker.collect_snapshot = lambda: pytest.fail("broad scanner must not run")
    make_manager(broker).run_once()
    assert broker.submissions == []


def test_scheduler_position_cadence_is_five_minutes_and_ai_remains_fifteen():
    scheduler = MarketScheduler.__new__(MarketScheduler)
    scheduler.settings = settings()
    scheduler._next_trading_cycle = 0
    scheduler._next_position_management_cycle = 0
    scheduler.mark_position_management_run(100)
    scheduler.mark_trading_cycle_run(100)
    assert scheduler.position_management_due(399) is False
    assert scheduler.position_management_due(400) is True
    assert scheduler.trading_cycle_due(999) is False
    assert scheduler.trading_cycle_due(1000) is True


def test_scheduler_keeps_original_cadence_when_cycle_finishes_late():
    scheduler = MarketScheduler.__new__(MarketScheduler)
    scheduler.settings = settings()
    scheduler._next_trading_cycle = 100
    scheduler._next_position_management_cycle = 100

    scheduler.mark_position_management_run(140)
    scheduler.mark_trading_cycle_run(140)

    assert scheduler._next_position_management_cycle == 400
    assert scheduler._next_trading_cycle == 1000


@pytest.mark.parametrize("price, expected", [(102.99, 0), (103.00, 1)])
def test_partial_profit_boundary(price, expected):
    broker = FakeBroker([position()], {"AAPL": price})
    make_manager(broker).run_once()
    assert len(broker.submissions) == expected
    if expected:
        assert broker.submissions[0]["quantity"] == 5
        assert broker.submissions[0]["exit_reason"] == "PARTIAL_PROFIT_3_PERCENT"


def test_partial_order_is_not_repeated_on_later_checks_or_restart():
    broker = FakeBroker([position()], {"AAPL": 103})
    make_manager(broker).run_once()
    make_manager(broker).run_once()
    assert len(broker.submissions) == 1
    assert state()["partial_profit_order_id"] == "order-1"


def test_partial_fill_does_not_activate_trailing_until_fully_filled():
    broker = FakeBroker([position()], {"AAPL": 103})
    manager = make_manager(broker)
    manager.run_once()
    execution = database.load_execution_by_order_id("order-1")
    database.reconcile_execution(
        execution["id"],
        {"broker_status": "partially_filled", "filled_quantity": 2, "average_fill_price": 103},
    )
    broker.positions[0]["quantity"] = 8
    manager.run_once()
    assert state()["partial_profit_taken"] == 0
    assert state()["trailing_stop_activated"] == 0

    database.reconcile_execution(
        execution["id"],
        {"broker_status": "filled", "filled_quantity": 5, "average_fill_price": 103},
    )
    broker.positions[0]["quantity"] = 5
    manager.run_once()
    assert state()["partial_profit_taken"] == 1
    assert state()["trailing_stop_activated"] == 1
    assert state()["trailing_high_price"] == pytest.approx(103)


def activate_trailing(broker):
    broker.next_status = "filled"
    make_manager(broker).run_once()
    broker.positions[0]["quantity"] = 5
    broker.next_status = "accepted"


def test_trailing_high_rises_and_never_decreases():
    broker = FakeBroker([position()], {"AAPL": 103})
    activate_trailing(broker)
    broker.prices["AAPL"] = 110
    make_manager(broker).run_once()
    assert state()["trailing_high_price"] == pytest.approx(110)
    broker.prices["AAPL"] = 109
    make_manager(broker).run_once()
    assert state()["trailing_high_price"] == pytest.approx(110)


@pytest.mark.parametrize("pullback_price, expected_orders", [(107.811, 1), (107.8, 2)])
def test_trailing_stop_boundary(pullback_price, expected_orders):
    broker = FakeBroker([position()], {"AAPL": 103})
    activate_trailing(broker)
    broker.prices["AAPL"] = 110
    make_manager(broker).run_once()
    broker.prices["AAPL"] = pullback_price
    make_manager(broker).run_once()
    assert len(broker.submissions) == expected_orders
    if expected_orders == 2:
        assert broker.submissions[-1]["quantity"] == 5
        assert broker.submissions[-1]["exit_reason"] == "TRAILING_STOP_2_PERCENT"


def test_one_share_position_uses_safe_trailing_fallback_not_partial_close():
    broker = FakeBroker([position(quantity=1)], {"AAPL": 103})
    make_manager(broker).run_once()
    assert broker.submissions == []
    assert state()["trailing_stop_activated"] == 1
    assert state()["partial_profit_taken"] == 0


def test_whole_share_rounding_is_floor_half_original_and_capped():
    assert PositionManager.partial_quantity(Decimal("3"), Decimal("3")) == Decimal("1")
    assert PositionManager.partial_quantity(Decimal("10"), Decimal("4")) == Decimal("0")
    assert PositionManager.partial_quantity(Decimal("2.5"), Decimal("2.5")) == Decimal("1.250000")


def test_dry_run_and_missing_price_submit_no_orders():
    dry_broker = FakeBroker([position()], {"AAPL": 103}, dry_run=True)
    make_manager(dry_broker).run_once()
    assert dry_broker.submissions == []

    missing = FakeBroker([position()], {})
    result = make_manager(missing).run_once()
    assert result["skipped"] == 1
    assert missing.submissions == []


def test_one_symbol_failure_does_not_stop_other_positions():
    broker = FakeBroker(
        [position("BAD"), position("AAPL")], {"BAD": 103, "AAPL": 103}
    )
    broker.price_errors.add("BAD")
    result = make_manager(broker).run_once()
    assert result["checked"] == 1
    assert [row["symbol"] for row in broker.submissions] == ["AAPL"]


def test_legacy_position_is_adopted_with_explicit_conservative_metadata():
    broker = FakeBroker([position(quantity=7)], {"AAPL": 100})
    make_manager(broker).run_once()
    adopted = state()
    metadata = json.loads(adopted["raw_metadata"])
    assert adopted["current_quantity"] == 7
    assert metadata["adopted_legacy_position"] is True
    assert metadata["historical_original_unknown"] is True


def test_closed_broker_position_closes_stale_state():
    broker = FakeBroker([position()], {"AAPL": 100})
    manager = make_manager(broker)
    manager.run_once()
    broker.positions = []
    manager.run_once()
    assert database.load_active_position_management("AAPL") == []


def test_execution_records_source_reason_and_mechanical_realised_pl():
    broker = FakeBroker([position()], {"AAPL": 103})
    broker.next_status = "filled"
    make_manager(broker).run_once()
    report = database.load_daily_report_data(database.datetime.now().date())
    execution = report["executions"][0]
    assert execution["exit_source"] == "partial_profit"
    assert execution["exit_reason"] == "PARTIAL_PROFIT_3_PERCENT"
    assert execution["realised_pl"] == pytest.approx(15)
    assert report["exit_source_summary"] == [
        {
            "exit_source": "partial_profit",
            "execution_count": 1,
            "filled_count": 1,
            "realised_pl": pytest.approx(15),
        }
    ]


def test_schema_migration_is_idempotent_and_non_destructive(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE executions (id INTEGER PRIMARY KEY, timestamp TEXT, broker_order_id TEXT)"
        )
        connection.execute("INSERT INTO executions VALUES (1, 'legacy', NULL)")
    assert database.init_database(path)
    assert database.init_database(path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(executions)")}
        preserved = connection.execute("SELECT timestamp FROM executions WHERE id = 1").fetchone()[0]
        pm_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='position_management'"
        ).fetchone()
    assert {"exit_source", "exit_reason"}.issubset(columns)
    assert preserved == "legacy"
    assert pm_exists == (1,)


@pytest.mark.parametrize("price, expected", [(98.01, 0), (98.00, 1)])
def test_stop_loss_boundary(price, expected):
    broker = FakeBroker([position()], {"AAPL": price})
    make_manager(broker).run_once()
    assert len(broker.submissions) == expected
    if expected:
        assert broker.submissions[0]["quantity"] == 10
        assert broker.submissions[0]["exit_source"] == "stop_loss"
        assert broker.submissions[0]["exit_reason"] == "STOP_LOSS_2_PERCENT"
        assert state()["status"] == "stop_loss_submitted"


def test_time_stop_closes_dead_zone_position_after_two_hours():
    broker = FakeBroker([position()], {"AAPL": 100.5})
    make_manager(broker).run_once()
    age_position("AAPL", hours=2)
    make_manager(broker).run_once()
    assert len(broker.submissions) == 1
    assert broker.submissions[0]["quantity"] == 10
    assert broker.submissions[0]["exit_source"] == "time_stop"
    assert broker.submissions[0]["exit_reason"] == "TIME_STOP_DEAD_ZONE_2H"
    assert state()["status"] == "time_stop_submitted"


def test_time_stop_does_not_fire_outside_band_or_before_two_hours():
    outside_band = FakeBroker([position("AAPL")], {"AAPL": 102})
    make_manager(outside_band).run_once()
    age_position("AAPL", hours=2)
    make_manager(outside_band).run_once()
    assert outside_band.submissions == []

    too_young = FakeBroker([position("MSFT", cost=50)], {"MSFT": 50.2})
    make_manager(too_young).run_once()
    age_position("MSFT", hours=1)
    make_manager(too_young).run_once()
    assert too_young.submissions == []


def test_trailing_position_is_not_stopped_out_by_stop_loss_or_time_stop():
    broker = FakeBroker([position()], {"AAPL": 103})
    activate_trailing(broker)
    submissions_before = len(broker.submissions)
    age_position("AAPL", hours=3)
    broker.prices["AAPL"] = 101
    make_manager(broker).run_once()
    assert len(broker.submissions) == submissions_before
    assert state()["status"] == "trailing_active"
    assert state()["trailing_stop_activated"] == 1


def test_eod_flatten_overrides_stop_loss_time_stop_and_trailing_states():
    broker = FakeBroker(
        [position("AAPL"), position("MSFT", quantity=5, cost=50)],
        {"AAPL": 103, "MSFT": 50},
    )
    activate_trailing(broker)
    submissions_before = len(broker.submissions)
    broker.prices["AAPL"] = 105
    broker.prices["MSFT"] = 50

    scheduler = FakeScheduler(close_offset_minutes=10)
    make_manager(broker, scheduler).run_once()

    new_submissions = broker.submissions[submissions_before:]
    assert len(new_submissions) == 2
    assert {row["exit_source"] for row in new_submissions} == {"eod_flatten"}
    assert {row["exit_reason"] for row in new_submissions} == {"EOD_FLATTEN"}
    assert state("AAPL")["status"] == "eod_flatten_submitted"
    assert state("MSFT")["status"] == "eod_flatten_submitted"


def test_eod_flatten_does_not_fire_before_the_deadline():
    broker = FakeBroker([position()], {"AAPL": 100.5})
    scheduler = FakeScheduler(close_offset_minutes=31)
    make_manager(broker, scheduler).run_once()
    assert broker.submissions == []
    assert state()["status"] == "open"


def test_eod_flatten_fires_thirty_minutes_before_close():
    broker = FakeBroker([position()], {"AAPL": 100.5})
    scheduler = FakeScheduler(close_offset_minutes=30)
    make_manager(broker, scheduler).run_once()
    assert len(broker.submissions) == 1
    assert broker.submissions[0]["exit_source"] == "eod_flatten"
    assert state()["status"] == "eod_flatten_submitted"


def test_eod_flatten_skips_safely_when_no_market_session_today():
    broker = FakeBroker([position()], {"AAPL": 100.5})
    scheduler = FakeScheduler(no_session=True)
    make_manager(broker, scheduler).run_once()
    assert broker.submissions == []


def test_stop_loss_order_is_not_resubmitted_while_pending_reconciliation():
    broker = FakeBroker([position()], {"AAPL": 98.0})
    make_manager(broker).run_once()
    assert len(broker.submissions) == 1
    assert state()["final_exit_order_id"] == "order-1"
    assert state()["status"] == "stop_loss_submitted"

    make_manager(broker).run_once()
    assert len(broker.submissions) == 1
    assert state()["status"] == "stop_loss_submitted"


def test_time_stop_order_is_not_resubmitted_while_pending_reconciliation():
    broker = FakeBroker([position()], {"AAPL": 100.5})
    make_manager(broker).run_once()
    age_position("AAPL", hours=2)
    make_manager(broker).run_once()
    assert len(broker.submissions) == 1
    assert state()["final_exit_order_id"] == "order-1"

    make_manager(broker).run_once()
    assert len(broker.submissions) == 1
    assert state()["status"] == "time_stop_submitted"


def test_eod_flatten_cancels_pending_partial_profit_order_before_flattening():
    broker = FakeBroker([position()], {"AAPL": 103})
    make_manager(broker).run_once()
    assert state()["partial_profit_order_id"] == "order-1"
    assert state()["partial_profit_taken"] == 0
    assert len(broker.submissions) == 1

    scheduler = FakeScheduler(close_offset_minutes=10)
    make_manager(broker, scheduler).run_once()

    assert broker.cancelled_order_ids == ["order-1"]
    assert len(broker.submissions) == 2
    flatten_order = broker.submissions[-1]
    assert flatten_order["exit_source"] == "eod_flatten"
    assert flatten_order["exit_reason"] == "EOD_FLATTEN"
    assert flatten_order["quantity"] == 10
    assert state()["status"] == "eod_flatten_submitted"


def test_eod_flatten_proceeds_when_broker_has_no_cancel_support():
    class NoCancelBroker(FakeBroker):
        cancel_position_management_order = None

    broker = NoCancelBroker([position()], {"AAPL": 103})
    make_manager(broker).run_once()
    assert state()["partial_profit_order_id"] == "order-1"

    scheduler = FakeScheduler(close_offset_minutes=10)
    make_manager(broker, scheduler).run_once()

    assert len(broker.submissions) == 2
    assert broker.submissions[-1]["exit_source"] == "eod_flatten"
    assert broker.submissions[-1]["quantity"] == 10
