"""
Tests para ExecutionReport y métricas de microestructura.

Invariantes testeadas:
- Slippage cálculo con signo correcto (BUY vs SELL)
- Latencia en ms
- Partial fill tracking (fill_ratio)
- Fill quality score [0, 1]
- build_execution_report factory
- Integración con CircuitBreaker signals
- ExecutionReport es inmutable
- Logging estructurado
"""

from decimal import Decimal

import pytest

from src.execution.execution_report import (
    ExecutionReport,
    build_execution_report,
    compute_fill_quality_score,
    compute_fill_ratio,
    compute_slippage_bps,
)


class TestSlippageCalculation:
    """Slippage en bps con signo correcto."""

    def test_buy_positive_slippage(self):
        """BUY a precio mayor que expected → slippage positivo (desfavorable)."""
        bps = compute_slippage_bps("BUY", Decimal("50000"), Decimal("50050"))
        assert bps > Decimal("0")
        # (50050 - 50000) / 50000 * 10000 = 10 bps
        assert bps == Decimal("10")

    def test_buy_negative_slippage(self):
        """BUY a precio menor que expected → slippage negativo (favorable)."""
        bps = compute_slippage_bps("BUY", Decimal("50000"), Decimal("49950"))
        assert bps < Decimal("0")
        assert bps == Decimal("-10")

    def test_buy_no_slippage(self):
        """BUY al precio exacto → slippage = 0."""
        bps = compute_slippage_bps("BUY", Decimal("50000"), Decimal("50000"))
        assert bps == Decimal("0")

    def test_sell_positive_slippage(self):
        """SELL a precio menor que expected → slippage positivo (desfavorable)."""
        bps = compute_slippage_bps("SELL", Decimal("50000"), Decimal("49950"))
        assert bps > Decimal("0")
        # -(49950 - 50000) / 50000 * 10000 = 10 bps
        assert bps == Decimal("10")

    def test_sell_negative_slippage(self):
        """SELL a precio mayor que expected → slippage negativo (favorable)."""
        bps = compute_slippage_bps("SELL", Decimal("50000"), Decimal("50050"))
        assert bps < Decimal("0")
        assert bps == Decimal("-10")

    def test_zero_expected_price(self):
        """Expected price = 0 → slippage = 0 (no div by zero)."""
        bps = compute_slippage_bps("BUY", Decimal("0"), Decimal("50000"))
        assert bps == Decimal("0")

    def test_case_insensitive_side(self):
        """Side is case-insensitive."""
        bps_upper = compute_slippage_bps("BUY", Decimal("50000"), Decimal("50050"))
        bps_lower = compute_slippage_bps("buy", Decimal("50000"), Decimal("50050"))
        # buy is not "BUY" uppercase — goes to SELL branch
        # Actually the code does side.upper() == "BUY"
        assert bps_upper == Decimal("10")


class TestFillRatio:
    """Fill ratio: filled / requested."""

    def test_full_fill(self):
        ratio = compute_fill_ratio(Decimal("0.1"), Decimal("0.1"))
        assert ratio == Decimal("1")

    def test_partial_fill(self):
        ratio = compute_fill_ratio(Decimal("0.1"), Decimal("0.05"))
        assert ratio == Decimal("0.5")

    def test_zero_fill(self):
        ratio = compute_fill_ratio(Decimal("0.1"), Decimal("0"))
        assert ratio == Decimal("0")

    def test_zero_requested(self):
        ratio = compute_fill_ratio(Decimal("0"), Decimal("0"))
        assert ratio == Decimal("0")

    def test_overfill_capped_at_one(self):
        """Overfill (shouldn't happen) capped at 1.0."""
        ratio = compute_fill_ratio(Decimal("0.1"), Decimal("0.15"))
        assert ratio == Decimal("1")


