"""
OMS Reconcile Service - Reconciliación de órdenes con Coinbase.

Procesa eventos del canal user y REST fills para mantener estado interno sincronizado.

NOTA: El canal user NO trae fills[] embebidos. Los fills se obtienen vía REST list_fills.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from src.accounting.ledger import Fill, TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState

logger = logging.getLogger("OMSReconcile")


def _iso_to_ms(iso_str: Optional[str]) -> int:
    """Convertir ISO-8601 string a timestamp en ms."""
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


@dataclass
class OrderUpdate:
    """Actualización de orden desde el exchange."""

    order_id: str
    client_order_id: str
    product_id: str
    side: str
    status: str
    filled_size: Decimal
    remaining_size: Decimal
    avg_filled_price: Decimal


class OMSReconcileService:
    """
    Servicio de reconciliación OMS.

    Procesa eventos del canal user y actualiza:
    - IdempotencyStore (estado de órdenes)
    - TradeLedger (fills y PnL) - vía REST list_fills

    NOTA: El canal user documentado por Coinbase NO incluye fills[] embebidos.
    Los fills se obtienen vía REST list_fills(order_id) cuando number_of_fills aumenta.
    """

    def __init__(
        self,
        idempotency: IdempotencyStore,
        ledger: TradeLedger,
        fill_fetcher: Optional[Callable[[str], List[Dict]]] = None,
        on_bootstrap_complete: Optional[Callable[[], None]] = None,
    ):
        self.idempotency = idempotency
        self.ledger = ledger
        self.fill_fetcher = fill_fetcher  # REST list_fills(order_id)
        self.on_bootstrap_complete = on_bootstrap_complete

        # Estado de bootstrap
        self._bootstrap_complete = False
        self._snapshot_batches = 0
        self._orders_in_snapshot = 0

        # Tracking de fills para evitar duplicados
        self._seen_trade_ids: set = set()
        self._last_fill_counts: Dict[str, int] = {}

        # Tracking de órdenes para rate limiting (orders per minute)
        self._order_timestamps_ms: List[int] = []

    def handle_user_event(
        self,
        event_type: str,
        orders: List[Dict],
    ) -> None:
        """
        Procesar evento del canal user.

        Args:
            event_type: Tipo de evento (snapshot, update, etc.)
            orders: Lista de órdenes en el evento
        """
        logger.debug(
            "OMS: Processing user event: type=%s orders=%d",
            event_type,
            len(orders),
        )

        # Detectar fin de bootstrap (snapshot con < 50 órdenes)
        if event_type == "snapshot":
            self._snapshot_batches += 1
            self._orders_in_snapshot += len(orders)

            if len(orders) < 50 and not self._bootstrap_complete:
                self._bootstrap_complete = True
                logger.info(
                    "OMS: Bootstrap complete after %d batches (%d orders total)",
                    self._snapshot_batches,
                    self._orders_in_snapshot,
                )
                if self.on_bootstrap_complete:
                    self.on_bootstrap_complete()

        # Procesar cada orden
        for order in orders:
            self._reconcile_order(order)

    def _reconcile_order(self, order: Dict) -> None:
        """
        Reconciliar una orden individual.

        Args:
            order: Datos de la orden desde el exchange (canal user)
        """
        order_id = order.get("order_id")
        client_order_id = order.get("client_order_id")
        status = order.get("status")

        if not order_id or not client_order_id:
            logger.warning("OMS: Order missing IDs: %s", order)
            return

        # Buscar en idempotency store
        record = self.idempotency.get_by_client_order_id(client_order_id)

        if not record:
            logger.debug(
                "OMS: Order not found in idempotency: %s",
                client_order_id,
            )
            return

        # Mapear status de exchange a OrderState
        if not isinstance(status, str):
            return
        new_state = self._map_status_to_state(status)

        # Si cambió el estado, actualizar
        if new_state != record.state:
            logger.info(
                "OMS: Order %s state change: %s -> %s",
                client_order_id,
                record.state.name,
                new_state.name,
            )
            self.idempotency.update_state(
                intent_id=record.intent_id,
                state=new_state,
                exchange_order_id=order_id,
            )

        # Detectar nuevos fills via number_of_fills (campo documentado en user channel)
        # NOTA: El user channel NO trae fills[] embebidos
        fill_count = int(order.get("number_of_fills", "0") or 0)
        prev_count = self._last_fill_counts.get(order_id, 0)
        self._last_fill_counts[order_id] = fill_count

        if self.fill_fetcher and fill_count > prev_count:
            logger.debug(
                "OMS: Fetching fills for order %s (%d > %d)",
                order_id,
                fill_count,
                prev_count,
            )
            try:
                fills = self.fill_fetcher(order_id)
                for fill_data in fills:
                    self._apply_fill(fill_data, order)
            except Exception as e:
                logger.error("OMS: Error fetching fills: %s", e)

    def _apply_fill(self, fill_data: Dict, order: Dict) -> None:
        """
        Aplicar un fill al ledger.

        Args:
            fill_data: Datos del fill desde REST list_fills
            order: Orden asociada (del canal user)
        """
        trade_id = fill_data.get("trade_id")

        # Evitar duplicados
        if not trade_id or trade_id in self._seen_trade_ids:
            return

        try:
            # Schema de REST list_fills (no user channel):
            # - trade_id, product_id, side, size, price, commission, trade_time
            # El user channel trae: order_side (no side)
            fill_side = fill_data.get("side", order.get("order_side", "")).lower()

            # Inferir fee_currency del product_id
            product_id = order.get("product_id", "")
            fee_currency = product_id.split("-")[1] if "-" in product_id else ""

            fill = Fill(
                side=fill_side,
                amount=Decimal(str(fill_data.get("size", 0))),
                price=Decimal(str(fill_data.get("price", 0))),
                cost=Decimal(str(fill_data.get("size", 0)))
                * Decimal(str(fill_data.get("price", 0))),
                fee_cost=Decimal(str(fill_data.get("commission", 0))),
                fee_currency=fee_currency,
                ts_ms=_iso_to_ms(fill_data.get("trade_time")),
                trade_id=trade_id,
                order_id=order.get("order_id", ""),
            )

            added = self.ledger.add_fill(fill)
            if added:
                self._seen_trade_ids.add(trade_id)
                logger.info(
                    "OMS: Fill applied: %s %s @ %s (fee: %s %s)",
                    fill.side,
                    fill.amount,
                    fill.price,
                    fill.fee_cost,
                    fill.fee_currency,
                )
        except Exception as e:
            logger.error("OMS: Error applying fill: %s", e)

    def _map_status_to_state(self, status: str) -> OrderState:
        """
        Mapear status de Coinbase a OrderState interno.

        Args:
            status: Status desde Coinbase (OPEN, FILLED, CANCELLED, etc.)

        Returns:
            OrderState correspondiente
        """
        status_map = {
            "OPEN": OrderState.OPEN_RESTING,
            "PENDING": OrderState.OPEN_PENDING,
            "CANCEL_QUEUED": OrderState.CANCEL_QUEUED,  # Estado documentado
            "FILLED": OrderState.FILLED,
            "CANCELLED": OrderState.CANCELLED,
            "EXPIRED": OrderState.EXPIRED,
            "FAILED": OrderState.FAILED,
        }

        return status_map.get(status.upper(), OrderState.OPEN_PENDING)

    def is_bootstrap_complete(self) -> bool:
        """Verificar si el bootstrap está completo."""
        return self._bootstrap_complete

    def get_stats(self) -> Dict:
        """Obtener estadísticas del servicio."""
        return {
            "bootstrap_complete": self._bootstrap_complete,
            "snapshot_batches": self._snapshot_batches,
            "orders_in_snapshot": self._orders_in_snapshot,
            "seen_trade_ids": len(self._seen_trade_ids),
        }
