"""Risk checks that validate AI suggestions before execution."""

import logging
from typing import Any

from config import Settings


logger = logging.getLogger(__name__)


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
        if self.settings.allowed_symbols and symbol not in self.settings.allowed_symbols:
            return False, f"{symbol or 'Missing symbol'} is not in ALLOWED_SYMBOLS."

        confidence = float(decision.get("confidence", 0))
        if confidence < self.settings.min_confidence:
            return False, f"Confidence {confidence:.2f} is below minimum {self.settings.min_confidence:.2f}."

        allocation = float(decision.get("suggested_allocation_percent", 0))
        if allocation <= 0:
            return False, "Suggested allocation must be greater than 0."

        if allocation > self.settings.max_position_allocation_percent:
            return False, (
                f"Suggested allocation {allocation:.2f}% exceeds maximum "
                f"{self.settings.max_position_allocation_percent:.2f}%."
            )

        logger.info("Risk manager approved decision for %s.", symbol)
        return True, "Approved by starter risk checks."
