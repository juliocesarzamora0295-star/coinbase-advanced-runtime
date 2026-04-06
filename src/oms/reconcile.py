"""
OMS Reconcile Service - Reconciliación de órdenes con Coinbase.

Procesa eventos del canal user y REST fills para mantener estado interno sincronizado.

NOTA: El canal user NO trae fills[] embebidos. Los fills se obtienen vía REST list_fills.

Invariantes:
- is_ready() = False hasta que bootstrap complete + reconcile limpio + no degradado
- Orphan order (desconocida en OMS) = incidente crítico → degradado + callback
- fill_fetcher failure = OMS degradado → trading bloqueado
- Trading no puede ocurrir con OMS incompleta
"""

import logging
from dataclasses import dataclass, field
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


@dataclass
class OMSIncident:
    """Registro de incidente OMS."""

    incident_type: str  # "ORPHAN_ORDER", "FILL_FETCH_FAILED", "DIVERGENCE"
    detail: str
    timestamp_ms: int
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None


class OMSReconcileService:
    """
    Servicio de reconciliación OMS.

    Procesa eventos del canal user y actualiza:
    - IdempotencyStore (estado de órdenes)
    - TradeLedger (fills y PnL) - vía REST list_fills

    Readiness gate: is_ready() = bootstrap_complete AND NOT degraded
    Orphan handling: orden desconocida = incidente crítico
    Fill failure: marca OMS como degradado
    """

    def __init__(
        self,
        idempotency: IdempotencyStore,
        ledger: TradeLedger,
        fill_fetcher: Optional[Callable[[str], List[Dict]]] = None,
        on_bootstrap_complete: Optional[Callable[[], None]] = None,
        on_degraded: Optional[Callable[[OMSIncident], None]] = None,
        on_fill_applied: Optional[Callable[[str, Decimal, Decimal, str, str], None]] = None,
    ):
        self.idempotency = idempotency
        self.ledger = ledger
        self.fill_fetcher = fill_fetcher  # REST list_fills(order_id)
        self.on_bootstrap_complete = on_bootstrap_complete
        self.on_degraded = on_degraded
        self.on_fill_applied = on_fill_applied  # (client_order_id, price, qty, symbol, side)

        # Estado de bootstrap
        self._bootstrap_complete = False
        self._snapshot_batches = 0
        self._orders_in_snapshot = 0

        # Degradation state
        self._degraded = False
        self._degraded_reason: str = ""
        self._incidents: List[OMSIncident] = []

        # Reconcile health tracking
        self._consecutive_clean_reconciles: int = 0
        self.clean_reconcile_threshold: int = 3  # auto-clear after N clean cycles

        # Tracking de fills para evitar duplicados
        self._seen_trade_ids: set = set()
        self._last_fill_counts: Dict[str, int] = {}

    # ──────────────────────────────────────────────
    # Readiness gate
    # ──────────────────────────────────────────────

    def is_ready(self) -> bool:
        """
        OMS está listo para trading.

        True solo cuando:
        - Bootstrap completo (snapshot procesado)
        - No degradado (sin orphans, sin fill fetch failures)
        """
        return self._bootstrap_complete and not self._degraded

    def is_bootstrap_complete(self) -> bool:
        """Verificar si el bootstrap está completo."""
        return self._bootstrap_complete

    def is_degraded(self) -> bool:
        """Verificar si el OMS está degradado."""
        return self._degraded

    def clear_degraded(self) -> None:
        """
        Limpiar estado degradado explícitamente.

        Solo debe llamarse después de un reconcile full exitoso.
        """
        if self._degraded:
            logger.info(
                "OMS: Degradation cleared (was: %s)", self._degraded_reason
            )
            self._degraded = False
            self._degraded_reason = ""

    def record_clean_reconcile(self) -> None:
        """
        Registrar un ciclo de reconcile limpio (sin errores).

        Después de `clean_reconcile_threshold` ciclos limpios consecutivos,
        auto-clear degraded state.
        """
        self._consecutive_clean_reconciles += 1
        if (
            self._degraded
            and self._consecutive_clean_reconciles >= self.clean_reconcile_threshold
            and self.last_external_reconcile_clean
        ):
            logger.info(
                "OMS: Auto-clearing degradation after %d clean reconciles "
                "(external reconcile also clean)",
                self._consecutive_clean_reconciles,
            )
            self.clear_degraded()
            self._consecutive_clean_reconciles = 0

    def record_dirty_reconcile(self) -> None:
        """Registrar un ciclo de reconcile con errores. Reset del contador limpio."""
        self._consecutive_clean_reconciles = 0

    def reconcile_against_exchange(
        self,
        exchange_open_orders: List[Dict],
        exchange_recent_fills: List[Dict],
    ) -> tuple[bool, List[str]]:
        """
        Compare internal OMS state against exchange REST snapshot.

        Returns:
            (clean, drifts) — clean=True if no drift, drifts lists issues.
        """
        drifts: List[str] = []

        # OMS orders that exchange doesn't know about
        oms_open = self.idempotency.get_pending_or_open()
        exchange_cids = {o.get("client_order_id") for o in exchange_open_orders}
        for record in oms_open:
            if record.client_order_id not in exchange_cids:
                drifts.append(f"OMS_OPEN_NOT_ON_EXCHANGE: {record.client_order_id}")

        # Exchange orders that OMS doesn't know about
        oms_cids = {r.client_order_id for r in oms_open}
        for order in exchange_open_orders:
            cid = order.get("client_order_id", "")
            if cid and cid not in oms_cids:
                existing = self.idempotency.get_by_client_order_id(cid)
                if existing is None:
                    drifts.append(f"EXCHANGE_OPEN_NOT_IN_OMS: {cid}")

        # Fills we haven't seen
        for fill in exchange_recent_fills:
            tid = fill.get("trade_id", "")
            if tid and tid not in self._seen_trade_ids:
                drifts.append(f"UNSEEN_FILL: {tid}")

        if drifts:
            for d in drifts:
                logger.warning("RECONCILE DRIFT: %s", d)
            self._last_external_reconcile_clean = False
            self.record_dirty_reconcile()
            self.report_divergence(f"External reconcile: {len(drifts)} drifts")
        else:
            self.record_clean_reconcile()
            self._last_external_reconcile_clean = True

        return len(drifts) == 0, drifts

    @property
    def last_external_reconcile_clean(self) -> bool:
        """Whether the last external reconcile was clean."""
        return getattr(self, "_last_external_reconcile_clean", False)

    # ──────────────────────────────────────────────
    # Event handling
    # ──────────────────────────────────────────────

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

        Orphan detection: si la orden no está en el idempotency store,
        es un incidente crítico → OMS degradado.
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
            # ORPHAN: orden en exchange que no está en nuestro OMS
            self._on_orphan_detected(order_id, client_order_id, status)
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
                client_order_id=record.client_order_id,
                state=new_state,
                exchange_order_id=order_id,
            )

        # Detectar nuevos fills via number_of_fills
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
                # fill_fetcher failure → OMS degradado
                self._on_fill_fetch_failed(order_id, str(e))

    # ──────────────────────────────────────────────
    # Orphan handling
    # ──────────────────────────────────────────────

    def _on_orphan_detected(
        self, order_id: str, client_order_id: str, status: Optional[str]
    ) -> None:
        """
        Orden en exchange no encontrada en OMS → incidente crítico.

        Acción: marcar OMS como degradado. El caller (main.py) debe:
        - Abrir circuit breaker
        - Iniciar reconcile full
        - No emitir nuevas órdenes
        """
        incident = OMSIncident(
            incident_type="ORPHAN_ORDER",
            detail=f"Order {order_id} (client={client_order_id}, status={status}) "
            f"not found in OMS idempotency store",
            timestamp_ms=int(datetime.now().timestamp() * 1000),
            order_id=order_id,
            client_order_id=client_order_id,
        )
        self._mark_degraded(incident)

    def _on_fill_fetch_failed(self, order_id: str, error: str) -> None:
        """
        fill_fetcher falló → OMS degradado.

        No podemos confirmar fills → estado inconsistente posible.
        """
        incident = OMSIncident(
            incident_type="FILL_FETCH_FAILED",
            detail=f"Failed to fetch fills for order {order_id}: {error}",
            timestamp_ms=int(datetime.now().timestamp() * 1000),
            order_id=order_id,
        )
        self._mark_degraded(incident)

    def report_divergence(self, detail: str) -> None:
        """
        Reportar divergencia detectada externamente (e.g. reconcile periódico).

        Marca OMS como degradado. Invalidates external reconcile clean state.
        """
        self._last_external_reconcile_clean = False
        incident = OMSIncident(
            incident_type="DIVERGENCE",
            detail=detail,
            timestamp_ms=int(datetime.now().timestamp() * 1000),
        )
        self._mark_degraded(incident)

    def _mark_degraded(self, incident: OMSIncident) -> None:
        """Marcar OMS como degradado y notificar."""
        self._degraded = True
        self._degraded_reason = incident.detail
        self._incidents.append(incident)
        logger.error(
            "OMS DEGRADED: [%s] %s",
            incident.incident_type,
            incident.detail,
        )
        if self.on_degraded:
            self.on_degraded(incident)

    # ──────────────────────────────────────────────
    # Fill handling
    # ──────────────────────────────────────────────

    def _apply_fill(self, fill_data: Dict, order: Dict) -> None:
        """Aplicar un fill al ledger."""
        trade_id = fill_data.get("trade_id")

        if not trade_id or trade_id in self._seen_trade_ids:
            return

        try:
            fill_side = fill_data.get("side", order.get("order_side", "")).lower()
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

                # Notify for real ExecutionReport generation
                if self.on_fill_applied:
                    client_order_id = order.get("client_order_id", "")
                    self.on_fill_applied(
                        client_order_id,
                        fill.price,
                        fill.amount,
                        order.get("product_id", ""),
                        fill.side.upper(),
                    )

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

    # ──────────────────────────────────────────────
    # Status mapping
    # ──────────────────────────────────────────────

    def _map_status_to_state(self, status: str) -> OrderState:
        """Mapear status de Coinbase a OrderState interno."""
        status_map = {
            "OPEN": OrderState.OPEN_RESTING,
            "PENDING": OrderState.OPEN_PENDING,
            "CANCEL_QUEUED": OrderState.CANCEL_QUEUED,
            "FILLED": OrderState.FILLED,
            "CANCELLED": OrderState.CANCELLED,
            "EXPIRED": OrderState.EXPIRED,
            "FAILED": OrderState.FAILED,
        }
        return status_map.get(status.upper(), OrderState.OPEN_PENDING)

    # ──────────────────────────────────────────────
    # Stats
    # ──────────────────────────────────────────────

    def get_stats(self) -> Dict:
        """Obtener estadísticas del servicio."""
        return {
            "bootstrap_complete": self._bootstrap_complete,
            "degraded": self._degraded,
            "degraded_reason": self._degraded_reason,
            "snapshot_batches": self._snapshot_batches,
            "orders_in_snapshot": self._orders_in_snapshot,
            "seen_trade_ids": len(self._seen_trade_ids),
            "incidents": len(self._incidents),
        }

    def get_incidents(self) -> List[OMSIncident]:
        """Obtener lista de incidentes."""
        return list(self._incidents)
