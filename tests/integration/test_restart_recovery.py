"""
Tests de integración: Restart Recovery.

Verifica que OMS (IdempotencyStore) y TradeLedger producen el mismo estado
tras un restart simulado (nueva instancia con mismo DB SQLite).

No requiere Coinbase API. Usa SQLite temporal en tmp_path.

Invariantes testeadas:
- OMS: órdenes abiertas pre-restart → recuperadas post-restart
- OMS: órdenes terminales pre-restart → no aparecen en get_pending_or_open()
- OMS: CANCEL_QUEUED persiste y se recupera correctamente
- Ledger: posición pre-restart → idéntica post-restart
- Ledger: fills no se doble-cuentan al recargar desde DB
- Ledger: realized PnL persiste tras restart
- Integración: OMS + Ledger independientes pero coherentes (mismo DB path)
"""

import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.accounting.ledger import Fill, TradeLedger
from src.execution.idempotency import IdempotencyStore, OrderIntent, OrderState

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "event_replays"


def make_oms_intent(
    intent_id: str,
    client_order_id: str,
    product_id: str = "BTC-USD",
    side: str = "BUY",
    order_type: str = "LIMIT",
    qty: str = "0.1",
    price: str = "50000",
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        client_order_id=client_order_id,
        product_id=product_id,
        side=side,
        order_type=order_type,
        qty=Decimal(qty),
        price=Decimal(price),
        stop_price=None,
        post_only=False,
        created_ts_ms=int(datetime.now().timestamp() * 1000),
    )


def make_fill_from_dict(d: dict) -> Fill:
    return Fill(
        side=d["side"],
        amount=Decimal(d["amount"]),
        price=Decimal(d["price"]),
        cost=Decimal(d["cost"]),
        fee_cost=Decimal(d["fee_cost"]),
        fee_currency=d["fee_currency"],
        ts_ms=d["ts_ms"],
        trade_id=d["trade_id"],
        order_id=d["order_id"],
    )


class TestOMSRestartRecovery:
    """OMS persiste y recupera estado completo tras restart."""

    def test_open_resting_order_recovered(self, tmp_path):
        """Orden OPEN_RESTING pre-restart → recuperada y activa post-restart."""
        db_path = str(tmp_path / "oms_restart.db")

        intent_id = str(uuid.uuid4())
        store1 = IdempotencyStore(db_path=db_path)
        intent = make_oms_intent(intent_id, str(uuid.uuid4()))
        store1.save_intent(intent, OrderState.NEW)
        store1.update_state(
            intent_id=intent_id,
            state=OrderState.OPEN_RESTING,
            exchange_order_id="ex-restart-001",
        )

        # Restart
        store2 = IdempotencyStore(db_path=db_path)
        record = store2.get_by_intent_id(intent_id)

        assert record is not None
        assert record.state == OrderState.OPEN_RESTING
        assert record.is_active
        assert record.exchange_order_id == "ex-restart-001"

    def test_cancel_queued_recovered(self, tmp_path):
        """Orden CANCEL_QUEUED pre-restart → recuperada y activa post-restart."""
        db_path = str(tmp_path / "oms_cq_restart.db")

        intent_id = str(uuid.uuid4())
        store1 = IdempotencyStore(db_path=db_path)
        intent = make_oms_intent(intent_id, str(uuid.uuid4()))
        store1.save_intent(intent, OrderState.OPEN_RESTING)
        store1.update_state(intent_id=intent_id, state=OrderState.CANCEL_QUEUED)

        store2 = IdempotencyStore(db_path=db_path)
        record = store2.get_by_intent_id(intent_id)

        assert record.state == OrderState.CANCEL_QUEUED
        assert record.is_active
        # Debe aparecer en get_pending_or_open
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]
        assert intent_id in active_ids

    def test_terminal_orders_not_in_pending_after_restart(self, tmp_path):
        """Órdenes terminales (FILLED, CANCELLED) no aparecen post-restart."""
        db_path = str(tmp_path / "oms_terminal_restart.db")

        store1 = IdempotencyStore(db_path=db_path)
        terminal_ids = []

        for state in [OrderState.FILLED, OrderState.CANCELLED, OrderState.EXPIRED]:
            intent_id = str(uuid.uuid4())
            intent = make_oms_intent(intent_id, str(uuid.uuid4()))
            store1.save_intent(intent, state)
            terminal_ids.append(intent_id)

        store2 = IdempotencyStore(db_path=db_path)
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]

        for intent_id in terminal_ids:
            assert (
                intent_id not in active_ids
            ), f"Intent terminal {intent_id} no debe estar en get_pending_or_open() post-restart"

    def test_multiple_open_orders_all_recovered(self, tmp_path):
        """N órdenes abiertas pre-restart → todas recuperadas post-restart."""
        db_path = str(tmp_path / "oms_multi_restart.db")

        store1 = IdempotencyStore(db_path=db_path)
        open_ids = []

        for i in range(5):
            intent_id = str(uuid.uuid4())
            intent = make_oms_intent(intent_id, str(uuid.uuid4()), qty=f"0.0{i+1}")
            store1.save_intent(intent, OrderState.OPEN_RESTING)
            open_ids.append(intent_id)

        store2 = IdempotencyStore(db_path=db_path)
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]

        for intent_id in open_ids:
            assert intent_id in active_ids


