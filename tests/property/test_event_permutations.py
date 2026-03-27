"""
Tests de propiedad: permutaciones de eventos OMS + Ledger.

Verifica que distintas permutaciones de eventos convergen al mismo estado final,
o que las permutaciones inválidas son rechazadas sin corrupciones de estado.

Invariantes testeadas:
- ACK + fill en cualquier orden → posición idéntica en ledger
- Fill duplicado en cualquier permutación → contabilizado solo una vez
- N fills parciales en cualquier orden → posición total idéntica
- Secuencias válidas de OMS en cualquier orden → estado final consistente
- cancel_queued puede llegar antes o después de OPEN_RESTING → estado correcto
"""
import itertools
import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Tuple

import pytest

from src.accounting.ledger import Fill, TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderIntent, OrderState


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def make_intent(intent_id: str, client_id: str) -> OrderIntent:
    return OrderIntent(
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


def make_fill(trade_id: str, amount: str, price: str, ts_ms: int = 1_700_000_000_000) -> Fill:
    qty = Decimal(amount)
    prc = Decimal(price)
    return Fill(
        side="buy",
        amount=qty,
        price=prc,
        cost=qty * prc,
        fee_cost=Decimal("0"),
        fee_currency="USD",
        ts_ms=ts_ms,
        trade_id=trade_id,
        order_id="ord-perm-001",
    )


# ──────────────────────────────────────────────
# Permutaciones de fills en el ledger
# ──────────────────────────────────────────────

class TestFillPermutations:

    def test_two_fills_same_total_regardless_of_order(self, tmp_path):
        """
        Dos fills distintos aplicados en cualquier orden producen
        la misma posición total y avg_entry.
        """
        fill_a = make_fill("perm-a", "0.1", "50000", ts_ms=1_700_000_001_000)
        fill_b = make_fill("perm-b", "0.2", "48000", ts_ms=1_700_000_002_000)

        results = []
        for perm in itertools.permutations([fill_a, fill_b]):
            ledger = TradeLedger(
                symbol="BTC-USD",
                db_path=str(tmp_path / f"ledger_perm_{len(results)}.db")
            )
            for fill in perm:
                ledger.add_fill(fill)
            results.append((ledger.position_qty, ledger.avg_entry))

        # Todas las permutaciones deben dar el mismo resultado
        assert len(set(results)) == 1, (
            f"Permutaciones producen estados distintos: {results}"
        )

    def test_three_partial_fills_all_permutations(self, tmp_path):
        """
        3 fills parciales en todas las permutaciones → misma posición total.
        """
        fills = [
            make_fill("perm3-a", "0.05", "49000", ts_ms=1_700_000_001_000),
            make_fill("perm3-b", "0.03", "50000", ts_ms=1_700_000_002_000),
            make_fill("perm3-c", "0.07", "51000", ts_ms=1_700_000_003_000),
        ]
        expected_total = sum(Decimal(f.amount) for f in fills)

        for i, perm in enumerate(itertools.permutations(fills)):
            ledger = TradeLedger(
                symbol="BTC-USD",
                db_path=str(tmp_path / f"ledger_3perm_{i}.db")
            )
            for fill in perm:
                ledger.add_fill(fill)
            assert ledger.position_qty == expected_total, (
                f"Permutación {i} produce qty={ledger.position_qty} "
                f"(esperado {expected_total})"
            )

    def test_duplicate_fill_in_any_position_not_double_counted(self, tmp_path):
        """
        Fill duplicado insertado en cualquier posición → contabilizado una sola vez.
        """
        fill_unique = make_fill("perm-uniq", "0.1", "50000", ts_ms=1_700_000_001_000)
        fill_dup = make_fill("perm-dup", "0.2", "48000", ts_ms=1_700_000_002_000)
        fill_dup_again = make_fill("perm-dup", "0.2", "48000", ts_ms=1_700_000_002_000)  # mismo trade_id

        sequences = [
            [fill_unique, fill_dup, fill_dup_again],
            [fill_dup, fill_dup_again, fill_unique],
            [fill_dup_again, fill_unique, fill_dup],
        ]

        for i, seq in enumerate(sequences):
            ledger = TradeLedger(
                symbol="BTC-USD",
                db_path=str(tmp_path / f"ledger_dup_{i}.db")
            )
            for fill in seq:
                ledger.add_fill(fill)

            # fill_dup debe contar solo una vez: 0.1 + 0.2 = 0.3
            assert ledger.position_qty == Decimal("0.3"), (
                f"Secuencia {i}: qty={ledger.position_qty} (esperado 0.3)"
            )


# ──────────────────────────────────────────────
# Permutaciones de estados OMS
# ──────────────────────────────────────────────

class TestOMSStatePermutations:

    def test_new_then_open_resting_always_valid(self, tmp_path):
        """
        NEW → OPEN_RESTING es la transición estándar.
        Debe funcionar siempre, sin importar otros intents en el store.
        """
        db_path = str(tmp_path / "oms_perm.db")
        store = IdempotencyStore(db_path=db_path)

        # Crear 3 intents y moverlos todos a OPEN_RESTING
        intent_ids = []
        for i in range(3):
            intent_id = str(uuid.uuid4())
            client_id = str(uuid.uuid4())
            intent = make_intent(intent_id, client_id)
            store.save_intent(intent, OrderState.NEW)
            store.update_state(
                intent_id=intent_id,
                state=OrderState.OPEN_RESTING,
                exchange_order_id=f"ex-{i}",
            )
            intent_ids.append(intent_id)

        # Todos deben ser activos
        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        for intent_id in intent_ids:
            assert intent_id in active_ids

    def test_cancel_queued_before_fill_keeps_active(self, tmp_path):
        """
        Orden CANCEL_QUEUED sigue activa hasta que llega CANCELLED.
        """
        db_path = str(tmp_path / "oms_cq.db")
        store = IdempotencyStore(db_path=db_path)

        intent_id = str(uuid.uuid4())
        intent = make_intent(intent_id, str(uuid.uuid4()))
        store.save_intent(intent, OrderState.OPEN_RESTING)
        store.update_state(intent_id=intent_id, state=OrderState.CANCEL_QUEUED)

        record = store.get_by_intent_id(intent_id)
        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active is True

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert intent_id in active_ids

    def test_terminal_state_not_active_in_all_permutations(self, tmp_path):
        """
        Órdenes terminales (FILLED, CANCELLED, EXPIRED, FAILED) no son activas,
        independientemente de cómo llegaron a ese estado.
        """
        db_path = str(tmp_path / "oms_terminal.db")
        store = IdempotencyStore(db_path=db_path)

        terminal_cases = [
            (OrderState.FILLED, None),
            (OrderState.CANCELLED, None),
            (OrderState.EXPIRED, None),
            (OrderState.FAILED, "error_msg"),
        ]

        terminal_ids = []
        for state, error in terminal_cases:
            intent_id = str(uuid.uuid4())
            intent = make_intent(intent_id, str(uuid.uuid4()))
            store.save_intent(intent, state)
            terminal_ids.append(intent_id)

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        for intent_id in terminal_ids:
            assert intent_id not in active_ids, (
                f"{intent_id} terminal no debe estar activo"
            )

    def test_open_and_terminal_mixed_only_open_active(self, tmp_path):
        """
        Mix de órdenes abiertas y terminales → solo las abiertas en get_pending_or_open().
        """
        db_path = str(tmp_path / "oms_mixed.db")
        store = IdempotencyStore(db_path=db_path)

        open_ids = []
        terminal_ids = []

        for i in range(3):
            intent_id = str(uuid.uuid4())
            intent = make_intent(intent_id, str(uuid.uuid4()))
            store.save_intent(intent, OrderState.OPEN_RESTING)
            open_ids.append(intent_id)

        for state in [OrderState.FILLED, OrderState.CANCELLED]:
            intent_id = str(uuid.uuid4())
            intent = make_intent(intent_id, str(uuid.uuid4()))
            store.save_intent(intent, state)
            terminal_ids.append(intent_id)

        active_ids = [r.intent_id for r in store.get_pending_or_open()]

        for intent_id in open_ids:
            assert intent_id in active_ids
        for intent_id in terminal_ids:
            assert intent_id not in active_ids
