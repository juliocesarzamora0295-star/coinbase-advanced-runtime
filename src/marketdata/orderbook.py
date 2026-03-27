"""
L2 Order Book mínimo para Fortress v4.

Procesa feed Level2 para exponer best_bid, best_ask, spread.
Solo lo mínimo operativamente útil — no microstructure.

Invariantes:
- best_bid < best_ask siempre (is_consistent=False en caso contrario)
- spread = best_ask - best_bid solo cuando consistente y fresco
- is_fresh=False después de gap o si age > max_age_ms
- invalidate_on_gap() marca el book como stale
- spread=None cuando stale o inconsistente
"""
import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional

logger = logging.getLogger("OrderBook")


class OrderBook:
    """
    L2 Order Book con stale detection y consistency check.

    Soporta snapshot y delta updates del feed Level2 de Coinbase.
    """

    def __init__(self, symbol: str, max_age_ms: int = 5000) -> None:
        self.symbol = symbol
        self.max_age_ms = max_age_ms
        # price → size (Decimal)
        self._bids: Dict[Decimal, Decimal] = {}
        self._asks: Dict[Decimal, Decimal] = {}
        self._last_update_ms: Optional[float] = None
        self._gap_invalidated: bool = False

    def update(self, events: List[dict]) -> None:
        """
        Aplicar lista de eventos L2 al book.

        Cada evento tiene:
          - "type": "snapshot" | "update"
          - "side": "bid" | "ask"
          - "price": str | Decimal
          - "size": str | Decimal  (0 = remove level)

        Args:
            events: Lista de eventos del feed Level2.
        """
        if not events:
            return

        # Determinar qué lados fueron snapshot en este batch — limpiar solo una vez
        snapshot_sides_cleared: set[str] = set()
        for event in events:
            event_type = event.get("type", "update")
            side = event.get("side", "")
            try:
                price = Decimal(str(event["price"]))
                size = Decimal(str(event["size"]))
            except (KeyError, Exception) as exc:
                logger.warning("OrderBook(%s): invalid event %s: %s", self.symbol, event, exc)
                continue

            if event_type == "snapshot" and side not in snapshot_sides_cleared:
                # Snapshot limpia el lado una sola vez por batch
                if side == "bid":
                    self._bids.clear()
                elif side == "ask":
                    self._asks.clear()
                snapshot_sides_cleared.add(side)

            book = self._bids if side == "bid" else self._asks if side == "ask" else None
            if book is None:
                logger.warning("OrderBook(%s): unknown side '%s'", self.symbol, side)
                continue

            if size <= Decimal("0"):
                book.pop(price, None)  # remove level
            else:
                book[price] = size

        self._last_update_ms = time.time() * 1000
        self._gap_invalidated = False

    def best_bid(self) -> Optional[Decimal]:
        """Mejor precio de compra (más alto en bids)."""
        if not self._bids:
            return None
        return max(self._bids.keys())

    def best_ask(self) -> Optional[Decimal]:
        """Mejor precio de venta (más bajo en asks)."""
        if not self._asks:
            return None
        return min(self._asks.keys())

    def spread(self) -> Optional[Decimal]:
        """
        Spread = best_ask - best_bid.

        Returns None si el book está stale, inconsistente o vacío.
        """
        if not self.is_fresh(self.max_age_ms) or not self.is_consistent():
            return None
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def is_fresh(self, max_age_ms: int) -> bool:
        """
        True si el book fue actualizado recientemente.

        False si:
        - Nunca fue actualizado.
        - Pasó más de max_age_ms desde el último update.
        - Se detectó un gap (invalidate_on_gap fue llamado).
        """
        if self._gap_invalidated:
            return False
        if self._last_update_ms is None:
            return False
        age_ms = time.time() * 1000 - self._last_update_ms
        return age_ms <= max_age_ms

    def is_consistent(self) -> bool:
        """
        True si best_bid < best_ask (spread positivo).

        False si:
        - book vacío (no hay bids o asks).
        - bid >= ask (libro cruzado).
        """
        bid = self.best_bid()
        ask = self.best_ask()
        if bid is None or ask is None:
            return False
        return bid < ask

    def invalidate_on_gap(self) -> None:
        """
        Marcar el book como stale después de un gap de WebSocket.

        Llamar cuando el gap detector detecta secuencia rota.
        El book debe ser reconstruido con un snapshot completo.
        """
        self._gap_invalidated = True
        logger.warning("OrderBook(%s): invalidated due to gap", self.symbol)

    def clear(self) -> None:
        """Limpiar completamente el book (para reinicio)."""
        self._bids.clear()
        self._asks.clear()
        self._last_update_ms = None
        self._gap_invalidated = False
