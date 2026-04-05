"""
Tests de integración: Atomicidad de recuperación SQLite.

Verifica qué pasa cuando OMS y Ledger quedan en estados divergentes
(ej. OMS dice FILLED pero Ledger no tiene el fill).

Invariantes testeadas:
- OMS FILLED + Ledger sin fill → divergencia detectada por comparación
- OMS OPEN_RESTING + Ledger con fill → divergencia (fill huérfano)
- OMS y Ledger en sync → no divergencia
- Restart con OMS y Ledger en sync → estado recuperado correctamente
- Fill aplicado al Ledger antes del restart → no se doble-cuenta al recargar
- Orden en CANCEL_QUEUED + restart → sigue activa (no pierde estado)

Sin Coinbase API. Usa SQLite temporal en tmp_path.
"""

import uuid
from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import OrderIntent

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def make_intent(client_order_id: str) -> OrderIntent:
    return OrderIntent(
        client_order_id=client_order_id,
        signal_id="test-signal",
        strategy_id="test-strategy",
        symbol="BTC-USD",
        side="BUY",
        final_qty=Decimal("0.1"),
        order_type="LIMIT",
        price=Decimal("50000"),
        reduce_only=False,
        post_only=False,
        viable=True,
        planner_version="test",
    )


def make_fill(trade_id: str, amount: str = "0.1", price: str = "50000") -> Fill:
    qty = Decimal(amount)
    prc = Decimal(price)
    return Fill(
        side="buy",
        amount=qty,
        price=prc,
        cost=qty * prc,
        fee_cost=Decimal("0"),
        fee_currency="USD",
        ts_ms=1_700_000_000_000,
        trade_id=trade_id,
        order_id="ord-atomicity-001",
    )


# ──────────────────────────────────────────────
# Divergencia OMS ↔ Ledger
# ──────────────────────────────────────────────


class TestOMSLedgerDivergence:

    def test_oms_filled_ledger_empty_is_divergent(self, tmp_path):
        """
        OMS dice FILLED pero Ledger no tiene fills → estado divergente.
        Verificamos que la comparación explícita lo detecta.
        """
        oms_db = str(tmp_path / "oms.db")
        ledger_db = str(tmp_path / "ledger.db")

        client_order_id = str(uuid.uuid4())
        store = IdempotencyStore(db_path=oms_db)
        intent = make_intent(client_order_id)
        store.save_intent(intent, OrderState.NEW)
        store.update_state(
            client_order_id=client_order_id,
            state=OrderState.FILLED,
            exchange_order_id="ex-div-001",
        )

        ledger = TradeLedger(symbol="BTC-USD", db_path=ledger_db)
        # Ledger vacío — no se aplicó ningún fill

        # Divergencia: OMS dice filled pero position=0
        record = store.get_by_intent_id(client_order_id)
        assert record.state == OrderState.FILLED
        assert ledger.position_qty == Decimal("0")
        # La aplicación debería detectar esto como divergencia;
        # no hay auto-corrección — se registra para revisión manual.

    def test_oms_open_ledger_with_fill_is_divergent(self, tmp_path):
        """
        Ledger tiene fill pero OMS sigue OPEN → fill huérfano.
        """
        oms_db = str(tmp_path / "oms.db")
        ledger_db = str(tmp_path / "ledger.db")

        client_order_id = str(uuid.uuid4())
        store = IdempotencyStore(db_path=oms_db)
        intent = make_intent(client_order_id)
        store.save_intent(intent, OrderState.OPEN_RESTING)

        ledger = TradeLedger(symbol="BTC-USD", db_path=ledger_db)
        fill = make_fill("orphan-fill-001")
        ledger.add_fill(fill)

        record = store.get_by_intent_id(client_order_id)
        assert record.state == OrderState.OPEN_RESTING
        assert ledger.position_qty == Decimal("0.1")
        # Divergencia: ledger tiene fill pero la orden sigue abierta

    def test_oms_and_ledger_in_sync(self, tmp_path):
        """
        OMS FILLED + Ledger con fill → estado coherente (no divergente).
        """
        oms_db = str(tmp_path / "oms.db")
        ledger_db = str(tmp_path / "ledger.db")

        client_order_id = str(uuid.uuid4())
        store = IdempotencyStore(db_path=oms_db)
        intent = make_intent(client_order_id)
        store.save_intent(intent, OrderState.NEW)
        store.update_state(
            client_order_id=client_order_id,
            state=OrderState.FILLED,
            exchange_order_id="ex-sync-001",
        )

        ledger = TradeLedger(symbol="BTC-USD", db_path=ledger_db)
        fill = make_fill("sync-fill-001")
        ledger.add_fill(fill)

        record = store.get_by_intent_id(client_order_id)
        assert record.state == OrderState.FILLED
        assert ledger.position_qty == Decimal("0.1")


