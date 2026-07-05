"""Broker integration layer for Lumibot and Alpaca Paper Trading."""

import logging
from dataclasses import dataclass
from typing import Any

from config import Settings


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerSnapshot:
    """Account, position, and market context passed into the strategy."""

    account: dict[str, Any]
    positions: list[dict[str, Any]]
    market_data: dict[str, Any]


class BrokerClient:
    """Small broker facade that can later be swapped from Alpaca to IBKR."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._broker = None

    def connect(self) -> None:
        """Initialize the broker connection.

        TODO: Wire Lumibot's Alpaca broker implementation here once execution
        behaviour is ready to be tested.
        TODO: Replace or extend broker implementation when moving from Alpaca to IBKR.
        """
        if not self.settings.paper_trading:
            logger.warning("PAPER_TRADING is false. Live trading is not implemented.")
            return

        logger.info("Broker configured for Alpaca Paper Trading.")

    def collect_snapshot(self) -> BrokerSnapshot:
        """Collect account, position, and market data for the trading cycle."""
        logger.info("Collecting broker/account/position/market data.")

        # TODO: Pull real account data from Lumibot/Alpaca.
        account = {
            "broker": "alpaca",
            "paper_trading": self.settings.paper_trading,
            "cash": None,
            "buying_power": None,
            "portfolio_value": None,
        }

        # TODO: Pull current positions from Lumibot/Alpaca.
        positions: list[dict[str, Any]] = []

        # TODO: Pull market data for allowed symbols.
        market_data = {
            "symbols": self.settings.allowed_symbols,
            "prices": {},
        }

        return BrokerSnapshot(account=account, positions=positions, market_data=market_data)

    def execute_order(self, approved_decision: dict[str, Any]) -> dict[str, Any]:
        """Execute an approved trade through Lumibot.

        The safe starter default records the intended action without placing a
        real order. Remove this guard only after paper-trading execution is
        explicitly implemented and tested.
        """
        logger.info("Approved decision received for execution: %s", approved_decision)
        logger.warning("Order execution is disabled in the base project scaffold.")

        # TODO: Execute paper trades through Lumibot after risk rules are complete.
        return {
            "executed": False,
            "reason": "Execution placeholder only. No order was placed.",
            "decision": approved_decision,
        }
