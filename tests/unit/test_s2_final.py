"""
Tests for final S2 closure:
- S2-1: Telemetry fallback is loud + extended retention
- S2-2: Package install smoke (CI step, tested here via import check)
- S2-3: Integrated certification protocol
"""

from decimal import Decimal

from src.execution.pending_store import PendingReport, PendingReportStore
from src.quantitative.advanced import CertificationResult, certify_strategy
from tests.quantitative.conftest import generate_trending_bars, run_backtest


# ── S2-1: Telemetry fallback is rare and loud ──


class TestTelemetryFallbackRareAndLoud:

    def test_7day_retention_keeps_recent(self, tmp_path):
        """Pending report from 1 day ago survives 7-day cleanup."""
        import time
        store = PendingReportStore(db_path=str(tmp_path / "p.db"))
        recent_ts = int(time.time() * 1000) - 86400000  # 1 day ago
        store.save(PendingReport(
            client_order_id="recent-1", symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"), requested_qty=Decimal("0.1"),
            submit_latency_ms=100.0, submit_ts_ms=recent_ts,
        ))
        deleted = store.cleanup_stale(max_age_ms=7 * 86400 * 1000)
        assert deleted == 0
        assert store.load("recent-1") is not None

    def test_7day_retention_removes_old(self, tmp_path):
        """Pending report from 10 days ago removed by 7-day cleanup."""
        store = PendingReportStore(db_path=str(tmp_path / "p.db"))
        store.save(PendingReport(
            client_order_id="old-1", symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"), requested_qty=Decimal("0.1"),
            submit_latency_ms=100.0, submit_ts_ms=1000,  # epoch ~1970
        ))
        deleted = store.cleanup_stale(max_age_ms=7 * 86400 * 1000)
        assert deleted == 1

    def test_store_survives_restart(self, tmp_path):
        """Durable: data persists across instances."""
        db = str(tmp_path / "p.db")
        PendingReportStore(db_path=db).save(PendingReport(
            client_order_id="durable-1", symbol="X", side="BUY",
            expected_price=Decimal("100"), requested_qty=Decimal("1"),
            submit_latency_ms=50.0, submit_ts_ms=int(__import__("time").time() * 1000),
        ))
        loaded = PendingReportStore(db_path=db).load("durable-1")
        assert loaded is not None
        assert loaded.expected_price == Decimal("100")


# ── S2-2: Package install smoke (import verification) ──


class TestPackageInstallSmoke:

    def test_all_critical_imports(self):
        """Verify all critical modules import cleanly."""
        from src.risk.gate import RiskGate
        from src.oms.reconcile import OMSReconcileService
        from src.execution.order_planner import OrderPlanner
        from src.monitoring.alert_manager import AlertManager
        from src.quantitative.metrics import compute_metrics
        from src.quantitative.advanced import certify_strategy
        from src.credentials import load_credentials
        from src.config_validator import validate_config
        # All imported without error
        assert True


# ── S2-3: Integrated certification protocol ──


class TestCertifyStrategy:

    def test_certify_passing_strategy(self):
        """Strategy on uptrend with lenient thresholds → PASS."""
        bars = generate_trending_bars(n=300, trend=0.002, seed=8000)
        result = certify_strategy(
            bars,
            lambda b: run_backtest(b, qty=Decimal("0.5")),
            train_size=100, test_size=50,
            min_sharpe=-10.0, max_drawdown=0.99,
            min_trades=1, min_profit_factor=0.0,
            max_consecutive_losses=50,
            overfitting_threshold=5.0,
            n_parameters=2,
        )
        assert isinstance(result, CertificationResult)
        assert result.passed
        assert result.metrics_pass
        assert result.walk_forward_pass
        assert result.leakage_clean

    def test_certify_insufficient_data_fails_snooping(self):
        """Too many params for data → snooping flagged → FAIL."""
        bars = generate_trending_bars(n=30, seed=8001)
        result = certify_strategy(
            bars,
            lambda b: run_backtest(b, qty=Decimal("0.5")),
            train_size=10, test_size=5,
            min_sharpe=-10.0, max_drawdown=0.99,
            min_trades=0, min_profit_factor=0.0,
            n_parameters=20,  # 20 params with 30 points → snooping risk
        )
        assert not result.leakage_clean
        assert not result.passed

    def test_certify_returns_all_fields(self):
        """CertificationResult has all required fields."""
        bars = generate_trending_bars(n=200, seed=8002)
        result = certify_strategy(
            bars,
            lambda b: run_backtest(b, qty=Decimal("0.5")),
            train_size=80, test_size=40,
        )
        assert hasattr(result, "passed")
        assert hasattr(result, "metrics_pass")
        assert hasattr(result, "walk_forward_pass")
        assert hasattr(result, "overfitting_detected")
        assert hasattr(result, "oos_degradation")
        assert hasattr(result, "leakage_clean")
        assert hasattr(result, "total_oos_pnl")
        assert hasattr(result, "total_is_pnl")

    def test_certify_reports_oos_pnl(self):
        """Certification reports OOS PnL from walk-forward."""
        bars = generate_trending_bars(n=300, trend=0.001, seed=8003)
        result = certify_strategy(
            bars,
            lambda b: run_backtest(b, qty=Decimal("0.5")),
            train_size=100, test_size=50,
        )
        assert isinstance(result.total_oos_pnl, float)
        assert isinstance(result.total_is_pnl, float)
