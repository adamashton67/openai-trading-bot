"""Safe local utility for running only the dynamic watchlist scanner."""

from __future__ import annotations

import logging
from typing import Any

from broker import BrokerClient
from config import Settings


logger = logging.getLogger(__name__)


def run_scanner_test(settings: Settings, broker: BrokerClient) -> int:
    """Collect scanner output and exit without OpenAI, risk checks, or execution."""
    logger.info("Starting scanner test command.")
    logger.info("Dynamic watchlist enabled for scanner test: %s", settings.dynamic_watchlist_enabled)
    logger.info("Broad market scan enabled: %s", settings.broad_market_scan_enabled)
    logger.info("Watchlist size: %s", settings.watchlist_size)
    logger.info("Broad scanner max symbols: %s", settings.broad_market_max_symbols)
    logger.info(
        "Max scanner candidates after filters: %s",
        settings.max_scanner_candidates_after_filters,
    )

    snapshot = broker.collect_snapshot()
    market_data = snapshot.market_data or {}
    final_symbols = [str(symbol).upper() for symbol in market_data.get("symbols", [])]
    dynamic_watchlist = market_data.get("dynamic_watchlist", [])

    logger.info("Scanner status: %s", market_data.get("scanner_status"))
    logger.info("Scanner mode: %s", market_data.get("scanner_mode"))
    _log_optional_count("Assets after price filters", market_data.get("broad_price_filtered_count"))
    _log_optional_count("Capped indicator candidates", market_data.get("broad_capped_candidate_count"))
    _log_optional_count("Liquid broad candidates", market_data.get("broad_candidate_count"))
    logger.info("Final watchlist size: %s", len(final_symbols))
    logger.info("Final selected symbols: %s", ", ".join(final_symbols) or "none")

    if isinstance(dynamic_watchlist, list) and dynamic_watchlist:
        logger.info("Top scanner candidates: %s", _candidate_summary(dynamic_watchlist[:10]))

    logger.info("Scanner test complete. No OpenAI call, risk check, or order execution was run.")
    return 0


def _log_optional_count(label: str, value: Any) -> None:
    if value is not None:
        logger.info("%s: %s", label, value)


def _candidate_summary(candidates: list[dict[str, Any]]) -> str:
    parts = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol", "")).upper()
        score = candidate.get("score")
        if symbol:
            parts.append(f"{symbol}({score})")
    return ", ".join(parts) or "none"
