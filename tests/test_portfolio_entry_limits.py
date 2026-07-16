"""Portfolio BUY-limit coverage with all broker interactions mocked."""

from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import database
from broker import BrokerClient, BrokerSnapshot
from config import load_settings
from storage import TradingJournal
from strategy import TradingStrategy


def _position(symbol: str, value: float, price: float = 100):
    return types.SimpleNamespace(
        symbol=symbol,
        qty=str(value / price),
        market_value=str(value),
        avg_entry_price=str(price),
    )


def _pending_buy(symbol: str, *, notional: float | None = None, qty: float | None = None):
    return types.SimpleNamespace(
        id=f"pending-{symbol}",
        symbol=symbol,
        side="buy",
        status="accepted",
        notional=str(notional) if notional is not None else None,
        qty=str(qty) if qty is not None else None,
        filled_qty="0",
    )


class _Api:
    def __init__(self, portfolio_value=100_000, positions=None, orders=None):
        self.portfolio_value = portfolio_value
        self.positions = list(positions or [])
        self.orders = list(orders or [])
        self.calls = []

    def get_account(self):
        self.calls.append("account")
        value = None if self.portfolio_value is None else str(self.portfolio_value)
        return types.SimpleNamespace(portfolio_value=value, equity=value)

    def get_all_positions(self):
        self.calls.append("positions")
        return self.positions

    def get_orders(self, status="open"):
        self.calls.append("orders")
        return self.orders


class _Broker:
    def __init__(self, api):
        self.api = api
        self.submitted_orders = []

    def submit_order(self, order):
        self.submitted_orders.append(order)
        return types.SimpleNamespace(identifier="paper-buy", status="accepted")


def _settings(**overrides):
    values = {
        "bot_enabled": True,
        "paper_trading": True,
        "dry_run": False,
        "dynamic_watchlist_enabled": False,
        "allowed_symbols": [
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
            "AMD", "NFLX", "AVGO", "PLTR", "TTD",
        ],
        "max_position_allocation_percent": 5,
        "max_open_positions": 10,
        "max_total_invested_percent": 60,
        "alpaca_api_key": "test",
        "alpaca_secret_key": "test",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _client(*, positions=None, orders=None, portfolio_value=100_000, settings=None):
    api = _Api(portfolio_value, positions, orders)
    fake_broker = _Broker(api)
    client = BrokerClient(
        settings or _settings(),
        order_factory=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    client._broker = fake_broker
    symbols = (settings or client.settings).allowed_symbols
    client._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": portfolio_value},
        positions=[],
        market_data={
            "prices": {symbol: {"last_price": 100} for symbol in symbols}
        },
    )
    return client, fake_broker, api


def _buy(symbol="AAPL", allocation=5):
    return {
        "symbol": symbol,
        "action": "BUY",
        "confidence": 0.9,
        "suggested_allocation_percent": allocation,
        "reason": "test",
    }


def _symbols(count: int):
    names = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "NFLX", "AVGO"]
    return [_position(symbol, 1_000) for symbol in names[:count]]


def test_new_buy_allowed_below_both_limits():
    client, broker, _ = _client(positions=[_position("MSFT", 50_000)])
    result = client.execute_order(_buy())
    assert result["executed"] is True
    assert broker.submitted_orders


def test_new_symbol_rejected_at_max_open_positions():
    client, broker, _ = _client(positions=_symbols(10))
    result = client.execute_order(_buy("PLTR", 1))
    assert result["executed"] is False
    assert result["reason"] == "Maximum open positions reached: 10/10."
    assert broker.submitted_orders == []


def test_existing_position_does_not_increase_distinct_count():
    client, broker, _ = _client(positions=_symbols(10))
    result = client.execute_order(_buy("AAPL", 1))
    assert result["executed"] is True
    assert broker.submitted_orders


def test_existing_position_still_obeys_per_symbol_limit():
    client, broker, _ = _client(positions=[_position("AAPL", 4_000)])
    result = client.execute_order(_buy("AAPL", 2))
    assert result["executed"] is False
    assert "Projected AAPL allocation exceeds maximum" in result["reason"]
    assert broker.submitted_orders == []


def test_projected_invested_percent_exactly_at_limit_is_allowed():
    client, broker, _ = _client(positions=[_position("MSFT", 55_000)])
    result = client.execute_order(_buy("AAPL", 5))
    assert result["executed"] is True
    assert broker.submitted_orders


@pytest.mark.parametrize("held_value", [55_001, 61_000])
def test_projected_or_current_investment_above_limit_rejects_buy(held_value):
    client, broker, _ = _client(positions=[_position("MSFT", held_value)])
    result = client.execute_order(_buy("AAPL", 5))
    assert result["executed"] is False
    assert "exceeds maximum 60.0%" in result["reason"]
    assert broker.submitted_orders == []