class TestLedgerRestartRecovery:
    """Ledger persiste y recupera estado completo tras restart."""

    def test_position_and_avg_entry_recovered(self, tmp_path):
        """position_qty y avg_entry idénticos post-restart."""
        db_path = str(tmp_path / "ledger_restart.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        fill = Fill(
            side="buy",
            amount=Decimal("0.15"),
            price=Decimal("48000"),
            cost=Decimal("7200"),
            fee_cost=Decimal("7.20"),
            fee_currency="USD",
            ts_ms=1_700_000_000_000,
            trade_id="t-pos-restart-001",
            order_id="o-001",
        )
        ledger1.add_fill(fill)

        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)

        assert ledger2.position_qty == ledger1.position_qty
        assert ledger2.avg_entry == ledger1.avg_entry

    def test_realized_pnl_recovered(self, tmp_path):
        """realized_pnl_quote idéntico post-restart."""
        db_path = str(tmp_path / "ledger_pnl_restart.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        buy = Fill(
            side="buy",
            amount=Decimal("0.1"),
            price=Decimal("50000"),
            cost=Decimal("5000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_000_000,
            trade_id="t-pnl-buy",
            order_id="o-buy",
        )
        sell = Fill(
            side="sell",
            amount=Decimal("0.1"),
            price=Decimal("55000"),
            cost=Decimal("5500"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_001_000,
            trade_id="t-pnl-sell",
            order_id="o-sell",
        )
        ledger1.add_fill(buy)
        ledger1.add_fill(sell)
        pnl_before = ledger1.realized_pnl_quote

        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)

        assert ledger2.realized_pnl_quote == pnl_before

    def test_fills_not_double_counted_on_restart(self, tmp_path):
        """Fills no se doble-cuentan al recargar desde DB."""
        db_path = str(tmp_path / "ledger_dedup_restart.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        fill = Fill(
            side="buy",
            amount=Decimal("0.1"),
            price=Decimal("50000"),
            cost=Decimal("5000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_000_000,
            trade_id="t-unique-restart",
            order_id="o-uniq",
        )
        ledger1.add_fill(fill)
        qty_before = ledger1.position_qty

        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        result = ledger2.add_fill(fill)  # mismo fill

        assert result is False, "Fill pre-existente no debe re-aplicarse"
        assert ledger2.position_qty == qty_before


class TestRestartRecoveryFromFixture:
    """Restart recovery usando fixture restart_recovery.json."""

    def test_restart_recovery_fixture(self, tmp_path):
        """Simular estado pre-restart desde fixture y verificar post-restart."""
        fixture_path = FIXTURES_DIR / "restart_recovery.json"
        if not fixture_path.exists():
            pytest.skip("Fixture restart_recovery.json no encontrado")

        with open(fixture_path) as f:
            fixture = json.load(f)

        oms_db = str(tmp_path / "oms_fixture.db")
        ledger_db = str(tmp_path / "ledger_fixture.db")

        # --- Pre-restart: cargar OMS state ---
        store1 = IdempotencyStore(db_path=oms_db)
        for oms_entry in fixture["pre_restart_oms_state"]:
            intent = make_oms_intent(
                intent_id=oms_entry["intent_id"],
                client_order_id=oms_entry["client_order_id"],
                product_id=oms_entry["product_id"],
                side=oms_entry["side"],
                order_type=oms_entry["order_type"],
                qty=oms_entry["qty"],
                price=oms_entry["price"],
            )
            state = OrderState[oms_entry["state"]]
            if state == OrderState.OPEN_RESTING:
                store1.save_intent(intent, OrderState.NEW)
                store1.update_state(
                    intent_id=oms_entry["intent_id"],
                    state=state,
                    exchange_order_id=oms_entry.get("exchange_order_id"),
                )
            else:
                store1.save_intent(intent, state)

        # --- Pre-restart: cargar Ledger fills ---
        ledger1 = TradeLedger(symbol=fixture["symbol"], db_path=ledger_db)
        for fill_data in fixture["pre_restart_fills"]:
            fill = make_fill_from_dict(fill_data)
            ledger1.add_fill(fill)

        # --- Restart ---
        store2 = IdempotencyStore(db_path=oms_db)
        ledger2 = TradeLedger(symbol=fixture["symbol"], db_path=ledger_db)

        # --- Verificar OMS post-restart ---
        expectations = fixture["post_restart_expectations"]
        active_ids = [r.intent_id for r in store2.get_pending_or_open()]

        for expected_id in expectations["open_oms_orders"]:
            assert expected_id in active_ids, f"Intent {expected_id} debe estar activo post-restart"

        for not_expected_id in expectations.get("filled_oms_orders", []):
            assert not_expected_id not in active_ids

        # --- Verificar Ledger post-restart ---
        assert ledger2.position_qty == Decimal(expectations["ledger_position_qty"])
        assert ledger2.avg_entry == Decimal(expectations["ledger_avg_entry"])