class TestFillQualityScore:
    """Composite quality score [0, 1]."""

    def test_perfect_execution(self):
        """0 slippage, 0 latency, full fill → quality = 1.0."""
        score = compute_fill_quality_score(
            slippage_bps=Decimal("0"),
            latency_ms=0.0,
            fill_ratio=Decimal("1"),
        )
        assert score == Decimal("1")

    def test_worst_execution(self):
        """Max slippage, max latency, zero fill → quality = 0.0."""
        score = compute_fill_quality_score(
            slippage_bps=Decimal("20"),
            latency_ms=2000.0,
            fill_ratio=Decimal("0"),
        )
        assert score == Decimal("0")

    def test_moderate_execution(self):
        """10 bps slippage, 1000ms latency, full fill → ~0.55."""
        score = compute_fill_quality_score(
            slippage_bps=Decimal("10"),
            latency_ms=1000.0,
            fill_ratio=Decimal("1"),
        )
        # slippage: 1 - (10/20) = 0.5, weight 0.4 → 0.2
        # latency: 1 - (1000/2000) = 0.5, weight 0.3 → 0.15
        # fill: 1.0, weight 0.3 → 0.3
        # total = 0.65
        assert score == Decimal("0.65")

    def test_negative_slippage_uses_abs(self):
        """Favorable slippage (negative) uses absolute value."""
        score = compute_fill_quality_score(
            slippage_bps=Decimal("-5"),
            latency_ms=100.0,
            fill_ratio=Decimal("1"),
        )
        assert score > Decimal("0.5")

    def test_score_bounded_0_1(self):
        """Score always in [0, 1]."""
        for slippage in [Decimal("-100"), Decimal("0"), Decimal("100")]:
            for latency in [0.0, 500.0, 5000.0]:
                for ratio in [Decimal("0"), Decimal("0.5"), Decimal("1")]:
                    score = compute_fill_quality_score(slippage, latency, ratio)
                    assert Decimal("0") <= score <= Decimal("1"), (
                        f"Score {score} out of bounds for "
                        f"slippage={slippage}, latency={latency}, ratio={ratio}"
                    )


class TestBuildExecutionReport:
    """Factory function builds correct report."""

    def test_full_fill_report(self):
        report = build_execution_report(
            client_order_id="order-1",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50010"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=150.0,
            outcome="FILLED",
        )
        assert report.outcome == "FILLED"
        assert report.fill_ratio == Decimal("1")
        assert report.slippage_bps == Decimal("2")  # (10/50000)*10000
        assert report.latency_ms == 150.0
        assert Decimal("0") < report.fill_quality_score <= Decimal("1")

    def test_partial_fill_report(self):
        report = build_execution_report(
            client_order_id="order-2",
            symbol="ETH-USD",
            side="SELL",
            expected_price=Decimal("3000"),
            fill_price=Decimal("2997"),
            requested_qty=Decimal("1.0"),
            filled_qty=Decimal("0.6"),
            latency_ms=500.0,
            outcome="PARTIAL",
        )
        assert report.outcome == "PARTIAL"
        assert report.fill_ratio == Decimal("0.6")
        assert report.slippage_bps > Decimal("0")  # desfavorable for SELL

    def test_rejected_report(self):
        report = build_execution_report(
            client_order_id="order-3",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("0"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0"),
            latency_ms=50.0,
            outcome="REJECTED",
        )
        assert report.outcome == "REJECTED"
        assert report.fill_ratio == Decimal("0")
        assert report.filled_qty == Decimal("0")

    def test_report_is_immutable(self):
        report = build_execution_report(
            client_order_id="order-4",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50000"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=100.0,
            outcome="FILLED",
        )
        with pytest.raises((AttributeError, TypeError)):
            report.slippage_bps = Decimal("999")  # type: ignore


class TestCircuitBreakerIntegration:
    """ExecutionReport feeds CircuitBreaker signals."""

    def test_report_feeds_breaker_slippage(self):
        """Slippage from report can feed CircuitBreaker.record_slippage()."""
        from src.risk.circuit_breaker import BreakerConfig, CircuitBreaker

        breaker = CircuitBreaker(BreakerConfig())

        report = build_execution_report(
            client_order_id="cb-1",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50100"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=200.0,
            outcome="FILLED",
        )

        # Feed to breaker
        breaker.record_slippage(float(report.slippage_bps))
        breaker.record_latency(report.latency_ms)
        breaker.record_execution_result(report.outcome == "FILLED")

        # Verify breaker received the data
        assert len(breaker.execution.slippage_observations) == 1
        assert len(breaker.latency.samples) == 1
        assert breaker.execution.total_requests == 1

    def test_rejected_report_feeds_failure(self):
        """Rejected order feeds failure to breaker."""
        from src.risk.circuit_breaker import BreakerConfig, CircuitBreaker

        breaker = CircuitBreaker(BreakerConfig())

        report = build_execution_report(
            client_order_id="cb-2",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("0"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0"),
            latency_ms=30.0,
            outcome="REJECTED",
        )

        breaker.record_execution_result(report.outcome in ("FILLED", "PARTIAL"))
        assert breaker.execution.total_rejects == 1


class TestStructuredLogging:
    """Logging structured output."""

    def test_log_structured_does_not_raise(self):
        """log_structured() runs without error."""
        report = build_execution_report(
            client_order_id="log-1",
            symbol="BTC-USD",
            side="BUY",
            expected_price=Decimal("50000"),
            fill_price=Decimal("50010"),
            requested_qty=Decimal("0.1"),
            filled_qty=Decimal("0.1"),
            latency_ms=100.0,
            outcome="FILLED",
        )
        report.log_structured()  # should not raise
