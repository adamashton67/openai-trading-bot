"""Safe execution-path test utilities."""

from __future__ import annotations

import logging
from dataclasses import replace

from broker import BrokerClient, BrokerSnapshot
from config import Settings


logger = logging.getLogger(__name__)


def run_execution_test(settings: Settings) -> int:
    """Run a fake execution test without submitting any real order."""
    test_settings = replace(settings, bot_enabled=True, paper_trading=True, dry_run=True)
    broker = BrokerClient(test_settings)
    broker._last_snapshot = BrokerSnapshot(
        account={
            "broker": "alpaca",
            "paper_trading": True,
            "cash": 25000,
            "buying_power": 25000,
            "portfolio_value": 100000,
        },
        positions=[],
        market_data={
            "symbols": ["AAPL", "MSFT", "SPY"],
            "prices": {
                "AAPL": {"last_price": 214.33},
                "MSFT": {"last_price": 434.80},
                "SPY": {"last_price": 550.25},
            },
        },
    )
    decision = {
        "symbol": "AAPL",
        "action": "BUY",
        "confidence": 0.85,
        "suggested_allocation_percent": 5,
        "reason": "Safe execution test decision.",
    }

    logger.info("Starting safe execution-path test in DRY_RUN mode.")
    result = broker.execute_order(decision)
    logger.info("Execution test result: %s", result)
    logger.info("No real broker order was submitted.")
    return 0 if result.get("executed") is False else 1
