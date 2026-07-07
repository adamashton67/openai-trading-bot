"""Broker integration layer for Lumibot and Alpaca Paper Trading."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

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
            for symbol in candidate_symbols:
                daily_bars = daily_bars_result["bars"].get(symbol)
                if daily_bars is None:
                    missing_bar_count += 1
                    continue

                indicators = calculate_market_indicators(symbol, None, daily_bars)
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
        message = str(exc).strip()
        return message if message else "<empty>"

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
        allowed_symbols = self._allowed_symbols_for_decision(decision)
        if allowed_symbols and symbol not in allowed_symbols:
            if self._uses_cycle_allowed_symbols(decision):
                return f"{symbol or 'Missing symbol'} is not in the final watchlist."
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
