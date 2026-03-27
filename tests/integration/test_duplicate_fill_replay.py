"""
Tests de integración: Deduplicación de fills en replay.

Verifica que fills duplicados (mismo trade_id) no se doble-cuentan
en el TradeLedger, independientemente del orden de llegada.

No requiere Coinbase API. Usa SQLite temporal en tmp_path.

Invariantes testeadas:
- fill duplicado (mismo trade_id) → add_fill retorna False
- posición no cambia tras fill duplicado
- PnL realizado no cambia tras fill duplicado de sell
- fills out-of-order con mismo trade_id → deduplicado correctamente
- N fills distintos del mismo order_id pero distintos trade_ids → todos cuentan
- replay completo desde fixture duplicate_fill.json → estado correcto
- replay desde fixture out_of_order_events.json → deduplicación correcta
"""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.accounting.ledger import Fill, TradeLedger


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "event_replays"


def make_fill(
    side: str,
    amount: str,
    price: str,
    trade_id: str,
    order_id: str = "ord-001",
    fee_cost: str = "0",
    fee_currency: str = "USD",
    ts_ms: int = 1_700_000_000_000,
) -> Fill:
    amt = Decimal(amount)
    prc = Decimal(price)
    return Fill(
        side=side,
        amount=amt,
        price=prc,
        cost=amt * prc,
        fee_cost=Decimal(fee_cost),
        fee_currency=fee_currency,
        ts_ms=ts_ms,
        trade_id=trade_id,
        order_id=order_id,
    )


@pytest.fixture
def ledger(tmp_path):
    db_path = str(tmp_path / "dedup_ledger.db")
    return TradeLedger(symbol="BTC-USD", db_path=db_path)


class TestBuyFillDeduplication:

    def test_duplicate_buy_returns_false(self, ledger):
        """Mismo trade_id en buy → segundo add_fill retorna False."""
        fill = make_fill("buy", "0.1", "50000", trade_id="dup-buy-001")
        assert ledger.add_fill(fill) is True
        assert ledger.add_fill(fill) is False

    def test_duplicate_buy_does_not_increase_position(self, ledger):
        """Fill duplicado no incrementa position_qty."""
        fill = make_fill("buy", "0.1", "50000", trade_id="dup-pos-001")
        ledger.add_fill(fill)
        qty_after_first = ledger.position_qty

        ledger.add_fill(fill)

        assert ledger.position_qty == qty_after_first

    def test_duplicate_buy_does_not_change_avg_entry(self, ledger):
        """Fill duplicado no altera avg_entry."""
        fill = make_fill("buy", "0.1", "50000", trade_id="dup-avg-001")
        ledger.add_fill(fill)
        avg_after_first = ledger.avg_entry

        ledger.add_fill(fill)

        assert ledger.avg_entry == avg_after_first


class TestSellFillDeduplication:

    def test_duplicate_sell_returns_false(self, ledger):
        """Mismo trade_id en sell → segundo add_fill retorna False."""
        buy = make_fill("buy", "0.1", "50000", trade_id="dup-sell-setup")
        sell = make_fill("sell", "0.1", "55000", trade_id="dup-sell-001")
        ledger.add_fill(buy)

        assert ledger.add_fill(sell) is True
        assert ledger.add_fill(sell) is False

    def test_duplicate_sell_does_not_decrease_position_further(self, ledger):
        """Fill de sell duplicado no reduce posición adicional."""
        buy = make_fill("buy", "0.2", "50000", trade_id="dup-pos-buy")
        sell = make_fill("sell", "0.1", "55000", trade_id="dup-pos-sell")

        ledger.add_fill(buy)
        ledger.add_fill(sell)
        qty_after_sell = ledger.position_qty  # debería ser 0.1

        ledger.add_fill(sell)  # duplicado

        assert ledger.position_qty == qty_after_sell

    def test_duplicate_sell_does_not_double_realized_pnl(self, ledger):
        """Fill de sell duplicado no dobla el realized PnL."""
        buy = make_fill("buy", "0.1", "50000", trade_id="dup-pnl-buy")
        sell = make_fill("sell", "0.1", "55000", trade_id="dup-pnl-sell")

        ledger.add_fill(buy)
        ledger.add_fill(sell)
        pnl_after_sell = ledger.realized_pnl_quote  # debería ser 500

        ledger.add_fill(sell)  # duplicado

        assert ledger.realized_pnl_quote == pnl_after_sell


