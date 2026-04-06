"""
Runtime smoke test — verifica que los componentes core se construyen
y conectan sin error con config de test.

No requiere Coinbase API, WebSocket, ni conexión externa.
"""

import os
import tempfile
from decimal import Decimal

from src.accounting.ledger import TradeLedger
from src.execution.execution_report import build_execution_report
from src.execution.idempotency import IdempotencyStore
from src.observability import get_collector, reset_collector
from src.oms.reconcile import OMSReconcileService
from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskVerdict
from src.risk.kill_switch import KillSwitch, KillSwitchMode
from src.risk.position_sizer import PositionSizer, SizingMode, SymbolConstraints


class TestCoreComponentsInit:
    """Todos los componentes core se construyen sin error."""

    def test_ledger_creates(self, tmp_path):
        ledger = TradeLedger(
            "BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        assert ledger.cash == Decimal("10000")
        assert ledger.get_equity(Decimal("50000")) == Decimal("10000")

    def test_risk_gate_creates(self):
        gate = RiskGate(RiskLimits())
        snap = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            symbol="BTC-USD",
            side="BUY",
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        verdict = gate.evaluate(snap)
        assert isinstance(verdict, RiskVerdict)

    def test_circuit_breaker_creates(self):
        breaker = CircuitBreaker(BreakerConfig())
        assert breaker.state == BreakerState.CLOSED
        breaker.reset_day(Decimal("10000"))
        ok, _ = breaker.check_before_trade()
        assert ok is True

    def test_kill_switch_creates(self, tmp_path):
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        assert not ks.is_active
        ks.activate(KillSwitchMode.BLOCK_NEW, "test", "ci")
        assert ks.is_active
        ks.clear("ci")
        assert not ks.is_active

    def test_position_sizer_creates(self):
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=SymbolConstraints(
                step_size=Decimal("0.00001"),
                min_qty=Decimal("0.00001"),
                max_qty=Decimal("Infinity"),
                min_notional=Decimal("1"),
            ),
            max_notional=Decimal("10000"),
        )
        assert d.target_qty > Decimal("0")
        assert d.sizing_mode == SizingMode.NOTIONAL

    def test_oms_creates(self, tmp_path):
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "ledger.db")),
        )
        assert not oms.is_ready()
        oms.handle_user_event("snapshot", [])
        assert oms.is_ready()

    def test_idempotency_store_creates(self, tmp_path):
        store = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        assert store.get_pending_or_open() == []

    def test_metrics_collector_creates(self):
        reset_collector()
        c = get_collector()
        c.inc("smoke.test")
        assert c.get_counter("smoke.test") == 1
        reset_collector()

    def test_execution_report_creates(self):
        report = build_execution_report(
            client_order_id="smoke-1",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50010"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=100.0,
            outcome="FILLED",
        )
        assert report.outcome == "FILLED"
        assert report.slippage_bps > Decimal("0")


class TestSystemLifecycle:
    """Sistema arranca y para limpiamente."""

    def test_full_component_stack(self, tmp_path):
        """Construir todo el stack, operar un ciclo, y verificar estado."""
        # Build components
        ledger = TradeLedger(
            "BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        gate = RiskGate(RiskLimits())
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=ledger,
        )

        # Bootstrap OMS
        oms.handle_user_event("snapshot", [])
        assert oms.is_ready()

        # Verify gates pass
        assert not ks.is_active
        ok, _ = breaker.check_before_trade()
        assert ok

        # Evaluate risk
        snap = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            symbol="BTC-USD",
            side="BUY",
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("50000"),
        )
        verdict = gate.evaluate(snap)
        assert verdict.allowed

        # System is coherent
        assert ledger.get_equity(Decimal("50000")) == Decimal("10000")
        assert breaker.state == BreakerState.CLOSED
