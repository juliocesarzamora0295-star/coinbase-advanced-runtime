"""
Tests para verificar CANCEL_QUEUED en get_pending_or_open.

Bug corregido: CANCEL_QUEUED estaba en is_active pero no en get_pending_or_open(),
rompiendo cualquier reconcile/restart que dependa de 'órdenes activas'.
"""
import os
import sys
import tempfile
from decimal import Decimal

sys.path.insert(0, '/mnt/okcomputer/output/fortress_v4')

from src.execution.idempotency import IdempotencyStore, OrderIntent, OrderState


class TestCancelQueuedConsistency:
    """Test consistencia entre is_active y get_pending_or_open."""
    
    def setup_method(self):
        """Crear store temporal para cada test."""
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_idempotency.db")
        self.store = IdempotencyStore(db_path=self.db_path)
    
    def teardown_method(self):
        """Limpiar después de cada test."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_cancel_queued_in_get_pending_or_open(self):
        """
        Verificar que CANCEL_QUEUED aparezca en get_pending_or_open().
        
        Bug corregido: CANCEL_QUEUED estaba en is_active pero no en get_pending_or_open().
        """
        # Crear intent en estado CANCEL_QUEUED
        intent = OrderIntent(
            intent_id="test-intent-1",
            client_order_id="test-client-1",
            product_id="BTC-USD",
            side="SELL",
            order_type="LIMIT",
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            stop_price=None,
            post_only=True,
            created_ts_ms=1234567890,
        )
        
        self.store.save_intent(intent, state=OrderState.CANCEL_QUEUED)
        
        # get_pending_or_open debe incluir CANCEL_QUEUED
        pending = self.store.get_pending_or_open()
        
        assert len(pending) == 1, f"Expected 1 pending order, got {len(pending)}"
        assert pending[0].state == OrderState.CANCEL_QUEUED
    
    def test_is_active_consistent_with_get_pending(self):
        """
        Verificar que is_active y get_pending_or_open sean consistentes.
        
        Todos los estados donde is_active=True deben aparecer en get_pending_or_open().
        """
        active_states = [
            OrderState.NEW,
            OrderState.OPEN_RESTING,
            OrderState.OPEN_PENDING,
            OrderState.CANCEL_QUEUED,
        ]
        
        for i, state in enumerate(active_states):
            intent = OrderIntent(
                intent_id=f"test-intent-{i}",
                client_order_id=f"test-client-{i}",
                product_id="BTC-USD",
                side="SELL",
                order_type="LIMIT",
                qty=Decimal("0.1"),
                price=Decimal("50000"),
                stop_price=None,
                post_only=True,
                created_ts_ms=1234567890 + i,
            )
            self.store.save_intent(intent, state=state)
        
        # get_pending_or_open debe retornar todos los estados activos
        pending = self.store.get_pending_or_open()
        pending_states = {p.state for p in pending}
        
        assert pending_states == set(active_states), \
            f"Pending states {pending_states} != active states {set(active_states)}"
    
    def test_terminal_states_not_in_pending(self):
        """
        Verificar que estados terminales no aparezcan en get_pending_or_open.
        """
        terminal_states = [
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        ]
        
        for i, state in enumerate(terminal_states):
            intent = OrderIntent(
                intent_id=f"test-intent-term-{i}",
                client_order_id=f"test-client-term-{i}",
                product_id="BTC-USD",
                side="SELL",
                order_type="LIMIT",
                qty=Decimal("0.1"),
                price=Decimal("50000"),
                stop_price=None,
                post_only=True,
                created_ts_ms=1234567890 + i,
            )
            self.store.save_intent(intent, state=state)
        
        # get_pending_or_open no debe retornar estados terminales
        pending = self.store.get_pending_or_open()
        assert len(pending) == 0, f"Expected 0 pending orders, got {len(pending)}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
