"""
Tests for S2 risk closure: R5-R9.

R5: JSONLineSink wired in main.py
R6: metrics.flush() called periodically
R7: Mock exchange integration (separate file)
R8: mypy strict overrides (verified by CI)
R9: RISK_BASED sizing mode
"""

import json
import os
import tempfile
import time
from decimal import Decimal

from src.observability.json_sink import JSONLineSink
from src.observability.metrics import MetricsCollector
from src.risk.position_sizer import (
    PositionSizer,
    SizingMode,
    SymbolConstraints,
)


CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("Infinity"),
    min_notional=Decimal("1"),
)


# ──────────────────────────────────────────────
# R5: JSONLineSink wired
# ──────────────────────────────────────────────


class TestR5SinkWiring:

    def test_sink_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "metrics.jsonl")
            sink = JSONLineSink(path=path)
            collector = MetricsCollector()

            collector.inc("orders.submitted")
            collector.gauge("equity.current", 10000.0)
            snap = collector.generic_snapshot()
            sink.write_snapshot(snap)
            sink.close()

            with open(path) as f:
                data = json.loads(f.readline())
                assert data["name"] == "_snapshot"
                assert "counters" in data["data"]
                assert "gauges" in data["data"]

    def test_sink_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "metrics.jsonl")
            sink = JSONLineSink(path=path)
            sink.write("test", 1)
            sink.close()
            assert os.path.exists(path)


# ──────────────────────────────────────────────
# R6: Periodic flush
# ──────────────────────────────────────────────


class TestR6PeriodicFlush:

    def test_flush_writes_to_logger(self, caplog):
        import logging

        collector = MetricsCollector()
        collector.inc("test.counter")
        with caplog.at_level(logging.INFO, logger="Metrics"):
            collector.flush()
        assert len(caplog.records) >= 1
        data = json.loads(caplog.records[-1].getMessage())
        assert "ts_ms" in data

    def test_generic_snapshot_has_timestamps(self):
        collector = MetricsCollector()
        snap = collector.generic_snapshot()
        assert "ts_ms" in snap
        assert snap["ts_ms"] > 0


# ──────────────────────────────────────────────
# R9: RISK_BASED sizing mode
# ──────────────────────────────────────────────


class TestR9RiskBasedSizing:

    def test_risk_based_with_stop(self):
        """RISK_BASED mode with stop_price → qty = budget / stop_distance."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),  # budget = 100
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("49000"),  # distance = 1000
            preferred_mode=SizingMode.RISK_BASED,
        )
        # qty = 100 / 1000 = 0.1
        assert d.target_qty == Decimal("0.10000")
        assert d.sizing_mode == SizingMode.RISK_BASED

    def test_risk_based_without_stop_falls_back_to_notional(self):
        """RISK_BASED without stop_price → fallback to NOTIONAL with warning."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=None,
            preferred_mode=SizingMode.RISK_BASED,
        )
        assert d.sizing_mode == SizingMode.NOTIONAL
        assert d.target_qty > Decimal("0")

    def test_risk_based_vs_notional_different_qty(self):
        """RISK_BASED produces different qty than NOTIONAL for same parameters."""
        sizer = PositionSizer()
        common = dict(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
        )

        notional = sizer.compute(**common, preferred_mode=SizingMode.NOTIONAL)
        risk_based = sizer.compute(
            **common,
            stop_price=Decimal("49000"),
            preferred_mode=SizingMode.RISK_BASED,
        )

        # NOTIONAL: qty = 100 / 50000 = 0.002
        # RISK_BASED: qty = 100 / 1000 = 0.1
        assert risk_based.target_qty > notional.target_qty

    def test_risk_based_tight_stop_large_raw_qty_capped(self):
        """Tight stop → large raw qty, capped by max_notional."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),  # budget = 100
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("49990"),  # distance = 10, raw qty = 10
            preferred_mode=SizingMode.RISK_BASED,
        )
        # raw qty = 100/10 = 10, notional = 10 * 50000 = 500000 > max 100000
        # capped: qty = 100000 / 50000 = 2
        assert d.target_qty == Decimal("2.00000")
        assert d.sizing_mode == SizingMode.RISK_BASED

    def test_risk_based_wide_stop_large_qty(self):
        """Wide stop → smaller qty (risk is bounded)."""
        sizer = PositionSizer()
        d = sizer.compute(
            symbol="BTC-USD",
            equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.01"),  # budget = 100
            constraints=CONSTRAINTS,
            max_notional=Decimal("100000"),
            stop_price=Decimal("40000"),  # distance = 10000
            preferred_mode=SizingMode.RISK_BASED,
        )
        # qty = 100 / 10000 = 0.01
        assert d.target_qty == Decimal("0.01000")

    def test_sizing_mode_enum_has_risk_based(self):
        """SizingMode.RISK_BASED exists as valid enum value."""
        assert SizingMode.RISK_BASED.value == "RISK_BASED"
        assert SizingMode.RISK_BASED != SizingMode.NOTIONAL

    def test_config_sizing_mode_field(self):
        """TradingConfig has sizing_mode field."""
        from src.config import TradingConfig
        tc = TradingConfig()
        assert tc.sizing_mode == "NOTIONAL"
