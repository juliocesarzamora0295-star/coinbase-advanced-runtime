"""
Tests de invariantes del TradeLedger.

Invariantes testeadas:
- add_fill(buy) → posición incrementa correctamente
- add_fill(sell) → posición decrementa + realized PnL calculado
- fill duplicado por trade_id → returns False, no doble-cuenta
- partial fills: N buys → posición = suma
- cierre completo: buy + sell = posición ≈ 0
- avg_entry correcto tras múltiples buys (VWAP)
- realized PnL correcto en cierre lucrativo y en pérdida
- unrealized PnL correcto con posición abierta
- fee en quote currency reduce proceeds → menor PnL realizado
- fee en base currency ajusta qty neta en buy
- restart: nuevo ledger con mismo DB → estado idéntico
- fills no se doble-cuentan en restart
- validate_equity_invariant pasa tras fills normales
"""
import uuid
from decimal import Decimal

import pytest

from src.accounting.ledger import Fill, TradeLedger


def make_fill(
    side: str,
    amount: Decimal,
    price: Decimal,
    fee_cost: Decimal = Decimal("0"),
    fee_currency: str = "USD",
    trade_id: str | None = None,
    order_id: str | None = None,
    ts_ms: int = 1_700_000_000_000,
) -> Fill:
    """Factory para Fill de prueba."""
    return Fill(
        side=side,
        amount=amount,
        price=price,
        cost=amount * price,
        fee_cost=fee_cost,
        fee_currency=fee_currency,
        ts_ms=ts_ms,
        trade_id=trade_id or str(uuid.uuid4()),
        order_id=order_id or str(uuid.uuid4()),
    )


@pytest.fixture
def ledger(tmp_path):
    """TradeLedger con SQLite temporal."""
    db_path = str(tmp_path / "test_ledger.db")
    return TradeLedger(symbol="BTC-USD", db_path=db_path)


class TestLedgerPositionInvariants:
    """Invariantes de posición."""

    def test_buy_increases_position(self, ledger):
        """add_fill(buy) incrementa position_qty."""
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        result = ledger.add_fill(fill)

        assert result is True
        assert ledger.position_qty == Decimal("0.1")

    def test_sell_decreases_position(self, ledger):
        """add_fill(sell) decrementa position_qty."""
        buy = make_fill("buy", Decimal("0.2"), Decimal("50000"))
        sell = make_fill("sell", Decimal("0.1"), Decimal("51000"))

        ledger.add_fill(buy)
        ledger.add_fill(sell)

        assert ledger.position_qty == Decimal("0.1")

    def test_full_close_zeroes_position(self, ledger):
        """buy + sell igual cantidad → posición ≈ 0."""
        qty = Decimal("0.1")
        buy = make_fill("buy", qty, Decimal("50000"))
        sell = make_fill("sell", qty, Decimal("51000"))

        ledger.add_fill(buy)
        ledger.add_fill(sell)

        assert ledger.position_qty < Decimal("1e-10")

    def test_multiple_partial_buys_accumulate(self, ledger):
        """Múltiples buys parciales acumulan posición correctamente."""
        fills = [
            make_fill("buy", Decimal("0.05"), Decimal("50000")),
            make_fill("buy", Decimal("0.03"), Decimal("51000")),
            make_fill("buy", Decimal("0.02"), Decimal("49000")),
        ]
        for f in fills:
            ledger.add_fill(f)

        assert ledger.position_qty == Decimal("0.10")

    def test_avg_entry_single_buy(self, ledger):
        """avg_entry correcto tras un único buy."""
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        ledger.add_fill(fill)

        assert ledger.avg_entry == Decimal("50000")

    def test_avg_entry_two_equal_buys(self, ledger):
        """avg_entry es VWAP de dos buys iguales."""
        fill1 = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        fill2 = make_fill("buy", Decimal("0.1"), Decimal("52000"))

        ledger.add_fill(fill1)
        ledger.add_fill(fill2)

        # VWAP = (0.1×50000 + 0.1×52000) / 0.2 = 51000
        assert ledger.avg_entry == Decimal("51000")

    def test_add_fill_returns_true_for_new_fill(self, ledger):
        """add_fill retorna True para fill nuevo."""
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        assert ledger.add_fill(fill) is True


