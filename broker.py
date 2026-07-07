"""Broker integration layer for Lumibot and Alpaca Paper Trading."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import Settings
from market_indicators import calculate_market_indicators
from watchlist_scanner import DynamicWatchlistScanner


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BrokerSnapshot:
    """Account, position, and market context passed into the strategy."""

    account: dict[str, Any]
    positions: list[dict[str, Any]]
    market_data: dict[str, Any]


class LumibotExecutionStrategyAdapter:
    """Minimal strategy-owned order submission adapter for Lumibot brokers."""

    def __init__(
        self,
        broker: Any,
        name: str,
        order_factory: Any | None = None,
    ) -> None:
        self.broker = broker
        self.name = name
        self._order_factory = order_factory

    def create_order(self, symbol: str, action: str, quantity: int) -> Any:
        """Create a market order with this adapter as the strategy owner."""
        if self._order_factory is not None:
            return self._order_factory(
                symbol=symbol,
                action=action,
                quantity=quantity,
                strategy=self.name,
            )

        from lumibot.entities import Order

        return Order(
            strategy=self.name,
            asset=symbol,
            quantity=quantity,
            side=action.lower(),
            order_type="market",
            time_in_force="day",
        )

    def submit_order(self, order: Any) -> Any:
        """Submit through the broker while preserving a strategy-owned order."""
        if getattr(order, "strategy", None) is None:
            setattr(order, "strategy", self.name)
        return self.broker.submit_order(order)


class BrokerClient:
    """Small broker facade that can later be swapped from Alpaca to IBKR."""

    def __init__(
        self,
        settings: Settings,
        broker_factory: Any | None = None,
        order_factory: Any | None = None,
        execution_strategy_factory: Any | None = None,
    ) -> None:
        self.settings = settings
        self._broker_factory = broker_factory
        self._order_factory = order_factory
        self._execution_strategy_factory = execution_strategy_factory
        self._execution_strategy = None
        self._broker = None
        self._last_snapshot: BrokerSnapshot | None = None
        self._broker_available = True
        self._broker_unavailable_reason: str | None = None
        self._execution_strategy_name = "openai_trading_bot_executor"

    def connect(self) -> None:
        """Initialize the broker connection.

        TODO: Replace or extend broker implementation when moving from Alpaca to IBKR.
        """
        if not self.settings.paper_trading:
            logger.warning("PAPER_TRADING is false. Live trading is not implemented.")
            self._mark_broker_unavailable("PAPER_TRADING is false.")
            return

        if not self.settings.alpaca_api_key or not self.settings.alpaca_secret_key:
            logger.warning("Alpaca broker connection skipped because credentials are missing.")
            self._mark_broker_unavailable("Alpaca credentials are missing.")
            return

        try:
            self._broker = self._create_alpaca_broker()
        except Exception as exc:
            logger.error("Broker initialisation failed safely: %s.", exc.__class__.__name__)
            self._mark_broker_unavailable("Broker unavailable")
            return

        self._broker_available = True
        self._broker_unavailable_reason = None
        if self.settings.dry_run:
            logger.info("Broker connected for Alpaca Paper Trading data collection. DRY_RUN still blocks orders.")
            return

        logger.info("Broker connected for Alpaca Paper Trading.")

    def collect_snapshot(self) -> BrokerSnapshot:
        """Collect account, position, and market data for the trading cycle."""
        logger.info("Collecting broker/account/position/market data.")

        account = self._collect_account_data()
        positions = self._collect_positions()
        market_data = self._collect_market_data()

        snapshot = BrokerSnapshot(account=account, positions=positions, market_data=market_data)
        self._last_snapshot = snapshot
        self._log_snapshot_gaps(snapshot)
        return snapshot

    def _collect_account_data(self) -> dict[str, Any]:
        account = {
            "broker": "alpaca",
            "paper_trading": self.settings.paper_trading,
            "cash": None,
            "buying_power": None,
            "equity": None,
            "portfolio_value": None,
        }

        if self._broker is None:
            logger.warning("Broker unavailable; account data was not collected.")
            return account

        try:
            alpaca_account = self._broker.api.get_account()
            account.update(
                {
                    "cash": self._to_float(getattr(alpaca_account, "cash", None)),
                    "buying_power": self._to_float(getattr(alpaca_account, "buying_power", None)),
                    "equity": self._to_float(getattr(alpaca_account, "equity", None)),
                    "portfolio_value": self._to_float(getattr(alpaca_account, "portfolio_value", None)),
                }
            )
        except Exception as exc:
            logger.warning("Could not collect Alpaca account data: %s.", exc.__class__.__name__)
        return account

    def _collect_positions(self) -> list[dict[str, Any]]:
        if self._broker is None:
            logger.warning("Broker unavailable; positions were not collected.")
            return []

        try:
            broker_positions = self._broker.api.get_all_positions()
        except Exception as exc:
            logger.warning("Could not collect Alpaca positions: %s.", exc.__class__.__name__)
            return []

        positions = []
        for position in broker_positions:
            positions.append(
                {
                    "symbol": str(getattr(position, "symbol", "")).upper(),
                    "quantity": self._to_float(getattr(position, "qty", None)),
                    "market_value": self._to_float(getattr(position, "market_value", None)),
                    "average_price": self._to_float(getattr(position, "avg_entry_price", None)),
                }
            )
        return positions

    def _collect_market_data(self) -> dict[str, Any]:
        symbols = self._analysis_symbols()
        market_data = {
            "symbols": symbols,
            "prices": {},
            "market_intelligence": {},
            "dynamic_watchlist_enabled": self.settings.dynamic_watchlist_enabled,
            "dynamic_watchlist": [],
            "scanner_status": "disabled",
        }

        if self._broker is None:
            logger.warning("Broker unavailable; latest prices were not collected.")
            return market_data

        if not self.settings.dynamic_watchlist_enabled:
            self._collect_market_data_for_symbols(symbols, market_data)
            return market_data

        market_data["scanner_status"] = "enabled"

        universe_data = {
            "symbols": self.settings.scanner_universe,
            "prices": {},
            "market_intelligence": {},
            "dynamic_watchlist_enabled": True,
            "dynamic_watchlist": [],
        }
        try:
            self._collect_market_data_for_symbols(self.settings.scanner_universe, universe_data)
            scanner = DynamicWatchlistScanner(self.settings.watchlist_size)
            selected = scanner.rank(
                self.settings.scanner_universe,
                universe_data["market_intelligence"],
            )
        except Exception as exc:
            logger.warning("Dynamic watchlist scanner failed safely: %s.", exc.__class__.__name__)
            market_data["scanner_status"] = "fallback_static"
            self._collect_market_data_for_symbols(symbols, market_data)
            return market_data

        if not selected:
            logger.warning("Dynamic watchlist scanner returned no candidates; using HOLD-only scanner status.")
            market_data["symbols"] = self._hold_only_symbols()
            market_data["scanner_status"] = "no_candidates"
            return market_data

        final_symbols = [candidate.symbol for candidate in selected]
        market_data["symbols"] = final_symbols
        market_data["scanner_status"] = "generated"
        market_data["dynamic_watchlist"] = [candidate.to_dict() for candidate in selected]
        market_data["scanner_universe"] = self.settings.scanner_universe
        for symbol in final_symbols:
            if symbol in universe_data["prices"]:
                market_data["prices"][symbol] = universe_data["prices"][symbol]
            if symbol in universe_data["market_intelligence"]:
                market_data["market_intelligence"][symbol] = universe_data["market_intelligence"][symbol]
        return market_data

    def _analysis_symbols(self) -> list[str]:
        return self.settings.allowed_symbols

    def _hold_only_symbols(self) -> list[str]:
        if "SPY" in self.settings.allowed_symbols:
            return ["SPY"]
        if self.settings.allowed_symbols:
            return [self.settings.allowed_symbols[0]]
        return ["SPY"]

    def _collect_market_data_for_symbols(self, symbols: list[str], market_data: dict[str, Any]) -> None:
        for symbol in symbols:
            normalized_symbol = symbol.upper()
            try:
                latest_price = self._get_last_price(normalized_symbol)
                market_data["prices"][normalized_symbol] = {"last_price": latest_price}
                if latest_price is None:
                    logger.warning("Latest price for %s is missing.", normalized_symbol)
            except Exception as exc:
                logger.warning(
                    "Could not collect latest price for %s: %s.",
                    normalized_symbol,
                    exc.__class__.__name__,
                )

            try:
                minute_bars = self._get_historical_bars(normalized_symbol, length=120, timestep="minute")
                daily_bars = self._get_historical_bars(normalized_symbol, length=60, timestep="day")
                indicators = calculate_market_indicators(normalized_symbol, minute_bars, daily_bars)
                if indicators.get("current_price") is None:
                    indicators["current_price"] = market_data["prices"].get(normalized_symbol, {}).get("last_price")
                market_data["market_intelligence"][normalized_symbol] = indicators
            except Exception as exc:
                logger.warning(
                    "Could not calculate market indicators for %s: %s.",
                    normalized_symbol,
                    exc.__class__.__name__,
                )

    def _get_last_price(self, symbol: str) -> float | None:
        from lumibot.entities import Asset

        return self._to_float(self._broker.get_last_price(Asset(symbol=symbol, asset_type="stock")))

    def _get_historical_bars(self, symbol: str, length: int, timestep: str) -> Any:
        from lumibot.entities import Asset

        asset = Asset(symbol=symbol, asset_type="stock")
        data_source = getattr(self._broker, "data_source", None)
        if data_source is not None and hasattr(data_source, "get_historical_prices"):
            return data_source.get_historical_prices(
                asset,
                length=length,
                timestep=timestep,
                include_after_hours=False,
            )

        if hasattr(self._broker, "get_historical_prices"):
            return self._broker.get_historical_prices(
                asset,
                length=length,
                timestep=timestep,
                include_after_hours=False,
            )

        raise AttributeError("Broker does not expose historical bar data.")

    def _log_snapshot_gaps(self, snapshot: BrokerSnapshot) -> None:
        missing_account_fields = [
            field_name
            for field_name in ("cash", "buying_power", "portfolio_value")
            if snapshot.account.get(field_name) is None
        ]
        if missing_account_fields:
            logger.warning(
                "Broker snapshot missing account fields: %s.",
                ", ".join(missing_account_fields),
            )

        if not snapshot.positions:
            logger.info("Broker snapshot returned no open positions.")

        missing_price_symbols = []
        missing_indicator_symbols = []
        prices = snapshot.market_data.get("prices", {})
        market_intelligence = snapshot.market_data.get("market_intelligence", {})
        for symbol in snapshot.market_data.get("symbols", self.settings.allowed_symbols):
            price_data = prices.get(symbol)
            latest_price = price_data.get("last_price") if isinstance(price_data, dict) else None
            if latest_price is None:
                missing_price_symbols.append(symbol)
            if not market_intelligence.get(symbol):
                missing_indicator_symbols.append(symbol)

        if missing_price_symbols:
            logger.warning(
                "Broker snapshot missing latest prices for: %s.",
                ", ".join(missing_price_symbols),
            )

        if missing_indicator_symbols:
            logger.warning(
                "Broker snapshot missing market intelligence for: %s.",
                ", ".join(missing_indicator_symbols),
            )

    def execute_order(self, approved_decision: dict[str, Any]) -> dict[str, Any]:
        """Execute an approved trade through Lumibot.

        Execution remains paper-trading only and is guarded independently from
        the risk manager so direct calls also fail safely.
        """
        logger.info("Approved decision received for execution: %s", approved_decision)

        guard_failure = self._execution_guard_failure(approved_decision)
        if guard_failure:
            logger.info("Order execution rejected: %s", guard_failure)
            return self._build_execution_result(approved_decision, False, guard_failure)

        if not self._broker_available:
            logger.info("Order execution rejected: Broker unavailable.")
            return self._build_execution_result(
                approved_decision,
                False,
                "Broker unavailable",
            )

        symbol = str(approved_decision.get("symbol", "")).upper()
        action = str(approved_decision.get("action", "")).upper()
        price = self._latest_price(symbol)
        if price is None or price <= 0:
            reason = f"Missing or invalid latest price for {symbol}."
            logger.info("Order execution rejected: %s", reason)
            return self._build_execution_result(approved_decision, False, reason)

        position_failure = self._position_guard_failure(
            approved_decision,
            price,
        )
        if position_failure:
            logger.info("Order execution rejected: %s", position_failure)
            return self._build_execution_result(approved_decision, False, position_failure)

        quantity = self._calculate_quantity(approved_decision, price)
        if quantity <= 0:
            reason = "Calculated quantity is 0. No order placed."
            logger.info("Order execution rejected: %s", reason)
            return self._build_execution_result(
                approved_decision,
                False,
                reason,
                quantity=quantity,
            )

        try:
            broker = self._get_broker()
            execution_strategy = self._get_execution_strategy(broker)
            order = self._create_market_order(
                symbol=symbol,
                action=action,
                quantity=quantity,
                execution_strategy=execution_strategy,
            )
            logger.info("Submitting Alpaca paper market order: %s %s %s shares.", action, symbol, quantity)
            broker_order = execution_strategy.submit_order(order)
        except Exception as exc:
            if not self._broker_available:
                logger.info("Order execution rejected: Broker unavailable.")
                return self._build_execution_result(
                    approved_decision,
                    False,
                    "Broker unavailable",
                    quantity=quantity,
                    raw_status=exc.__class__.__name__,
                )

            logger.warning("Broker order submission failed safely: %s.", exc.__class__.__name__)
            return self._build_execution_result(
                approved_decision,
                False,
                "Broker order submission failed.",
                quantity=quantity,
                raw_status=exc.__class__.__name__,
            )

        logger.info("Alpaca paper order submitted for %s %s.", action, symbol)
        return self._build_execution_result(
            approved_decision,
            True,
            "Order submitted to Alpaca Paper Trading.",
            quantity=quantity,
            broker_order_id=self._broker_order_id(broker_order),
            raw_status=self._broker_order_status(broker_order),
        )

    def _execution_guard_failure(self, decision: dict[str, Any]) -> str | None:
        if not self.settings.bot_enabled:
            return "BOT_ENABLED is false."

        if not self.settings.paper_trading:
            return "PAPER_TRADING is false. Live trading is not implemented."

        if self.settings.dry_run:
            return "DRY_RUN is true. No order placed."

        action = str(decision.get("action", "")).upper()
        if action == "HOLD":
            return "HOLD decision. No order placed."

        if action not in {"BUY", "SELL"}:
            return f"Unsupported action {action or 'UNKNOWN'}."

        symbol = str(decision.get("symbol", "")).upper()
        if self.settings.allowed_symbols and symbol not in self.settings.allowed_symbols:
            return f"{symbol or 'Missing symbol'} is not in ALLOWED_SYMBOLS."

        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        if allocation is None or allocation <= 0:
            return "Suggested allocation must be greater than 0."

        if allocation > self.settings.max_position_allocation_percent:
            return (
                f"Suggested allocation {allocation:.2f}% exceeds maximum "
                f"{self.settings.max_position_allocation_percent:.2f}%."
            )

        return None

    def _calculate_quantity(self, decision: dict[str, Any], latest_price: float) -> int:
        snapshot = self._last_snapshot
        portfolio_value = self._to_float(snapshot.account.get("portfolio_value")) if snapshot else None
        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        if portfolio_value is None or portfolio_value <= 0 or allocation is None:
            return 0

        notional = portfolio_value * (allocation / 100)
        return int(notional // latest_price)

    def _position_guard_failure(self, decision: dict[str, Any], latest_price: float) -> str | None:
        action = str(decision.get("action", "")).upper()
        symbol = str(decision.get("symbol", "")).upper()
        current_market_value = self._current_position_market_value(symbol, latest_price)

        if action == "SELL" and current_market_value <= 0:
            return f"No existing {symbol} position to sell."

        if action != "BUY":
            return None

        snapshot = self._last_snapshot
        portfolio_value = self._to_float(snapshot.account.get("portfolio_value")) if snapshot else None
        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        if portfolio_value is None or portfolio_value <= 0 or allocation is None:
            return "Portfolio value or allocation unavailable for position limit check."

        requested_notional = portfolio_value * (allocation / 100)
        projected_market_value = current_market_value + requested_notional
        max_market_value = portfolio_value * (self.settings.max_position_allocation_percent / 100)

        if projected_market_value > max_market_value:
            return (
                f"Projected {symbol} allocation exceeds maximum "
                f"{self.settings.max_position_allocation_percent:.2f}%."
            )

        return None

    def _current_position_market_value(self, symbol: str, latest_price: float) -> float:
        snapshot = self._last_snapshot
        if snapshot is None:
            return 0.0

        for position in snapshot.positions or []:
            if str(position.get("symbol", "")).upper() != symbol:
                continue

            market_value = self._to_float(position.get("market_value"))
            if market_value is not None:
                return abs(market_value)

            quantity = self._to_float(
                position.get("quantity")
                or position.get("qty")
                or position.get("shares")
            )
            if quantity is not None:
                return abs(quantity * latest_price)

        return 0.0

    def _latest_price(self, symbol: str) -> float | None:
        if self._last_snapshot is None:
            return None

        market_data = self._last_snapshot.market_data or {}
        prices = market_data.get("prices", {})

        value = prices.get(symbol)
        if isinstance(value, dict):
            value = value.get("last_price") or value.get("price") or value.get("close")

        if value is None and symbol in market_data:
            symbol_data = market_data[symbol]
            if isinstance(symbol_data, dict):
                value = symbol_data.get("last_price") or symbol_data.get("price") or symbol_data.get("close")

        return self._to_float(value)

    def _get_broker(self) -> Any:
        if self._broker is None:
            try:
                self._broker = self._create_alpaca_broker()
            except Exception as exc:
                logger.error("Broker initialisation failed safely: %s.", exc.__class__.__name__)
                self._mark_broker_unavailable("Broker unavailable")
                raise
        return self._broker

    def _get_execution_strategy(self, broker: Any) -> Any:
        if self._execution_strategy is not None:
            return self._execution_strategy

        if self._execution_strategy_factory is not None:
            self._execution_strategy = self._execution_strategy_factory(
                broker=broker,
                name=self._execution_strategy_name,
            )
            return self._execution_strategy

        self._execution_strategy = LumibotExecutionStrategyAdapter(
            broker=broker,
            name=self._execution_strategy_name,
            order_factory=self._order_factory,
        )
        return self._execution_strategy

    def _mark_broker_unavailable(self, reason: str) -> None:
        self._broker = None
        self._broker_available = False
        self._broker_unavailable_reason = reason

    def _create_alpaca_broker(self) -> Any:
        if self._broker_factory is not None:
            return self._broker_factory(self._alpaca_config())

        from lumibot.brokers import Alpaca

        return Alpaca(self._alpaca_config())

    def _alpaca_config(self) -> dict[str, Any]:
        return {
            "API_KEY": self.settings.alpaca_api_key,
            "API_SECRET": self.settings.alpaca_secret_key,
            "PAPER": True,
        }

    def _create_market_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        execution_strategy: Any,
    ) -> Any:
        if hasattr(execution_strategy, "create_order"):
            return execution_strategy.create_order(
                symbol=symbol,
                action=action,
                quantity=quantity,
            )

        from lumibot.entities import Order

        return Order(
            strategy=self._execution_strategy_name,
            asset=symbol,
            quantity=quantity,
            side=action.lower(),
            order_type="market",
            time_in_force="day",
        )

    def _build_execution_result(
        self,
        decision: dict[str, Any],
        executed: bool,
        reason: str,
        quantity: int | None = None,
        broker_order_id: str | None = None,
        raw_status: str | None = None,
    ) -> dict[str, Any]:
        action = str(decision.get("action", "")).upper()
        symbol = str(decision.get("symbol", "")).upper()
        return {
            "executed": executed,
            "reason": reason,
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "order_type": "market",
            "broker_order_id": broker_order_id,
            "submitted_at": datetime.now().isoformat(),
            "raw_status": raw_status,
            "decision": decision,
        }

    def _broker_order_id(self, broker_order: Any) -> str | None:
        value = (
            getattr(broker_order, "identifier", None)
            or getattr(broker_order, "id", None)
            or getattr(broker_order, "order_id", None)
        )
        return str(value) if value else None

    def _broker_order_status(self, broker_order: Any) -> str | None:
        value = getattr(broker_order, "status", None)
        return str(value) if value else None

    def _to_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
