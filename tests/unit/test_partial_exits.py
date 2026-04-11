"""
Tests for partial exits (TP1 half-close) in StrategyAdapter (Fase 4B').

Partial exits are OFF by default so existing backtests and baselines are
unchanged. This suite exercises the opt-in path explicitly.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

import pandas as pd

from src.backtest.data_feed import Bar
from src.backtest.ledger import BacktestLedger
from src.backtest.paper_executor import PaperExecutor
from src.backtest.strategy_adapter import StrategyAdapter
from src.strategy.base import Strategy
from src.strategy.signal import Signal, make_signal


class _ScriptedStrategy(Strategy):
    def __init__(self, symbol: str, script: List[Optional[Signal]]) -> None:
        super().__init__(symbol=symbol, config={})
        self._script = list(script)
        self._i = 0

    def update_market_data(self, market_data: pd.DataFrame) -> None:
        self._df = market_data

    def generate_signals(self, *, mid: Decimal, bar_timestamp=None) -> List[Signal]:
        if self._i >= len(self._script):
            return []
        s = self._script[self._i]
        self._i += 1
        return [s] if s is not None else []

    def update_positions(self, fill) -> None:
        return None


def _bar(ts: int, close: float) -> Bar:
    return Bar(
        timestamp_ms=ts,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=Decimal("1"),
    )


def _sig(direction: str, strength: Decimal = Decimal("1")) -> Signal:
    return make_signal(
        symbol="BTC-USD",
        direction=direction,  # type: ignore[arg-type]
        strength=strength,
        strategy_id="scripted",
        bar_timestamp=datetime.now(tz=timezone.utc),
    )


def test_partial_exits_off_by_default_no_tp1() -> None:
    buy = _sig("BUY")
    sell = _sig("SELL")
    strategy = _ScriptedStrategy("BTC-USD", [buy, None, None, None, None, sell])
    adapter = StrategyAdapter(strategy=strategy, qty=Decimal("0.02"))

    # BUY at 100, price rises to 130, then SELL.
    bars = [
        _bar(1_000, 100.0),
        _bar(2_000, 105.0),
        _bar(3_000, 115.0),
        _bar(4_000, 125.0),
        _bar(5_000, 130.0),
        _bar(6_000, 128.0),
    ]
    history: List[Bar] = []
    outs: List[Optional] = []
    for b in bars:
        outs.append(adapter(b, list(history)))
        history.append(b)

    # Only BUY at bar 0, SELL at bar 5 — no partial exits.
    side_counts = {"BUY": 0, "SELL": 0}
    for o in outs:
        if o is not None:
            side_counts[o.side] += 1
    assert side_counts == {"BUY": 1, "SELL": 1}


def test_partial_exits_sells_half_at_tp1() -> None:
    buy = _sig("BUY")
    sell = _sig("SELL")
    # The partial-exit path short-circuits generate_signals, so the TP1 bar
    # does not consume a script entry. Total script entries = bars − TP1_bars.
    script = [None, None, None, buy, None, sell]
    strategy = _ScriptedStrategy("BTC-USD", script)
    adapter = StrategyAdapter(
        strategy=strategy,
        qty=Decimal("0.02"),
        partial_exits=True,
        atr_period=3,
    )

    bars = [
        _bar(1_000, 99.0),
        _bar(2_000, 99.5),
        _bar(3_000, 100.0),
        _bar(4_000, 100.0),   # BUY (script[3])
        _bar(5_000, 103.0),   # TP1 fires — no script consumed
        _bar(6_000, 108.0),   # script[4] = None
        _bar(7_000, 110.0),   # script[5] = SELL
    ]
    history: List[Bar] = []
    outs = []
    for b in bars:
        outs.append(adapter(b, list(history)))
        history.append(b)

    sides = [o.side for o in outs if o is not None]
    qtys = [o.qty for o in outs if o is not None]
    # Expect: BUY, then one partial SELL (half), then a final SELL.
    assert sides.count("BUY") == 1
    assert sides.count("SELL") >= 2
    # First SELL is the partial (0.01), last SELL is the remainder (0.01).
    sell_qtys = [q for s, q in zip(sides, qtys) if s == "SELL"]
    assert sell_qtys[0] == Decimal("0.02") / Decimal("2")
    # Remaining closes should sum with the partial to equal the original qty.
    assert sum(sell_qtys) == Decimal("0.02")


def test_tp1_does_not_fire_when_price_never_reaches_level() -> None:
    buy = _sig("BUY")
    sell = _sig("SELL")
    strategy = _ScriptedStrategy("BTC-USD", [buy, None, None, sell])
    adapter = StrategyAdapter(
        strategy=strategy,
        qty=Decimal("0.02"),
        partial_exits=True,
        atr_period=3,
    )

    bars = [
        _bar(1_000, 100.0),
        _bar(2_000, 100.1),
        _bar(3_000, 99.9),
        _bar(4_000, 100.0),
    ]
    history: List[Bar] = []
    outs = []
    for b in bars:
        outs.append(adapter(b, list(history)))
        history.append(b)

    sides = [o.side for o in outs if o is not None]
    # No TP1 — only BUY + final SELL.
    assert sides == ["BUY", "SELL"]
    # Final SELL closes the full size.
    sell_qty = [o.qty for o in outs if o is not None and o.side == "SELL"][0]
    assert sell_qty == Decimal("0.02")


def test_partial_then_final_ledger_sees_two_trades() -> None:
    """Integration: PaperExecutor + BacktestLedger see both exits."""
    buy = _sig("BUY")
    sell = _sig("SELL")
    # Warmup 3, BUY, TP1 (no consume), filler None, SELL.
    script = [None, None, None, buy, None, sell]
    strategy = _ScriptedStrategy("BTC-USD", script)
    adapter = StrategyAdapter(
        strategy=strategy,
        qty=Decimal("0.02"),
        partial_exits=True,
        atr_period=3,
    )

    ledger = BacktestLedger(initial_cash=Decimal("10000"))
    executor = PaperExecutor(ledger=ledger)

    bars = [
        _bar(1_000, 99.0),
        _bar(2_000, 99.5),
        _bar(3_000, 100.0),
        _bar(4_000, 100.0),  # BUY
        _bar(5_000, 104.0),  # TP1
        _bar(6_000, 110.0),  # filler (script None)
        _bar(7_000, 112.0),  # SELL
    ]
    history: List[Bar] = []
    for b in bars:
        sig = adapter(b, list(history))
        if sig is not None:
            executor.execute(
                side=sig.side,
                qty=sig.qty,
                price=b.close,
                ts_ms=b.timestamp_ms,
            )
        history.append(b)

    # Ledger should have been flat->long->partial close->flat.
    assert ledger.position_qty == Decimal("0")
    # At least two trades closed (TP1 + final).
    assert len(ledger.trades) >= 2
