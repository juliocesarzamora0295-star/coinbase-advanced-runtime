"""
Tests unitarios: MetricsCollector.

Verifica contadores, setters, snapshot y flush.
"""
import json
import logging
from decimal import Decimal

import pytest

from src.observability.metrics import MetricsCollector, RuntimeMetrics


@pytest.fixture
def collector():
    return MetricsCollector()


class TestCounters:

    def test_ws_gap_increments(self, collector):
        collector.record_ws_gap()
        collector.record_ws_gap()
        snap = collector.snapshot()
        assert snap.ws_gap_count == 2

    def test_duplicate_fill_increments(self, collector):
        collector.record_duplicate_fill()
        snap = collector.snapshot()
        assert snap.duplicate_fill_count == 1

    def test_sizing_blocked_increments(self, collector):
        collector.record_sizing_blocked()
        collector.record_sizing_blocked()
        snap = collector.snapshot()
        assert snap.sizing_blocked_count == 2

    def test_spread_used_and_stale(self, collector):
        collector.record_spread_used()
        collector.record_spread_stale()
        collector.record_spread_stale()
        snap = collector.snapshot()
        assert snap.spread_used_count == 1
        assert snap.spread_stale_count == 2

    def test_signal_count_per_symbol(self, collector):
        collector.record_signal("BTC-USD")
        collector.record_signal("BTC-USD")
        collector.record_signal("ETH-USD")
        snap = collector.snapshot()
        assert snap.signal_count_per_symbol["BTC-USD"] == 2
        assert snap.signal_count_per_symbol["ETH-USD"] == 1


class TestOrderRateTracking:

    def test_reject_rate_zero_when_no_orders(self, collector):
        snap = collector.snapshot()
        assert snap.order_reject_rate == 0.0

    def test_reject_rate_calculated_correctly(self, collector):
        collector.record_order_submitted()
        collector.record_order_submitted()
        collector.record_order_rejected("EQUITY_ZERO")
        snap = collector.snapshot()
        # 1 rejected out of 3 total
        assert abs(snap.order_reject_rate - 1/3) < 0.001

    def test_riskgate_rejection_reason_counted(self, collector):
        collector.record_order_rejected("CIRCUIT_BREAKER_OPEN")
        collector.record_order_rejected("CIRCUIT_BREAKER_OPEN")
        collector.record_order_rejected("EQUITY_ZERO")
        snap = collector.snapshot()
        assert snap.riskgate_rejection_reason_count["CIRCUIT_BREAKER_OPEN"] == 2
        assert snap.riskgate_rejection_reason_count["EQUITY_ZERO"] == 1

    def test_total_and_rejected_counts(self, collector):
        collector.record_order_submitted()
        collector.record_order_rejected("X")
        snap = collector.snapshot()
        assert snap.order_total == 2
        assert snap.order_rejected == 1


class TestSetters:

    def test_set_open_orders_count(self, collector):
        collector.set_open_orders_count(5)
        assert collector.snapshot().open_orders_count == 5

    def test_record_reconcile_lag(self, collector):
        collector.record_reconcile_lag(123.5)
        assert collector.snapshot().reconcile_lag_ms == 123.5

    def test_set_ledger_equity(self, collector):
        collector.set_ledger_equity(Decimal("9876.50"))
        assert abs(collector.snapshot().ledger_equity - 9876.50) < 0.01

    def test_set_unrealized_pnl(self, collector):
        collector.set_unrealized_pnl(Decimal("-150.25"))
        assert abs(collector.snapshot().unrealized_pnl - (-150.25)) < 0.01

    def test_set_drawdown_pct(self, collector):
        collector.set_drawdown_pct(Decimal("0.075"))
        assert abs(collector.snapshot().drawdown_pct - 0.075) < 0.001

    def test_set_circuit_breaker_state(self, collector):
        collector.set_circuit_breaker_state("OPEN")
        assert collector.snapshot().circuit_breaker_state == "OPEN"


class TestSnapshot:

    def test_snapshot_has_ts_ms(self, collector):
        snap = collector.snapshot()
        assert snap.ts_ms > 0

    def test_snapshot_is_independent_copy(self, collector):
        """Modificar snap no afecta al collector."""
        snap1 = collector.snapshot()
        collector.record_ws_gap()
        snap2 = collector.snapshot()
        assert snap1.ws_gap_count == 0
        assert snap2.ws_gap_count == 1

    def test_snapshot_signal_dict_is_copy(self, collector):
        """Modificar el dict de señales en snapshot no afecta al collector."""
        collector.record_signal("BTC-USD")
        snap = collector.snapshot()
        snap.signal_count_per_symbol["BTC-USD"] = 999
        collector.record_signal("BTC-USD")
        snap2 = collector.snapshot()
        assert snap2.signal_count_per_symbol["BTC-USD"] == 2


class TestFlush:

    def test_flush_logs_json(self, collector, caplog):
        """flush() escribe una línea JSON válida al logger."""
        collector.record_ws_gap()
        collector.record_signal("BTC-USD")
        with caplog.at_level(logging.INFO, logger="Metrics"):
            collector.flush()

        assert len(caplog.records) >= 1
        last_msg = caplog.records[-1].getMessage()
        data = json.loads(last_msg)
        assert "ws_gap_count" in data
        assert "signal_count_per_symbol" in data

    def test_flush_json_contains_ts_ms(self, collector, caplog):
        with caplog.at_level(logging.INFO, logger="Metrics"):
            collector.flush()
        data = json.loads(caplog.records[-1].getMessage())
        assert data["ts_ms"] > 0


class TestReset:

    def test_reset_clears_all_counters(self, collector):
        collector.record_ws_gap()
        collector.record_signal("BTC-USD")
        collector.record_order_rejected("X")
        collector.reset()
        snap = collector.snapshot()
        assert snap.ws_gap_count == 0
        assert snap.signal_count_per_symbol == {}
        assert snap.order_total == 0