# ──────────────────────────────────────────────
# Atomicidad de restart
# ──────────────────────────────────────────────


class TestRestartAtomicity:

    def test_fill_persisted_before_restart_not_double_counted(self, tmp_path):
        """
        Fill persistido en DB antes del restart → no se doble-cuenta al recargar.
        """
        ledger_db = str(tmp_path / "ledger.db")

        # Pre-restart
        ledger1 = TradeLedger(symbol="BTC-USD", db_path=ledger_db)
        fill = make_fill("atomic-fill-001")
        result1 = ledger1.add_fill(fill)
        assert result1 is True
        qty_pre = ledger1.position_qty

        # Restart
        ledger2 = TradeLedger(symbol="BTC-USD", db_path=ledger_db)
        result2 = ledger2.add_fill(fill)  # mismo fill

        assert result2 is False, "Fill pre-existente no debe re-aplicarse"
        assert ledger2.position_qty == qty_pre

    def test_oms_state_persisted_before_restart(self, tmp_path):
        """
        Estado OMS persistido antes del restart → idéntico post-restart.
        """
        oms_db = str(tmp_path / "oms.db")

        client_order_id = str(uuid.uuid4())
        store1 = IdempotencyStore(db_path=oms_db)
        intent = make_intent(client_order_id)
        store1.save_intent(intent, OrderState.OPEN_RESTING)
        store1.update_state(
            client_order_id=client_order_id,
            state=OrderState.CANCEL_QUEUED,
        )

        # Restart
        store2 = IdempotencyStore(db_path=oms_db)
        record = store2.get_by_intent_id(client_order_id)

        assert record is not None
        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active is True

    def test_multiple_fills_all_persisted_after_restart(self, tmp_path):
        """
        N fills persistidos → todos recuperados post-restart, sin duplicados.
        """
        ledger_db = str(tmp_path / "ledger.db")

        fills = [make_fill(f"multi-fill-{i}", amount="0.05") for i in range(4)]

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=ledger_db)
        for fill in fills:
            ledger1.add_fill(fill)
        qty_pre = ledger1.position_qty  # 4 × 0.05 = 0.20

        # Restart — nueva instancia
        ledger2 = TradeLedger(symbol="BTC-USD", db_path=ledger_db)

        assert ledger2.position_qty == qty_pre
        assert len(ledger2.fills) == 4

        # Re-aplicar todos → todos deduplicados
        for fill in fills:
            result = ledger2.add_fill(fill)
            assert result is False

        assert ledger2.position_qty == qty_pre

    def test_partial_oms_write_open_resting_recovers(self, tmp_path):
        """
        Orden NEW guardada → update a OPEN_RESTING → restart → sigue activa.
        """
        oms_db = str(tmp_path / "oms.db")

        client_order_id = str(uuid.uuid4())
        store1 = IdempotencyStore(db_path=oms_db)
        intent = make_intent(client_order_id)
        store1.save_intent(intent, OrderState.NEW)
        store1.update_state(
            client_order_id=client_order_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="ex-partial-001",
        )

        # Restart
        store2 = IdempotencyStore(db_path=oms_db)
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]
        assert client_order_id in active_ids

    def test_only_filled_before_crash_no_replay_on_restart(self, tmp_path):
        """
        Orden FILLED pre-crash → no aparece en pending post-restart.
        """
        oms_db = str(tmp_path / "oms.db")

        client_order_id = str(uuid.uuid4())
        store1 = IdempotencyStore(db_path=oms_db)
        intent = make_intent(client_order_id)
        store1.save_intent(intent, OrderState.FILLED)

        # Restart
        store2 = IdempotencyStore(db_path=oms_db)
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]
        assert client_order_id not in active_ids