class TestLedgerPnLInvariants:
    """Invariantes de PnL."""

    def test_realized_pnl_on_profitable_close(self, ledger):
        """Cierre lucrativo → realized_pnl > 0."""
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        sell = make_fill("sell", Decimal("0.1"), Decimal("55000"))

        ledger.add_fill(buy)
        ledger.add_fill(sell)

        # (55000 - 50000) × 0.1 = 500
        assert ledger.realized_pnl_quote == Decimal("500")

    def test_realized_pnl_on_losing_close(self, ledger):
        """Cierre con pérdida → realized_pnl < 0."""
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        sell = make_fill("sell", Decimal("0.1"), Decimal("45000"))

        ledger.add_fill(buy)
        ledger.add_fill(sell)

        # (45000 - 50000) × 0.1 = -500
        assert ledger.realized_pnl_quote == Decimal("-500")

    def test_unrealized_pnl_with_open_position(self, ledger):
        """get_unrealized_pnl con posición abierta y precio actual."""
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        ledger.add_fill(buy)

        current_price = Decimal("55000")
        unrealized = ledger.get_unrealized_pnl(current_price)

        # (55000 - 50000) × 0.1 = 500
        assert unrealized == Decimal("500")

    def test_unrealized_pnl_zero_without_position(self, ledger):
        """get_unrealized_pnl sin posición → 0."""
        assert ledger.get_unrealized_pnl(Decimal("50000")) == Decimal("0")

    def test_equity_invariant_holds_after_buy(self, ledger):
        """validate_equity_invariant pasa tras buy normal."""
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        ledger.add_fill(buy)

        ok, msg = ledger.validate_equity_invariant(Decimal("50000"))
        assert ok, f"Equity invariant failed: {msg}"

    def test_partial_fill_pnl_on_partial_close(self, ledger):
        """Cierre parcial calcula PnL correcto sobre la fracción vendida."""
        buy = make_fill("buy", Decimal("0.2"), Decimal("50000"))
        sell = make_fill("sell", Decimal("0.1"), Decimal("55000"))

        ledger.add_fill(buy)
        ledger.add_fill(sell)

        # PnL de la porción vendida: (55000 - 50000) × 0.1 = 500
        assert ledger.realized_pnl_quote == Decimal("500")
        # Posición restante: 0.2 - 0.1 = 0.1
        assert ledger.position_qty == Decimal("0.1")


class TestLedgerDeduplication:
    """Deduplicación de fills por trade_id."""

    def test_duplicate_fill_returns_false(self, ledger):
        """Mismo trade_id → add_fill retorna False."""
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"), trade_id="trade-001")

        result1 = ledger.add_fill(fill)
        result2 = ledger.add_fill(fill)

        assert result1 is True
        assert result2 is False

    def test_duplicate_fill_does_not_change_position(self, ledger):
        """Fill duplicado no modifica position_qty."""
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"), trade_id="t-dedup")
        ledger.add_fill(fill)
        position_before = ledger.position_qty

        ledger.add_fill(fill)

        assert ledger.position_qty == position_before

    def test_duplicate_sell_does_not_change_pnl(self, ledger):
        """Fill de venta duplicado no cambia realized PnL."""
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"), trade_id="buy-dedup")
        sell = make_fill("sell", Decimal("0.1"), Decimal("55000"), trade_id="sell-dedup")

        ledger.add_fill(buy)
        ledger.add_fill(sell)
        pnl_before = ledger.realized_pnl_quote

        ledger.add_fill(sell)  # duplicado

        assert ledger.realized_pnl_quote == pnl_before

    def test_partial_fills_same_order_different_trade_ids(self, ledger):
        """Partial fills del mismo order con distintos trade_ids → todos cuentan."""
        fill1 = make_fill("buy", Decimal("0.05"), Decimal("50000"), trade_id="t-001", order_id="ord-1")
        fill2 = make_fill("buy", Decimal("0.05"), Decimal("50000"), trade_id="t-002", order_id="ord-1")

        ledger.add_fill(fill1)
        ledger.add_fill(fill2)

        assert ledger.position_qty == Decimal("0.10")


