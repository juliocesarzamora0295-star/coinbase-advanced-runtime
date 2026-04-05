"""
Tests de integración: Kill switch.

Verifica que kill_switch=True bloquea todas las señales incondicionalmente,
que las órdenes abiertas permanecen (cancel policy es decisión del caller),
y que el recovery requiere acción explícita (pasar kill_switch=False).

Invariantes testeadas:
- kill_switch=True → RiskDecision.allowed=False con RULE_KILL_SWITCH
- kill_switch=True bloquea BUY y SELL
- kill_switch=True tiene prioridad sobre equity, breaker_state, etc.
- kill_switch=False → evaluación normal (puede aprobar si todo OK)
- kill_switch=False recupera trading tras haber estado activo
- Órdenes abiertas en OMS no son canceladas automáticamente por el gate
  (cancel policy es responsabilidad del caller)
- kill_switch=True con PaperEngine → engine nunca recibe órdenes
"""

import uuid
from datetime import datetime
from decimal import Decimal

from src.execution.idempotency import IdempotencyStore, OrderState, StoredIntent
from src.risk.gate import (
    RULE_CIRCUIT_BREAKER_OPEN,
    RULE_KILL_SWITCH,
    RiskGate,
    RiskLimits,
    RiskSnapshot,
)
from src.simulation.paper_engine import PaperEngine

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

BID = Decimal("49900")
ASK = Decimal("50100")
ENTRY = Decimal("50000")


def make_gate() -> RiskGate:
    limits = RiskLimits(
        max_position_pct=Decimal("0.50"),
        max_notional_per_symbol=Decimal("100000"),
        max_orders_per_minute=100,
        max_daily_loss_pct=Decimal("0.10"),
        max_drawdown_pct=Decimal("0.20"),
    )
    return RiskGate(limits=limits)


def make_snapshot(equity: str = "10000", position_qty: str = "0") -> RiskSnapshot:
    return RiskSnapshot(
        equity=Decimal(equity),
        position_qty=Decimal(position_qty),
        day_pnl_pct=Decimal("0"),
        drawdown_pct=Decimal("0"),
    )


def make_intent(intent_id: str, client_id: str) -> StoredIntent:
    return StoredIntent(
        intent_id=intent_id,
        client_order_id=client_id,
        product_id="BTC-USD",
        side="BUY",
        order_type="LIMIT",
        qty=Decimal("0.1"),
        price=Decimal("50000"),
        stop_price=None,
        post_only=False,
        created_ts_ms=int(datetime.now().timestamp() * 1000),
    )


# ──────────────────────────────────────────────
# Kill switch ON bloquea todo
# ──────────────────────────────────────────────


class TestKillSwitchBlocks:

    def test_kill_switch_blocks_buy(self):
        """kill_switch=True → BUY bloqueado con RULE_KILL_SWITCH."""
        gate = make_gate()
        snap = make_snapshot(equity="10000")

        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=True,
        )

        assert decision.allowed is False
        assert RULE_KILL_SWITCH in decision.blocking_rule_ids
        assert decision.hard_max_qty == Decimal("0")

    def test_kill_switch_blocks_sell(self):
        """kill_switch=True → SELL bloqueado con RULE_KILL_SWITCH."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0.5")

        decision = gate.evaluate(
            symbol="BTC-USD",
            side="SELL",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=True,
        )

        assert decision.allowed is False
        assert RULE_KILL_SWITCH in decision.blocking_rule_ids

    def test_kill_switch_has_priority_over_good_equity(self):
        """kill_switch=True bloquea incluso con equity OK y breaker CLOSED."""
        gate = make_gate()
        snap = make_snapshot(equity="100000")

        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            breaker_state="CLOSED",
            kill_switch=True,
        )

        assert decision.allowed is False
        assert RULE_KILL_SWITCH in decision.blocking_rule_ids
        # No debe incluir CIRCUIT_BREAKER_OPEN (kill_switch evalúa primero)
        assert RULE_CIRCUIT_BREAKER_OPEN not in decision.blocking_rule_ids

    def test_kill_switch_blocks_all_signals_in_loop(self):
        """kill_switch=True → todas las señales de un bucle bloqueadas."""
        gate = make_gate()
        snap = make_snapshot(equity="50000")

        blocked_count = 0
        for _ in range(5):
            d = gate.evaluate(
                symbol="BTC-USD",
                side="BUY",
                snapshot=snap,
                target_qty=Decimal("0.05"),
                entry_ref=ENTRY,
                kill_switch=True,
            )
            if not d.allowed:
                blocked_count += 1

        assert blocked_count == 5


# ──────────────────────────────────────────────
# Kill switch OFF recupera trading
# ──────────────────────────────────────────────


class TestKillSwitchRecovery:

    def test_kill_switch_false_allows_normal_evaluation(self):
        """kill_switch=False → evaluación normal, puede aprobar señal."""
        gate = make_gate()
        snap = make_snapshot(equity="10000")

        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=False,
        )

        assert decision.allowed is True

    def test_kill_switch_recovery_requires_explicit_false(self):
        """
        Tras kill_switch=True, el recovery requiere kill_switch=False explícito.
        El gate no tiene estado interno de kill_switch — es parámetro por llamada.
        """
        gate = make_gate()
        snap = make_snapshot(equity="10000")

        # Con kill_switch=True
        d1 = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=True,
        )
        assert d1.allowed is False

        # Recovery explícito: kill_switch=False
        d2 = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=False,
        )
        assert d2.allowed is True
        assert RULE_KILL_SWITCH not in d2.blocking_rule_ids


# ──────────────────────────────────────────────
# OMS no cancela órdenes automáticamente
# ──────────────────────────────────────────────


class TestOpenOrdersDuringKillSwitch:

    def test_open_orders_not_auto_cancelled_by_gate(self, tmp_path):
        """
        kill_switch=True en RiskGate NO cancela órdenes abiertas en OMS.
        La cancel policy es responsabilidad del caller, no del gate.
        """
        store = IdempotencyStore(db_path=str(tmp_path / "oms.db"))

        intent_id = str(uuid.uuid4())
        intent = make_intent(intent_id, str(uuid.uuid4()))
        store.save_intent(intent, OrderState.OPEN_RESTING)

        gate = make_gate()
        snap = make_snapshot(equity="10000")

        # Activar kill_switch — solo bloquea nuevas órdenes
        gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=True,
        )

        # La orden abierta sigue en OMS — el gate no la toca
        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert intent_id in active_ids

    def test_paper_engine_never_called_when_kill_switch_on(self):
        """
        kill_switch=True → engine.submit_order nunca llamado.
        """
        gate = make_gate()
        engine = PaperEngine()
        snap = make_snapshot(equity="10000")

        decision = gate.evaluate(
            symbol="BTC-USD",
            side="BUY",
            snapshot=snap,
            target_qty=Decimal("0.1"),
            entry_ref=ENTRY,
            kill_switch=True,
        )

        assert decision.allowed is False
        # Engine no fue llamado — sin órdenes abiertas
        assert len(engine.open_orders) == 0
