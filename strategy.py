"""Trading strategy orchestration for AI suggestions and risk checks."""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import database
from broker import BrokerClient, BrokerSnapshot
from config import Settings
from openai_logic import AIDecisionError, OpenAIDecisionClient, TradingContext
from risk_manager import RiskManager
from storage import TradingJournal


logger = logging.getLogger(__name__)


class TradingStrategy:
    """Coordinates prompt loading, AI decisions, risk checks, and execution."""

    def __init__(
        self,
        settings: Settings,
        broker: BrokerClient,
        risk_manager: RiskManager,
        journal: TradingJournal | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.risk_manager = risk_manager
        self.journal = journal
        self.ai_client: OpenAIDecisionClient | None = None
        self._last_ai_raw_response: str | None = None

    def run_cycle(self) -> None:
        """Run one complete trading cycle."""
        logger.info("Starting trading cycle.")

        snapshot = self.broker.collect_snapshot()
        current_time = datetime.now(ZoneInfo(self.settings.market_timezone))
        self._record_market_snapshots(snapshot, current_time)
        if self.journal is not None:
            self.journal.record_balance_snapshot(
                trading_day=current_time.date(),
                account=snapshot.account,
                positions=snapshot.positions,
                timestamp=current_time,
            )

        decision = self.get_ai_decision(snapshot)
        logger.info("AI decision returned: %s", decision)
        if self.journal is not None:
            self.journal.record_ai_decision(
                trading_day=current_time.date(),
                decision=decision,
                timestamp=current_time,
            )

        approved, reason = self.risk_manager.validate(decision)
        if not approved:
            logger.info("Trading cycle skipped by risk manager: %s", reason)
            if self.journal is not None and decision.get("action") != "HOLD":
                self.journal.record_rejected_trade(
                    trading_day=current_time.date(),
                    decision=decision,
                    reason=reason,
                    timestamp=current_time,
                )
            self._record_database_decision(
                decision=decision,
                approved=False,
                approval_reason=reason,
                executed=False,
                timestamp=current_time,
            )
            return

        result = self.broker.execute_order(decision)
        logger.info("Execution result: %s", result)
        if self.journal is not None:
            self.journal.record_trade_result(
                trading_day=current_time.date(),
                decision=decision,
                result=result,
                timestamp=current_time,
            )
            if not result.get("executed"):
                self.journal.record_rejected_trade(
                    trading_day=current_time.date(),
                    decision=decision,
                    reason=result.get("reason", "Execution rejected."),
                    timestamp=current_time,
                )
        self._record_database_decision(
            decision=decision,
            approved=True,
            approval_reason=reason,
            executed=bool(result.get("executed")),
            timestamp=current_time,
        )

    def get_ai_decision(self, snapshot: BrokerSnapshot) -> dict[str, Any]:
        """Ask OpenAI for a validated suggestion and return it for risk checks."""
        context = self._build_ai_context(snapshot)
        self._last_ai_raw_response = None

        try:
            ai_client = self._get_ai_client()
            decision = ai_client.get_decision(context)
            self._last_ai_raw_response = ai_client.last_raw_response
        except AIDecisionError as exc:
            logger.warning("AI decision unavailable. Falling back to HOLD: %s", exc)
            return {
                "symbol": self._fallback_hold_symbol(),
                "action": "HOLD",
                "confidence": 0,
                "suggested_allocation_percent": 0,
                "reason": str(exc),
            }

        return decision.to_risk_manager_dict()

    def _record_database_decision(
        self,
        decision: dict[str, Any],
        approved: bool | None,
        approval_reason: str | None,
        executed: bool | None,
        timestamp: datetime,
    ) -> None:
        """Persist decision metadata without affecting the trading cycle."""
        database.insert_decision(
            decision=decision,
            raw_response=self._last_ai_raw_response or decision,
            approved=approved,
            approval_reason=approval_reason,
            executed=executed,
            timestamp=timestamp,
        )

    def _record_market_snapshots(self, snapshot: BrokerSnapshot, timestamp: datetime) -> None:
        """Persist enriched market indicators without affecting trading flow."""
        market_intelligence = snapshot.market_data.get("market_intelligence", {})
        if not isinstance(market_intelligence, dict):
            return

        recorded_symbols = set()
        for symbol, indicators in market_intelligence.items():
            normalized_symbol = str(symbol).upper()
            if not normalized_symbol or normalized_symbol in recorded_symbols:
                continue
            if isinstance(indicators, dict):
                try:
                    database.insert_market_snapshot(
                        symbol=normalized_symbol,
                        snapshot=indicators,
                        timestamp=timestamp,
                    )
                except Exception as exc:
                    logger.error(
                        "Database market snapshot insert failed safely: %s.",
                        exc.__class__.__name__,
                    )
                recorded_symbols.add(normalized_symbol)

    def _get_ai_client(self) -> OpenAIDecisionClient:
        """Create the OpenAI client only when a trading cycle needs it."""
        if self.ai_client is None:
            self.ai_client = OpenAIDecisionClient(
                api_key=self.settings.openai_api_key,
                model=self.settings.openai_model,
                prompts_dir=self.settings.prompts_dir,
            )
        return self.ai_client

    def _build_ai_context(self, snapshot: BrokerSnapshot) -> TradingContext:
        """Create the OpenAI context object from broker and strategy state."""
        account = snapshot.account

        return TradingContext(
            current_datetime=datetime.now(ZoneInfo(self.settings.market_timezone)),
            market_status="open",
            account_cash=account.get("cash"),
            buying_power=account.get("buying_power"),
            portfolio_value=account.get("portfolio_value"),
            current_positions=snapshot.positions,
            watchlist_symbols=self.settings.allowed_symbols,
            recent_price_data=snapshot.market_data,
            risk_rules={
                "paper_trading": self.settings.paper_trading,
                "min_confidence": self.settings.min_confidence,
                "max_position_allocation_percent": (
                    self.settings.max_position_allocation_percent
                ),
                "allowed_symbols": self.settings.allowed_symbols,
                "risk_manager_required": True,
            },
            previous_trade_summary=None,
        )

    def _fallback_hold_symbol(self) -> str:
        """Return a watchlist-backed symbol for HOLD fallbacks."""
        if "SPY" in self.settings.allowed_symbols:
            return "SPY"
        if self.settings.allowed_symbols:
            return self.settings.allowed_symbols[0]
        return "SPY"
