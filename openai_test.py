"""Safe OpenAI integration test utilities."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from config import Settings
from openai_logic import AIDecisionError, OpenAIDecisionClient, TradingContext
from risk_manager import RiskManager


logger = logging.getLogger(__name__)


def build_mock_trading_context(settings: Settings) -> TradingContext:
    """Build realistic fake paper-trading context for OpenAI testing."""
    watchlist_symbols = settings.allowed_symbols or ["AAPL", "MSFT", "SPY"]

    return TradingContext(
        current_datetime=datetime.now(ZoneInfo(settings.market_timezone)),
        market_status="TEST_MODE_MARKET_OPEN",
        account_cash=25000,
        buying_power=25000,
        portfolio_value=100000,
        current_positions=[
            {
                "symbol": "MSFT",
                "quantity": 2,
                "average_price": 431.21,
                "market_price": 434.80,
            },
            {
                "symbol": "SPY",
                "quantity": 1,
                "average_price": 548.10,
                "market_price": 550.25,
            },
        ],
        watchlist_symbols=watchlist_symbols,
        recent_price_data={
            "AAPL": {
                "last_price": 214.33,
                "previous_close": 212.41,
                "day_change_percent": 0.90,
                "volume": 48500000,
            },
            "MSFT": {
                "last_price": 434.80,
                "previous_close": 431.21,
                "day_change_percent": 0.83,
                "volume": 22100000,
            },
            "SPY": {
                "last_price": 550.25,
                "previous_close": 548.10,
                "day_change_percent": 0.39,
                "volume": 61200000,
            },
        },
        risk_rules={
            "max_allocation_percent": settings.max_position_allocation_percent,
            "allowed_symbols": watchlist_symbols,
            "minimum_confidence": settings.min_confidence,
            "paper_trading_only": True,
            "dry_run_enabled": True,
            "risk_manager_required": True,
        },
        previous_trade_summary=(
            "Previous test day: 2 paper trades, 1 rejected trade, no live orders."
        ),
    )


def run_openai_integration_test(settings: Settings) -> int:
    """Call OpenAI with mock context, validate the response, and exit safely."""
    logger.info("Starting safe OpenAI integration test.")

    try:
        client = OpenAIDecisionClient(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            prompts_dir=settings.prompts_dir,
        )
        client._load_prompt("system_prompt.md")
        client._load_prompt("user_prompt_template.md")
    except (AIDecisionError, OSError) as exc:
        logger.error("OpenAI integration test failed safely: %s", exc)
        logger.info("No trades were placed.")
        return 1

    logger.info("Prompts loaded successfully.")

    context = build_mock_trading_context(settings)
    logger.info("Mock trading context built for %s.", ", ".join(context.watchlist_symbols))
    logger.info("OpenAI API call started.")

    try:
        decision = client.get_decision(context)
    except AIDecisionError as exc:
        logger.error("OpenAI integration test failed safely: %s", exc)
        logger.info("No trades were placed.")
        return 1

    logger.info("Raw OpenAI response received and parsed.")
    logger.info("Validated AI decision: %s", decision.model_dump(exclude_none=True))

    risk_settings = replace(
        settings,
        bot_enabled=True,
        paper_trading=True,
        dry_run=True,
    )
    approved, reason = RiskManager(risk_settings).validate(decision.to_risk_manager_dict())
    logger.info("Risk manager result in DRY_RUN mode: approved=%s reason=%s", approved, reason)
    logger.info("No trades were placed. Lumibot and broker execution were not called.")
    return 0
