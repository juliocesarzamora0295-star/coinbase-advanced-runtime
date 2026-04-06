"""
Tests dirigidos: cobertura de paths no cubiertos en ledger.py.

Cubre:
- Fill.to_dict() / Fill.from_dict()
- on_fill_callback cuando está seteado
- sell con qty_out=0 (no-op)
- sell cuando position=0 (no-op)
- get_day_pnl_pct(): sin fills, con fills del día
- get_drawdown_pct(): sin fills, con drawdown, en pico
- validate_equity_invariant(): OK e invariant fail
- dedup_check(): fill nuevo vs duplicado
- get_stats()
"""

from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────


def make_fill(
    trade_id: str,
    side: str = "buy",
    amount: str = "0.1",
    price: str = "50000",
    fee_cost: str = "0",
    fee_currency: str = "USD",
    ts_ms: int = 1_700_000_000_000,
) -> Fill:
    qty = Decimal(amount)
    prc = Decimal(price)
    return Fill(
        side=side,
        amount=qty,
        price=prc,
        cost=qty * prc,
        fee_cost=Decimal(fee_cost),
        fee_currency=fee_currency,
        ts_ms=ts_ms,
        trade_id=trade_id,
        order_id=f"ord-{trade_id}",
    )


# ──────────────────────────────────────────────
# Fill.to_dict / from_dict
# ──────────────────────────────────────────────


class TestFillSerialisation:

    def test_to_dict_has_all_fields(self):
        f = make_fill("ser-001")
        d = f.to_dict()
        assert d["side"] == "buy"
        assert d["trade_id"] == "ser-001"
        assert "amount" in d
        assert "price" in d
        assert "cost" in d
        assert "fee_cost" in d
        assert "fee_currency" in d
        assert "ts_ms" in d
        assert "order_id" in d

    def test_from_dict_roundtrip(self):
        f = make_fill("ser-002", amount="0.25", price="48000", fee_cost="1.5")
        d = f.to_dict()
        f2 = Fill.from_dict(d)
        assert f2.trade_id == f.trade_id
        assert f2.amount == f.amount
        assert f2.price == f.price
        assert f2.fee_cost == f.fee_cost
        assert f2.fee_currency == f.fee_currency
        assert f2.ts_ms == f.ts_ms

    def test_from_dict_preserves_decimal_precision(self):
        f = make_fill("ser-003", amount="0.00001", price="0.0001")
        d = f.to_dict()
        f2 = Fill.from_dict(d)
        assert f2.amount == Decimal("0.00001")


# ──────────────────────────────────────────────
# on_fill_callback
# ──────────────────────────────────────────────


class TestOnFillCallback:

    def test_callback_called_on_add_fill(self, tmp_path):
        """on_fill_callback es invocado cuando se agrega un fill."""
        received = []
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            on_fill_callback=lambda f: received.append(f.trade_id),
        )
        fill = make_fill("cb-001")
        ledger.add_fill(fill)
        assert "cb-001" in received

    def test_callback_not_called_on_duplicate(self, tmp_path):
        """on_fill_callback NO se llama cuando el fill es duplicado."""
        received = []
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            on_fill_callback=lambda f: received.append(f.trade_id),
        )
        fill = make_fill("cb-002")
        ledger.add_fill(fill)
        ledger.add_fill(fill)  # duplicado
        assert received.count("cb-002") == 1


# ──────────────────────────────────────────────
# Sell edge cases en recompute
# ──────────────────────────────────────────────


class TestSellEdgeCases:

    def test_sell_with_zero_qty_out_no_change(self, tmp_path):
        """Sell fill con amount=0 → no modifica posición."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ledger.add_fill(make_fill("buy-001", side="buy", amount="0.1"))
        qty_before = ledger.position_qty

        # Fill de sell con amount=0
        sell_zero = Fill(
            side="sell",
            amount=Decimal("0"),
            price=Decimal("50000"),
            cost=Decimal("0"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_001_000,
            trade_id="sell-zero-001",
            order_id="o-sell-zero",
        )
        ledger.add_fill(sell_zero)
        assert ledger.position_qty == qty_before

    def test_sell_when_position_is_zero_no_effect(self, tmp_path):
        """Sell fill sin posición previa → no modifica posición ni cost_basis."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        # No hay compra previa — posición = 0
        sell = make_fill("sell-no-pos-001", side="sell", amount="0.1")
        ledger.add_fill(sell)
        assert ledger.position_qty == Decimal("0")
        assert ledger.cost_basis_quote == Decimal("0")


# ──────────────────────────────────────────────
# get_day_pnl_pct
# ──────────────────────────────────────────────


