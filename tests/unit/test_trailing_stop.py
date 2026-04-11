"""
Tests for TrailingStop — shared ATR stop-loss component (Fase 4A).

Covers the 10 required cases from the plan:
1.  activate long
2.  activate short
3.  trailing up only (LONG monotone)
4.  hit long
5.  hit short
6.  NaN ATR refuses activation (I6 fail-closed)
7.  zero ATR refuses activation (I6 fail-closed)
8.  NaN ATR at update preserves stop
9.  reset clears state
10. negative atr_mult raises
"""

import math

import pytest

from src.strategy.trailing_stop import TrailingStop


def test_activate_long_sets_stop_below_entry() -> None:
    ts = TrailingStop(atr_mult=2.0)
    ok = ts.activate(entry_price=100.0, current_atr=5.0, direction="LONG")
    assert ok is True
    assert ts.is_active is True
    assert ts.direction == "LONG"
    assert ts.stop_price == pytest.approx(100.0 - 2.0 * 5.0)  # 90.0


def test_activate_short_sets_stop_above_entry() -> None:
    ts = TrailingStop(atr_mult=2.0)
    ok = ts.activate(entry_price=100.0, current_atr=5.0, direction="SHORT")
    assert ok is True
    assert ts.direction == "SHORT"
    assert ts.stop_price == pytest.approx(100.0 + 2.0 * 5.0)  # 110.0


def test_trailing_moves_up_only_for_long() -> None:
    ts = TrailingStop(atr_mult=2.0)
    ts.activate(entry_price=100.0, current_atr=5.0, direction="LONG")
    assert ts.stop_price == pytest.approx(90.0)

    # Price rises — stop trails up.
    hit = ts.update(current_price=110.0, current_atr=5.0)
    assert hit is False
    assert ts.stop_price == pytest.approx(100.0)  # 110 - 10

    # Price pulls back but not to stop — stop must NOT move down.
    hit = ts.update(current_price=105.0, current_atr=5.0)
    assert hit is False
    assert ts.stop_price == pytest.approx(100.0), "LONG stop must not retreat"

    # Price rises again — stop trails up again.
    hit = ts.update(current_price=120.0, current_atr=5.0)
    assert hit is False
    assert ts.stop_price == pytest.approx(110.0)


def test_hit_long_when_price_crosses_stop() -> None:
    ts = TrailingStop(atr_mult=2.0)
    ts.activate(entry_price=100.0, current_atr=5.0, direction="LONG")
    # Stop at 90. Price drops to 89 — hit.
    hit = ts.update(current_price=89.0, current_atr=5.0)
    assert hit is True


def test_hit_short_when_price_crosses_stop() -> None:
    ts = TrailingStop(atr_mult=2.0)
    ts.activate(entry_price=100.0, current_atr=5.0, direction="SHORT")
    # Stop at 110. Price rises to 111 — hit.
    hit = ts.update(current_price=111.0, current_atr=5.0)
    assert hit is True


def test_nan_atr_refuses_activation() -> None:
    """I6: fail-closed on NaN ATR at entry."""
    ts = TrailingStop(atr_mult=2.0)
    ok = ts.activate(entry_price=100.0, current_atr=float("nan"), direction="LONG")
    assert ok is False
    assert ts.is_active is False
    assert ts.stop_price is None


def test_zero_atr_refuses_activation() -> None:
    """I6: zero ATR means no volatility signal — refuse to set a stop."""
    ts = TrailingStop(atr_mult=2.0)
    ok = ts.activate(entry_price=100.0, current_atr=0.0, direction="LONG")
    assert ok is False
    assert ts.is_active is False

    ok_neg = ts.activate(entry_price=100.0, current_atr=-1.0, direction="LONG")
    assert ok_neg is False

    ok_none = ts.activate(entry_price=100.0, current_atr=None, direction="LONG")
    assert ok_none is False


def test_nan_atr_at_update_preserves_stop_and_still_checks_hit() -> None:
    """
    Position is already open. Invalid ATR mid-run must not move the stop,
    but must still check whether price hit the existing level.
    """
    ts = TrailingStop(atr_mult=2.0)
    ts.activate(entry_price=100.0, current_atr=5.0, direction="LONG")
    # Trail up to 100 first.
    ts.update(current_price=110.0, current_atr=5.0)
    assert ts.stop_price == pytest.approx(100.0)

    # NaN ATR: stop frozen, no move. Price above stop → no hit.
    hit = ts.update(current_price=108.0, current_atr=float("nan"))
    assert hit is False
    assert ts.stop_price == pytest.approx(100.0), "Stop must not move on NaN ATR"

    # Still frozen stop, but price dropped below — must fire.
    hit = ts.update(current_price=99.0, current_atr=float("nan"))
    assert hit is True
    assert ts.stop_price == pytest.approx(100.0)


def test_reset_clears_state() -> None:
    ts = TrailingStop(atr_mult=2.0)
    ts.activate(entry_price=100.0, current_atr=5.0, direction="LONG")
    assert ts.is_active is True

    ts.reset()
    assert ts.is_active is False
    assert ts.stop_price is None
    assert ts.direction is None

    # After reset, a fresh activate works.
    ok = ts.activate(entry_price=200.0, current_atr=10.0, direction="SHORT")
    assert ok is True
    assert ts.direction == "SHORT"
    assert ts.stop_price == pytest.approx(220.0)


def test_negative_atr_mult_raises() -> None:
    with pytest.raises(ValueError):
        TrailingStop(atr_mult=-1.0)
    with pytest.raises(ValueError):
        TrailingStop(atr_mult=0.0)


def test_update_before_activate_is_noop() -> None:
    """Safety: calling update() on an inactive stop must not crash or fire."""
    ts = TrailingStop(atr_mult=2.0)
    assert ts.update(current_price=50.0, current_atr=1.0) is False
    assert ts.is_active is False


def test_can_activate_independent_check() -> None:
    ts = TrailingStop(atr_mult=2.0)
    assert ts.can_activate(5.0) is True
    assert ts.can_activate(0.0) is False
    assert ts.can_activate(-1.0) is False
    assert ts.can_activate(None) is False
    assert ts.can_activate(float("nan")) is False
    assert ts.can_activate(float("inf")) is False


def test_short_trailing_moves_down_only() -> None:
    """Symmetric monotonicity check for SHORT."""
    ts = TrailingStop(atr_mult=2.0)
    ts.activate(entry_price=100.0, current_atr=5.0, direction="SHORT")
    assert ts.stop_price == pytest.approx(110.0)

    # Price drops — stop trails down.
    hit = ts.update(current_price=90.0, current_atr=5.0)
    assert hit is False
    assert ts.stop_price == pytest.approx(100.0)

    # Price bounces back up but not to stop — stop must NOT move up.
    hit = ts.update(current_price=95.0, current_atr=5.0)
    assert hit is False
    assert ts.stop_price == pytest.approx(100.0), "SHORT stop must not retreat"
