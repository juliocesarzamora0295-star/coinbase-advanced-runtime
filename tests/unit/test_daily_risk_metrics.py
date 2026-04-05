"""
Tests para DailyRiskMetrics y DailyRiskTracker.

Valida:
- drawdown_pct = (equity_peak - equity_current) / equity_peak  [determinista]
- daily_pnl = equity_current - equity_day_start                [determinista]
- daily_pnl_pct = daily_pnl / equity_day_start
- Rollover de día UTC: reinicia equity_day_start y equity_peak
- Inmutabilidad de DailyRiskMetrics (frozen=True)
- Fail-closed: equity_peak=0 → drawdown=0; equity_day_start<=0 → pnl_pct=0
- Integración con PortfolioSnapshot
"""

from decimal import Decimal

import pytest

from src.risk.daily_risk_metrics import DailyRiskMetrics, DailyRiskTracker, _utc_day_start_ms

# Timestamps fijos para tests deterministas
_DAY_1_MS = 1_700_000_000_000  # algún día UTC
_DAY_1_START = _utc_day_start_ms(_DAY_1_MS)
_DAY_2_START = _DAY_1_START + 24 * 60 * 60 * 1000


# ──────────────────────────────────────────────
# Tests de DailyRiskMetrics (propiedades)
# ──────────────────────────────────────────────


class TestDailyRiskMetricsProperties:

    def _snap(self, day_start, peak, current):
        return DailyRiskMetrics(
            equity_day_start=Decimal(str(day_start)),
            equity_peak=Decimal(str(peak)),
            equity_current=Decimal(str(current)),
            day_start_ts_ms=_DAY_1_START,
        )

    def test_daily_pnl_positive(self):
        m = self._snap(10000, 12000, 11000)
        assert m.daily_pnl == Decimal("1000")

    def test_daily_pnl_negative(self):
        m = self._snap(10000, 10000, 8000)
        assert m.daily_pnl == Decimal("-2000")

    def test_daily_pnl_zero(self):
        m = self._snap(10000, 10000, 10000)
        assert m.daily_pnl == Decimal("0")

    def test_daily_pnl_pct_gain(self):
        m = self._snap(10000, 11000, 11000)
        assert m.daily_pnl_pct == Decimal("0.1")

    def test_daily_pnl_pct_loss(self):
        m = self._snap(10000, 10000, 9500)
        assert m.daily_pnl_pct == Decimal("-0.05")

    def test_daily_pnl_pct_zero_day_start(self):
        """Fail-closed: day_start = 0 → daily_pnl_pct = 0 (no divide por cero)."""
        m = self._snap(0, 0, 1000)
        assert m.daily_pnl_pct == Decimal("0")

    def test_drawdown_at_peak(self):
        """En el pico: drawdown = 0."""
        m = self._snap(10000, 12000, 12000)
        assert m.drawdown_pct == Decimal("0")

    def test_drawdown_from_peak(self):
        """Caída de 12000 a 9000 desde pico = (12000-9000)/12000 = 25%."""
        m = self._snap(10000, 12000, 9000)
        expected = Decimal("3000") / Decimal("12000")
        assert m.drawdown_pct == expected

    def test_drawdown_zero_peak(self):
        """Fail-closed: equity_peak = 0 → drawdown = 0."""
        m = self._snap(0, 0, 0)
        assert m.drawdown_pct == Decimal("0")

    def test_drawdown_clamped_to_zero(self):
        """Si equity_current > equity_peak (estado anómalo), drawdown = 0."""
        m = self._snap(10000, 10000, 11000)
        assert m.drawdown_pct == Decimal("0")

    def test_is_at_peak_true(self):
        m = self._snap(10000, 12000, 12000)
        assert m.is_at_peak is True

    def test_is_at_peak_false(self):
        m = self._snap(10000, 12000, 11000)
        assert m.is_at_peak is False

    def test_immutable(self):
        m = self._snap(10000, 12000, 11000)
        with pytest.raises(Exception):
            m.equity_current = Decimal("99999")  # type: ignore[misc]


