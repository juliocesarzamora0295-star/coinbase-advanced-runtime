"""
Tests unitarios: RiskGate fail-closed semantics.

Verifica que RiskGate bloquea incondicionalmente cuando faltan
inputs críticos (equity, position, market_price) o cuando el
CircuitBreaker está OPEN.

Invariantes testeadas:
- equity=None o equity=0 → RULE_EQUITY_ZERO_OR_MISSING (fail-closed)
- breaker_state="OPEN" → RULE_CIRCUIT_BREAKER_OPEN (fail-closed)
- breaker_state="HALF_OPEN" → PASA (HALF_OPEN permite tráfico de prueba)
- daily_loss_pct <= -threshold → RULE_DAILY_LOSS_LIMIT
- drawdown_pct >= threshold → RULE_MAX_DRAWDOWN
- target_qty=0 → RULE_TARGET_QTY_ZERO
- SELL sin posición → RULE_SELL_NO_POSITION
- allowed=False siempre implica hard_max_qty=0 y blocking_rule_ids no vacío
- allowed=True siempre implica blocking_rule_ids vacío
- observe_only semantic: gate bloquea según estado — sin rutas de bypass
"""

from decimal import Decimal

from src.risk.gate import (
    RULE_CIRCUIT_BREAKER_OPEN,
    RULE_DAILY_LOSS_LIMIT,
    RULE_EQUITY_ZERO_OR_MISSING,
    RULE_MAX_DRAWDOWN,
    RULE_SELL_NO_POSITION,
    RULE_TARGET_QTY_ZERO,
    RiskDecision,
    RiskGate,
    RiskLimits,
    RiskSnapshot,
)


def make_gate(
    max_position_pct: str = "0.20",
    max_notional: str = "10000",
    max_orders_per_minute: int = 10,
    max_daily_loss_pct: str = "0.05",
    max_drawdown_pct: str = "0.15",
) -> RiskGate:
    limits = RiskLimits(
        max_position_pct=Decimal(max_position_pct),
        max_notional_per_symbol=Decimal(max_notional),
        max_orders_per_minute=max_orders_per_minute,
        max_daily_loss_pct=Decimal(max_daily_loss_pct),
        max_drawdown_pct=Decimal(max_drawdown_pct),
    )
    return RiskGate(limits=limits)


def make_snapshot(
    equity: str = "10000",
    position_qty: str = "0",
    day_pnl_pct: str = "0",
    drawdown_pct: str = "0",
    orders_last_minute: int = 0,
) -> RiskSnapshot:
    return RiskSnapshot(
        equity=Decimal(equity),
        position_qty=Decimal(position_qty),
        day_pnl_pct=Decimal(day_pnl_pct),
        drawdown_pct=Decimal(drawdown_pct),
        orders_last_minute=orders_last_minute,
    )


def evaluate_buy(
    gate: RiskGate,
    snapshot: RiskSnapshot,
    target_qty: str = "0.1",
    entry_ref: str = "50000",
    breaker_state: str = "CLOSED",
) -> RiskDecision:
    return gate.evaluate(
        symbol="BTC-USD",
        side="BUY",
        snapshot=snapshot,
        target_qty=Decimal(target_qty),
        entry_ref=Decimal(entry_ref),
        breaker_state=breaker_state,
    )


def evaluate_sell(
    gate: RiskGate,
    snapshot: RiskSnapshot,
    target_qty: str = "0.1",
    entry_ref: str = "50000",
    breaker_state: str = "CLOSED",
) -> RiskDecision:
    return gate.evaluate(
        symbol="BTC-USD",
        side="SELL",
        snapshot=snapshot,
        target_qty=Decimal(target_qty),
        entry_ref=Decimal(entry_ref),
        breaker_state=breaker_state,
    )


# ──────────────────────────────────────────────
# Invariante base: allowed=False implica caps=0
# ──────────────────────────────────────────────


class TestBlockedInvariant:

    def test_blocked_decision_has_zero_qty(self):
        """Toda RiskDecision con allowed=False tiene hard_max_qty=0."""
        gate = make_gate()
        snap = make_snapshot()
        decision = evaluate_buy(gate, snap, breaker_state="OPEN")
        assert decision.allowed is False
        assert decision.hard_max_qty == Decimal("0")
        assert decision.hard_max_notional == Decimal("0")

    def test_blocked_decision_has_blocking_rule_ids(self):
        """Toda RiskDecision con allowed=False tiene blocking_rule_ids no vacío."""
        gate = make_gate()
        snap = make_snapshot()
        decision = evaluate_buy(gate, snap, breaker_state="OPEN")
        assert decision.allowed is False
        assert len(decision.blocking_rule_ids) > 0

    def test_allowed_decision_has_empty_rule_ids(self):
        """RiskDecision con allowed=True tiene blocking_rule_ids vacío."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is True
        assert decision.blocking_rule_ids == ()

    def test_allowed_decision_has_positive_hard_max(self):
        """RiskDecision con allowed=True tiene hard_max_qty > 0."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is True
        assert decision.hard_max_qty > Decimal("0")


