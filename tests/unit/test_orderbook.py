"""
Tests unitarios: OrderBook L2 mínimo.

Invariantes testeadas:
- snapshot L2 → best_bid/ask correctos
- delta update → best_bid/ask actualizados
- remove level (size=0) → level eliminado, best actualizado
- bid >= ask → is_consistent() False
- book vacío → is_consistent() False
- age > max_age_ms → is_fresh() False
- gap detectado → is_fresh() False post-invalidate
- spread = best_ask - best_bid cuando consistente y fresco
- spread = None cuando inconsistente
- spread = None cuando stale (age excedido)
- spread = None cuando post-gap
- invalidate_on_gap + nuevo update → fresco de nuevo
"""
import time
from decimal import Decimal

import pytest

from src.marketdata.orderbook import OrderBook


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_snapshot(bids: list[tuple], asks: list[tuple]) -> list[dict]:
    """Construir lista de eventos snapshot."""
    events = []
    for price, size in bids:
        events.append({"type": "snapshot", "side": "bid", "price": str(price), "size": str(size)})
    for price, size in asks:
        events.append({"type": "snapshot", "side": "ask", "price": str(price), "size": str(size)})
    return events


def make_delta(side: str, price, size) -> dict:
    return {"type": "update", "side": side, "price": str(price), "size": str(size)}


def fresh_book(max_age_ms: int = 5000) -> OrderBook:
    return OrderBook(symbol="BTC-USD", max_age_ms=max_age_ms)


# ──────────────────────────────────────────────
# Snapshot
# ──────────────────────────────────────────────

class TestSnapshot:

    def test_snapshot_sets_best_bid(self):
        """Snapshot → best_bid = precio más alto en bids."""
        book = fresh_book()
        book.update(make_snapshot(
            bids=[(49900, 1.0), (49800, 2.0), (49700, 0.5)],
            asks=[(50100, 1.0)],
        ))
        assert book.best_bid() == Decimal("49900")

    def test_snapshot_sets_best_ask(self):
        """Snapshot → best_ask = precio más bajo en asks."""
        book = fresh_book()
        book.update(make_snapshot(
            bids=[(49900, 1.0)],
            asks=[(50100, 1.0), (50200, 2.0), (50300, 0.5)],
        ))
        assert book.best_ask() == Decimal("50100")

    def test_empty_book_best_bid_none(self):
        """Book vacío → best_bid=None."""
        book = fresh_book()
        assert book.best_bid() is None

    def test_empty_book_best_ask_none(self):
        """Book vacío → best_ask=None."""
        book = fresh_book()
        assert book.best_ask() is None

    def test_snapshot_clears_previous_bids(self):
        """Nuevo snapshot de bids limpia los anteriores."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(50000, 1.0)], asks=[(50100, 1.0)]))
        # Nuevo snapshot de bid con precio diferente
        book.update([{"type": "snapshot", "side": "bid", "price": "49500", "size": "2.0"}])
        assert book.best_bid() == Decimal("49500")


# ──────────────────────────────────────────────
# Delta updates
# ──────────────────────────────────────────────

class TestDeltaUpdates:

    def test_delta_add_new_bid_level(self):
        """Delta update agrega nuevo nivel de bid."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        book.update([make_delta("bid", 50000, 0.5)])  # mejor bid ahora
        assert book.best_bid() == Decimal("50000")

    def test_delta_add_new_ask_level(self):
        """Delta update agrega nuevo nivel de ask."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50200, 1.0)]))
        book.update([make_delta("ask", 50050, 0.5)])  # mejor ask ahora
        assert book.best_ask() == Decimal("50050")

    def test_delta_remove_best_bid_level(self):
        """Delta size=0 elimina nivel, best_bid actualizado."""
        book = fresh_book()
        book.update(make_snapshot(
            bids=[(49900, 1.0), (49800, 2.0)],
            asks=[(50100, 1.0)],
        ))
        book.update([make_delta("bid", 49900, 0)])  # eliminar mejor bid
        assert book.best_bid() == Decimal("49800")

    def test_delta_remove_best_ask_level(self):
        """Delta size=0 elimina nivel de ask, best_ask actualizado."""
        book = fresh_book()
        book.update(make_snapshot(
            bids=[(49900, 1.0)],
            asks=[(50100, 1.0), (50200, 2.0)],
        ))
        book.update([make_delta("ask", 50100, 0)])  # eliminar mejor ask
        assert book.best_ask() == Decimal("50200")

    def test_delta_remove_nonexistent_level_no_error(self):
        """Eliminar nivel que no existe → no error."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        # No debe lanzar
        book.update([make_delta("bid", 99999, 0)])
        assert book.best_bid() == Decimal("49900")


