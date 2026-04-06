"""
Tests para modelo contable institucional del ledger.

Invariantes testeadas:
- cash = initial_cash + proceeds - costs - fees
- equity = cash + inventory * mark_price
- daily PnL = (equity_now - equity_day_start) / equity_day_start
- drawdown = (equity_peak - equity_now) / equity_peak
- equity_peak se actualiza automáticamente
- reset_day captura equity_day_start
- fees_paid_quote acumula fees
"""

from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger


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


class TestCashAccounting:
    """cash = initial_cash - buy_costs + sell_proceeds - fees"""

    def test_initial_cash_preserved(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        assert ledger.cash == Decimal("10000")
        assert ledger.initial_cash == Decimal("10000")

    def test_buy_reduces_cash(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        # Buy 0.1 BTC @ 50000 = cost 5000
        ledger.add_fill(make_fill("buy-1", side="buy", amount="0.1", price="50000"))
        assert ledger.cash == Decimal("5000")
        assert ledger.position_qty == Decimal("0.1")

    def test_sell_increases_cash(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("buy-2", side="buy", amount="0.1", price="50000"))
        ledger.add_fill(make_fill(
            "sell-2", side="sell", amount="0.1", price="52000",
            ts_ms=1_700_000_001_000,
        ))
        # Cash = 10000 - 5000 + 5200 = 10200
        assert ledger.cash == Decimal("10200")
        assert ledger.position_qty == Decimal("0")

    def test_fees_reduce_cash(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill(
            "buy-fee", side="buy", amount="0.1", price="50000",
            fee_cost="10", fee_currency="USD",
        ))
        # Cash = 10000 - 5000 - 10 = 4990
        assert ledger.cash == Decimal("4990")

    def test_zero_initial_cash_backward_compat(self, tmp_path):
        """Ledger sin initial_cash → cash empieza en 0 (backward compat)."""
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
        )
        assert ledger.cash == Decimal("0")
        assert ledger.initial_cash == Decimal("0")


class TestEquityMTM:
    """equity = cash + inventory * mark_price"""

    def test_equity_with_no_position(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        assert ledger.get_equity(Decimal("50000")) == Decimal("10000")

    def test_equity_with_position(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("eq-buy", side="buy", amount="0.1", price="50000"))
        # Cash = 5000, inventory = 0.1 * 50000 = 5000, equity = 10000
        assert ledger.get_equity(Decimal("50000")) == Decimal("10000")

    def test_equity_increases_with_price(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("eq-buy2", side="buy", amount="0.1", price="50000"))
        # Cash = 5000, inventory = 0.1 * 55000 = 5500, equity = 10500
        assert ledger.get_equity(Decimal("55000")) == Decimal("10500")

    def test_equity_decreases_with_price(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("eq-buy3", side="buy", amount="0.1", price="50000"))
        # Cash = 5000, inventory = 0.1 * 45000 = 4500, equity = 9500
        assert ledger.get_equity(Decimal("45000")) == Decimal("9500")

    def test_equity_invariant_holds(self, tmp_path):
        """validate_equity_invariant passes with new model."""
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("inv-buy", side="buy", amount="0.1", price="50000"))
        ok, msg = ledger.validate_equity_invariant(Decimal("50000"))
        assert ok, msg


class TestDailyPnL:
    """daily_pnl = (equity_now - equity_day_start) / equity_day_start"""

    def test_none_without_day_start(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        assert ledger.get_day_pnl_pct(Decimal("50000")) is None

    def test_zero_pnl_no_change(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.reset_day(Decimal("50000"))
        assert ledger.get_day_pnl_pct(Decimal("50000")) == Decimal("0")

    def test_positive_pnl_price_up(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("pnl-buy", side="buy", amount="0.1", price="50000"))
        ledger.reset_day(Decimal("50000"))  # equity = 10000
        # Price goes to 51000 → equity = 5000 + 0.1*51000 = 10100
        pnl = ledger.get_day_pnl_pct(Decimal("51000"))
        assert pnl == Decimal("0.01")  # 1%

    def test_negative_pnl_price_down(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("pnl-buy2", side="buy", amount="0.1", price="50000"))
        ledger.reset_day(Decimal("50000"))  # equity = 10000
        # Price goes to 45000 → equity = 5000 + 0.1*45000 = 9500
        pnl = ledger.get_day_pnl_pct(Decimal("45000"))
        assert pnl == Decimal("-0.05")  # -5%


class TestDrawdown:
    """drawdown = (equity_peak - equity_now) / equity_peak"""

    def test_zero_drawdown_at_peak(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.equity_peak = Decimal("10000")
        assert ledger.get_drawdown_pct(Decimal("50000")) == Decimal("0")

    def test_drawdown_when_below_peak(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("dd-buy", side="buy", amount="0.1", price="50000"))
        # Set peak at 10000 (equity at 50k price)
        ledger.equity_peak = Decimal("10000")
        # Price drops to 45000 → equity = 5000 + 4500 = 9500
        dd = ledger.get_drawdown_pct(Decimal("45000"))
        assert dd == Decimal("0.05")  # 5% drawdown

    def test_peak_auto_updates(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.equity_peak = Decimal("10000")
        # Price rises → equity = 10000, new peak
        ledger.mark_equity(Decimal("50000"))
        assert ledger.equity_peak == Decimal("10000")

        ledger.add_fill(make_fill("pk-buy", side="buy", amount="0.1", price="50000"))
        # Price rises to 55000 → equity = 5000 + 5500 = 10500
        ledger.mark_equity(Decimal("55000"))
        assert ledger.equity_peak == Decimal("10500")

    def test_drawdown_after_peak_and_drop(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("dd2-buy", side="buy", amount="0.1", price="50000"))
        # Mark at 55000 → equity = 10500 → peak = 10500
        ledger.mark_equity(Decimal("55000"))
        assert ledger.equity_peak == Decimal("10500")

        # Price drops to 45000 → equity = 5000 + 4500 = 9500
        dd = ledger.get_drawdown_pct(Decimal("45000"))
        # dd = (10500 - 9500) / 10500 = 1000/10500
        expected = Decimal("1000") / Decimal("10500")
        assert abs(dd - expected) < Decimal("1e-10")


class TestResetDay:
    """reset_day captura equity_day_start."""

    def test_reset_day_captures_equity(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.reset_day(Decimal("50000"))
        assert ledger.equity_day_start == Decimal("10000")

    def test_reset_day_with_position(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill("rd-buy", side="buy", amount="0.1", price="50000"))
        ledger.reset_day(Decimal("52000"))
        # equity = 5000 + 0.1*52000 = 10200
        assert ledger.equity_day_start == Decimal("10200")


class TestFeesTracking:
    """fees_paid_quote acumula fees."""

    def test_quote_fees_accumulated(self, tmp_path):
        ledger = TradeLedger(
            symbol="BTC-USD",
            db_path=str(tmp_path / "ledger.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(make_fill(
            "fee-1", side="buy", amount="0.1", price="50000",
            fee_cost="10", fee_currency="USD",
        ))
        ledger.add_fill(make_fill(
            "fee-2", side="buy", amount="0.1", price="50000",
            fee_cost="15", fee_currency="USD",
            ts_ms=1_700_000_001_000,
        ))
        assert ledger.fees_paid_quote == Decimal("25")
