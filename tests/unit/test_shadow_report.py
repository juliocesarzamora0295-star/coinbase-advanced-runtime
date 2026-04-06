"""Tests for ShadowReport."""

from src.shadow_report import ShadowCriteria, ShadowReport, analyze_shadow_run
from src.shadow_runner import ShadowMetricsSample, ShadowRunResult


def _make_result(**overrides) -> ShadowRunResult:
    defaults = dict(
        duration_s=60.0,
        total_ticks=100,
        total_trades=5,
        total_signals=10,
        signals_blocked=2,
        equity_start=10000.0,
        equity_end=10100.0,
        max_drawdown_pct=0.02,
        avg_latency_ms=50.0,
        breaker_trips=0,
        crashes=0,
        samples=[
            ShadowMetricsSample(
                timestamp_ms=i * 1000, equity=10000.0 + i * 10,
                drawdown_pct=0.0, total_trades=i, latency_ms=50.0,
                breaker_state="closed",
            )
            for i in range(10)
        ],
        errors=[],
    )
    defaults.update(overrides)
    return ShadowRunResult(**defaults)


class TestAnalyzeReport:

    def test_healthy_run_passes(self):
        result = _make_result()
        report = analyze_shadow_run(result)
        assert report.passed
        assert report.failures == []

    def test_high_drawdown_fails(self):
        result = _make_result(max_drawdown_pct=0.10)
        report = analyze_shadow_run(result, ShadowCriteria(max_drawdown_pct=5.0))
        assert not report.passed
        assert any("max_dd" in f for f in report.failures)

    def test_crashes_fail(self):
        result = _make_result(crashes=1)
        report = analyze_shadow_run(result, ShadowCriteria(max_crashes=0))
        assert not report.passed
        assert any("crashes" in f for f in report.failures)

    def test_high_latency_fails(self):
        result = _make_result(avg_latency_ms=600.0)
        report = analyze_shadow_run(result, ShadowCriteria(max_avg_latency_ms=500.0))
        assert not report.passed

    def test_too_few_trades_fails(self):
        result = _make_result(total_trades=0)
        report = analyze_shadow_run(result, ShadowCriteria(min_trades=1))
        assert not report.passed


class TestReportOutput:

    def test_to_dict(self):
        result = _make_result()
        report = analyze_shadow_run(result)
        d = report.to_dict()
        assert "passed" in d
        assert "sharpe_ratio" in d
        assert "max_drawdown_pct" in d

    def test_to_json(self):
        result = _make_result()
        report = analyze_shadow_run(result)
        j = report.to_json()
        import json
        data = json.loads(j)
        assert data["passed"] is True

    def test_summary_readable(self):
        result = _make_result()
        report = analyze_shadow_run(result)
        s = report.summary()
        assert "Shadow Report" in s
        assert "PASS" in s
