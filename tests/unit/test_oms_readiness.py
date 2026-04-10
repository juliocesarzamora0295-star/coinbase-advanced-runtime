"""
Tests para OMS readiness gate, orphan handling, y degradation.

Invariantes testeadas:
- is_ready() = False antes de bootstrap
- is_ready() = False cuando degradado
- is_ready() = True solo cuando bootstrap completo + no degradado
- Orphan order → OMS degradado + on_degraded callback
- fill_fetcher failure → OMS degradado
- report_divergence → OMS degradado
- clear_degraded → OMS listo de nuevo (si bootstrap completo)
- on_bootstrap_complete callback se invoca
"""

import os
import tempfile
from decimal import Decimal
from unittest.mock import MagicMock

from src.accounting.ledger import TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent
from src.oms.reconcile import OMSReconcileService


def _make_oms(
    temp_dir: str,
    fill_fetcher=None,
    on_bootstrap_complete=None,
    on_degraded=None,
):
    idem_path = os.path.join(temp_dir, "idempotency.db")
    idempotency = IdempotencyStore(db_path=idem_path)

    ledger_path = os.path.join(temp_dir, "ledger.db")
    ledger = TradeLedger("BTC-USD", db_path=ledger_path)

    oms = OMSReconcileService(
        idempotency=idempotency,
        ledger=ledger,
        fill_fetcher=fill_fetcher or MagicMock(return_value=[]),
        on_bootstrap_complete=on_bootstrap_complete,
        on_degraded=on_degraded,
    )
    return oms, idempotency, ledger


def _complete_bootstrap(oms):
    """Helper: enviar snapshot vacío para completar bootstrap."""
    oms.handle_user_event("snapshot", [])


class TestReadinessGate:
    """is_ready() como gate obligatorio."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_not_ready_before_bootstrap(self):
        """is_ready() = False antes de bootstrap."""
        oms, _, _ = _make_oms(self.temp_dir)
        assert not oms.is_ready()
        assert not oms.is_bootstrap_complete()

    def test_ready_after_bootstrap(self):
        """is_ready() = True después de bootstrap con snapshot vacío."""
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)
        assert oms.is_ready()
        assert oms.is_bootstrap_complete()

    def test_not_ready_when_degraded(self):
        """is_ready() = False si OMS degradado (aunque bootstrap completo)."""
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)
        assert oms.is_ready()

        oms.report_divergence("test divergence")
        assert not oms.is_ready()
        assert oms.is_degraded()

    def test_ready_after_clear_degraded(self):
        """is_ready() = True después de clear_degraded (si bootstrap completo)."""
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)
        oms.report_divergence("test")
        assert not oms.is_ready()

        oms.clear_degraded()
        assert oms.is_ready()

    def test_not_ready_if_clear_degraded_without_bootstrap(self):
        """clear_degraded no hace ready si bootstrap no completo."""
        oms, _, _ = _make_oms(self.temp_dir)
        oms.report_divergence("test")
        oms.clear_degraded()
        assert not oms.is_ready()  # bootstrap todavía incompleto


class TestBootstrapCallback:
    """on_bootstrap_complete callback se invoca."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_on_bootstrap_complete_called(self):
        """on_bootstrap_complete se invoca cuando bootstrap termina."""
        callback = MagicMock()
        oms, _, _ = _make_oms(self.temp_dir, on_bootstrap_complete=callback)

        _complete_bootstrap(oms)
        callback.assert_called_once()

    def test_on_bootstrap_complete_called_once(self):
        """on_bootstrap_complete se invoca solo una vez."""
        callback = MagicMock()
        oms, _, _ = _make_oms(self.temp_dir, on_bootstrap_complete=callback)

        _complete_bootstrap(oms)
        # Segundo snapshot no debe re-invocar
        oms.handle_user_event("snapshot", [])
        callback.assert_called_once()