class TestOutOfOrderDeduplication:

    def test_out_of_order_fill_deduplicated(self, ledger):
        """Fill out-of-order (mismo trade_id, ts menor) → ignorado."""
        fill_original = make_fill("buy", "0.1", "50000", trade_id="ooo-001", ts_ms=1_700_000_001_000)
        fill_late = make_fill("buy", "0.1", "50000", trade_id="ooo-001", ts_ms=1_700_000_000_000)

        ledger.add_fill(fill_original)
        qty_after_first = ledger.position_qty

        result = ledger.add_fill(fill_late)

        assert result is False, "Fill con mismo trade_id (out-of-order) debe ser deduplicado"
        assert ledger.position_qty == qty_after_first

    def test_different_trade_ids_all_counted(self, ledger):
        """Fills distintos (distintos trade_ids) del mismo order_id → todos se cuentan."""
        fills = [
            make_fill("buy", "0.03", "50000", trade_id=f"multi-{i}", order_id="ord-multi")
            for i in range(4)
        ]
        for f in fills:
            result = ledger.add_fill(f)
            assert result is True

        expected_qty = Decimal("0.12")  # 4 × 0.03
        assert ledger.position_qty == expected_qty


class TestReplayFromFixture:

    def test_duplicate_fill_fixture(self, tmp_path):
        """Replay desde fixture duplicate_fill.json → estado correcto."""
        fixture_path = FIXTURES_DIR / "duplicate_fill.json"
        if not fixture_path.exists():
            pytest.skip("Fixture duplicate_fill.json no encontrado")

        with open(fixture_path) as f:
            fixture = json.load(f)

        db_path = str(tmp_path / "dedup_fixture_ledger.db")
        ledger = TradeLedger(symbol=fixture["symbol"], db_path=db_path)

        results = []
        for fill_data in fixture["fills"]:
            fill = Fill(
                side=fill_data["side"],
                amount=Decimal(fill_data["amount"]),
                price=Decimal(fill_data["price"]),
                cost=Decimal(fill_data["cost"]),
                fee_cost=Decimal(fill_data["fee_cost"]),
                fee_currency=fill_data["fee_currency"],
                ts_ms=fill_data["ts_ms"],
                trade_id=fill_data["trade_id"],
                order_id=fill_data["order_id"],
            )
            results.append(ledger.add_fill(fill))

        expected = fixture["expected_final_state"]
        assert ledger.position_qty == Decimal(expected["position_qty"])
        assert len(ledger.fills) == int(expected["total_fills"])

        # Solo el primero debe haber sido aceptado
        assert results[0] is True
        assert results[1] is False

    def test_out_of_order_fixture(self, tmp_path):
        """Replay desde fixture out_of_order_events.json → deduplicación correcta."""
        fixture_path = FIXTURES_DIR / "out_of_order_events.json"
        if not fixture_path.exists():
            pytest.skip("Fixture out_of_order_events.json no encontrado")

        with open(fixture_path) as f:
            fixture = json.load(f)

        db_path = str(tmp_path / "ooo_fixture_ledger.db")
        ledger = TradeLedger(symbol=fixture["symbol"], db_path=db_path)

        for fill_data in fixture["fills"]:
            fill = Fill(
                side=fill_data["side"],
                amount=Decimal(fill_data["amount"]),
                price=Decimal(fill_data["price"]),
                cost=Decimal(fill_data["cost"]),
                fee_cost=Decimal(fill_data["fee_cost"]),
                fee_currency=fill_data["fee_currency"],
                ts_ms=fill_data["ts_ms"],
                trade_id=fill_data["trade_id"],
                order_id=fill_data["order_id"],
            )
            ledger.add_fill(fill)

        expected = fixture["expected_final_state"]
        assert ledger.position_qty == Decimal(expected["position_qty"])
        assert len(ledger.fills) == int(expected["total_fills"])