# ──────────────────────────────────────────────
# Consistencia
# ──────────────────────────────────────────────

class TestConsistency:

    def test_normal_book_is_consistent(self):
        """bid < ask → is_consistent() True."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        assert book.is_consistent() is True

    def test_crossed_book_is_not_consistent(self):
        """bid >= ask → is_consistent() False."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(50200, 1.0)], asks=[(50100, 1.0)]))
        assert book.is_consistent() is False

    def test_equal_bid_ask_is_not_consistent(self):
        """bid == ask → is_consistent() False."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(50000, 1.0)], asks=[(50000, 1.0)]))
        assert book.is_consistent() is False

    def test_empty_book_is_not_consistent(self):
        """Book vacío → is_consistent() False."""
        book = fresh_book()
        assert book.is_consistent() is False

    def test_only_bids_is_not_consistent(self):
        """Solo bids (sin asks) → is_consistent() False."""
        book = fresh_book()
        book.update([{"type": "snapshot", "side": "bid", "price": "49900", "size": "1.0"}])
        assert book.is_consistent() is False


# ──────────────────────────────────────────────
# Freshness
# ──────────────────────────────────────────────

class TestFreshness:

    def test_fresh_book_after_update(self):
        """Después de update → is_fresh(5000) True."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        assert book.is_fresh(5000) is True

    def test_never_updated_book_is_not_fresh(self):
        """Book nunca actualizado → is_fresh=False."""
        book = fresh_book()
        assert book.is_fresh(5000) is False

    def test_stale_book_is_not_fresh(self):
        """Book con age > max_age_ms → is_fresh=False."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        # Forzar _last_update_ms a pasado
        book._last_update_ms = (time.time() - 10) * 1000  # 10 segundos atrás
        assert book.is_fresh(5000) is False

    def test_gap_invalidated_book_is_not_fresh(self):
        """Post-gap → is_fresh=False."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        book.invalidate_on_gap()
        assert book.is_fresh(5000) is False

    def test_update_after_gap_restores_freshness(self):
        """Nuevo update después de gap → is_fresh=True."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        book.invalidate_on_gap()
        assert book.is_fresh(5000) is False
        # Nuevo snapshot post-gap
        book.update(make_snapshot(bids=[(49950, 1.0)], asks=[(50050, 1.0)]))
        assert book.is_fresh(5000) is True


# ──────────────────────────────────────────────
# Spread
# ──────────────────────────────────────────────

class TestSpread:

    def test_spread_correct_when_consistent_and_fresh(self):
        """spread = best_ask - best_bid cuando consistente y fresco."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        assert book.spread() == Decimal("200")

    def test_spread_none_when_inconsistent(self):
        """spread=None cuando bid >= ask."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(50200, 1.0)], asks=[(50100, 1.0)]))
        assert book.spread() is None

    def test_spread_none_when_stale(self):
        """spread=None cuando book stale (age excedido)."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        book._last_update_ms = (time.time() - 10) * 1000  # stale
        assert book.spread() is None

    def test_spread_none_when_post_gap(self):
        """spread=None después de gap."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        book.invalidate_on_gap()
        assert book.spread() is None

    def test_spread_none_when_empty(self):
        """spread=None cuando book vacío."""
        book = fresh_book()
        assert book.spread() is None

    def test_spread_positive_when_consistent(self):
        """spread > 0 cuando consistente."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(49900, 1.0)], asks=[(50100, 1.0)]))
        s = book.spread()
        assert s is not None
        assert s > Decimal("0")

    def test_inconsistent_book_never_produces_positive_spread(self):
        """Book inconsistente nunca produce spread positivo (invariante clave)."""
        book = fresh_book()
        book.update(make_snapshot(bids=[(50200, 1.0)], asks=[(50100, 1.0)]))
        s = book.spread()
        # spread debe ser None (no negativo, no positivo)
        assert s is None
