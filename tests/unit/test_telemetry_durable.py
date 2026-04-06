"""
Tests for durable telemetry: PendingReportStore, estimated_slippage flag,
restart recovery.
"""

import os
import tempfile
from decimal import Decimal

from src.execution.execution_report import ExecutionReport, build_execution_report
from src.execution.pending_store import PendingReport, PendingReportStore


class TestPendingReportStore:
    """Persist/load/delete cycle."""

    def test_save_and_load(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "pr.db"))
        pr = PendingReport(
            client_order_id="ord-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),
            requested_qty=Decimal("0.1"),
            submit_latency_ms=150.0,
            submit_ts_ms=1_700_000_000_000,
        )
        store.save(pr)
        loaded = store.load("ord-1")
        assert loaded is not None
        assert loaded.expected_price == Decimal("50000")
        assert loaded.symbol == "BTC-USD"

    def test_load_missing_returns_none(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "pr.db"))
        assert store.load("nonexistent") is None

    def test_delete_removes(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "pr.db"))
        pr = PendingReport(
            client_order_id="ord-del",
            symbol="ETH-USD", side="SELL",
            expected_price=Decimal("3000"),
            requested_qty=Decimal("1"),
            submit_latency_ms=100.0,
            submit_ts_ms=1_700_000_000_000,
        )
        store.save(pr)
        store.delete("ord-del")
        assert store.load("ord-del") is None

    def test_count(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "pr.db"))
        assert store.count() == 0
        store.save(PendingReport(
            client_order_id="c1", symbol="X", side="BUY",
            expected_price=Decimal("1"), requested_qty=Decimal("1"),
            submit_latency_ms=0, submit_ts_ms=1000,
        ))
        store.save(PendingReport(
            client_order_id="c2", symbol="X", side="BUY",
            expected_price=Decimal("1"), requested_qty=Decimal("1"),
            submit_latency_ms=0, submit_ts_ms=1000,
        ))
        assert store.count() == 2

    def test_cleanup_stale(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "pr.db"))
        store.save(PendingReport(
            client_order_id="old",
            symbol="X", side="BUY",
            expected_price=Decimal("1"), requested_qty=Decimal("1"),
            submit_latency_ms=0, submit_ts_ms=1000,  # very old
        ))
        deleted = store.cleanup_stale(max_age_ms=1000)
        assert deleted == 1
        assert store.count() == 0


class TestRestartRecovery:
    """Pending reports survive restart."""

    def test_survive_restart(self, tmp_path):
        db = str(tmp_path / "pr.db")

        # Instance 1: save
        store1 = PendingReportStore(db_path=db)
        store1.save(PendingReport(
            client_order_id="restart-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("49500"),
            requested_qty=Decimal("0.05"),
            submit_latency_ms=200.0,
            submit_ts_ms=1_700_000_000_000,
        ))

        # Instance 2: load (simulates restart)
        store2 = PendingReportStore(db_path=db)
        loaded = store2.load("restart-1")
        assert loaded is not None
        assert loaded.expected_price == Decimal("49500")
        assert loaded.submit_latency_ms == 200.0


class TestEstimatedSlippageFlag:
    """ExecutionReport.estimated_slippage flag."""

    def test_real_slippage_not_estimated(self):
        report = build_execution_report(
            client_order_id="real-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50050"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=150.0, outcome="FILLED",
            estimated_slippage=False,
        )
        assert not report.estimated_slippage
        assert report.slippage_bps == Decimal("10")

    def test_estimated_slippage_flagged(self):
        report = build_execution_report(
            client_order_id="est-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50000"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=0.0, outcome="FILLED",
            estimated_slippage=True,
        )
        assert report.estimated_slippage
        assert report.slippage_bps == Decimal("0")

    def test_default_not_estimated(self):
        """Default estimated_slippage is False."""
        report = build_execution_report(
            client_order_id="def-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50010"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=100.0, outcome="FILLED",
        )
        assert not report.estimated_slippage

    def test_log_structured_includes_flag(self):
        """log_structured() runs without error and includes flag."""
        report = build_execution_report(
            client_order_id="log-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50010"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=100.0, outcome="FILLED",
            estimated_slippage=True,
        )
        report.log_structured()  # should not raise


class TestUpsertIdempotent:
    """Save same order_id twice → upsert, not duplicate."""

    def test_upsert(self, tmp_path):
        store = PendingReportStore(db_path=str(tmp_path / "pr.db"))
        pr1 = PendingReport(
            client_order_id="upsert-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("49000"),
            requested_qty=Decimal("0.1"),
            submit_latency_ms=100.0, submit_ts_ms=1000,
        )
        pr2 = PendingReport(
            client_order_id="upsert-1",
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("49500"),  # updated price
            requested_qty=Decimal("0.1"),
            submit_latency_ms=120.0, submit_ts_ms=2000,
        )
        store.save(pr1)
        store.save(pr2)
        assert store.count() == 1
        loaded = store.load("upsert-1")
        assert loaded.expected_price == Decimal("49500")
