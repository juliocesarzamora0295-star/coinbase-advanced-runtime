"""
Tests for PaperExecutor SELL qty capping (BUG-1 fix, Fase 0).

Invariant: fee must be computed on qty capped to ledger.position_qty,
not on the raw signal qty. SELL with zero position returns None.
"""

from decimal import Decimal

from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor


def _make_executor(fee_rate: str = "0.001") -> tuple[BacktestLedger, PaperExecutor]:
    ledger = BacktestLedger(initial_cash=Decimal("10000"))
    executor = PaperExecutor(
        ledger=ledger,
        slippage_bps=Decimal("0"),
        fee_rate=Decimal(fee_rate),
    )
    return ledger, executor


def test_sell_qty_exceeds_position_caps_fee() -> None:
    """Fee on SELL must be computed on min(qty, position_qty), not raw qty."""
    ledger, executor = _make_executor(fee_rate="0.001")

    # Buy 0.01 BTC @ 50000
    executor.execute(side="BUY", qty=Decimal("0.01"), price=Decimal("50000"), ts_ms=1)
    assert ledger.position_qty == Decimal("0.01")
    fees_after_buy = ledger.fees_paid
    # Buy notional = 500, fee = 0.5
    assert fees_after_buy == Decimal("0.5")

    # Try to sell 0.05 BTC (5x more than owned) @ 50000.
    # Without the cap, fee would be computed on 0.05 * 50000 = 2500 → fee 2.5.
    # With the cap, qty becomes 0.01 → notional 500 → fee 0.5.
    fill = executor.execute(side="SELL", qty=Decimal("0.05"), price=Decimal("50000"), ts_ms=2)

    assert fill is not None
    assert fill.qty == Decimal("0.01"), "SELL qty must be capped to position"
    assert fill.fee == Decimal("0.5"), "fee must be on capped qty, not raw qty"
    assert ledger.position_qty == Decimal("0")
    # Total fees = buy fee + capped sell fee = 0.5 + 0.5 = 1.0
    assert ledger.fees_paid == Decimal("1.0")


def test_sell_zero_position_returns_none() -> None:
    """SELL with no position must return None, not crash and not alter ledger."""
    ledger, executor = _make_executor()

    assert ledger.position_qty == Decimal("0")
    cash_before = ledger.cash
    fees_before = ledger.fees_paid

    fill = executor.execute(side="SELL", qty=Decimal("0.01"), price=Decimal("50000"), ts_ms=1)

    assert fill is None
    assert ledger.position_qty == Decimal("0")
    assert ledger.cash == cash_before
    assert ledger.fees_paid == fees_before
    assert ledger.trades == []


def test_buy_unaffected_by_cap_logic() -> None:
    """BUY path must still execute normally (cap only applies to SELL)."""
    ledger, executor = _make_executor(fee_rate="0.001")

    fill = executor.execute(side="BUY", qty=Decimal("0.02"), price=Decimal("40000"), ts_ms=1)

    assert fill is not None
    assert fill.qty == Decimal("0.02")
    assert fill.fee == Decimal("0.8")  # 0.02 * 40000 * 0.001
    assert ledger.position_qty == Decimal("0.02")


def test_sell_partial_within_position_unchanged() -> None:
    """SELL with qty < position_qty must pass through without cap."""
    ledger, executor = _make_executor(fee_rate="0.001")

    executor.execute(side="BUY", qty=Decimal("0.02"), price=Decimal("50000"), ts_ms=1)
    fill = executor.execute(side="SELL", qty=Decimal("0.01"), price=Decimal("50000"), ts_ms=2)

    assert fill is not None
    assert fill.qty == Decimal("0.01")
    assert fill.fee == Decimal("0.5")  # 0.01 * 50000 * 0.001
    assert ledger.position_qty == Decimal("0.01")