# ──────────────────────────────────────────────
# Tests de DailyRiskTracker — mismo día
# ──────────────────────────────────────────────


class TestDailyRiskTrackerSameDay:

    def test_initial_state(self):
        tracker = DailyRiskTracker(
            equity_day_start=Decimal("10000"),
            day_start_ts_ms=_DAY_1_START,
        )
        assert tracker.equity_day_start == Decimal("10000")
        assert tracker.equity_peak == Decimal("10000")

    def test_update_above_start_updates_peak(self):
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        m = tracker.update(Decimal("11000"), now_ms=_DAY_1_MS)
        assert m.equity_peak == Decimal("11000")
        assert m.equity_current == Decimal("11000")
        assert m.daily_pnl == Decimal("1000")

    def test_update_below_peak_does_not_change_peak(self):
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        tracker.update(Decimal("12000"), now_ms=_DAY_1_MS)
        m = tracker.update(Decimal("9000"), now_ms=_DAY_1_MS + 1000)
        assert m.equity_peak == Decimal("12000")  # peak no retrocede
        assert m.equity_current == Decimal("9000")

    def test_drawdown_deterministic(self):
        """drawdown = (peak - current) / peak — verificación aritmética exacta."""
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        tracker.update(Decimal("15000"), now_ms=_DAY_1_MS)
        m = tracker.update(Decimal("12000"), now_ms=_DAY_1_MS + 1000)
        expected = (Decimal("15000") - Decimal("12000")) / Decimal("15000")
        assert m.drawdown_pct == expected

    def test_daily_pnl_deterministic(self):
        """daily_pnl = current - day_start — no depende de fills."""
        tracker = DailyRiskTracker(Decimal("50000"), day_start_ts_ms=_DAY_1_START)
        m = tracker.update(Decimal("47000"), now_ms=_DAY_1_MS)
        assert m.daily_pnl == Decimal("-3000")
        assert m.equity_day_start == Decimal("50000")

    def test_multiple_updates_same_day(self):
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        tracker.update(Decimal("10500"), now_ms=_DAY_1_MS)
        tracker.update(Decimal("11000"), now_ms=_DAY_1_MS + 1000)
        tracker.update(Decimal("10800"), now_ms=_DAY_1_MS + 2000)
        m = tracker.update(Decimal("10200"), now_ms=_DAY_1_MS + 3000)
        assert m.equity_peak == Decimal("11000")
        assert m.equity_day_start == Decimal("10000")
        assert m.daily_pnl == Decimal("200")


# ──────────────────────────────────────────────
# Tests de DailyRiskTracker — rollover de día
# ──────────────────────────────────────────────


class TestDailyRiskTrackerRollover:

    def test_rollover_resets_day_start(self):
        """Al detectar nuevo día UTC, equity_day_start se reinicia."""
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        tracker.update(Decimal("12000"), now_ms=_DAY_1_MS)

        # Primer tick del día 2
        m = tracker.update(Decimal("11000"), now_ms=_DAY_2_START + 1000)
        assert m.equity_day_start == Decimal("11000")
        assert m.equity_peak == Decimal("11000")
        assert m.day_start_ts_ms == _DAY_2_START

    def test_rollover_daily_pnl_is_zero_on_first_tick(self):
        """En el primer tick del nuevo día, daily_pnl = 0 (current == day_start)."""
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        m = tracker.update(Decimal("9000"), now_ms=_DAY_2_START)
        assert m.daily_pnl == Decimal("0")
        assert m.drawdown_pct == Decimal("0")

    def test_rollover_subsequent_ticks_compute_correctly(self):
        """Tras rollover, métricas del nuevo día son deterministas."""
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        # Día 1: sube a 13000
        tracker.update(Decimal("13000"), now_ms=_DAY_1_MS)

        # Día 2: empieza en 11000, luego sube a 14000, luego baja a 12000
        tracker.update(Decimal("11000"), now_ms=_DAY_2_START)
        tracker.update(Decimal("14000"), now_ms=_DAY_2_START + 1000)
        m = tracker.update(Decimal("12000"), now_ms=_DAY_2_START + 2000)

        assert m.equity_day_start == Decimal("11000")
        assert m.equity_peak == Decimal("14000")
        assert m.daily_pnl == Decimal("1000")
        expected_dd = (Decimal("14000") - Decimal("12000")) / Decimal("14000")
        assert m.drawdown_pct == expected_dd

    def test_no_rollover_within_same_day(self):
        """Timestamps dentro del mismo día no disparan rollover."""
        tracker = DailyRiskTracker(Decimal("10000"), day_start_ts_ms=_DAY_1_START)
        end_of_day = _DAY_1_START + _MS_PER_DAY - 1  # 23:59:59.999
        m = tracker.update(Decimal("10500"), now_ms=end_of_day)
        assert m.equity_day_start == Decimal("10000")