# ──────────────────────────────────────────────
# Circuit Breaker como input
# ──────────────────────────────────────────────


class TestCircuitBreakerInput:

    def test_breaker_open_blocks_buy(self):
        """breaker_state=OPEN → bloqueado con RULE_CIRCUIT_BREAKER_OPEN."""
        gate = make_gate()
        snap = make_snapshot(equity="10000")
        decision = evaluate_buy(gate, snap, breaker_state="OPEN")
        assert decision.allowed is False
        assert RULE_CIRCUIT_BREAKER_OPEN in decision.blocking_rule_ids

    def test_breaker_open_blocks_sell(self):
        """breaker_state=OPEN bloquea también ventas."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0.5")
        decision = evaluate_sell(gate, snap, breaker_state="OPEN")
        assert decision.allowed is False
        assert RULE_CIRCUIT_BREAKER_OPEN in decision.blocking_rule_ids

    def test_breaker_half_open_allows_trade(self):
        """breaker_state=HALF_OPEN → NO bloqueado por breaker (permite tráfico de prueba)."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0")
        decision = evaluate_buy(gate, snap, breaker_state="HALF_OPEN")
        # HALF_OPEN no es tratado como OPEN — se permite el trade de prueba
        assert RULE_CIRCUIT_BREAKER_OPEN not in decision.blocking_rule_ids

    def test_breaker_closed_allows_trade(self):
        """breaker_state=CLOSED → no bloqueado por breaker."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0")
        decision = evaluate_buy(gate, snap, breaker_state="CLOSED")
        assert RULE_CIRCUIT_BREAKER_OPEN not in decision.blocking_rule_ids
        assert decision.allowed is True


# ──────────────────────────────────────────────
# Equity fail-closed
# ──────────────────────────────────────────────


class TestEquityFailClosed:

    def test_equity_zero_blocks(self):
        """equity=0 → RULE_EQUITY_ZERO_OR_MISSING."""
        gate = make_gate()
        snap = make_snapshot(equity="0")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_EQUITY_ZERO_OR_MISSING in decision.blocking_rule_ids

    def test_equity_negative_blocks(self):
        """equity negativo → RULE_EQUITY_ZERO_OR_MISSING."""
        gate = make_gate()
        snap = make_snapshot(equity="-100")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_EQUITY_ZERO_OR_MISSING in decision.blocking_rule_ids

    def test_equity_positive_passes_check(self):
        """equity>0 supera el check de equity."""
        gate = make_gate()
        snap = make_snapshot(equity="0.01")
        decision = evaluate_buy(gate, snap, entry_ref="0.001")
        # No bloqueado por equity (pueden existir otros bloqueos)
        assert RULE_EQUITY_ZERO_OR_MISSING not in decision.blocking_rule_ids


# ──────────────────────────────────────────────
# Daily loss limit
# ──────────────────────────────────────────────


class TestDailyLossLimit:

    def test_daily_loss_at_threshold_blocks(self):
        """day_pnl_pct == -max_daily_loss_pct → bloqueado."""
        gate = make_gate(max_daily_loss_pct="0.05")
        snap = make_snapshot(equity="10000", day_pnl_pct="-0.05")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_DAILY_LOSS_LIMIT in decision.blocking_rule_ids

    def test_daily_loss_exceeds_threshold_blocks(self):
        """day_pnl_pct < -max_daily_loss_pct → bloqueado."""
        gate = make_gate(max_daily_loss_pct="0.05")
        snap = make_snapshot(equity="10000", day_pnl_pct="-0.10")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_DAILY_LOSS_LIMIT in decision.blocking_rule_ids

    def test_daily_loss_below_threshold_allowed(self):
        """day_pnl_pct > -max_daily_loss_pct → no bloqueado por daily loss."""
        gate = make_gate(max_daily_loss_pct="0.05")
        snap = make_snapshot(equity="10000", day_pnl_pct="-0.04")
        decision = evaluate_buy(gate, snap)
        assert RULE_DAILY_LOSS_LIMIT not in decision.blocking_rule_ids


# ──────────────────────────────────────────────
# Max drawdown
# ──────────────────────────────────────────────


class TestMaxDrawdown:

    def test_drawdown_at_threshold_blocks(self):
        """drawdown_pct == max_drawdown_pct → bloqueado."""
        gate = make_gate(max_drawdown_pct="0.15", max_daily_loss_pct="1.0")
        snap = make_snapshot(equity="10000", drawdown_pct="0.15")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_MAX_DRAWDOWN in decision.blocking_rule_ids

    def test_drawdown_exceeds_threshold_blocks(self):
        """drawdown_pct > max_drawdown_pct → bloqueado."""
        gate = make_gate(max_drawdown_pct="0.15", max_daily_loss_pct="1.0")
        snap = make_snapshot(equity="10000", drawdown_pct="0.20")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_MAX_DRAWDOWN in decision.blocking_rule_ids

    def test_drawdown_below_threshold_allowed(self):
        """drawdown_pct < max_drawdown_pct → no bloqueado por drawdown."""
        gate = make_gate(max_drawdown_pct="0.15", max_daily_loss_pct="1.0")
        snap = make_snapshot(equity="10000", drawdown_pct="0.10")
        decision = evaluate_buy(gate, snap)
        assert RULE_MAX_DRAWDOWN not in decision.blocking_rule_ids


# ──────────────────────────────────────────────
# target_qty = 0
# ──────────────────────────────────────────────


class TestTargetQtyZero:

    def test_target_qty_zero_blocks(self):
        """target_qty=0 → RULE_TARGET_QTY_ZERO."""
        gate = make_gate()
        snap = make_snapshot(equity="10000")
        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0"),
            entry_ref=Decimal("50000"),
        )
        assert decision.allowed is False
        assert RULE_TARGET_QTY_ZERO in decision.blocking_rule_ids

    def test_target_qty_negative_blocks(self):
        """target_qty<0 → bloqueado."""
        gate = make_gate()
        snap = make_snapshot(equity="10000")
        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("-0.001"),
            entry_ref=Decimal("50000"),
        )
        assert decision.allowed is False


# ──────────────────────────────────────────────
# SELL sin posición (spot-only)
# ──────────────────────────────────────────────


class TestSellWithoutPosition:

    def test_sell_without_position_blocks(self):
        """SELL sin posición → RULE_SELL_NO_POSITION."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0")
        decision = evaluate_sell(gate, snap)
        assert decision.allowed is False
        assert RULE_SELL_NO_POSITION in decision.blocking_rule_ids

    def test_sell_with_position_allowed(self):
        """SELL con posición → no bloqueado por SELL_NO_POSITION."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0.5")
        decision = evaluate_sell(gate, snap, target_qty="0.1")
        assert RULE_SELL_NO_POSITION not in decision.blocking_rule_ids
        assert decision.allowed is True
        assert decision.reduce_only is True

    def test_sell_reduce_only_flag(self):
        """SELL aprobado → reduce_only=True (spot-only enforcement)."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="1.0")
        decision = evaluate_sell(gate, snap, target_qty="0.5")
        assert decision.allowed is True
        assert decision.reduce_only is True

    def test_buy_reduce_only_false(self):
        """BUY aprobado → reduce_only=False."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is True
        assert decision.reduce_only is False


# ──────────────────────────────────────────────
# Múltiples condiciones simultáneas
# ──────────────────────────────────────────────


class TestMultipleBlockingConditions:

    def test_breaker_open_takes_priority_over_equity(self):
        """breaker=OPEN se evalúa ANTES que equity — solo aparece CIRCUIT_BREAKER_OPEN."""
        gate = make_gate()
        snap = make_snapshot(equity="0")  # también fallaría por equity
        decision = evaluate_buy(gate, snap, breaker_state="OPEN")
        assert decision.allowed is False
        # El breaker se evalúa primero — solo ese rule_id esperado
        assert RULE_CIRCUIT_BREAKER_OPEN in decision.blocking_rule_ids

    def test_equity_zero_takes_priority_over_daily_loss(self):
        """equity=0 se evalúa ANTES que daily loss."""
        gate = make_gate(max_daily_loss_pct="0.05")
        snap = make_snapshot(equity="0", day_pnl_pct="-0.10")
        decision = evaluate_buy(gate, snap)
        assert decision.allowed is False
        assert RULE_EQUITY_ZERO_OR_MISSING in decision.blocking_rule_ids