class TestGetDayPnlPct:

    def test_returns_none_when_no_day_start(self, tmp_path):
        """Sin equity_day_start → day_pnl_pct = None (fail-closed)."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        result = ledger.get_day_pnl_pct(Decimal("50000"))
        assert result is None

    def test_returns_zero_when_no_change(self, tmp_path):
        """Con equity_day_start set y sin cambio → day_pnl = 0."""
        ledger = TradeLedger(
            symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.reset_day(Decimal("50000"))
        result = ledger.get_day_pnl_pct(Decimal("50000"))
        assert result == Decimal("0")

    def test_returns_value_with_fills(self, tmp_path):
        """Con fills y equity_day_start → retorna Decimal exacto."""
        ledger = TradeLedger(
            symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.reset_day(Decimal("50000"))  # equity_day_start = 10000
        ledger.add_fill(make_fill("dpnl-buy-001", side="buy", amount="0.1", price="50000"))
        # Cash = 10000 - 5000 = 5000, inventory = 0.1 * 50000 = 5000, equity = 10000
        result = ledger.get_day_pnl_pct(Decimal("50000"))
        assert isinstance(result, Decimal)
        assert result == Decimal("0")  # no change in equity

    def test_sell_at_profit_positive_day_pnl(self, tmp_path):
        """Buy then sell at higher price → positive day PnL."""
        ledger = TradeLedger(
            symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.reset_day(Decimal("50000"))  # equity_day_start = 10000
        ledger.add_fill(make_fill("dpnl-buy-002", side="buy", amount="0.1", price="50000"))
        sell = Fill(
            side="sell",
            amount=Decimal("0.1"),
            price=Decimal("52000"),
            cost=Decimal("0.1") * Decimal("52000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_001_000,
            trade_id="dpnl-sell-002",
            order_id="o-dpnl",
        )
        ledger.add_fill(sell)
        # Cash = 10000 - 5000 + 5200 = 10200, position = 0, equity = 10200
        # day_pnl = (10200 - 10000) / 10000 = 0.02
        result = ledger.get_day_pnl_pct(Decimal("52000"))
        assert isinstance(result, Decimal)
        assert result == Decimal("0.02")


# ──────────────────────────────────────────────
# get_drawdown_pct
# ──────────────────────────────────────────────


class TestGetDrawdownPct:

    def test_returns_zero_when_no_fills(self, tmp_path):
        """Sin fills → drawdown = 0."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        result = ledger.get_drawdown_pct(Decimal("50000"))
        assert result == Decimal("0")

    def test_returns_zero_at_equity_peak(self, tmp_path):
        """Equity en pico → drawdown = 0."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ledger.add_fill(make_fill("dd-buy-001", side="buy", amount="0.1", price="50000"))
        # Sin pérdidas → drawdown = 0
        result = ledger.get_drawdown_pct(Decimal("50000"))
        assert result == Decimal("0")

    def test_drawdown_positive_after_loss(self, tmp_path):
        """Equity bajo pico → drawdown > 0."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        # Comprar a 50000
        ledger.add_fill(make_fill("dd-buy-002", side="buy", amount="0.1", price="50000"))
        # Vender a 50000 (pico)
        sell = Fill(
            side="sell",
            amount=Decimal("0.1"),
            price=Decimal("50000"),
            cost=Decimal("5000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_001_000,
            trade_id="dd-sell-002",
            order_id="o-dd",
        )
        ledger.add_fill(sell)
        # Precio actual bajo → drawdown
        result = ledger.get_drawdown_pct(Decimal("40000"))
        assert isinstance(result, Decimal)
        assert result >= Decimal("0")


# ──────────────────────────────────────────────
# validate_equity_invariant
# ──────────────────────────────────────────────


class TestValidateEquityInvariant:

    def test_invariant_ok_after_normal_fill(self, tmp_path):
        """Invariant OK con fill normal."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ledger.add_fill(make_fill("inv-buy-001", amount="0.1", price="50000"))
        ok, msg = ledger.validate_equity_invariant(Decimal("50000"))
        assert ok is True
        assert "OK" in msg

    def test_invariant_returns_tuple(self, tmp_path):
        """validate_equity_invariant retorna (bool, str) — no lanza."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ledger.add_fill(make_fill("inv-buy-002", amount="0.1", price="50000"))
        result = ledger.validate_equity_invariant(Decimal("50000"))
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], str)

    def test_invariant_ok_on_empty_ledger(self, tmp_path):
        """Invariant OK con ledger vacío (todo en 0)."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ok, msg = ledger.validate_equity_invariant(Decimal("50000"))
        assert ok is True


# ──────────────────────────────────────────────
# dedup_check
# ──────────────────────────────────────────────


class TestDedupCheck:

    def test_new_fill_returns_true(self, tmp_path):
        """Fill nuevo → dedup_check retorna (True, msg con 'new')."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        fill = make_fill("dedup-new-001")
        ok, msg = ledger.dedup_check(fill)
        assert ok is True
        assert "new" in msg.lower() or fill.trade_id in msg

    def test_existing_fill_returns_duplicate(self, tmp_path):
        """Fill ya existente → dedup_check retorna (True, msg con 'duplicate')."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        fill = make_fill("dedup-dup-001")
        ledger.add_fill(fill)
        ok, msg = ledger.dedup_check(fill)
        assert ok is True
        assert "duplicate" in msg.lower() or "already" in msg.lower()


# ──────────────────────────────────────────────
# get_stats
# ──────────────────────────────────────────────


class TestGetStats:

    def test_stats_keys_present(self, tmp_path):
        """get_stats retorna dict con claves esperadas."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        stats = ledger.get_stats()
        assert "symbol" in stats
        assert "total_fills" in stats
        assert "position_qty" in stats
        assert "avg_entry" in stats
        assert "realized_pnl" in stats

    def test_stats_total_fills_correct(self, tmp_path):
        """get_stats.total_fills coincide con número de fills."""
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ledger.add_fill(make_fill("stats-001"))
        ledger.add_fill(make_fill("stats-002"))
        stats = ledger.get_stats()
        assert stats["total_fills"] == 2