class TestOrphanHandling:
    """Orphan order → OMS degradado."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_orphan_order_degrades_oms(self):
        """Orden en exchange no en OMS → degradado."""
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)
        assert oms.is_ready()

        # Enviar update con orden desconocida
        orphan = {
            "order_id": "unknown-exchange-id",
            "client_order_id": "unknown-client-id",
            "product_id": "BTC-USD",
            "status": "OPEN",
            "number_of_fills": "0",
        }
        oms.handle_user_event("update", [orphan])

        assert oms.is_degraded()
        assert not oms.is_ready()
        incidents = oms.get_incidents()
        assert len(incidents) == 1
        assert incidents[0].incident_type == "ORPHAN_ORDER"
        assert "unknown-client-id" in incidents[0].detail

    def test_orphan_triggers_on_degraded_callback(self):
        """Orphan invoca on_degraded callback."""
        degraded_cb = MagicMock()
        oms, _, _ = _make_oms(self.temp_dir, on_degraded=degraded_cb)
        _complete_bootstrap(oms)

        orphan = {
            "order_id": "orphan-1",
            "client_order_id": "orphan-client-1",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }
        oms.handle_user_event("update", [orphan])

        degraded_cb.assert_called_once()
        incident = degraded_cb.call_args[0][0]
        assert incident.incident_type == "ORPHAN_ORDER"

    def test_known_order_does_not_degrade(self):
        """Orden conocida no degrada OMS."""
        oms, idempotency, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)

        # Pre-populate con orden conocida
        intent = OrderIntent(
            client_order_id="known-client",
            signal_id="test-signal",
            strategy_id="test-strategy",
            symbol="BTC-USD",
            side="BUY",
            final_qty=Decimal("0.1"),
            order_type="LIMIT",
            price=Decimal("50000"),
            reduce_only=False,
            post_only=True,
            viable=True,
            planner_version="test",
        )
        idempotency.save_intent(intent, OrderState.OPEN_PENDING)

        update = {
            "order_id": "exchange-1",
            "client_order_id": "known-client",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "0",
        }
        oms.handle_user_event("update", [update])

        assert not oms.is_degraded()
        assert oms.is_ready()


class TestFillFetcherDegradation:
    """fill_fetcher failure → OMS degradado."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fill_fetcher_exception_degrades_oms(self):
        """fill_fetcher lanza excepción → OMS degradado."""
        failing_fetcher = MagicMock(side_effect=Exception("network error"))
        oms, idempotency, _ = _make_oms(
            self.temp_dir, fill_fetcher=failing_fetcher
        )
        _complete_bootstrap(oms)

        # Pre-populate
        intent = OrderIntent(
            client_order_id="fill-client",
            signal_id="test-signal",
            strategy_id="test-strategy",
            symbol="BTC-USD",
            side="BUY",
            final_qty=Decimal("0.1"),
            order_type="MARKET",
            price=None,
            reduce_only=False,
            post_only=False,
            viable=True,
            planner_version="test",
        )
        idempotency.save_intent(intent, OrderState.OPEN_PENDING)

        update = {
            "order_id": "fill-exchange",
            "client_order_id": "fill-client",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }
        oms.handle_user_event("update", [update])

        assert oms.is_degraded()
        assert not oms.is_ready()
        incidents = oms.get_incidents()
        assert any(i.incident_type == "FILL_FETCH_FAILED" for i in incidents)

    def test_fill_fetcher_success_no_degradation(self):
        """fill_fetcher exitoso no degrada OMS."""
        fetcher = MagicMock(return_value=[{
            "trade_id": "t-1",
            "side": "BUY",
            "size": "0.1",
            "price": "50000",
            "commission": "5",
            "trade_time": "2024-01-01T00:00:00Z",
        }])
        oms, idempotency, _ = _make_oms(
            self.temp_dir, fill_fetcher=fetcher
        )
        _complete_bootstrap(oms)

        intent = OrderIntent(
            client_order_id="ok-client",
            signal_id="test-signal",
            strategy_id="test-strategy",
            symbol="BTC-USD",
            side="BUY",
            final_qty=Decimal("0.1"),
            order_type="MARKET",
            price=None,
            reduce_only=False,
            post_only=False,
            viable=True,
            planner_version="test",
        )
        idempotency.save_intent(intent, OrderState.OPEN_PENDING)

        update = {
            "order_id": "ok-exchange",
            "client_order_id": "ok-client",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }
        oms.handle_user_event("update", [update])

        assert not oms.is_degraded()
        assert oms.is_ready()


class TestDivergenceReporting:
    """report_divergence → OMS degradado."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_report_divergence_degrades(self):
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)

        oms.report_divergence("ledger position != exchange position")

        assert oms.is_degraded()
        assert not oms.is_ready()
        incidents = oms.get_incidents()
        assert incidents[0].incident_type == "DIVERGENCE"

    def test_multiple_incidents_tracked(self):
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)

        oms.report_divergence("issue 1")
        oms.report_divergence("issue 2")

        assert len(oms.get_incidents()) == 2

    def test_stats_reflect_degradation(self):
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)
        oms.report_divergence("test")

        stats = oms.get_stats()
        assert stats["degraded"] is True
        assert stats["incidents"] == 1
        assert "test" in stats["degraded_reason"]


class TestBootstrapForceComplete:
    """complete_bootstrap_if_no_snapshot() for accounts with 0 open orders."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_force_complete_when_no_snapshot(self):
        """Force-completes bootstrap when no snapshot received."""
        oms, _, _ = _make_oms(self.temp_dir)
        assert not oms.is_bootstrap_complete()

        result = oms.complete_bootstrap_if_no_snapshot()
        assert result is True
        assert oms.is_bootstrap_complete()
        assert oms.is_ready()

    def test_force_complete_invokes_callback(self):
        """on_bootstrap_complete callback fires on force-complete."""
        callback = MagicMock()
        oms, _, _ = _make_oms(self.temp_dir, on_bootstrap_complete=callback)

        oms.complete_bootstrap_if_no_snapshot()
        callback.assert_called_once()

    def test_noop_if_already_bootstrapped(self):
        """Returns False if bootstrap already complete."""
        oms, _, _ = _make_oms(self.temp_dir)
        _complete_bootstrap(oms)

        result = oms.complete_bootstrap_if_no_snapshot()
        assert result is False

    def test_noop_if_snapshot_received(self):
        """Does not force-complete if a snapshot batch was already received."""
        oms, _, _ = _make_oms(self.temp_dir)
        # Simulate a partial snapshot (batch with 50+ orders, bootstrap not yet done)
        oms._snapshot_batches = 1
        oms._orders_in_snapshot = 60

        result = oms.complete_bootstrap_if_no_snapshot()
        assert result is False
        assert not oms.is_bootstrap_complete()

    def test_force_complete_idempotent(self):
        """Calling twice returns False the second time."""
        callback = MagicMock()
        oms, _, _ = _make_oms(self.temp_dir, on_bootstrap_complete=callback)

        assert oms.complete_bootstrap_if_no_snapshot() is True
        assert oms.complete_bootstrap_if_no_snapshot() is False
        callback.assert_called_once()
