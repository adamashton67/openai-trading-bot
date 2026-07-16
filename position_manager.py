"""Deterministic management of existing positions, independent of OpenAI."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from zoneinfo import ZoneInfo

import database
from config import Settings


logger = logging.getLogger(__name__)

PARTIAL_TARGET_PERCENT = Decimal("3.0")
TRAILING_STOP_PERCENT = Decimal("2.0")
FRACTIONAL_QUANTUM = Decimal("0.000001")
OPEN_ORDER_STATUSES = {"new", "accepted", "pending_new", "partially_filled", "partial_fill", "held", "open"}


class PositionManager:
    """Apply partial-profit and post-partial trailing rules to broker holdings."""

    def __init__(self, settings: Settings, broker: Any) -> None:
        self.settings = settings
        self.broker = broker

    def run_once(self) -> dict[str, int]:
        """Reconcile state and manage each current position independently."""
        counts = {"checked": 0, "submitted": 0, "skipped": 0, "closed": 0, "errors": 0}
        now = datetime.now(ZoneInfo(self.settings.market_timezone))
        try:
            reconcile = getattr(self.broker, "reconcile_executions", None)
            if callable(reconcile):
                reconcile()
            positions = self.broker.refresh_open_positions()
        except Exception as exc:
            logger.warning("Position management skipped: broker holdings refresh failed (%s).", exc.__class__.__name__)
            counts["errors"] += 1
            return counts

        positions_by_symbol = {
            str(position.get("symbol") or "").upper(): position
            for position in positions
            if str(position.get("symbol") or "").strip()
        }
        for state in database.load_active_position_management():
            symbol = str(state["symbol"]).upper()
            if symbol not in positions_by_symbol:
                database.close_position_management(symbol, closed_at=now)
                logger.info("Position management closed stale state for %s; broker quantity is zero.", symbol)
                counts["closed"] += 1

        logger.info("Position management checking %s open positions.", len(positions_by_symbol))
        for symbol, position in positions_by_symbol.items():
            try:
                self._manage_symbol(symbol, position, now, counts)
            except Exception as exc:
                counts["errors"] += 1
                logger.warning("Position management skipped %s safely: %s.", symbol, exc.__class__.__name__)
        logger.info("Position management run complete: %s", counts)
        return counts

    def _manage_symbol(
        self,
        symbol: str,
        position: dict[str, Any],
        now: datetime,
        counts: dict[str, int],
    ) -> None:
        quantity = self._decimal(position.get("quantity"))
        cost_basis = self._decimal(position.get("average_price"))
        if quantity is None or quantity <= 0:
            counts["skipped"] += 1
            logger.info("Position management skipped %s: no positive broker quantity.", symbol)
            return
        if cost_basis is None or cost_basis <= 0:
            counts["skipped"] += 1
            logger.info("Position management skipped %s: broker cost basis is unavailable.", symbol)
            return

        state = self._load_or_adopt(symbol, quantity, cost_basis, now)
        if state is None:
            counts["skipped"] += 1
            return
        state_original = self._decimal(state.get("original_quantity")) or quantity
        sync_changes: dict[str, Any] = {
            "current_quantity": float(quantity),
            "cost_basis_per_share": float(cost_basis),
            "partial_profit_trigger_price": float(
                cost_basis * (Decimal("1") + PARTIAL_TARGET_PERCENT / Decimal("100"))
            ),
            "last_checked_at": now.isoformat(),
        }
        if (
            not bool(state.get("partial_profit_taken"))
            and not state.get("partial_profit_order_id")
            and quantity > state_original
        ):
            sync_changes["original_quantity"] = float(quantity)
        database.update_position_management(symbol, **sync_changes)
        state = self._state(symbol) or state
        self._reconcile_management_orders(state, quantity, now)
        state = self._state(symbol) or state

        try:
            price = self._decimal(self.broker.get_current_price(symbol))
        except Exception as exc:
            price = None
            logger.info("Position management skipped %s: current price fetch failed (%s).", symbol, exc.__class__.__name__)
        if price is None or price <= 0:
            counts["skipped"] += 1
            logger.info("Position management skipped %s: no current broker/data price.", symbol)
            return

        gain_percent = ((price - cost_basis) / cost_basis) * Decimal("100")
        counts["checked"] += 1
        logger.info(
            "Position management %s: cost=%s current=%s gain=%s%%.",
            symbol, self._money(cost_basis), self._money(price), self._percent(gain_percent),
        )

        if bool(state.get("trailing_stop_activated")):
            self._manage_trailing(state, quantity, cost_basis, price, now, counts)
            return

        if state.get("partial_profit_order_id") and not bool(state.get("partial_profit_taken")):
            logger.info("Position management %s: partial-profit order pending reconciliation.", symbol)
            return
        if bool(state.get("partial_profit_taken")):
            # Defensive migration repair: a filled partial must always have trailing state.
            self._activate_trailing(symbol, price, now)
            self._manage_trailing(self._state(symbol) or state, quantity, cost_basis, price, now, counts)
            return
        if gain_percent < PARTIAL_TARGET_PERCENT:
            logger.info("Position management %s: partial target pending.", symbol)
            return

        original = self._decimal(state.get("original_quantity")) or quantity
        partial_quantity = self.partial_quantity(original, quantity)
        if partial_quantity <= 0:
            logger.info(
                "Position management %s: quantity is too small to split; leaving the full position for trailing management.",
                symbol,
            )
            database.update_position_management(
                symbol,
                partial_profit_quantity=0,
                trailing_stop_activated=1,
                trailing_high_price=float(price),
                trailing_stop_price=float(self._stop_price(price)),
                status="trailing_small_position",
                last_checked_at=now.isoformat(),
            )
            return

        result = self._submit_sell(
            symbol,
            partial_quantity,
            price,
            cost_basis,
            source="partial_profit",
            reason="PARTIAL_PROFIT_3_PERCENT",
            now=now,
        )
        if result.get("executed") and result.get("broker_order_id"):
            database.update_position_management(
                symbol,
                partial_profit_quantity=float(partial_quantity),
                partial_profit_order_id=str(result["broker_order_id"]),
                status="partial_profit_submitted",
                last_checked_at=now.isoformat(),
            )
            counts["submitted"] += 1
            logger.info("Position management %s: submitted partial-profit SELL %s.", symbol, partial_quantity)
            if self._status(result) == "filled":
                self._confirm_partial_fill(symbol, result, quantity, price, now)
        elif result.get("duplicate_prevented"):
            counts["skipped"] += 1
            logger.info("Position management %s: duplicate SELL prevented by existing open order.", symbol)
        else:
            counts["skipped"] += 1
            logger.info("Position management %s: partial-profit SELL not submitted (%s).", symbol, result.get("reason"))

    @staticmethod
    def partial_quantity(original_quantity: Decimal, current_quantity: Decimal) -> Decimal:
        """Return half the original, capped to holdings while preserving a remainder.

        Whole-share positions round down to the nearest whole share. Fractional
        positions round down to six decimal places. A result that would consume
        the full holding becomes zero so the 3% rule never closes the position.
        """
        whole_shares = original_quantity == original_quantity.to_integral_value()
        quantum = Decimal("1") if whole_shares else FRACTIONAL_QUANTUM
        target = (original_quantity / Decimal("2")).quantize(quantum, rounding=ROUND_DOWN)
        capped = min(target, current_quantity)
        if capped <= 0 or capped >= current_quantity:
            return Decimal("0")
        return capped

    def _manage_trailing(
        self,
        state: dict[str, Any],
        quantity: Decimal,
        cost_basis: Decimal,
        price: Decimal,
        now: datetime,
        counts: dict[str, int],
    ) -> None:
        symbol = str(state["symbol"]).upper()
        if state.get("final_exit_order_id"):
            logger.info("Position management %s: final exit pending reconciliation.", symbol)
            return
        previous_high = self._decimal(state.get("trailing_high_price")) or price
        high = max(previous_high, price)
        stop = self._stop_price(high)
        database.update_position_management(
            symbol,
            trailing_high_price=float(high),
            trailing_stop_price=float(stop),
            current_quantity=float(quantity),
            status="trailing_active",
            last_checked_at=now.isoformat(),
        )
        logger.info(
            "Position management %s: trailing high=%s stop=%s current=%s.",
            symbol, self._money(high), self._money(stop), self._money(price),
        )
        if price > stop:
            return
        result = self._submit_sell(
            symbol,
            quantity,
            price,
            cost_basis,
            source="trailing_stop",
            reason="TRAILING_STOP_2_PERCENT",
            now=now,
        )
        if result.get("executed") and result.get("broker_order_id"):
            database.update_position_management(
                symbol,
                final_exit_order_id=str(result["broker_order_id"]),
                status="final_exit_submitted",
                last_checked_at=now.isoformat(),
            )
            counts["submitted"] += 1
            logger.info("Position management %s: submitted trailing-stop SELL %s.", symbol, quantity)
        elif result.get("duplicate_prevented"):
            counts["skipped"] += 1
            logger.info("Position management %s: duplicate trailing SELL prevented.", symbol)
        else:
            counts["skipped"] += 1
            logger.info("Position management %s: trailing SELL not submitted (%s).", symbol, result.get("reason"))

    def _submit_sell(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        cost_basis: Decimal,
        *,
        source: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        result = self.broker.execute_position_management_sell(
            symbol,
            float(quantity),
            observed_price=float(price),
            cost_basis_per_share=float(cost_basis),
            exit_source=source,
            exit_reason=reason,
        )
        if str(result.get("action") or "").upper() == "SELL":
            database.insert_execution(result, timestamp=now)
            database.record_daily_execution_result(now.date(), result)
        return result

    def _reconcile_management_orders(
        self, state: dict[str, Any], broker_quantity: Decimal, now: datetime
    ) -> None:
        symbol = str(state["symbol"]).upper()
        partial_order_id = state.get("partial_profit_order_id")
        if partial_order_id and not bool(state.get("partial_profit_taken")):
            execution = database.load_execution_by_order_id(str(partial_order_id))
            if execution:
                filled = self._decimal(execution.get("filled_quantity")) or Decimal("0")
                database.update_position_management(
                    symbol,
                    partial_profit_filled_quantity=float(filled),
                    partial_profit_fill_price=execution.get("average_fill_price") or execution.get("fill_price"),
                    current_quantity=float(broker_quantity),
                    last_checked_at=now.isoformat(),
                )
                if self._status(execution) == "filled":
                    self._confirm_partial_fill(symbol, execution, broker_quantity, None, now)
                elif self._status(execution) in OPEN_ORDER_STATUSES:
                    logger.info("Position management %s: partial-profit order is %s.", symbol, self._status(execution))

        final_order_id = state.get("final_exit_order_id")
        if final_order_id:
            execution = database.load_execution_by_order_id(str(final_order_id))
            if execution:
                filled = self._decimal(execution.get("filled_quantity")) or Decimal("0")
                database.update_position_management(
                    symbol,
                    final_exit_filled_quantity=float(filled),
                    current_quantity=float(broker_quantity),
                    last_checked_at=now.isoformat(),
                )

    def _confirm_partial_fill(
        self,
        symbol: str,
        execution: dict[str, Any],
        broker_quantity: Decimal,
        observed_price: Decimal | None,
        now: datetime,
    ) -> None:
        fill_price = self._decimal(
            execution.get("average_fill_price") or execution.get("fill_price") or observed_price
        )
        if fill_price is None or fill_price <= 0:
            logger.info("Position management %s: filled partial awaits a reliable trailing start price.", symbol)
            return
        filled = self._decimal(execution.get("filled_quantity")) or Decimal("0")
        database.update_position_management(
            symbol,
            current_quantity=float(broker_quantity),
            partial_profit_taken=1,
            partial_profit_filled_quantity=float(filled),
            partial_profit_fill_price=float(fill_price),
            partial_profit_taken_at=now.isoformat(),
            trailing_stop_activated=1,
            trailing_high_price=float(fill_price),
            trailing_stop_price=float(self._stop_price(fill_price)),
            status="trailing_active",
            last_checked_at=now.isoformat(),
        )
        logger.info("Position management %s: partial-profit fill confirmed; trailing management activated.", symbol)

    def _activate_trailing(self, symbol: str, price: Decimal, now: datetime) -> None:
        database.update_position_management(
            symbol,
            trailing_stop_activated=1,
            trailing_high_price=float(price),
            trailing_stop_price=float(self._stop_price(price)),
            status="trailing_active",
            last_checked_at=now.isoformat(),
        )

    def _load_or_adopt(
        self, symbol: str, quantity: Decimal, cost_basis: Decimal, now: datetime
    ) -> dict[str, Any] | None:
        state = self._state(symbol)
        if state:
            return state
        original, metadata = database.infer_original_position_quantity(symbol, float(quantity))
        trigger = cost_basis * (Decimal("1") + PARTIAL_TARGET_PERCENT / Decimal("100"))
        state = database.create_position_management(
            {
                "symbol": symbol,
                "opened_at": now.isoformat(),
                "original_quantity": original,
                "current_quantity": float(quantity),
                "cost_basis_per_share": float(cost_basis),
                "partial_profit_target_percent": float(PARTIAL_TARGET_PERCENT),
                "partial_profit_trigger_price": float(trigger),
                "trailing_stop_percent": float(TRAILING_STOP_PERCENT),
                "status": "open",
                "last_checked_at": now.isoformat(),
                "raw_metadata": metadata,
            }
        )
        logger.info(
            "Position management adopted %s: current=%s original-baseline=%s source=%s.",
            symbol,
            quantity,
            original,
            metadata.get("original_quantity_source"),
        )
        return state

    @staticmethod
    def _state(symbol: str) -> dict[str, Any] | None:
        rows = database.load_active_position_management(symbol)
        return rows[-1] if rows else None

    @staticmethod
    def _status(value: dict[str, Any]) -> str:
        return str(value.get("broker_status") or value.get("raw_status") or value.get("status") or "").lower()

    @staticmethod
    def _stop_price(high: Decimal) -> Decimal:
        return high * (Decimal("1") - TRAILING_STOP_PERCENT / Decimal("100"))

    @staticmethod
    def _decimal(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _money(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.0001'))}"

    @staticmethod
    def _percent(value: Decimal) -> str:
        return f"{value.quantize(Decimal('0.01'))}"
