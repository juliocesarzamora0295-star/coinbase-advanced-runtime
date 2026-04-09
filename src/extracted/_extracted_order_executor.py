"""
Extracted: Order Executor from Kimi_Agent fortress_v4.

Origin: Kimi_Agent_Especificacion API Coinbase/fortress_v4/src/execution/orders.py
Reason: Alternative order submission pattern that generates its own UUIDs and
        exposes create_limit_order() / create_market_order() as public API.
        The canonical version uses planner-driven submit_order(intent) with
        deterministic client_order_id from signal hashing.

Differences vs src/execution/orders.py (canonical):
  - Generates own UUID intent_id (not planner-driven deterministic hash)
  - Public create_limit_order(product_id, side, qty, price) API
  - Public create_market_order(product_id, side, qty, quote_size) API
  - cancel_order() uses intent_id (not client_order_id)
  - cancel_order() transitions to CANCELLED directly (not CANCEL_QUEUED)
  - OrderResult includes intent_id field (canonical uses client_order_id only)
  - No submit_order() unified entry point
  - Builds OrderIntent internally (canonical receives it from planner)

Verdict: canonical version is superior for the fortress-v4 pipeline
         (signal -> planner -> executor). This version is useful if you need
         a standalone executor without the planner layer.

TODO: fortress-v4 integration — this is exchange-agnostic scaffolding only.
      All Coinbase-specific imports have been replaced with stubs.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ExtractedOrderExecutor")


# --- Stubs replacing Coinbase-specific imports ---

class OrderState(Enum):
    """Order lifecycle states (mirrors src/execution/idempotency.py)."""
    NEW = "new"
    OPEN_RESTING = "open_resting"
    OPEN_PENDING = "open_pending"
    CANCEL_QUEUED = "cancel_queued"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED, OrderState.FAILED)

    @property
    def is_active(self) -> bool:
        return self in (OrderState.NEW, OrderState.OPEN_RESTING, OrderState.OPEN_PENDING, OrderState.CANCEL_QUEUED)


@dataclass
class OrderIntent:
    """Minimal order intent stub."""
    intent_id: str
    client_order_id: str
    product_id: str
    side: str
    order_type: str
    qty: Decimal
    price: Optional[Decimal]
    stop_price: Optional[Decimal]
    post_only: bool
    created_ts_ms: int


class ExchangeAPIError(Exception):
    """Stub for exchange-specific API errors."""
    pass


# --- Extracted classes ---

@dataclass
class OrderResult:
    """Resultado de ejecucion de orden."""
    success: bool
    intent_id: str
    client_order_id: str
    exchange_order_id: Optional[str]
    state: OrderState
    error_message: Optional[str] = None


class OrderExecutor:
    """
    Order executor with self-generated UUIDs and direct limit/market methods.

    Unlike the canonical version (which receives OrderIntent from planner),
    this creates intents internally from raw parameters.

    TODO: fortress-v4 integration — to use this pattern, you would:
      1. Replace exchange_client stub with CoinbaseRESTClient
      2. Replace quantizer stub with src.core.quantization.Quantizer
      3. Replace idempotency stub with src.execution.idempotency.IdempotencyStore
      4. Or preferably, use the canonical submit_order(intent) pattern instead
    """

    def __init__(
        self,
        exchange_client: Any = None,
        idempotency_store: Any = None,
        quantizer: Any = None,
        max_retries: int = 3,
    ):
        self.client = exchange_client
        self.idempotency = idempotency_store
        self.quantizer = quantizer
        self.max_retries = max_retries
        self._order_timestamps_ms: List[int] = []

    def create_limit_order(
        self,
        product_id: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        post_only: bool = True,
    ) -> OrderResult:
        """
        Create limit order with self-generated UUID.

        This is the original pattern: caller provides raw params,
        executor generates intent_id internally.
        """
        intent_id = str(uuid.uuid4())
        client_order_id = intent_id  # 1:1 mapping

        intent = OrderIntent(
            intent_id=intent_id,
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_type="LIMIT",
            qty=qty,
            price=price,
            stop_price=None,
            post_only=post_only,
            created_ts_ms=int(time.time() * 1000),
        )

        # TODO: fortress-v4 integration — persist via IdempotencyStore
        if self.idempotency:
            self.idempotency.save_intent(intent, OrderState.NEW)

        try:
            if not self.client:
                raise ExchangeAPIError("No exchange client configured")

            response = self.client.create_limit_order_gtc(
                client_order_id=client_order_id,
                product_id=product_id,
                side=side,
                base_size=qty,
                limit_price=price,
                post_only=post_only,
            )

            exchange_order_id = response.get("order_id")

            if self.idempotency:
                self.idempotency.update_state(
                    intent_id=intent_id,
                    state=OrderState.OPEN_RESTING,
                    exchange_order_id=exchange_order_id,
                )

            logger.info(f"Limit order created: {exchange_order_id}")
            self._record_order_timestamp()

            return OrderResult(
                success=True, intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                state=OrderState.OPEN_RESTING,
            )

        except (ExchangeAPIError, Exception) as e:
            if self.idempotency:
                self.idempotency.update_state(
                    intent_id=intent_id, state=OrderState.FAILED,
                    error_message=str(e),
                )
            logger.error(f"Failed to create limit order: {e}")
            return OrderResult(
                success=False, intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=None, state=OrderState.FAILED,
                error_message=str(e),
            )

    def create_market_order(
        self,
        product_id: str,
        side: str,
        qty: Optional[Decimal] = None,
        quote_size: Optional[Decimal] = None,
    ) -> OrderResult:
        """
        Create market order with self-generated UUID.

        Supports both base_size and quote_size modes.
        """
        if qty is None and quote_size is None:
            raise ValueError("Must provide qty or quote_size")

        intent_id = str(uuid.uuid4())
        client_order_id = intent_id

        intent = OrderIntent(
            intent_id=intent_id,
            client_order_id=client_order_id,
            product_id=product_id,
            side=side,
            order_type="MARKET",
            qty=qty or Decimal("0"),
            price=None,
            stop_price=None,
            post_only=False,
            created_ts_ms=int(time.time() * 1000),
        )

        if self.idempotency:
            self.idempotency.save_intent(intent, OrderState.NEW)

        try:
            if not self.client:
                raise ExchangeAPIError("No exchange client configured")

            response = self.client.create_market_order(
                client_order_id=client_order_id,
                product_id=product_id,
                side=side,
                base_size=qty,
                quote_size=quote_size,
            )

            exchange_order_id = response.get("order_id")

            if self.idempotency:
                self.idempotency.update_state(
                    intent_id=intent_id,
                    state=OrderState.OPEN_PENDING,
                    exchange_order_id=exchange_order_id,
                )

            logger.info(f"Market order created: {exchange_order_id}")
            self._record_order_timestamp()

            return OrderResult(
                success=True, intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                state=OrderState.OPEN_PENDING,
            )

        except (ExchangeAPIError, Exception) as e:
            if self.idempotency:
                self.idempotency.update_state(
                    intent_id=intent_id, state=OrderState.FAILED,
                    error_message=str(e),
                )
            logger.error(f"Failed to create market order: {e}")
            return OrderResult(
                success=False, intent_id=intent_id,
                client_order_id=client_order_id,
                exchange_order_id=None, state=OrderState.FAILED,
                error_message=str(e),
            )

    def cancel_order(self, intent_id: str) -> bool:
        """
        Cancel order by intent_id.

        NOTE: Original transitions directly to CANCELLED.
        Canonical version correctly uses CANCEL_QUEUED (Coinbase-documented state).
        """
        if not self.idempotency:
            logger.warning("No idempotency store configured")
            return False

        record = self.idempotency.get_by_intent_id(intent_id)
        if not record:
            logger.warning(f"Intent not found: {intent_id}")
            return False
        if not record.is_active:
            logger.warning(f"Order not active: {record.state.name}")
            return False
        if not record.exchange_order_id:
            logger.warning(f"No exchange order ID for intent: {intent_id}")
            return False

        try:
            self.client.cancel_orders([record.exchange_order_id])
            # NOTE: Original used CANCELLED here. Canonical correctly uses CANCEL_QUEUED.
            self.idempotency.update_state(
                intent_id=intent_id, state=OrderState.CANCELLED,
            )
            logger.info(f"Order cancelled: {record.exchange_order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
            return False

    def get_order_status(self, intent_id: str) -> Optional[OrderState]:
        if not self.idempotency:
            return None
        record = self.idempotency.get_by_intent_id(intent_id)
        return record.state if record else None

    def _record_order_timestamp(self) -> None:
        now_ms = int(time.time() * 1000)
        self._order_timestamps_ms.append(now_ms)
        cutoff_ms = now_ms - 60000
        self._order_timestamps_ms = [ts for ts in self._order_timestamps_ms if ts > cutoff_ms]

    def get_orders_last_minute(self) -> int:
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - 60000
        self._order_timestamps_ms = [ts for ts in self._order_timestamps_ms if ts > cutoff_ms]
        return len(self._order_timestamps_ms)