class TestLedgerFeesInvariants:
    """Manejo de fees — quote y base currency."""

    def test_fee_in_quote_reduces_realized_pnl(self, ledger):
        """Fee en QUOTE reduce proceeds → menor PnL realizado."""
        fee = Decimal("2.5")
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"))
        sell = make_fill(
            "sell", Decimal("0.1"), Decimal("55000"),
            fee_cost=fee, fee_currency="USD",
        )

        ledger.add_fill(buy)
        ledger.add_fill(sell)

        # PnL bruto = 500, fee USD = 2.5 → PnL neto = 497.5
        assert ledger.realized_pnl_quote == Decimal("500") - fee

    def test_fee_in_base_adjusts_net_qty_on_buy(self, ledger):
        """Fee en BASE (BTC) reduce qty neta en buy — P1 FIX."""
        fee = Decimal("0.001")  # 0.001 BTC
        buy = make_fill(
            "buy", Decimal("0.1"), Decimal("50000"),
            fee_cost=fee, fee_currency="BTC",
        )
        ledger.add_fill(buy)

        # qty neta = 0.1 - 0.001 = 0.099
        assert ledger.position_qty == Decimal("0.099")


class TestLedgerRestart:
    """Restart: nuevo ledger con mismo DB → estado idéntico."""

    def test_position_persists_after_restart(self, tmp_path):
        """position_qty persiste cuando se crea nuevo TradeLedger con el mismo DB."""
        db_path = str(tmp_path / "ledger_restart.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"), trade_id="restart-fill-001")
        ledger1.add_fill(fill)

        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)

        assert ledger2.position_qty == ledger1.position_qty
        assert ledger2.avg_entry == ledger1.avg_entry

    def test_realized_pnl_persists_after_restart(self, tmp_path):
        """realized_pnl persiste tras restart."""
        db_path = str(tmp_path / "ledger_pnl_restart.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        buy = make_fill("buy", Decimal("0.1"), Decimal("50000"), trade_id="r-buy")
        sell = make_fill("sell", Decimal("0.1"), Decimal("55000"), trade_id="r-sell")
        ledger1.add_fill(buy)
        ledger1.add_fill(sell)
        pnl_before = ledger1.realized_pnl_quote

        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)

        assert ledger2.realized_pnl_quote == pnl_before

    def test_fills_not_double_counted_on_restart(self, tmp_path):
        """Fills no se doble-cuentan al recargar desde DB."""
        db_path = str(tmp_path / "ledger_no_double.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        fill = make_fill("buy", Decimal("0.1"), Decimal("50000"), trade_id="unique-001")
        ledger1.add_fill(fill)
        position_before = ledger1.position_qty

        # Restart
        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)

        # Intentar reinsertar: debe ser rechazado como duplicado
        result = ledger2.add_fill(fill)

        assert result is False, "Fill no debe re-aplicarse tras restart"
        assert ledger2.position_qty == position_before

    def test_snapshot_consistent_after_restart(self, tmp_path):
        """snapshot() tras restart produce los mismos valores."""
        db_path = str(tmp_path / "ledger_snapshot.db")

        ledger1 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        buy = make_fill("buy", Decimal("0.05"), Decimal("48000"), trade_id="snap-buy")
        ledger1.add_fill(buy)
        snap1 = ledger1.snapshot()

        ledger2 = TradeLedger(symbol="BTC-USD", db_path=db_path)
        snap2 = ledger2.snapshot()

        assert snap2.position_qty == snap1.position_qty
        assert snap2.avg_entry == snap1.avg_entry
        assert snap2.realized_pnl_quote == snap1.realized_pnl_quote
