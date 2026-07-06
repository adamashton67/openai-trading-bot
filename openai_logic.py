"""OpenAI decision layer for trade suggestions.

OpenAI suggests. Python validates. Lumibot executes only after risk approval.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


logger = logging.getLogger(__name__)


Action = Literal["BUY", "SELL", "HOLD"]


class AIDecisionError(Exception):
    """Raised when OpenAI cannot produce a validated trade decision."""


class TradingContext(BaseModel):
    """Live trading context sent to OpenAI for one decision cycle."""

    model_config = ConfigDict(extra="forbid")

    current_datetime: datetime
    market_status: str
    account_cash: float | None = None
    buying_power: float | None = None
    portfolio_value: float | None = None
    current_positions: list[dict[str, Any]] = Field(default_factory=list)
    watchlist_symbols: list[str] = Field(default_factory=list)
    recent_price_data: dict[str, Any] = Field(default_factory=dict)
    risk_rules: dict[str, Any] = Field(default_factory=dict)
    previous_trade_summary: str | None = None

    @field_validator("watchlist_symbols")
    @classmethod
    def uppercase_symbols(cls, symbols: list[str]) -> list[str]:
        """Normalize watchlist symbols before rendering prompts."""
        return [symbol.upper() for symbol in symbols]


class AIDecision(BaseModel):
    """Validated AI trade suggestion passed to the Python risk manager."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1)
    action: Action
    confidence: float = Field(ge=0, le=1)
    suggested_allocation_percent: float = Field(ge=0)
    reason: str = Field(min_length=1)
    stop_loss_percent: float | None = Field(default=None, gt=0, le=100)
    take_profit_percent: float | None = Field(default=None, gt=0, le=100)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, symbol: str) -> str:
        """Normalize ticker symbols for downstream risk checks."""
        normalized_symbol = symbol.strip().upper()
        if normalized_symbol in {"CASH", "NONE"}:
            raise ValueError("symbol must be a tradable watchlist symbol")
        return normalized_symbol

    @field_validator("action", mode="before")
    @classmethod
    def normalize_action(cls, action: Any) -> str:
        """Reject unsupported actions while accepting lowercase JSON values."""
        if not isinstance(action, str):
            raise ValueError("action must be BUY, SELL, or HOLD")
        return action.strip().upper()

    def to_risk_manager_dict(self) -> dict[str, Any]:
        """Return a plain dictionary compatible with the risk manager."""
        return self.model_dump(exclude_none=True)


class OpenAIDecisionClient:
    """Loads prompts, calls OpenAI, and validates the JSON trade suggestion."""

    def __init__(
        self,
        api_key: str,
        model: str,
        prompts_dir: Path,
    ) -> None:
        if not api_key:
            raise AIDecisionError("OPENAI_API_KEY is required for AI decisions.")

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise AIDecisionError(
                "The openai package is required for AI decisions. "
                "Install dependencies from requirements.txt."
            ) from exc

        self.model = model
        self.prompts_dir = prompts_dir
        self.client = OpenAI(api_key=api_key)
        self.last_raw_response: str | None = None

    def get_decision(self, context: TradingContext) -> AIDecision:
        """Request and validate one structured trade decision from OpenAI."""
        system_prompt = self._load_prompt("system_prompt.md")
        user_prompt = self._render_user_prompt(context)

        logger.info("Requesting AI decision from OpenAI model %s.", self.model)

        response = self.client.chat.completions.create(
            **self._build_chat_completion_request(system_prompt, user_prompt)
        )

        raw_content = response.choices[0].message.content
        if not raw_content:
            raise AIDecisionError("OpenAI returned an empty decision.")

        self.last_raw_response = raw_content
        logger.debug("Raw OpenAI decision content: %s", raw_content)
        return self._parse_decision(raw_content)

    def _build_chat_completion_request(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        """Build model-aware Chat Completions request parameters."""
        request_params: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        if self._model_supports_temperature():
            request_params["temperature"] = 0.2

        return request_params

    def _model_supports_temperature(self) -> bool:
        """Return whether the configured model accepts temperature."""
        return not self.model.lower().startswith("gpt-5")

    def _load_prompt(self, file_name: str) -> str:
        """Load a prompt file from the configured prompts directory."""
        prompt_path = self.prompts_dir / file_name
        logger.debug("Loading prompt file: %s", prompt_path)
        return prompt_path.read_text(encoding="utf-8")

    def _render_user_prompt(self, context: TradingContext) -> str:
        """Fill the user prompt template with live trading context."""
        template_path = self.prompts_dir / "user_prompt_template.md"
        template = template_path.read_text(encoding="utf-8")

        context_json = context.model_dump_json(indent=2)
        context_dict = context.model_dump(mode="json")
        account_summary = {
            "cash": context_dict["account_cash"],
            "buying_power": context_dict["buying_power"],
            "portfolio_value": context_dict["portfolio_value"],
        }
        replacements = {
            "current_datetime": context_dict["current_datetime"],
            "market_status": context_dict["market_status"],
            "account_summary": json.dumps(account_summary, indent=2),
            "positions": json.dumps(context_dict["current_positions"], indent=2),
            "watchlist": json.dumps(context_dict["watchlist_symbols"], indent=2),
            "recent_market_data": json.dumps(
                context_dict["recent_price_data"],
                indent=2,
            ),
            "risk_rules": json.dumps(context_dict["risk_rules"], indent=2),
            "previous_trades": context.previous_trade_summary or "None",
            "context": context_json,
        }

        rendered = template
        for placeholder, value in replacements.items():
            rendered = rendered.replace(f"{{{{{placeholder}}}}}", str(value))
        return rendered

    def _parse_decision(self, raw_content: str) -> AIDecision:
        """Parse JSON and validate the response against the decision schema."""
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            logger.warning("OpenAI returned invalid JSON: %s", exc)
            raise AIDecisionError("OpenAI returned invalid JSON.") from exc

        parsed = self._normalize_optional_exit_percentages(parsed)

        try:
            decision = AIDecision.model_validate(parsed)
        except ValidationError as exc:
            logger.warning("OpenAI decision failed validation: %s", exc)
            raise AIDecisionError("OpenAI decision failed validation.") from exc

        logger.info(
            "Validated AI decision: action=%s symbol=%s confidence=%.2f",
            decision.action,
            decision.symbol,
            decision.confidence,
        )
        return decision

    def _normalize_optional_exit_percentages(self, parsed: Any) -> Any:
        """Treat model-supplied zero optional exit percentages as absent values."""
        if not isinstance(parsed, dict):
            return parsed

        for field_name in ("stop_loss_percent", "take_profit_percent"):
            if parsed.get(field_name) == 0:
                parsed[field_name] = None
        return parsed
