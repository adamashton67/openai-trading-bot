"""Technical indicator calculations for OpenAI market context."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd


logger = logging.getLogger(__name__)


INDICATOR_FIELDS = (
    "current_price",
    "5m_change_percent",
    "15m_change_percent",
    "1h_change_percent",
    "day_change_percent",
    "5d_change_percent",
    "20d_change_percent",
    "volume",
    "average_20d_volume",
    "relative_volume",
    "EMA20",
    "EMA50",
    "RSI14",
    "VWAP",
)


def calculate_market_indicators(
    symbol: str,
    minute_bars: Any,
    daily_bars: Any,
) -> dict[str, Any]:
    """Calculate technical indicators from recent minute and daily bars."""
    indicators = {field_name: None for field_name in INDICATOR_FIELDS}
    indicators["symbol"] = symbol.upper()

    try:
        minute_df = _normalize_bars(minute_bars)
        daily_df = _normalize_bars(daily_bars)
    except Exception as exc:
        logger.warning("Could not normalize bars for %s: %s.", symbol, exc.__class__.__name__)
        return indicators

    if minute_df.empty and daily_df.empty:
        logger.warning("No bars available for %s; indicators are null.", symbol)
        return indicators

    close_source = minute_df if not minute_df.empty else daily_df
    indicators["current_price"] = _last_value(close_source, "close")
    indicators["volume"] = _current_volume(minute_df, daily_df)

    indicators["5m_change_percent"] = _period_change_percent(minute_df, 5, symbol, "5m")
    indicators["15m_change_percent"] = _period_change_percent(minute_df, 15, symbol, "15m")
    indicators["1h_change_percent"] = _period_change_percent(minute_df, 60, symbol, "1h")
    indicators["day_change_percent"] = _day_change_percent(minute_df, daily_df, symbol)
    indicators["5d_change_percent"] = _period_change_percent(daily_df, 5, symbol, "5d")
    indicators["20d_change_percent"] = _period_change_percent(daily_df, 20, symbol, "20d")
    indicators["average_20d_volume"] = _average_volume(daily_df, 20, symbol)
    indicators["relative_volume"] = _relative_volume(
        indicators["volume"],
        indicators["average_20d_volume"],
        symbol,
    )
    indicators["EMA20"] = _ema(close_source, 20, symbol)
    indicators["EMA50"] = _ema(close_source, 50, symbol)
    indicators["RSI14"] = _rsi(close_source, 14, symbol)
    indicators["VWAP"] = _vwap(minute_df if not minute_df.empty else daily_df, symbol)

    return indicators


def _normalize_bars(bars: Any) -> pd.DataFrame:
    if bars is None:
        return _empty_bars()

    if hasattr(bars, "df"):
        bars = bars.df
    elif hasattr(bars, "pandas_df"):
        bars = bars.pandas_df

    if isinstance(bars, pd.DataFrame):
        df = bars.copy()
    else:
        df = pd.DataFrame(bars)

    if df.empty:
        return _empty_bars()

    df.columns = [str(column).lower() for column in df.columns]
    if "timestamp" in df.columns:
        df = df.sort_values("timestamp")
    elif not df.index.is_monotonic_increasing:
        df = df.sort_index()

    for column in ("open", "high", "low", "close", "volume"):
        if column not in df.columns:
            df[column] = pd.NA
        df[column] = pd.to_numeric(df[column], errors="coerce")

    return df.dropna(subset=["close"]).reset_index(drop=True)


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _last_value(df: pd.DataFrame, column: str) -> float | None:
    if df.empty or column not in df.columns:
        return None
    return _to_float(df[column].iloc[-1])


def _current_volume(minute_df: pd.DataFrame, daily_df: pd.DataFrame) -> float | None:
    if not minute_df.empty and "volume" in minute_df.columns:
        return _to_float(minute_df["volume"].sum())
    return _last_value(daily_df, "volume")


def _period_change_percent(
    df: pd.DataFrame,
    periods_back: int,
    symbol: str,
    label: str,
) -> float | None:
    if df.empty or len(df) <= periods_back:
        logger.warning("Insufficient %s bars for %s change.", label, symbol)
        return None

    current = _to_float(df["close"].iloc[-1])
    previous = _to_float(df["close"].iloc[-(periods_back + 1)])
    return _percent_change(current, previous)


def _day_change_percent(minute_df: pd.DataFrame, daily_df: pd.DataFrame, symbol: str) -> float | None:
    current = _last_value(minute_df, "close") if not minute_df.empty else _last_value(daily_df, "close")
    if not minute_df.empty:
        opening_value = _to_float(minute_df["open"].dropna().iloc[0]) if minute_df["open"].notna().any() else None
        return _percent_change(current, opening_value)

    if len(daily_df) <= 1:
        logger.warning("Insufficient daily bars for %s day change.", symbol)
        return None
    return _percent_change(current, _to_float(daily_df["close"].iloc[-2]))


def _average_volume(df: pd.DataFrame, window: int, symbol: str) -> float | None:
    if df.empty or len(df) < window:
        logger.warning("Insufficient daily bars for %s average %sd volume.", symbol, window)
        return None

    return _to_float(df["volume"].tail(window).mean())


def _relative_volume(volume: Any, average_volume: Any, symbol: str) -> float | None:
    volume_value = _to_float(volume)
    average_value = _to_float(average_volume)
    if volume_value is None or average_value is None or average_value <= 0:
        logger.warning("Insufficient volume data for %s relative volume.", symbol)
        return None
    return volume_value / average_value


def _ema(df: pd.DataFrame, span: int, symbol: str) -> float | None:
    if df.empty or len(df) < span:
        logger.warning("Insufficient bars for %s EMA%s.", symbol, span)
        return None
    return _to_float(df["close"].ewm(span=span, adjust=False).mean().iloc[-1])


def _rsi(df: pd.DataFrame, period: int, symbol: str) -> float | None:
    if df.empty or len(df) <= period:
        logger.warning("Insufficient bars for %s RSI%s.", symbol, period)
        return None

    delta = df["close"].diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    average_gain = gains.rolling(window=period).mean()
    average_loss = losses.rolling(window=period).mean()
    loss_value = _to_float(average_loss.iloc[-1])
    gain_value = _to_float(average_gain.iloc[-1])
    if gain_value is None or loss_value is None:
        return None
    if loss_value == 0:
        return 100.0

    relative_strength = gain_value / loss_value
    return 100 - (100 / (1 + relative_strength))


def _vwap(df: pd.DataFrame, symbol: str) -> float | None:
    if df.empty or not {"high", "low", "close", "volume"}.issubset(df.columns):
        logger.warning("Insufficient bars for %s VWAP.", symbol)
        return None

    total_volume = _to_float(df["volume"].sum())
    if total_volume is None or total_volume <= 0:
        logger.warning("Insufficient volume for %s VWAP.", symbol)
        return None

    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    return _to_float((typical_price * df["volume"]).sum() / total_volume)


def _percent_change(current: Any, previous: Any) -> float | None:
    current_value = _to_float(current)
    previous_value = _to_float(previous)
    if current_value is None or previous_value is None or previous_value == 0:
        return None
    return ((current_value - previous_value) / previous_value) * 100


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if pd.isna(value):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric
