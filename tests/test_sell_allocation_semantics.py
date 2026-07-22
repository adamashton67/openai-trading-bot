"""SELL allocation semantics: target remaining allocation, 0 = full exit."""

from __future__ import annotations

import types

from broker import BrokerClient, BrokerSnapshot
from risk_manager import RiskManager


def _settings(**overrides):
    values = {
        "bot_enabled": True,
        "paper_trading": True,
        "dry_run": False,
        "dynamic_watchlist_enabled": False,
        "allowed_symbols": ["AAPL"],
        "min_confidence": 0.6,
        "max_position_allocation_percent": 5,
        "max_open_positions": 10,
        "max_total_invested_percent": 60,
        "alpaca_api_key": "test",
        "alpaca_secret_key": "test",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _decision(action="SELL", allocation=0.0, symbol="AAPL"):
    return {
        "symbol": symbol,
        "action": action,
        "confidence": 0.9,
        "suggested_allocation_percent": allocation,
        "reason": "test",
    }


def _broker_client(portfolio_value=100_000, last_price=100):
    client = BrokerClient(
        _settings(),
        order_factory=lambda **kwargs: types.SimpleNamespace(**kwargs),
    )
    client._last_snapshot = BrokerSnapshot(
        account={"portfolio_value": portfolio_value},
        positions=[],
        market_data={"prices": {"AAPL": {"last_price": last_price}}},
    )
    return client


def test_sell_quantity_targets_remaining_allocation():
    client = _broker_client()
    quantity = client._calculate_sell_quantity(_decision(allocation=5), 100, held_quantity=80)
    assert quantity == 30


def test_sell_quantity_with_zero_target_sells_entire_holding():
    client = _broker_client()
    quantity = client._calculate_sell_quantity(_decision(allocation=0), 100, held_quantity=80)
    assert quantity == 80


def test_sell_quantity_with_target_above_holding_is_zero():
    client = _broker_client()
    quantity = client._calculate_sell_quantity(_decision(allocation=5), 100, held_quantity=40)
    assert quantity == 0


def test_sell_quantity_with_missing_portfolio_value_fails_safely():
    client = _broker_client(portfolio_value=None)
    assert client._calculate_sell_quantity(_decision(allocation=5), 100, held_quantity=80) == 0
    assert client._calculate_sell_quantity(_decision(allocation=0), 100, held_quantity=80) == 80


def test_buy_quantity_calculation_is_unchanged():
    client = _broker_client()
    quantity = client._calculate_quantity(_decision(action="BUY", allocation=5), 100)
    assert quantity == 50


def test_risk_manager_accepts_sell_with_zero_allocation():
    approved, reason = RiskManager(_settings()).validate(_decision(action="SELL", allocation=0))
    assert approved is True, reason


def test_risk_manager_rejects_sell_with_negative_allocation():
    approved, reason = RiskManager(_settings()).validate(_decision(action="SELL", allocation=-1))
    assert approved is False
    assert "0 or greater" in reason


def test_risk_manager_still_rejects_buy_with_zero_allocation():
    approved, reason = RiskManager(_settings()).validate(_decision(action="BUY", allocation=0))
    assert approved is False
    assert "greater than 0" in reason


def test_risk_manager_still_rejects_sell_above_max_allocation():
    approved, reason = RiskManager(_settings()).validate(_decision(action="SELL", allocation=10))
    assert approved is False
    assert "exceeds maximum" in reason


def test_execution_guard_accepts_sell_with_zero_allocation():
    client = _broker_client()
    assert client._execution_guard_failure(_decision(action="SELL", allocation=0)) is None


def test_execution_guard_still_rejects_buy_with_zero_allocation():
    client = _broker_client()
    failure = client._execution_guard_failure(_decision(action="BUY", allocation=0))
    assert failure == "Suggested allocation must be greater than 0."