_MS_PER_DAY = 24 * 60 * 60 * 1000


# ──────────────────────────────────────────────
# Tests de from_equity factory
# ──────────────────────────────────────────────


class TestDailyRiskTrackerFactory:

    def test_from_equity_sets_day_start(self):
        tracker = DailyRiskTracker.from_equity(Decimal("5000"), now_ms=_DAY_1_MS)
        assert tracker.equity_day_start == Decimal("5000")
        assert tracker.equity_peak == Decimal("5000")
        assert tracker.day_start_ts_ms == _DAY_1_START

    def test_negative_equity_day_start_raises(self):
        with pytest.raises(ValueError, match="equity_day_start"):
            DailyRiskTracker(equity_day_start=Decimal("-1"))


# ──────────────────────────────────────────────
# Tests de integración con PortfolioSnapshot
# ──────────────────────────────────────────────


class TestIntegrationWithPortfolioSnapshot:

    def test_tracker_fed_from_portfolio_equity(self, tmp_path):
        """DailyRiskTracker consume PortfolioSnapshot.equity correctamente."""
        from src.accounting.ledger import Fill, TradeLedger
        from src.accounting.portfolio_snapshot import PortfolioSnapshot

        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "ledger.db"))
        ledger.add_fill(Fill(
            side="buy",
            amount=Decimal("1.0"),
            price=Decimal("50000"),
            cost=Decimal("50000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1000,
            trade_id="t1",
            order_id="o1",
        ))

        snap = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("55000"),
                                             ts_ms=_DAY_1_MS)
        assert snap.equity == Decimal("55000")  # realized(0) + mark(55000)

        tracker = DailyRiskTracker.from_equity(snap.equity, now_ms=_DAY_1_MS)
        # Precio cae
        snap2 = PortfolioSnapshot.from_ledger(ledger, mark_price=Decimal("48000"),
                                              ts_ms=_DAY_1_MS + 3600_000)
        m = tracker.update(snap2.equity, now_ms=_DAY_1_MS + 3600_000)

        assert m.equity_day_start == Decimal("55000")
        assert m.equity_current == Decimal("48000")
        assert m.daily_pnl == Decimal("-7000")
        assert m.drawdown_pct == Decimal("7000") / Decimal("55000")

    def test_risk_snapshot_fields_from_metrics(self):
        """DailyRiskMetrics alimenta correctamente RiskSnapshot."""
        from src.risk.gate import RiskSnapshot

        tracker = DailyRiskTracker.from_equity(Decimal("10000"), now_ms=_DAY_1_MS)
        tracker.update(Decimal("11000"), now_ms=_DAY_1_MS + 1000)
        m = tracker.update(Decimal("9500"), now_ms=_DAY_1_MS + 2000)

        risk_snap = RiskSnapshot(
            equity=m.equity_current,
            position_qty=Decimal("0"),
            day_pnl_pct=m.daily_pnl_pct,
            drawdown_pct=m.drawdown_pct,
        )

        assert risk_snap.day_pnl_pct == m.daily_pnl_pct
        assert risk_snap.drawdown_pct == m.drawdown_pct
        assert risk_snap.equity == Decimal("9500")
