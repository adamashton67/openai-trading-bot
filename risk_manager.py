"""Risk checks that validate AI suggestions before execution."""

import logging
from typing import Any

from config import Settings


logger = logging.getLogger(__name__)


ALLOCATION_EPSILON_PERCENT = 1e-9


class RiskManager:
    """Applies Python-owned guardrails to every AI trading suggestion."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def validate(self, decision: dict[str, Any]) -> tuple[bool, str]:
        """Return whether a decision is approved and why."""
        if not self.settings.bot_enabled:
            return False, "BOT_ENABLED is false."

        if not self.settings.paper_trading:
            return False, "Live trading is disabled in this starter project."

        if self.settings.dry_run:
            return False, "DRY_RUN is true."

        action = str(decision.get("action", "")).lower()
        if action in {"hold", "none", "skip"}:
            return False, "AI decision requested no trade."

        symbol = str(decision.get("symbol", "")).upper()
        allowed_symbols = self._allowed_symbols(decision)
        if allowed_symbols and symbol not in allowed_symbols:
            if self._uses_cycle_allowed_symbols(decision):
                return False, f"{symbol or 'Missing symbol'} is not in the final watchlist."
            return False, f"{symbol or 'Missing symbol'} is not in ALLOWED_SYMBOLS."

        buy_eligible_symbols = decision.get("cycle_buy_eligible_symbols")
        if (
            action == "buy"
            and isinstance(buy_eligible_symbols, list)
            and symbol not in {str(value).upper() for value in buy_eligible_symbols}
        ):
            return False, f"{symbol or 'Missing symbol'} is not currently eligible for additional BUY exposure."

        data_age = self._to_float(decision.get("market_data_age_seconds"))
        max_data_age = float(getattr(self.settings, "max_market_data_age_seconds", 180))
        if action == "buy" and data_age is not None and data_age > max_data_age:
            return False, (
                f"Market data for {symbol or 'the selected symbol'} is {data_age:.0f}s old, "
                f"above the {max_data_age:.0f}s limit."
            )

        confidence = float(decision.get("confidence", 0))
        if confidence < self.settings.min_confidence:
            return False, f"Confidence {confidence:.2f} is below minimum {self.settings.min_confidence:.2f}."

        allocation = float(decision.get("suggested_allocation_percent", 0))
        if action == "sell":
            if allocation < 0:
                return False, "Suggested allocation must be 0 or greater for SELL."
        elif allocation <= 0:
            return False, "Suggested allocation must be greater than 0."

        if (
            allocation
            > self.settings.max_position_allocation_percent + ALLOCATION_EPSILON_PERCENT
        ):
            return False, (
                f"Suggested allocation {allocation:.2f}% exceeds maximum "
                f"{self.settings.max_position_allocation_percent:.2f}%."
            )

        logger.info("Risk manager approved decision for %s.", symbol)
        return True, "Approved by starter risk checks."

    def _allowed_symbols(self, decision: dict[str, Any]) -> list[str]:
        cycle_symbols = decision.get("cycle_allowed_symbols")
        if self._uses_cycle_allowed_symbols(decision):
            return [str(symbol).upper() for symbol in cycle_symbols if str(symbol).strip()]
        return self.settings.allowed_symbols

    def _uses_cycle_allowed_symbols(self, decision: dict[str, Any]) -> bool:
        return self.settings.dynamic_watchlist_enabled and isinstance(
            decision.get("cycle_allowed_symbols"),
            list,
        )

    def _to_float(self, value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
