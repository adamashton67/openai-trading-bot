"""Broker integration layer for Lumibot and Alpaca Paper Trading."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from config import Settings
from market_indicators import calculate_market_indicators
from watchlist_scanner import (
    DynamicWatchlistScanner,
    broad_asset_symbol,
    is_broad_scan_asset_candidate,
)


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
        data_client_factory: Any | None = None,
    ) -> None:
        self.settings = settings
        self._broker_factory = broker_factory
        self._order_factory = order_factory
        self._execution_strategy_factory = execution_strategy_factory
        self._data_client_factory = data_client_factory
        self._execution_strategy = None
        self._alpaca_data_client = None
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

    def refresh_open_positions(self) -> list[dict[str, Any]]:
        """Return broker-current holdings without running market scans."""
        api = getattr(self._broker, "api", None)
        if api is None or not hasattr(api, "get_all_positions"):
            raise RuntimeError("Alpaca positions API is unavailable")
        broker_positions = api.get_all_positions()
        return [
            {
                "symbol": str(getattr(position, "symbol", "")).upper(),
                "quantity": self._to_float(getattr(position, "qty", None)) or 0,
                "market_value": self._to_float(getattr(position, "market_value", None)),
                "average_price": self._to_float(getattr(position, "avg_entry_price", None)),
            }
            for position in broker_positions
            if (self._to_float(getattr(position, "qty", None)) or 0) > 0
        ]

    def get_current_price(self, symbol: str) -> float | None:
        """Fetch a current broker/data price without collecting scanner indicators."""
        if self._broker is None:
            return None
        return self._get_last_price(symbol.upper())

    def find_covering_open_sell(self, symbol: str, quantity: float) -> dict[str, Any] | None:
        """Return concise metadata for an open SELL covering the intended quantity."""
        order = self._covering_open_sell_order(symbol.upper(), float(quantity))
        if order is None:
            return None
        return {
            "broker_order_id": self._broker_order_id(order),
            "broker_status": self._broker_order_status(order),
            "remaining_quantity": max(
                0.0,
                (self._to_float(getattr(order, "qty", None) or getattr(order, "quantity", None)) or 0)
                - (self._broker_filled_quantity(order) or 0),
            ),
        }

    def execute_position_management_sell(
        self,
        symbol: str,
        quantity: float,
        *,
        observed_price: float,
        cost_basis_per_share: float,
        exit_source: str,
        exit_reason: str,
    ) -> dict[str, Any]:
        """Submit a deterministic quantity-based SELL without AI or entry risk checks."""
        decision = {"symbol": symbol.upper(), "action": "SELL", "reason": exit_reason}
        details = {"exit_source": exit_source, "exit_reason": exit_reason}
        if not self.settings.bot_enabled:
            return self._build_execution_result(
                decision, False, "BOT_ENABLED is false.", quantity=quantity, **details
            )
        if not self.settings.paper_trading:
            return self._build_execution_result(
                decision,
                False,
                "PAPER_TRADING is false. Live trading is not implemented.",
                quantity=quantity,
                **details,
            )
        if self.settings.dry_run:
            return self._build_execution_result(
                decision, False, "DRY_RUN is true. No order placed.", quantity=quantity, **details
            )

        held_quantity, broker_cost_basis, refresh_error = self._refresh_sell_position(symbol.upper())
        if refresh_error:
            return self._build_execution_result(
                decision, False, refresh_error, quantity=quantity, error_reason=refresh_error, **details
            )
        if held_quantity is None or held_quantity <= 0:
            reason = f"No existing {symbol.upper()} position to sell."
            return self._build_execution_result(
                decision, False, reason, quantity=0, currently_held_quantity=0, **details
            )

        capped_quantity = min(float(quantity), held_quantity)
        if capped_quantity <= 0:
            return self._build_execution_result(
                decision, False, "Calculated quantity is 0. No order placed.", quantity=0, **details
            )
        if capped_quantity.is_integer():
            capped_quantity = int(capped_quantity)

        covering = self._covering_open_sell_order(symbol.upper(), float(capped_quantity))
        if covering is not None:
            reason = f"Existing open SELL order already covers {symbol.upper()}."
            return self._build_execution_result(
                decision,
                False,
                reason,
                quantity=capped_quantity,
                broker_order_id=self._broker_order_id(covering),
                raw_status=self._broker_order_status(covering),
                duplicate_prevented=True,
                currently_held_quantity=held_quantity,
                cost_basis_per_share=broker_cost_basis or cost_basis_per_share,
                error_reason=reason,
                **details,
            )

        try:
            broker = self._get_broker()
            execution_strategy = self._get_execution_strategy(broker)
            order = self._create_market_order(
                symbol=symbol.upper(),
                action="SELL",
                quantity=capped_quantity,
                execution_strategy=execution_strategy,
            )
            broker_order = execution_strategy.submit_order(order)
        except Exception as exc:
            safe_message = self._safe_exception_message(exc)
            logger.warning(
                "Mechanical SELL failed safely: symbol=%s quantity=%s source=%s exception=%s message=%s",
                symbol.upper(), capped_quantity, exit_source, exc.__class__.__name__, safe_message,
            )
            return self._build_execution_result(
                decision,
                False,
                "Broker order submission failed.",
                quantity=capped_quantity,
                currently_held_quantity=held_quantity,
                cost_basis_per_share=broker_cost_basis or cost_basis_per_share,
                error_reason=safe_message,
                **details,
            )

        status = self._broker_order_status(broker_order)
        executed = status not in {"rejected", "error", "cancelled", "canceled", "expired"}
        reason = (
            "Mechanical SELL submitted to Alpaca Paper Trading."
            if executed
            else "Alpaca rejected or closed the mechanical SELL before acceptance."
        )
        return self._build_execution_result(
            decision,
            executed,
            reason,
            quantity=capped_quantity,
            broker_order_id=self._broker_order_id(broker_order),
            raw_status=status,
            broker_status=status,
            currently_held_quantity=held_quantity,
            submitted_price=observed_price,
            filled_quantity=self._broker_filled_quantity(broker_order),
            average_fill_price=self._broker_average_fill_price(broker_order),
            cost_basis_per_share=broker_cost_basis or cost_basis_per_share,
            error_reason=self._broker_rejection_message(broker_order),
            **details,
        )

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
            "scanner_mode": "static",
        }

        if self._broker is None:
            logger.warning("Broker unavailable; latest prices were not collected.")
            return market_data

        if not self.settings.dynamic_watchlist_enabled:
            self._collect_market_data_for_symbols(symbols, market_data)
            return market_data

        if self.settings.broad_market_scan_enabled:
            try:
                return self._collect_broad_watchlist_market_data(market_data)
            except Exception as exc:
                stage = getattr(exc, "broad_scanner_stage", "unknown stage")
                logger.warning("Broad market scanner failed safely during %s.", stage)
                logger.warning("Broad scanner exception class: %s.", exc.__class__.__name__)
                logger.warning("Broad scanner exception message: %s.", self._safe_exception_message(exc))
                market_data["broad_scan_failed"] = True

        try:
            return self._collect_configured_watchlist_market_data(market_data)
        except Exception as exc:
            logger.warning("Dynamic watchlist scanner failed safely: %s.", exc.__class__.__name__)
            market_data["scanner_status"] = "fallback_static"
            market_data["scanner_mode"] = "static"
            self._collect_market_data_for_symbols(symbols, market_data)
            return market_data

    def _collect_configured_watchlist_market_data(self, market_data: dict[str, Any]) -> dict[str, Any]:
        market_data["scanner_status"] = "enabled"
        market_data["scanner_mode"] = "configured_universe"

        universe_data = self._empty_market_data(self.settings.scanner_universe)
        try:
            self._collect_market_data_for_symbols(self.settings.scanner_universe, universe_data)
            scanner = DynamicWatchlistScanner(self.settings.watchlist_size)
            selected = scanner.rank(
                self.settings.scanner_universe,
                universe_data["market_intelligence"],
            )
        except Exception as exc:
            logger.warning("Dynamic watchlist scanner failed safely: %s.", exc.__class__.__name__)
            raise

        if not selected:
            logger.warning("Dynamic watchlist scanner returned no candidates; using HOLD-only scanner status.")
            market_data["symbols"] = self._hold_only_symbols()
            market_data["scanner_status"] = "no_candidates"
            market_data["scanner_mode"] = "configured_universe"
            return market_data

        self._copy_selected_watchlist(market_data, universe_data, selected)
        market_data["scanner_status"] = "generated"
        market_data["scanner_mode"] = "configured_universe"
        market_data["scanner_universe"] = self.settings.scanner_universe
        logger.info(
            "Dynamic watchlist scanner selected %s symbols: %s.",
            len(selected),
            ", ".join(candidate.symbol for candidate in selected[:5]),
        )
        return market_data

    def _collect_broad_watchlist_market_data(self, market_data: dict[str, Any]) -> dict[str, Any]:
        market_data["scanner_status"] = "enabled"
        market_data["scanner_mode"] = "broad_market"

        stage = "starting broad market scan"
        try:
            logger.info("Broad scanner: starting broad market scan.")

            stage = "fetching tradable assets"
            logger.info("Broad scanner: fetching tradable assets.")
            assets = self._get_broad_market_assets()
            logger.info("Broad scanner: fetched %s assets.", len(assets))
            logger.info("Broad scanner: sample assets: %s.", self._safe_asset_samples(assets))

            stage = "applying asset filters"
            logger.info("Broad scanner: filtering tradable US equities.")
            candidate_symbols = [
                broad_asset_symbol(asset)
                for asset in assets
                if is_broad_scan_asset_candidate(
                    asset,
                    exclude_etfs=self.settings.exclude_etfs,
                    explicit_allowed_symbols=self.settings.allowed_symbols,
                )
            ]
            candidate_symbols = self._dedupe_symbols(candidate_symbols)
            logger.info("Broad scanner: %s symbols remain after asset filters.", len(candidate_symbols))
            if not candidate_symbols:
                logger.warning("Broad scanner: no symbols remained after asset filters. Sample assets: %s.", self._safe_asset_samples(assets))

            stage = "beginning market data collection"
            logger.info("Broad scanner: collecting market data.")
            logger.info(
                "Broad scanner: first 20 symbols before quality ordering/cap: %s.",
                ", ".join(candidate_symbols[:20]) or "none",
            )
            candidate_symbols = self._cap_symbols_before_native_data(candidate_symbols, assets)
            market_data["broad_capped_candidate_count"] = len(candidate_symbols)
            logger.info(
                "Broad scanner: capped %s symbols before native Alpaca data calls.",
                len(candidate_symbols),
            )
            logger.info(
                "Broad scanner: first 20 symbols after quality ordering/cap: %s.",
                ", ".join(candidate_symbols[:20]) or "none",
            )
            logger.info(
                "Broad scanner: Alpaca native data batch size is %s.",
                self.settings.broad_scan_data_batch_size,
            )

            stage = "applying liquidity filters"
            logger.info("Broad scanner: applying liquidity filters.")
            daily_bars_result = self._fetch_native_stock_bars(
                candidate_symbols,
                timeframe="day",
                limit=60,
            )
            market_data["broad_bar_request_count"] = daily_bars_result["request_count"]
            logger.info(
                "Broad scanner: made %s native daily bar requests.",
                daily_bars_result["request_count"],
            )

            preliminary_intelligence = {}
            low_price_count = 0
            low_volume_count = 0
            missing_bar_count = 0
            partial_intraday_indicator_count = 0
            for symbol in candidate_symbols:
                daily_bars = daily_bars_result["bars"].get(symbol)
                if daily_bars is None:
                    missing_bar_count += 1
                    continue

                indicators = calculate_market_indicators(symbol, None, daily_bars)
                if any(
                    indicators.get(field_name) is None
                    for field_name in ("5m_change_percent", "15m_change_percent", "1h_change_percent")
                ):
                    partial_intraday_indicator_count += 1
                price = self._to_float(indicators.get("current_price"))
                if price is None or price < self.settings.min_stock_price:
                    low_price_count += 1
                    continue

                average_volume = self._to_float(indicators.get("average_20d_volume"))
                if average_volume is None or average_volume < self.settings.min_average_volume:
                    low_volume_count += 1
                    continue

                preliminary_intelligence[symbol] = indicators

            logger.info("Broad scanner: %s symbols have usable native daily data.", len(preliminary_intelligence))
            logger.info(
                "Broad scanner: %s symbols remain after liquidity filters.",
                len(preliminary_intelligence),
            )
            logger.info("Broad scanner: skipped %s symbols with missing bars.", missing_bar_count)
            logger.info("Broad scanner: skipped %s symbols below minimum price.", low_price_count)
            logger.info(
                "Broad scanner: skipped %s symbols below minimum average volume.",
                low_volume_count,
            )
            if partial_intraday_indicator_count:
                logger.info(
                    "Market indicators: skipped/partial indicators for %s symbols due to insufficient intraday bars.",
                    partial_intraday_indicator_count,
                )
            market_data["broad_price_filtered_count"] = len(preliminary_intelligence)

            stage = "beginning ranking"
            logger.info("Broad scanner: beginning ranking.")
            scanner = DynamicWatchlistScanner(self.settings.watchlist_size)
            selected = scanner.rank(list(preliminary_intelligence), preliminary_intelligence)
            if not selected:
                raise RuntimeError("Broad scanner returned no candidates.")
            if len(selected) < self.settings.watchlist_size:
                raise RuntimeError(
                    "Broad scanner returned fewer symbols than WATCHLIST_SIZE."
                )

            stage = "finalizing watchlist"
            final_symbols = [candidate.symbol for candidate in selected]
            minute_bars_result = self._fetch_native_stock_bars(
                final_symbols,
                timeframe="minute",
                limit=120,
            )
            market_data["broad_bar_request_count"] += minute_bars_result["request_count"]
            logger.info(
                "Broad scanner: made %s native minute bar requests for final symbols.",
                minute_bars_result["request_count"],
            )

            broad_universe_data = self._empty_market_data(final_symbols)
            for symbol in final_symbols:
                daily_bars = daily_bars_result["bars"].get(symbol)
                minute_bars = minute_bars_result["bars"].get(symbol)
                indicators = calculate_market_indicators(symbol, minute_bars, daily_bars)
                if indicators.get("current_price") is None:
                    indicators = preliminary_intelligence[symbol]
                broad_universe_data["market_intelligence"][symbol] = indicators
                broad_universe_data["prices"][symbol] = {
                    "last_price": indicators.get("current_price"),
                }

            self._copy_selected_watchlist(market_data, broad_universe_data, selected)
            market_data["scanner_status"] = "broad_generated"
            market_data["scanner_mode"] = "broad_market"
            market_data["broad_candidate_count"] = len(preliminary_intelligence)
            logger.info("Broad scanner: final watchlist size is %s.", len(selected))
            logger.info(
                "Broad scanner: top selected symbols: %s.",
                ", ".join(
                    f"{candidate.symbol}({candidate.score}: {', '.join(candidate.reasons_added)})"
                    for candidate in selected[:10]
                ),
            )
            return market_data
        except Exception as exc:
            setattr(exc, "broad_scanner_stage", stage)
            if self._is_rate_limit_error(exc):
                market_data["broad_rate_limit_fallback"] = True
                logger.warning("Broad scanner: rate limit fallback triggered.")
            logger.warning("Broad scanner failed during %s.", stage)
            logger.warning("Exception: %s.", exc.__class__.__name__)
            logger.warning("Message: %s.", self._safe_exception_message(exc))
            raise

    def _empty_market_data(self, symbols: list[str]) -> dict[str, Any]:
        return {
            "symbols": symbols,
            "prices": {},
            "market_intelligence": {},
            "dynamic_watchlist_enabled": True,
            "dynamic_watchlist": [],
        }

    def _copy_selected_watchlist(
        self,
        market_data: dict[str, Any],
        source_data: dict[str, Any],
        selected: list[Any],
    ) -> None:
        final_symbols = [candidate.symbol for candidate in selected]
        market_data["symbols"] = final_symbols
        market_data["dynamic_watchlist"] = [candidate.to_dict() for candidate in selected]
        for symbol in final_symbols:
            if symbol in source_data["prices"]:
                market_data["prices"][symbol] = source_data["prices"][symbol]
            if symbol in source_data["market_intelligence"]:
                market_data["market_intelligence"][symbol] = source_data["market_intelligence"][symbol]

    def _get_broad_market_assets(self) -> list[Any]:
        if self._broker is None:
            return []

        api = getattr(self._broker, "api", None)
        if api is not None and hasattr(api, "get_all_assets"):
            return list(api.get_all_assets())

        if hasattr(self._broker, "get_all_assets"):
            return list(self._broker.get_all_assets())

        raise AttributeError("Broker does not expose tradable assets.")

    def _dedupe_symbols(self, symbols: list[str]) -> list[str]:
        deduped = []
        seen = set()
        for symbol in symbols:
            normalized_symbol = symbol.strip().upper()
            if normalized_symbol and normalized_symbol not in seen:
                seen.add(normalized_symbol)
                deduped.append(normalized_symbol)
        return deduped

    def _cap_symbols_before_native_data(self, symbols: list[str], assets: list[Any]) -> list[str]:
        cap = max(
            1,
            min(
                self.settings.broad_market_max_symbols,
                self.settings.max_scanner_candidates_after_filters,
            ),
        )
        asset_by_symbol = {broad_asset_symbol(asset): asset for asset in assets}
        ranked = sorted(
            symbols,
            key=lambda symbol: self._broad_asset_quality_sort_key(
                symbol,
                asset_by_symbol.get(symbol),
            ),
        )
        return ranked[:cap]

    def _broad_asset_quality_sort_key(self, symbol: str, asset: Any | None) -> tuple[Any, ...]:
        exchange_priority = {
            "NASDAQ": 0,
            "NYSE": 1,
            "ARCA": 2,
            "AMEX": 3,
        }
        exchange = self._safe_asset_field(getattr(asset, "exchange", "")) if asset is not None else ""
        exchange_value = str(exchange).upper()
        if "." in exchange_value:
            exchange_value = exchange_value.rsplit(".", 1)[-1]
        exchange_score = exchange_priority.get(exchange_value, 9)
        known_priority = self._known_liquid_common_symbol_priority().get(symbol, 999)
        known_score = 0 if known_priority != 999 else 1
        odd_suffix_score = 1 if self._looks_like_odd_suffix_symbol(symbol) else 0
        dollar_volume = self._asset_volume_for_symbol([asset], symbol) if asset is not None else None
        return (
            odd_suffix_score,
            exchange_score,
            known_score,
            known_priority,
            -(dollar_volume or 0),
            len(symbol),
            symbol,
        )

    def _looks_like_odd_suffix_symbol(self, symbol: str) -> bool:
        if symbol in self._known_liquid_common_symbols():
            return False
        return symbol.endswith(("WS", "W", "U", "R"))

    def _known_liquid_common_symbols(self) -> set[str]:
        return set(self._known_liquid_common_symbol_priority())

    def _known_liquid_common_symbol_priority(self) -> dict[str, int]:
        symbols = [
            "AAPL",
            "MSFT",
            "NVDA",
            "AMZN",
            "GOOGL",
            "GOOG",
            "META",
            "TSLA",
            "AMD",
            "AVGO",
            "NFLX",
            "COST",
            "CRM",
            "ADBE",
            "ORCL",
            "INTC",
            "UBER",
            "SHOP",
            "PLTR",
            "JPM",
            "BAC",
            "WFC",
            "GS",
            "V",
            "MA",
            "HD",
            "WMT",
            "PG",
            "KO",
            "PEP",
            "MRK",
            "PFE",
            "UNH",
            "XOM",
            "CVX",
            "BA",
            "CAT",
            "GE",
            "SPY",
            "QQQ",
            "IWM",
            "DIA",
        ]
        return {symbol: index for index, symbol in enumerate(symbols)}

    def _fetch_native_stock_bars(
        self,
        symbols: list[str],
        timeframe: str,
        limit: int,
    ) -> dict[str, Any]:
        batch_size = max(1, self.settings.broad_scan_data_batch_size)
        client = self._get_alpaca_data_client()
        bars: dict[str, Any] = {}
        request_count = 0
        for start in range(0, len(symbols), batch_size):
            batch = symbols[start : start + batch_size]
            request_count += 1
            logger.info(
                "Broad scanner: requesting native %s bars batch %s with %s symbols.",
                timeframe,
                request_count,
                len(batch),
            )
            request = self._build_stock_bars_request(batch, timeframe, limit)
            logger.info(
                "Broad scanner: native %s bars request range %s to %s.",
                timeframe,
                request.start,
                request.end,
            )
            response = client.get_stock_bars(request)
            parsed_bars = self._parse_native_bars_response(response)
            logger.info(
                "Broad scanner: completed native %s bars batch %s; requested %s symbols, returned %s.",
                timeframe,
                request_count,
                len(batch),
                len(parsed_bars),
            )
            bars.update(parsed_bars)

        return {
            "bars": bars,
            "request_count": request_count,
        }

    def _get_alpaca_data_client(self) -> Any:
        if self._alpaca_data_client is not None:
            return self._alpaca_data_client

        broker_data_client = getattr(self._broker, "native_data_client", None)
        if broker_data_client is not None:
            self._alpaca_data_client = broker_data_client
            return self._alpaca_data_client

        if self._data_client_factory is not None:
            self._alpaca_data_client = self._data_client_factory()
            return self._alpaca_data_client

        from alpaca.data.historical import StockHistoricalDataClient

        self._alpaca_data_client = StockHistoricalDataClient(
            api_key=self.settings.alpaca_api_key,
            secret_key=self.settings.alpaca_secret_key,
        )
        return self._alpaca_data_client

    def _build_stock_bars_request(self, symbols: list[str], timeframe: str, limit: int) -> Any:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        now = datetime.now(timezone.utc)
        if timeframe == "day":
            end = now - timedelta(days=1)
            start = end - timedelta(days=120)
        else:
            end = now - timedelta(minutes=15)
            start = end - timedelta(days=5)
        return StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=TimeFrame.Day if timeframe == "day" else TimeFrame.Minute,
            start=start,
            end=end,
            feed=DataFeed(self.settings.alpaca_data_feed),
        )

    def _parse_native_bars_response(self, response: Any) -> dict[str, Any]:
        if response is None:
            return {}

        if hasattr(response, "data"):
            data = response.data
            parsed_data = self._bars_from_symbol_mapping(data)
            if parsed_data:
                return parsed_data

        if hasattr(response, "df"):
            parsed_df = self._bars_from_dataframe(response.df)
            if parsed_df:
                return parsed_df

        if isinstance(response, dict):
            return self._bars_from_symbol_mapping(response)

        return {}

    def _bars_from_symbol_mapping(self, response: dict[Any, Any]) -> dict[str, Any]:
        parsed = {}
        for symbol, bars in response.items():
            if bars is None:
                continue
            normalized_symbol = str(symbol).upper()
            parsed_bars = self._native_bars_to_records(bars)
            if parsed_bars:
                parsed[normalized_symbol] = parsed_bars
        return parsed

    def _native_bars_to_records(self, bars: Any) -> list[dict[str, Any]]:
        if hasattr(bars, "df"):
            bars = bars.df
        if hasattr(bars, "to_dict") and not isinstance(bars, dict):
            try:
                records = bars.to_dict("records")
                if isinstance(records, list):
                    return records
            except TypeError:
                pass
        if isinstance(bars, dict):
            return [bars]
        try:
            iterator = iter(bars)
        except TypeError:
            iterator = [bars]
        records = []
        for bar in iterator:
            if isinstance(bar, dict):
                records.append(bar)
                continue
            records.append(
                {
                    "timestamp": getattr(bar, "timestamp", None),
                    "open": getattr(bar, "open", None),
                    "high": getattr(bar, "high", None),
                    "low": getattr(bar, "low", None),
                    "close": getattr(bar, "close", None),
                    "volume": getattr(bar, "volume", None),
                }
            )
        return records

    def _bars_from_dataframe(self, df: Any) -> dict[str, Any]:
        if df is None or getattr(df, "empty", True):
            return {}

        if hasattr(df.index, "names") and "symbol" in [str(name).lower() for name in df.index.names]:
            symbol_level = [
                index
                for index, name in enumerate(df.index.names)
                if str(name).lower() == "symbol"
            ][0]
            return {
                str(symbol).upper(): symbol_df.reset_index(level=symbol_level, drop=True).reset_index()
                for symbol, symbol_df in df.groupby(level=symbol_level)
            }

        if "symbol" in [str(column).lower() for column in df.columns]:
            symbol_column = next(column for column in df.columns if str(column).lower() == "symbol")
            return {
                str(symbol).upper(): symbol_df.drop(columns=[symbol_column])
                for symbol, symbol_df in df.groupby(symbol_column)
            }

        return {}

    def _is_rate_limit_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if str(status_code) == "429":
            return True
        message = str(exc).lower()
        return "429" in message or "too many requests" in message or "rate limit" in message

    def _safe_exception_message(self, exc: Exception) -> str:
        message = " ".join(str(exc).strip().split())
        if not message:
            return "<empty>"
        for secret in (self.settings.alpaca_api_key, self.settings.alpaca_secret_key):
            if secret:
                message = message.replace(secret, "<redacted>")
        return message[:500]

    def _safe_asset_samples(self, assets: list[Any], limit: int = 3) -> list[dict[str, Any]]:
        samples = []
        for asset in assets[:limit]:
            samples.append(
                {
                    "symbol": str(getattr(asset, "symbol", "") or ""),
                    "status": self._safe_asset_field(getattr(asset, "status", None)),
                    "tradable": getattr(asset, "tradable", None),
                    "asset_class": self._safe_asset_field(getattr(asset, "asset_class", None)),
                    "exchange": self._safe_asset_field(getattr(asset, "exchange", None)),
                    "asset_type": self._safe_asset_field(getattr(asset, "asset_type", None)),
                    "attributes": self._safe_asset_field(getattr(asset, "attributes", None)),
                }
            )
        return samples

    def _safe_asset_field(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            return [self._safe_asset_field(item) for item in value]
        enum_value = getattr(value, "value", None)
        if enum_value not in (None, ""):
            return str(enum_value)
        enum_name = getattr(value, "name", None)
        if enum_name not in (None, ""):
            return str(enum_name)
        return str(value)

    def _analysis_symbols(self) -> list[str]:
        return self.settings.allowed_symbols

    def _hold_only_symbols(self) -> list[str]:
        if "SPY" in self.settings.allowed_symbols:
            return ["SPY"]
        if self.settings.allowed_symbols:
            return [self.settings.allowed_symbols[0]]
        return ["SPY"]

    def _collect_market_data_for_symbols(
        self,
        symbols: list[str],
        market_data: dict[str, Any],
        warn_on_missing: bool = True,
        collect_prices: bool = True,
    ) -> dict[str, int]:
        stats = {"missing_prices": 0, "price_failures": 0, "indicator_failures": 0}
        for symbol in symbols:
            normalized_symbol = symbol.upper()
            if collect_prices:
                try:
                    latest_price = self._get_last_price(normalized_symbol)
                    market_data["prices"][normalized_symbol] = {"last_price": latest_price}
                    if latest_price is None:
                        stats["missing_prices"] += 1
                        if warn_on_missing:
                            logger.warning("Latest price for %s is missing.", normalized_symbol)
                except Exception as exc:
                    stats["price_failures"] += 1
                    if warn_on_missing:
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
                stats["indicator_failures"] += 1
                if warn_on_missing:
                    logger.warning(
                        "Could not calculate market indicators for %s: %s.",
                        normalized_symbol,
                        exc.__class__.__name__,
                    )
        return stats

    def _asset_volume_for_symbol(self, assets: list[Any], symbol: str) -> float | None:
        for asset in assets:
            if broad_asset_symbol(asset) != symbol:
                continue
            for field_name in (
                "average_volume",
                "avg_volume",
                "volume",
                "last_volume",
                "recent_volume",
            ):
                value = self._to_float(getattr(asset, field_name, None))
                if value is not None:
                    return value
        return None

    def _cap_broad_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cap = max(
            1,
            min(
                self.settings.broad_market_max_symbols,
                self.settings.max_scanner_candidates_after_filters,
            ),
        )
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                -self._candidate_dollar_volume(candidate),
                -(self._to_float(candidate.get("volume")) or 0),
                str(candidate.get("symbol", "")),
            ),
        )
        return ranked[:cap]

    def _candidate_dollar_volume(self, candidate: dict[str, Any]) -> float:
        price = self._to_float(candidate.get("price")) or 0
        volume = self._to_float(candidate.get("volume")) or 0
        return price * volume

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

        buy_risk_details: dict[str, Any] = {}
        if action == "BUY":
            buy_failure, buy_risk_details = self._buy_guard_failure(approved_decision, price)
            if buy_failure:
                logger.info(
                    "BUY portfolio validation rejected: current_positions=%s projected_positions=%s "
                    "max_positions=%s current_invested_percent=%.2f projected_invested_percent=%.2f "
                    "max_invested_percent=%.2f reason=%s",
                    buy_risk_details.get("current_open_position_count"),
                    buy_risk_details.get("projected_open_position_count"),
                    self.settings.max_open_positions,
                    buy_risk_details.get("current_invested_percent", 0.0),
                    buy_risk_details.get("projected_invested_percent", 0.0),
                    self.settings.max_total_invested_percent,
                    buy_failure,
                )
                return self._build_execution_result(
                    approved_decision,
                    False,
                    buy_failure,
                    portfolio_limit_rejection=True,
                    **buy_risk_details,
                )
            logger.info(
                "BUY portfolio validation approved: current_positions=%s projected_positions=%s "
                "max_positions=%s current_invested_percent=%.2f projected_invested_percent=%.2f "
                "max_invested_percent=%.2f",
                buy_risk_details["current_open_position_count"],
                buy_risk_details["projected_open_position_count"],
                self.settings.max_open_positions,
                buy_risk_details["current_invested_percent"],
                buy_risk_details["projected_invested_percent"],
                self.settings.max_total_invested_percent,
            )

        held_quantity = None
        cost_basis_per_share = None
        if action == "SELL":
            held_quantity, cost_basis_per_share, refresh_error = self._refresh_sell_position(symbol)
            if refresh_error:
                logger.warning("SELL position validation failed safely for %s: %s", symbol, refresh_error)
                return self._build_execution_result(
                    approved_decision,
                    False,
                    refresh_error,
                    currently_held_quantity=held_quantity,
                    error_reason=refresh_error,
                    requested_quantity=None,
                )
            if held_quantity is None or held_quantity <= 0:
                reason = f"No existing {symbol} position to sell."
                logger.info("Order execution rejected: %s", reason)
                return self._build_execution_result(
                    approved_decision,
                    False,
                    reason,
                    currently_held_quantity=held_quantity or 0,
                    error_reason=reason,
                    requested_quantity=None,
                )
            requested_quantity = self._calculate_sell_quantity(approved_decision, price, held_quantity)
        else:
            requested_quantity = self._calculate_quantity(
                approved_decision,
                price,
                portfolio_value=buy_risk_details.get("portfolio_value"),
            )
        quantity = requested_quantity
        if action == "SELL" and held_quantity is not None:
            quantity = min(float(requested_quantity), held_quantity)
            quantity = int(quantity) if float(quantity).is_integer() else quantity
        if quantity <= 0:
            reason = "Calculated quantity is 0. No order placed."
            logger.info("Order execution rejected: %s", reason)
            return self._build_execution_result(
                approved_decision,
                False,
                reason,
                quantity=quantity,
                requested_quantity=requested_quantity,
                currently_held_quantity=held_quantity,
            )

        if action == "SELL":
            covering_order = self._covering_open_sell_order(symbol, float(quantity))
            if covering_order is not None:
                order_id = self._broker_order_id(covering_order)
                reason = f"Existing open SELL order already covers {symbol}."
                logger.info("Order execution rejected: %s Broker order ID: %s", reason, order_id)
                return self._build_execution_result(
                    approved_decision,
                    False,
                    reason,
                    quantity=quantity,
                    requested_quantity=requested_quantity,
                    currently_held_quantity=held_quantity,
                    broker_order_id=order_id,
                    raw_status=self._broker_order_status(covering_order),
                    error_reason=reason,
                    cost_basis_per_share=cost_basis_per_share,
                    duplicate_prevented=True,
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
            safe_message = self._safe_exception_message(exc)
            if not self._broker_available:
                logger.info("Order execution rejected: Broker unavailable.")
                return self._build_execution_result(
                    approved_decision,
                    False,
                    "Broker unavailable",
                    quantity=quantity,
                    raw_status=exc.__class__.__name__,
                    requested_quantity=requested_quantity,
                    currently_held_quantity=held_quantity,
                    exception_class=exc.__class__.__name__,
                    error_reason=safe_message,
                    cost_basis_per_share=cost_basis_per_share,
                )

            if action == "SELL" and "subscriber" in safe_message.lower() and "not found" in safe_message.lower():
                recovered_order = self._covering_open_sell_order(symbol, float(quantity))
                if recovered_order is not None:
                    logger.warning(
                        "Lumibot event routing reported a missing subscriber, but Alpaca order %s is %s.",
                        self._broker_order_id(recovered_order), self._broker_order_status(recovered_order),
                    )
                    return self._build_execution_result(
                        approved_decision,
                        True,
                        "Alpaca order found after Lumibot event-routing warning.",
                        quantity=quantity,
                        broker_order_id=self._broker_order_id(recovered_order),
                        raw_status=self._broker_order_status(recovered_order),
                        broker_status=self._broker_order_status(recovered_order),
                        requested_quantity=requested_quantity,
                        currently_held_quantity=held_quantity,
                        submitted_price=price,
                        filled_quantity=self._broker_filled_quantity(recovered_order),
                        average_fill_price=self._broker_average_fill_price(recovered_order),
                        cost_basis_per_share=cost_basis_per_share,
                    )

            logger.warning(
                "Broker order submission failed safely: symbol=%s side=%s requested=%s held=%s "
                "exception=%s message=%s",
                symbol, action, requested_quantity, held_quantity, exc.__class__.__name__, safe_message,
            )
            return self._build_execution_result(
                approved_decision,
                False,
                "Broker order submission failed.",
                quantity=quantity,
                raw_status=exc.__class__.__name__,
                requested_quantity=requested_quantity,
                currently_held_quantity=held_quantity,
                exception_class=exc.__class__.__name__,
                error_reason=safe_message,
                cost_basis_per_share=cost_basis_per_share,
            )

        broker_status = self._broker_order_status(broker_order)
        if broker_status in {"rejected", "error", "cancelled", "canceled", "expired"}:
            rejection = self._broker_rejection_message(broker_order) or f"Alpaca order status: {broker_status}."
            logger.warning(
                "Alpaca order was not accepted: symbol=%s side=%s requested=%s held=%s "
                "order_id=%s status=%s message=%s",
                symbol, action, requested_quantity, held_quantity,
                self._broker_order_id(broker_order), broker_status, rejection,
            )
            return self._build_execution_result(
                approved_decision,
                False,
                "Alpaca rejected or closed the order before acceptance.",
                quantity=quantity,
                broker_order_id=self._broker_order_id(broker_order),
                raw_status=broker_status,
                broker_status=broker_status,
                requested_quantity=requested_quantity,
                currently_held_quantity=held_quantity,
                submitted_price=price,
                filled_quantity=self._broker_filled_quantity(broker_order),
                average_fill_price=self._broker_average_fill_price(broker_order),
                cost_basis_per_share=cost_basis_per_share,
                error_reason=rejection,
            )

        logger.info("Alpaca paper order submitted for %s %s.", action, symbol)
        return self._build_execution_result(
            approved_decision,
            True,
            "Order submitted to Alpaca Paper Trading.",
            quantity=quantity,
            broker_order_id=self._broker_order_id(broker_order),
            raw_status=broker_status,
            broker_status=broker_status,
            requested_quantity=requested_quantity,
            currently_held_quantity=held_quantity,
            submitted_price=price,
            filled_quantity=self._broker_filled_quantity(broker_order),
            average_fill_price=self._broker_average_fill_price(broker_order),
            cost_basis_per_share=cost_basis_per_share,
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
        allowed_symbols = self._allowed_symbols_for_decision(decision)
        if allowed_symbols and symbol not in allowed_symbols:
            if self._uses_cycle_allowed_symbols(decision):
                return f"{symbol or 'Missing symbol'} is not in the final watchlist."
            return f"{symbol or 'Missing symbol'} is not in ALLOWED_SYMBOLS."

        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        if allocation is None:
            return "Suggested allocation must be greater than 0."
        if action == "SELL":
            if allocation < 0:
                return "Suggested allocation must be 0 or greater for SELL."
        elif allocation <= 0:
            return "Suggested allocation must be greater than 0."

        if allocation > self.settings.max_position_allocation_percent:
            return (
                f"Suggested allocation {allocation:.2f}% exceeds maximum "
                f"{self.settings.max_position_allocation_percent:.2f}%."
            )

        return None

    def _calculate_quantity(
        self,
        decision: dict[str, Any],
        latest_price: float,
        *,
        portfolio_value: float | None = None,
    ) -> int:
        snapshot = self._last_snapshot
        if portfolio_value is None:
            portfolio_value = (
                self._to_float(snapshot.account.get("portfolio_value")) if snapshot else None
            )
        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        if portfolio_value is None or portfolio_value <= 0 or allocation is None:
            return 0

        notional = portfolio_value * (allocation / 100)
        return int(notional // latest_price)

    def _calculate_sell_quantity(
        self,
        decision: dict[str, Any],
        latest_price: float,
        held_quantity: float,
    ) -> float:
        """Shares to sell so the remaining position matches the target allocation.

        For SELL decisions suggested_allocation_percent is the target remaining
        allocation after the sell, where 0 means fully exit the position.
        """
        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        if allocation is None or allocation < 0:
            return 0
        if allocation == 0:
            return held_quantity

        snapshot = self._last_snapshot
        portfolio_value = (
            self._to_float(snapshot.account.get("portfolio_value")) if snapshot else None
        )
        if portfolio_value is None or portfolio_value <= 0:
            return 0

        target_shares = int(portfolio_value * (allocation / 100) // latest_price)
        return max(0.0, held_quantity - target_shares)

    def _allowed_symbols_for_decision(self, decision: dict[str, Any]) -> list[str]:
        cycle_symbols = decision.get("cycle_allowed_symbols")
        if self._uses_cycle_allowed_symbols(decision):
            return [str(symbol).upper() for symbol in cycle_symbols if str(symbol).strip()]
        return self.settings.allowed_symbols

    def _uses_cycle_allowed_symbols(self, decision: dict[str, Any]) -> bool:
        return self.settings.dynamic_watchlist_enabled and isinstance(
            decision.get("cycle_allowed_symbols"),
            list,
        )

    def _buy_guard_failure(
        self,
        decision: dict[str, Any],
        latest_price: float,
    ) -> tuple[str | None, dict[str, Any]]:
        """Validate all BUY exposure limits from one broker-current risk snapshot."""
        symbol = str(decision.get("symbol", "")).upper()
        allocation = self._to_float(decision.get("suggested_allocation_percent"))
        try:
            portfolio_value, positions, open_buy_orders = self._refresh_buy_risk_state()
        except RuntimeError as exc:
            return str(exc), {}

        if portfolio_value is None or portfolio_value <= 0 or allocation is None:
            return "Portfolio value is missing or non-positive; BUY rejected safely.", {}

        price_cache = {symbol: latest_price}
        held_values: dict[str, float] = {}
        for position in positions:
            position_symbol = self._risk_value(position, "symbol", default="").upper()
            quantity = self._to_float(self._risk_value(position, "qty", "quantity")) or 0
            if not position_symbol or quantity <= 0:
                continue
            market_value = self._to_float(self._risk_value(position, "market_value"))
            if market_value is None:
                position_price = self._risk_latest_price(position_symbol, price_cache)
                if position_price is None or position_price <= 0:
                    return (
                        f"Current market value is unavailable for {position_symbol}; BUY rejected safely.",
                        {},
                    )
                market_value = quantity * position_price
            held_values[position_symbol] = held_values.get(position_symbol, 0.0) + abs(market_value)

        pending_symbols: set[str] = set()
        pending_values: dict[str, float] = {}
        pending_value = 0.0
        for order in open_buy_orders:
            order_symbol = self._risk_value(order, "symbol", default="").upper()
            if not order_symbol:
                continue
            pending_symbols.add(order_symbol)
            order_value = self._pending_buy_value(order, portfolio_value, price_cache)
            pending_values[order_symbol] = pending_values.get(order_symbol, 0.0) + order_value
            pending_value += order_value

        held_symbols = set(held_values)
        current_count = len(held_symbols)
        projected_symbols = held_symbols | pending_symbols | {symbol}
        projected_count = len(projected_symbols)
        invested_value = sum(held_values.values())
        requested_notional = portfolio_value * allocation / 100
        projected_invested_value = invested_value + pending_value + requested_notional
        current_percent = invested_value / portfolio_value * 100
        projected_percent = projected_invested_value / portfolio_value * 100
        details = {
            "portfolio_value": portfolio_value,
            "current_open_position_count": current_count,
            "projected_open_position_count": projected_count,
            "current_invested_percent": current_percent,
            "projected_invested_percent": projected_percent,
            "pending_buy_value": pending_value,
            "requested_order_value": requested_notional,
        }

        if projected_count > self.settings.max_open_positions:
            if current_count >= self.settings.max_open_positions and symbol not in held_symbols:
                reason = (
                    f"Maximum open positions reached: {current_count}/"
                    f"{self.settings.max_open_positions}."
                )
            else:
                reason = (
                    f"Projected open positions {projected_count} exceeds maximum "
                    f"{self.settings.max_open_positions}."
                )
            return reason, details

        current_symbol_value = held_values.get(symbol, 0.0) + pending_values.get(symbol, 0.0)
        projected_symbol_percent = (current_symbol_value + requested_notional) / portfolio_value * 100
        if projected_symbol_percent > self.settings.max_position_allocation_percent:
            return (
                f"Projected {symbol} allocation exceeds maximum "
                f"{self.settings.max_position_allocation_percent:.2f}%.",
                details,
            )

        if projected_percent > self.settings.max_total_invested_percent:
            return (
                f"Projected invested allocation {projected_percent:.1f}% exceeds maximum "
                f"{self.settings.max_total_invested_percent:.1f}%.",
                details,
            )
        return None, details

    def _refresh_buy_risk_state(self) -> tuple[float | None, list[Any], list[Any]]:
        """Fetch current account, long positions, and pending BUYs where supported."""
        api = getattr(self._broker, "api", None)
        snapshot = self._last_snapshot
        if api is not None and hasattr(api, "get_account"):
            try:
                account = api.get_account()
            except Exception as exc:
                raise RuntimeError(
                    f"Could not refresh broker portfolio value: {self._safe_exception_message(exc)}"
                ) from exc
            portfolio_value = self._to_float(
                self._risk_value(account, "portfolio_value", "equity")
            )
        else:
            account = snapshot.account if snapshot else {}
            portfolio_value = self._to_float(
                account.get("portfolio_value") or account.get("equity")
            )

        if api is not None and hasattr(api, "get_all_positions"):
            try:
                positions = list(api.get_all_positions() or [])
            except Exception as exc:
                raise RuntimeError(
                    f"Could not refresh broker positions: {self._safe_exception_message(exc)}"
                ) from exc
        else:
            positions = list(snapshot.positions if snapshot else [])

        open_buy_orders: list[Any] = []
        if api is not None and hasattr(api, "get_orders"):
            try:
                try:
                    orders = api.get_orders(status="open")
                except TypeError:
                    orders = api.get_orders()
            except Exception as exc:
                raise RuntimeError(
                    f"Could not inspect open BUY orders: {self._safe_exception_message(exc)}"
                ) from exc
            open_statuses = {"new", "accepted", "pending_new", "partially_filled", "held", "open"}
            for order in orders or []:
                side = self._risk_value(order, "side", default="")
                side = getattr(side, "value", side)
                status = self._broker_order_status(order) or ""
                if str(side).lower() == "buy" and status in open_statuses:
                    open_buy_orders.append(order)
        return portfolio_value, positions, open_buy_orders

    def _pending_buy_value(
        self,
        order: Any,
        portfolio_value: float,
        price_cache: dict[str, float | None],
    ) -> float:
        """Value remaining BUY exposure, reserving a full symbol cap if unknowable."""
        total_quantity = self._to_float(self._risk_value(order, "qty", "quantity"))
        filled_quantity = self._broker_filled_quantity(order) or 0.0
        remaining_quantity = (
            max(0.0, total_quantity - filled_quantity)
            if total_quantity is not None
            else None
        )
        notional = self._to_float(self._risk_value(order, "notional"))
        if notional is not None and notional > 0:
            if total_quantity and remaining_quantity is not None:
                return notional * remaining_quantity / total_quantity
            return notional
        if remaining_quantity is not None and remaining_quantity > 0:
            order_price = self._to_float(
                self._risk_value(order, "limit_price", "stop_price")
            )
            if order_price is None:
                order_symbol = self._risk_value(order, "symbol", default="").upper()
                order_price = self._risk_latest_price(order_symbol, price_cache)
            if order_price is not None and order_price > 0:
                return remaining_quantity * order_price
        return portfolio_value * self.settings.max_position_allocation_percent / 100

    def _risk_latest_price(
        self,
        symbol: str,
        price_cache: dict[str, float | None],
    ) -> float | None:
        if symbol in price_cache:
            return price_cache[symbol]
        price = None
        if self._broker is not None and hasattr(self._broker, "get_last_price"):
            try:
                price = self._get_last_price(symbol)
            except Exception as exc:
                logger.warning(
                    "Could not fetch current price for BUY validation: symbol=%s error=%s",
                    symbol,
                    exc.__class__.__name__,
                )
        if price is None:
            price = self._latest_price(symbol)
        price_cache[symbol] = price
        return price

    def _risk_value(self, value: Any, *names: str, default: Any = None) -> Any:
        for name in names:
            candidate = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
            if candidate not in (None, ""):
                return candidate
        return default

    def _refresh_sell_position(self, symbol: str) -> tuple[float | None, float | None, str | None]:
        """Return broker-current quantity and average entry immediately before a SELL."""
        broker = self._broker
        api = getattr(broker, "api", None) if broker is not None else None
        if api is not None and hasattr(api, "get_all_positions"):
            try:
                positions = api.get_all_positions()
            except Exception as exc:
                message = self._safe_exception_message(exc)
                return None, None, f"Could not refresh broker position for {symbol}: {message}"
            for position in positions:
                if str(getattr(position, "symbol", "")).upper() == symbol:
                    return (
                        abs(self._to_float(getattr(position, "qty", None)) or 0),
                        self._to_float(getattr(position, "avg_entry_price", None)),
                        None,
                    )
            return 0.0, None, None

        snapshot = self._last_snapshot
        for position in snapshot.positions if snapshot else []:
            if str(position.get("symbol", "")).upper() == symbol:
                return (
                    abs(self._to_float(position.get("quantity") or position.get("qty")) or 0),
                    self._to_float(position.get("average_price") or position.get("avg_entry_price")),
                    None,
                )
        return 0.0, None, None

    def _covering_open_sell_order(self, symbol: str, requested_quantity: float) -> Any | None:
        """Find an accepted/open SELL that already covers the intended quantity."""
        api = getattr(self._broker, "api", None)
        if api is None or not hasattr(api, "get_orders"):
            return None
        try:
            try:
                orders = api.get_orders(status="open")
            except TypeError:
                orders = api.get_orders()
        except Exception as exc:
            logger.warning("Could not inspect open SELL orders safely: %s.", exc.__class__.__name__)
            return None
        open_statuses = {"new", "accepted", "pending_new", "partially_filled", "held", "open"}
        for order in orders or []:
            order_symbol = str(getattr(order, "symbol", "")).upper()
            side = getattr(order, "side", "")
            side = getattr(side, "value", side)
            status = self._broker_order_status(order) or ""
            if order_symbol != symbol or str(side).lower() != "sell" or status not in open_statuses:
                continue
            total = self._to_float(getattr(order, "qty", None) or getattr(order, "quantity", None)) or 0
            filled = self._broker_filled_quantity(order) or 0
            if max(0.0, total - filled) >= requested_quantity:
                return order
        return None

    def reconcile_executions(self) -> dict[str, int]:
        """Refresh pending persisted executions from native Alpaca order state."""
        import database

        counts = {"reconciled": 0, "historical_matches": 0, "unavailable": 0, "errors": 0}
        api = getattr(self._broker, "api", None)
        if api is None:
            logger.warning("Execution reconciliation skipped because Alpaca API is unavailable.")
            return counts
        getter = getattr(api, "get_order_by_id", None) or getattr(api, "get_order", None)
        for execution in database.load_reconcilable_executions():
            order = None
            direct_not_found = getter is None
            try:
                if getter is not None:
                    order = getter(self._canonical_order_id(execution["broker_order_id"]))
            except Exception as exc:
                if self._is_order_not_found(exc):
                    direct_not_found = True
                else:
                    counts["errors"] += 1
                    database.record_reconciliation_unavailable(
                        int(execution["id"]),
                        f"{exc.__class__.__name__}: {self._safe_exception_message(exc)}",
                        terminal=False,
                    )
                    logger.warning(
                        "Execution reconciliation lookup failed safely for order %s: %s: %s",
                        execution.get("broker_order_id"),
                        exc.__class__.__name__,
                        self._safe_exception_message(exc),
                    )
                    continue

            if order is None and direct_not_found:
                order, history_available = self._find_historical_order(api, execution)
                if order is not None:
                    counts["historical_matches"] += 1
                elif history_available:
                    counts["unavailable"] += 1
                    database.record_reconciliation_unavailable(
                        int(execution["id"]),
                        "Order was not returned by direct lookup or Alpaca historical order queries.",
                        terminal=True,
                    )
                    logger.info(
                        "Execution order %s marked historical_unavailable after direct and historical lookup.",
                        execution.get("broker_order_id"),
                    )
                    continue
                else:
                    counts["errors"] += 1
                    database.record_reconciliation_unavailable(
                        int(execution["id"]),
                        "Alpaca historical order lookup was unavailable.",
                        terminal=False,
                    )
                    continue

            if order is None:
                continue
            try:
                broker_result = {
                    "broker_order_id": self._broker_order_id(order) or execution["broker_order_id"],
                    "broker_status": self._broker_order_status(order),
                    "filled_quantity": self._broker_filled_quantity(order),
                    "average_fill_price": self._broker_average_fill_price(order),
                    "error_reason": self._broker_rejection_message(order),
                }
                if database.reconcile_execution(int(execution["id"]), broker_result):
                    counts["reconciled"] += 1
                else:
                    counts["errors"] += 1
            except Exception as exc:
                counts["errors"] += 1
                logger.warning(
                    "Execution reconciliation failed safely for order %s: %s: %s",
                    execution.get("broker_order_id"), exc.__class__.__name__, self._safe_exception_message(exc),
                )
        logger.info("Execution reconciliation result: %s", counts)
        return counts

    def _find_historical_order(
        self,
        api: Any,
        execution: dict[str, Any],
    ) -> tuple[Any | None, bool]:
        """Query closed/all native Alpaca orders and match normalized UUIDs."""
        get_orders = getattr(api, "get_orders", None)
        if get_orders is None:
            return None, False

        target_id = self._normalized_order_id(execution.get("broker_order_id"))
        successful_query = False
        for status_name in ("all", "closed"):
            try:
                orders = self._query_historical_orders(get_orders, execution, status_name)
                successful_query = True
            except Exception as exc:
                logger.warning(
                    "Historical Alpaca order query failed safely: status=%s exception=%s message=%s",
                    status_name,
                    exc.__class__.__name__,
                    self._safe_exception_message(exc),
                )
                continue
            for order in self._flatten_broker_orders(orders):
                if self._normalized_order_id(self._broker_order_id(order)) == target_id:
                    return order, True
        return None, successful_query

    def _query_historical_orders(
        self,
        get_orders: Any,
        execution: dict[str, Any],
        status_name: str,
    ) -> list[Any]:
        """Use alpaca-py's native historical filter, with a compatibility fallback for mocks/older clients."""
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest

            timestamp = self._parse_execution_timestamp(execution.get("timestamp"))
            request = GetOrdersRequest(
                status=QueryOrderStatus.ALL if status_name == "all" else QueryOrderStatus.CLOSED,
                limit=500,
                after=timestamp - timedelta(days=2) if timestamp else None,
                until=timestamp + timedelta(days=2) if timestamp else None,
                symbols=[str(execution["symbol"]).upper()] if execution.get("symbol") else None,
            )
            result = get_orders(filter=request)
        except TypeError:
            result = get_orders(status=status_name)
        if isinstance(result, dict):
            result = result.get("orders", [])
        return list(result or [])

    def _flatten_broker_orders(self, orders: list[Any]) -> list[Any]:
        flattened = []
        for order in orders:
            flattened.append(order)
            legs = order.get("legs") if isinstance(order, dict) else getattr(order, "legs", None)
            if legs:
                flattened.extend(self._flatten_broker_orders(list(legs)))
        return flattened

    def _canonical_order_id(self, value: Any) -> str:
        text = str(value or "").strip()
        try:
            return str(UUID(text))
        except (ValueError, AttributeError):
            return text

    def _normalized_order_id(self, value: Any) -> str:
        return self._canonical_order_id(value).replace("-", "").lower()

    def _is_order_not_found(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        message = str(exc).lower()
        return (
            str(status_code) == "404"
            or str(code) == "404"
            or "404" in message
            or "order not found" in message
        )

    def _parse_execution_timestamp(self, value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

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
        quantity: int | float | None = None,
        broker_order_id: str | None = None,
        raw_status: str | None = None,
        **details: Any,
    ) -> dict[str, Any]:
        action = str(decision.get("action", "")).upper()
        symbol = str(decision.get("symbol", "")).upper()
        result = {
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
        result.update({key: value for key, value in details.items() if value is not None})
        return result

    def _broker_order_id(self, broker_order: Any) -> str | None:
        if isinstance(broker_order, dict):
            value = (
                broker_order.get("identifier")
                or broker_order.get("id")
                or broker_order.get("order_id")
            )
            return str(value) if value else None
        value = (
            getattr(broker_order, "identifier", None)
            or getattr(broker_order, "id", None)
            or getattr(broker_order, "order_id", None)
        )
        return str(value) if value else None

    def _broker_order_status(self, broker_order: Any) -> str | None:
        value = (
            broker_order.get("status")
            if isinstance(broker_order, dict)
            else getattr(broker_order, "status", None)
        )
        if hasattr(value, "value"):
            value = value.value
        return str(value).lower() if value else None

    def _broker_filled_quantity(self, broker_order: Any) -> float | None:
        if isinstance(broker_order, dict):
            return self._to_float(broker_order.get("filled_qty") or broker_order.get("filled_quantity"))
        return self._to_float(
            getattr(broker_order, "filled_qty", None)
            or getattr(broker_order, "filled_quantity", None)
        )

    def _broker_average_fill_price(self, broker_order: Any) -> float | None:
        if isinstance(broker_order, dict):
            return self._to_float(
                broker_order.get("filled_avg_price")
                or broker_order.get("average_fill_price")
                or broker_order.get("avg_fill_price")
            )
        return self._to_float(
            getattr(broker_order, "filled_avg_price", None)
            or getattr(broker_order, "average_fill_price", None)
            or getattr(broker_order, "avg_fill_price", None)
        )

    def _broker_rejection_message(self, broker_order: Any) -> str | None:
        if isinstance(broker_order, dict):
            value = (
                broker_order.get("reject_reason")
                or broker_order.get("rejection_reason")
                or broker_order.get("error_message")
                or broker_order.get("message")
            )
        else:
            value = (
                getattr(broker_order, "reject_reason", None)
                or getattr(broker_order, "rejection_reason", None)
                or getattr(broker_order, "error_message", None)
                or getattr(broker_order, "message", None)
            )
        if value in (None, ""):
            return None
        return " ".join(str(value).split())[:500]

    def _to_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