def test_portfolio_above_limits_does_not_block_sell():
    client, broker, _ = _client(positions=[_position("AAPL", 70_000)])
    result = client.execute_order({**_buy("AAPL"), "action": "SELL"})
    assert result["executed"] is True
    assert broker.submitted_orders[0].action == "SELL"


def test_mechanical_trailing_or_partial_exit_bypasses_buy_limits():
    settings = _settings(max_open_positions=1, max_total_invested_percent=1)
    client, broker, _ = _client(
        positions=[_position("AAPL", 70_000)],
        settings=settings,
    )
    for source in ("partial_profit", "trailing_stop"):
        result = client.execute_position_management_sell(
            "AAPL",
            1,
            observed_price=110,
            cost_basis_per_share=100,
            exit_source=source,
            exit_reason=source.upper(),
        )
        assert result["executed"] is True
    assert len(broker.submitted_orders) == 2


def test_pending_buys_count_towards_projected_exposure():
    client, broker, _ = _client(
        positions=[_position("MSFT", 51_000)],
        orders=[_pending_buy("NVDA", notional=5_000)],
    )
    result = client.execute_order(_buy("AAPL", 5))
    assert result["executed"] is False
    assert result["pending_buy_value"] == pytest.approx(5_000)
    assert result["projected_invested_percent"] == pytest.approx(61)
    assert broker.submitted_orders == []


def test_pending_buy_for_held_symbol_does_not_add_distinct_position():
    positions = _symbols(9)
    client, broker, _ = _client(
        positions=positions,
        orders=[_pending_buy("AAPL", notional=1_000)],
    )
    result = client.execute_order(_buy("PLTR", 1))
    assert result["executed"] is True
    assert broker.submitted_orders


def test_unknown_pending_buy_value_uses_conservative_full_symbol_cap():
    client, _, _ = _client(
        positions=[_position("MSFT", 51_000)],
        orders=[_pending_buy("NVDA")],
    )
    result = client.execute_order(_buy("AAPL", 5))
    assert result["executed"] is False
    assert result["pending_buy_value"] == pytest.approx(5_000)


@pytest.mark.parametrize("portfolio_value", [None, 0, -1])
def test_missing_or_invalid_portfolio_value_fails_safely(portfolio_value):
    client, broker, _ = _client(portfolio_value=portfolio_value)
    result = client.execute_order(_buy())
    assert result["executed"] is False
    assert "missing or non-positive" in result["reason"]
    assert broker.submitted_orders == []


def test_dry_run_blocks_before_refresh_or_submission():
    client, broker, api = _client(settings=_settings(dry_run=True))
    result = client.execute_order(_buy())
    assert result["executed"] is False
    assert "DRY_RUN" in result["reason"]
    assert api.calls == []
    assert broker.submitted_orders == []


def test_portfolio_rejection_is_persisted_and_counted_for_daily_reporting(tmp_path):
    database.init_database(tmp_path / "trading.db")
    client, _, _ = _client(positions=_symbols(10))
    snapshot = client._last_snapshot
    client.collect_snapshot = lambda: snapshot
    settings = types.SimpleNamespace(
        **vars(client.settings),
        market_timezone="America/New_York",
        include_history_context=False,
        min_confidence=0.7,
    )
    journal = TradingJournal(tmp_path / "journal")
    strategy = TradingStrategy(
        settings=settings,
        broker=client,
        risk_manager=types.SimpleNamespace(validate=lambda decision: (True, "approved")),
        journal=journal,
    )
    strategy.ai_client = types.SimpleNamespace(
        last_raw_response='{"symbol":"PLTR","action":"BUY"}',
        get_decision=lambda context: types.SimpleNamespace(
            to_risk_manager_dict=lambda: _buy("PLTR", 1)
        ),
    )

    strategy.run_cycle()

    trading_day = datetime.now(ZoneInfo("America/New_York")).date()
    rejected = journal.load_day(trading_day)["rejected_trades"]
    report = database.load_daily_report_data(trading_day)
    assert rejected[-1]["reason"] == "Maximum open positions reached: 10/10."
    assert report["stats"]["risk_rejected_count"] == 1


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("MAX_OPEN_POSITIONS", "0", "at least 1"),
        ("MAX_TOTAL_INVESTED_PERCENT", "0", "greater than 0"),
        ("MAX_TOTAL_INVESTED_PERCENT", "101", "no more than 100"),
    ],
)
def test_invalid_portfolio_limit_configuration_is_rejected(monkeypatch, name, value, message):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "10")
    monkeypatch.setenv("MAX_TOTAL_INVESTED_PERCENT", "60")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=message):
        load_settings()
