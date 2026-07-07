"""Dynamic watchlist scanner using locally calculated market indicators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WatchlistCandidate:
    """Ranked scanner output for one symbol."""

    symbol: str
    score: int
    reasons_added: list[str]
    current_price: float | None
    day_change_percent: float | None
    volume: float | None
    relative_volume: float | None
    volatility_metric: float | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable scanner candidate."""
        return {
            "symbol": self.symbol,
            "score": self.score,
            "reasons_added": self.reasons_added,
            "current_price": self.current_price,
            "day_change_percent": self.day_change_percent,
            "volume": self.volume,
            "relative_volume": self.relative_volume,
            "volatility_metric": self.volatility_metric,
        }


class DynamicWatchlistScanner:
    """Scores a configured universe and returns the highest-ranked symbols."""

    def __init__(self, watchlist_size: int) -> None:
        self.watchlist_size = max(1, watchlist_size)

    def rank(
        self,
        universe: list[str],
        market_intelligence: dict[str, dict[str, Any]],
    ) -> list[WatchlistCandidate]:
        """Return a capped, deterministic ranking for the scanner universe."""
        merged_symbols = []
        seen_symbols = set()
        for symbol in universe:
            normalized_symbol = symbol.strip().upper()
            if normalized_symbol and normalized_symbol not in seen_symbols:
                seen_symbols.add(normalized_symbol)
                merged_symbols.append(normalized_symbol)

        candidates = [
            self._candidate(symbol, market_intelligence.get(symbol, {}))
            for symbol in merged_symbols
            if isinstance(market_intelligence.get(symbol, {}), dict)
            and market_intelligence.get(symbol, {})
        ]

        candidates.sort(
            key=lambda candidate: (
                -candidate.score,
                -(candidate.volume or 0),
                candidate.symbol,
            )
        )
        return candidates[: self.watchlist_size]

    def _candidate(self, symbol: str, indicators: dict[str, Any]) -> WatchlistCandidate:
        score = 0
        reasons = []

        volume = _to_float(indicators.get("volume"))
        day_change_percent = _to_float(indicators.get("day_change_percent"))
        relative_volume = _to_float(indicators.get("relative_volume"))
        volatility_metric = self._volatility_metric(indicators)

        if volume is not None and volume > 0:
            score += 2
            reasons.append("top volume")

        if day_change_percent is not None and day_change_percent >= 1:
            score += 2
            reasons.append("top gainer")

        if day_change_percent is not None and day_change_percent <= -1:
            score += 1
            reasons.append("top loser")

        if relative_volume is not None and relative_volume >= 1.5:
            score += 3
            reasons.append("high relative volume")

        if volatility_metric is not None and volatility_metric >= 2:
            score += 2
            reasons.append("high volatility")

        five_day_change = _to_float(indicators.get("5d_change_percent"))
        if five_day_change is not None and five_day_change >= 3:
            score += 2
            reasons.append("strong 5d momentum")

        twenty_day_change = _to_float(indicators.get("20d_change_percent"))
        if twenty_day_change is not None and twenty_day_change >= 5:
            score += 1
            reasons.append("strong 20d momentum")

        if not reasons:
            reasons.append("scanner universe")

        return WatchlistCandidate(
            symbol=symbol,
            score=score,
            reasons_added=reasons,
            current_price=_to_float(indicators.get("current_price")),
            day_change_percent=day_change_percent,
            volume=volume,
            relative_volume=relative_volume,
            volatility_metric=volatility_metric,
        )

    def _volatility_metric(self, indicators: dict[str, Any]) -> float | None:
        explicit_value = _to_float(indicators.get("ATR") or indicators.get("atr"))
        if explicit_value is not None:
            return explicit_value

        short_change = abs(_to_float(indicators.get("1h_change_percent")) or 0)
        day_change = abs(_to_float(indicators.get("day_change_percent")) or 0)
        if short_change == 0 and day_change == 0:
            return None
        return max(short_change, day_change)


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def broad_asset_symbol(asset: Any) -> str:
    """Return an uppercase symbol from an Alpaca asset-like object."""
    return str(getattr(asset, "symbol", "") or "").strip().upper()


def is_broad_scan_asset_candidate(asset: Any, exclude_etfs: bool = True) -> bool:
    """Return whether an Alpaca asset-like object is safe to consider."""
    symbol = broad_asset_symbol(asset)
    if not symbol:
        return False

    status = _normalized_asset_field(getattr(asset, "status", ""))
    if status and status != "active":
        return False

    tradable = getattr(asset, "tradable", True)
    if tradable is False:
        return False

    asset_class = _normalized_asset_field(
        getattr(asset, "asset_class", None)
        or getattr(asset, "class", None)
        or ""
    )
    if asset_class and asset_class not in {
        "us_equity",
        "usequity",
        "us equity",
        "stock",
        "equity",
    }:
        return False

    exchange = _normalized_asset_field(getattr(asset, "exchange", "")).upper()
    if exchange == "OTC":
        return False

    if exclude_etfs and _looks_like_etf(asset):
        return False

    return True


def passes_broad_liquidity_filters(
    indicators: dict[str, Any],
    min_stock_price: float,
    min_average_volume: float,
) -> bool:
    """Return whether indicator data meets broad scanner liquidity filters."""
    current_price = _to_float(indicators.get("current_price"))
    if current_price is None or current_price < min_stock_price:
        return False

    average_volume = _to_float(indicators.get("average_20d_volume"))
    if average_volume is not None and average_volume < min_average_volume:
        return False

    return True


def _looks_like_etf(asset: Any) -> bool:
    metadata_values = [
        getattr(asset, "asset_type", None),
        getattr(asset, "type", None),
    ]
    attributes = getattr(asset, "attributes", None)
    if isinstance(attributes, (list, tuple, set)):
        metadata_values.extend(attributes)

    normalized_values = {_normalized_asset_field(value) for value in metadata_values if value}
    return "etf" in normalized_values or "exchange_traded_fund" in normalized_values


def _normalized_asset_field(value: Any) -> str:
    """Normalize Alpaca enum/string fields without assuming one SDK shape."""
    if value in (None, ""):
        return ""

    enum_value = getattr(value, "value", None)
    if enum_value not in (None, ""):
        value = enum_value
    else:
        enum_name = getattr(value, "name", None)
        if enum_name not in (None, ""):
            value = enum_name

    normalized = str(value).strip().lower()
    if "." in normalized:
        normalized = normalized.rsplit(".", 1)[-1]
    return normalized.replace("-", "_").replace(" ", "_")
