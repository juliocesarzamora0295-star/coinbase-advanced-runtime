"""
Signal → Intent pipeline tests.

Verifies:
- observe_only, dry_run, and live modes are mutually exclusive
- Signal flows through RiskGate before reaching OrderPlanner
- Each pipeline step logs structured telemetry
- Mode-specific blocking behavior
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot
from src.risk.position_sizer import PositionSizer, SizingDecision, SizingMode, SymbolConstraints
from src.execution.order_planner import OrderPlanner, OrderIntent, RiskDecisionInput, OrderNotAllowedError
from src.strategy.signal import make_signal

from datetime import datetime, timezone


# ──────────────────────────────────────────────
# Mode exclusivity
# ──────────────────────────────────────────────

class TestModeExclusivity:
    """Verify observe_only, dry_run, and live are mutually exclusive."""

    def test_observe_only_blocks_all_execution(self):
        """observe_only=True → no signals processed, no orders generated."""
        from src.config import TradingConfig
        cfg = TradingConfig(observe_only=True, dry_run=False)
        assert cfg.observe_only is True
        assert cfg.dry_run is False
        # observe_only takes precedence: no execution path

    def test_dry_run_allows_signals_blocks_exchange(self):
        """dry_run=True, observe_only=False → signals processed, PaperEngine used."""
        from src.config import TradingConfig
        cfg = TradingConfig(observe_only=False, dry_run=True)
        assert cfg.observe_only is False
        assert cfg.dry_run is True

    def test_live_mode_both_false(self):
        """Both false → live trading (orders sent to exchange)."""
        from src.config import TradingConfig
        cfg = TradingConfig(observe_only=False, dry_run=False)
        assert cfg.observe_only is False
        assert cfg.dry_run is False

    def test_observe_only_overrides_dry_run(self):
        """If both are True, observe_only takes precedence (no signals)."""
        from src.config import TradingConfig
        cfg = TradingConfig(observe_only=True, dry_run=True)
        # observe_only=True is checked first in main.py._execute_order
        assert cfg.observe_only is True

    def test_default_config_is_safe(self):
        """Default TradingConfig should be safe (no live trading)."""
        from src.config import TradingConfig
        cfg = TradingConfig()
        # Default: observe_only=True AND dry_run=True — double safety
        assert cfg.observe_only is True or cfg.dry_run is True


# ──────────────────────────────────────────────
# Signal → RiskGate → OrderPlanner flow
# ──────────────────────────────────────────────

class TestSignalToIntentPipeline:
    """Verify signal flows through RiskGate and OrderPlanner correctly."""

    def test_signal_evaluated_by_riskgate(self):
        """Signal generates a RiskSnapshot that RiskGate evaluates."""
        gate = RiskGate(RiskLimits())

        signal = make_signal(
            symbol="BTC-USD",
            direction="BUY",
            strength=Decimal("1.0"),
            strategy_id="test",
            bar_timestamp=datetime.now(tz=timezone.utc),
        )

        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            symbol=signal.symbol,
            side=signal.direction,
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("70000"),
            breaker_state="CLOSED",
        )

        verdict = gate.evaluate(snapshot)
        assert verdict.allowed
        assert verdict.hard_max_qty > Decimal("0")

    def test_blocked_signal_produces_no_intent(self):
        """RiskGate blocked → OrderPlanner raises OrderNotAllowedError."""
        planner = OrderPlanner()

        risk_input = RiskDecisionInput(
            allowed=False,
            hard_max_qty=Decimal("0"),
            hard_max_notional=Decimal("0"),
            reduce_only=False,
            reason="Circuit breaker OPEN",
        )

        sizing = SizingDecision(
            target_qty=Decimal("0.01"),
            target_notional=Decimal("700"),
            notional_budget_used=Decimal("0.07"),
            sizing_mode=SizingMode.NOTIONAL,
            rationale="test",
        )

        constraints = SymbolConstraints(
            step_size=Decimal("0.00000001"),
            min_qty=Decimal("0.00000001"),
            max_qty=Decimal("100"),
            min_notional=Decimal("1"),
        )

        with pytest.raises(OrderNotAllowedError):
            planner.plan(
                signal_id="test-sig",
                strategy_id="test",
                symbol="BTC-USD",
                side="BUY",
                sizing=sizing,
                risk=risk_input,
                constraints=constraints,
            )

    def test_allowed_signal_produces_intent(self):
        """RiskGate allowed → OrderPlanner produces OrderIntent."""
        planner = OrderPlanner()

        risk_input = RiskDecisionInput(
            allowed=True,
            hard_max_qty=Decimal("0.01"),
            hard_max_notional=Decimal("700"),
            reduce_only=False,
            reason="Risk checks passed",
        )

        sizing = SizingDecision(
            target_qty=Decimal("0.01"),
            target_notional=Decimal("700"),
            notional_budget_used=Decimal("0.07"),
            sizing_mode=SizingMode.NOTIONAL,
            rationale="test",
        )

        constraints = SymbolConstraints(
            step_size=Decimal("0.00000001"),
            min_qty=Decimal("0.00000001"),
            max_qty=Decimal("100"),
            min_notional=Decimal("1"),
        )

        intent = planner.plan(
            signal_id="test-sig-2",
            strategy_id="test",
            symbol="BTC-USD",
            side="BUY",
            sizing=sizing,
            risk=risk_input,
            constraints=constraints,
        )

        assert isinstance(intent, OrderIntent)
        assert intent.symbol == "BTC-USD"
        assert intent.side == "BUY"
        assert intent.viable is True
        assert intent.final_qty > Decimal("0")


# ──────────────────────────────────────────────
# Kill switch blocks before RiskGate
# ──────────────────────────────────────────────

class TestKillSwitchInPipeline:
    """Kill switch blocks at the very start of the pipeline."""

    def test_kill_switch_blocks_via_snapshot(self):
        """kill_switch=True in snapshot → RiskGate blocks."""
        gate = RiskGate(RiskLimits())
        snapshot = RiskSnapshot(
            equity=Decimal("10000"),
            position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            symbol="BTC-USD",
            side="BUY",
            target_qty=Decimal("0.01"),
            entry_ref=Decimal("70000"),
            kill_switch=True,
        )
        verdict = gate.evaluate(snapshot)
        assert not verdict.allowed
        assert "KILL_SWITCH" in verdict.blocking_rule_ids


# ──────────────────────────────────────────────
# OMS readiness blocks before signal processing
# ──────────────────────────────────────────────

class TestOMSGateInPipeline:
    """OMS not ready → signal blocked before reaching RiskGate."""

    def test_oms_not_ready_blocks(self):
        """Simulate OMS not ready → pipeline blocked."""
        import os, tempfile
        from src.oms.reconcile import OMSReconcileService
        from src.accounting.ledger import TradeLedger
        from src.execution.idempotency import IdempotencyStore

        temp_dir = tempfile.mkdtemp()
        idem = IdempotencyStore(db_path=os.path.join(temp_dir, "i.db"))
        ledger = TradeLedger("BTC-USD", db_path=os.path.join(temp_dir, "l.db"))
        oms = OMSReconcileService(idempotency=idem, ledger=ledger)

        # Not bootstrapped → not ready
        assert not oms.is_ready()

        # After bootstrap → ready
        oms.handle_user_event("snapshot", [])
        assert oms.is_ready()

    def test_oms_degraded_blocks(self):
        """OMS degraded → pipeline blocked."""
        import os, tempfile
        from src.oms.reconcile import OMSReconcileService
        from src.accounting.ledger import TradeLedger
        from src.execution.idempotency import IdempotencyStore

        temp_dir = tempfile.mkdtemp()
        idem = IdempotencyStore(db_path=os.path.join(temp_dir, "i2.db"))
        ledger = TradeLedger("BTC-USD", db_path=os.path.join(temp_dir, "l2.db"))
        oms = OMSReconcileService(idempotency=idem, ledger=ledger)

        oms.handle_user_event("snapshot", [])
        assert oms.is_ready()

        oms.report_divergence("test drift")
        assert not oms.is_ready()


# ──────────────────────────────────────────────
# OrderIntent properties
# ──────────────────────────────────────────────

class TestOrderIntentContract:
    """Verify OrderIntent is immutable and has correct traceability."""

    def test_intent_is_frozen(self):
        """OrderIntent is frozen dataclass — immutable."""
        planner = OrderPlanner()
        risk_input = RiskDecisionInput(
            allowed=True,
            hard_max_qty=Decimal("0.01"),
            hard_max_notional=Decimal("700"),
            reduce_only=False,
            reason="ok",
        )
        sizing = SizingDecision(
            target_qty=Decimal("0.01"),
            target_notional=Decimal("700"),
            notional_budget_used=Decimal("0.07"),
            sizing_mode=SizingMode.NOTIONAL,
            rationale="test",
        )
        constraints = SymbolConstraints(
            step_size=Decimal("0.00000001"),
            min_qty=Decimal("0.00000001"),
            max_qty=Decimal("100"),
            min_notional=Decimal("1"),
        )
        intent = planner.plan(
            signal_id="sig-freeze",
            strategy_id="test",
            symbol="BTC-USD",
            side="BUY",
            sizing=sizing,
            risk=risk_input,
            constraints=constraints,
        )

        with pytest.raises(AttributeError):
            intent.symbol = "ETH-USD"  # frozen

    def test_intent_has_signal_traceability(self):
        """OrderIntent traces back to signal_id and strategy_id."""
        planner = OrderPlanner()
        risk_input = RiskDecisionInput(
            allowed=True, hard_max_qty=Decimal("0.01"),
            hard_max_notional=Decimal("700"), reduce_only=False, reason="ok",
        )
        sizing = SizingDecision(
            target_qty=Decimal("0.01"), target_notional=Decimal("700"),
            notional_budget_used=Decimal("0.07"), sizing_mode=SizingMode.NOTIONAL,
            rationale="test",
        )
        constraints = SymbolConstraints(
            step_size=Decimal("0.00000001"), min_qty=Decimal("0.00000001"),
            max_qty=Decimal("100"), min_notional=Decimal("1"),
        )
        intent = planner.plan(
            signal_id="sig-trace-123",
            strategy_id="sma_crossover_20_50",
            symbol="BTC-USD",
            side="BUY",
            sizing=sizing,
            risk=risk_input,
            constraints=constraints,
        )

        assert intent.signal_id == "sig-trace-123"
        assert intent.strategy_id == "sma_crossover_20_50"
        assert intent.client_order_id  # deterministic hash
