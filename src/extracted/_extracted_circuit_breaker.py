"""
Extracted: Circuit Breaker from Kimi_Agent fortress_v4.

Origin: Kimi_Agent_Especificacion API Coinbase/fortress_v4/src/risk/circuit_breaker.py
Reason: Simpler baseline version of the circuit breaker. The canonical
        coinbase-advanced-runtime version (src/risk/circuit_breaker.py) evolved from
        this code and adds: force_open(), metric reset on HALF_OPEN transition.
        This extraction preserves the original for comparison.

Differences vs src/risk/circuit_breaker.py:
  - Missing force_open() method (external trip from OMS/kill_switch)
  - Does NOT clear execution metrics when entering HALF_OPEN
  - Uses `from collections import deque` without Deque type hint
  - Import of `time` module (unused in canonical version)
  - Identical 8-check pipeline, BreakerConfig, LatencyMetrics, ExecutionMetrics

Verdict: canonical version is strictly superior. No merge needed.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

# TODO: fortress-v4 integration — this import was `from src.accounting.ledger import Fill`
#       Decoupled here to be standalone. Use a Protocol or the canonical Fill dataclass.


logger = logging.getLogger("ExtractedCircuitBreaker")


@dataclass
class _Fill:
    """Minimal fill stub for standalone import. Replace with canonical Fill."""
    side: str
    amount: Decimal
    price: Decimal
    cost: Decimal
    fee_cost: Decimal
    fee_currency: str
    ts_ms: int
    trade_id: str
    order_id: str


class BreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class LatencyMetrics:
    samples: deque = field(default_factory=lambda: deque(maxlen=100))

    def add(self, latency_ms: float) -> None:
        self.samples.append(latency_ms)

    def p95(self) -> float:
        if not self.samples:
            return 0.0
        sorted_samples = sorted(self.samples)
        idx = int(len(sorted_samples) * 0.95)
        return sorted_samples[min(idx, len(sorted_samples) - 1)]

    def mean(self) -> float:
        if not self.samples:
            return 0.0
        return sum(self.samples) / len(self.samples)

    def clear(self) -> None:
        self.samples.clear()


@dataclass
class ExecutionMetrics:
    total_requests: int = 0
    total_rejects: int = 0
    rate_limit_hits: int = 0
    ws_gaps: int = 0
    slippage_observations: deque = field(default_factory=lambda: deque(maxlen=50))
    expected_slippage_bps: float = 5.0

    def record_request(self, success: bool) -> None:
        self.total_requests += 1
        if not success:
            self.total_rejects += 1

    def record_rate_limit_hit(self) -> None:
        self.rate_limit_hits += 1

    def record_ws_gap(self) -> None:
        self.ws_gaps += 1

    def record_slippage(self, slippage_bps: float) -> None:
        self.slippage_observations.append(slippage_bps)

    def reject_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_rejects / self.total_requests

    def slippage_drift(self) -> float:
        if not self.slippage_observations:
            return 0.0
        avg_slippage = sum(self.slippage_observations) / len(self.slippage_observations)
        return avg_slippage - self.expected_slippage_bps


@dataclass
class BreakerConfig:
    max_daily_loss: float = 0.05
    max_drawdown: float = 0.15
    max_consecutive_losses: int = 3
    latency_p95_threshold_ms: float = 500.0
    latency_baseline_ms: Optional[float] = None
    reject_rate_threshold: float = 0.03
    slippage_drift_threshold_bps: float = 10.0
    rate_limit_threshold: int = 5
    ws_gap_threshold: int = 1
    recovery_cooldown_minutes: int = 30
    recovery_test_trades: int = 3


class CircuitBreaker:
    """
    Original fortress_v4 circuit breaker (pre-evolution).

    Differences from canonical src/risk/circuit_breaker.py:
      - No force_open() for external trips
      - No metric reset on HALF_OPEN entry
    """

    def __init__(self, config: BreakerConfig):
        self.cfg = config
        self.state = BreakerState.CLOSED
        self.trip_reason: Optional[str] = None
        self.trip_time: Optional[datetime] = None

        self.equity_day_start: Decimal = Decimal("0")
        self.equity_peak: Decimal = Decimal("0")
        self.equity_now: Decimal = Decimal("0")
        self.consecutive_losses: int = 0
        self.day_start: datetime = datetime.now()
        self.last_trade_time: Optional[datetime] = None

        self.latency = LatencyMetrics()
        self.execution = ExecutionMetrics()

        self.recovery_test_count: int = 0
        self.recovery_success_count: int = 0

    # TODO: fortress-v4 integration — wire this to TradeLedger.on_fill_callback
    def on_fill(self, fill: Any) -> None:
        self.last_trade_time = datetime.now()
        logger.debug(f"Circuit breaker received fill: {getattr(fill, 'trade_id', '?')} {getattr(fill, 'side', '?')}")

    def get_fill_callback(self) -> Callable:
        return self.on_fill

    def reset_day(self, equity: Decimal) -> None:
        self.day_start = datetime.now()
        self.equity_day_start = equity
        self.equity_peak = equity
        self.equity_now = equity
        self.consecutive_losses = 0
        self.latency.clear()
        self.execution = ExecutionMetrics()
        logger.info(f"Day reset: equity=${equity:,.2f}")

    def update_equity(self, equity: Decimal) -> None:
        self.equity_now = equity
        if equity > self.equity_peak:
            self.equity_peak = equity

    def record_trade_pnl(self, pnl: Decimal) -> None:
        self.last_trade_time = datetime.now()
        if pnl >= 0:
            self.consecutive_losses = 0
            if self.state == BreakerState.HALF_OPEN:
                self.recovery_success_count += 1
                logger.info(f"Recovery trade success: +${pnl:,.2f}")
        else:
            self.consecutive_losses += 1
            if self.state == BreakerState.HALF_OPEN:
                self._trip(f"Recovery trade failed: ${pnl:,.2f}")

    def record_latency(self, latency_ms: float) -> None:
        self.latency.add(latency_ms)
        if self.cfg.latency_baseline_ms is None and len(self.latency.samples) >= 20:
            self.cfg.latency_baseline_ms = self.latency.mean()

    def record_execution_result(self, success: bool) -> None:
        self.execution.record_request(success)

    def record_rate_limit_hit(self) -> None:
        self.execution.record_rate_limit_hit()

    def record_ws_gap(self) -> None:
        self.execution.record_ws_gap()
        logger.warning("WebSocket gap detected by circuit breaker")

    def record_slippage(self, slippage_bps: float) -> None:
        self.execution.record_slippage(slippage_bps)

    def check_before_trade(self) -> Tuple[bool, Optional[str]]:
        if datetime.now().date() > self.day_start.date():
            self.reset_day(self.equity_now)

        if self.state == BreakerState.OPEN:
            if self._can_attempt_recovery():
                self.state = BreakerState.HALF_OPEN
                self.recovery_test_count = 0
                self.recovery_success_count = 0
                # NOTE: Original does NOT clear execution metrics here.
                # Canonical version added this fix.
                logger.info("Circuit breaker -> HALF_OPEN (testing recovery)")
            else:
                remaining = self._recovery_remaining_minutes()
                return False, f"OPEN: {self.trip_reason} (recovery in {remaining}m)"

        if self.state == BreakerState.HALF_OPEN:
            if self.recovery_success_count >= self.cfg.recovery_test_trades:
                self.state = BreakerState.CLOSED
                self.trip_reason = None
                self.trip_time = None
                logger.info("Circuit breaker recovered -> CLOSED")

        checks = [
            (self._check_daily_loss, "Daily loss limit"),
            (self._check_drawdown, "Max drawdown"),
            (self._check_consecutive_losses, "Consecutive losses"),
            (self._check_latency, "High latency"),
            (self._check_reject_rate, "High reject rate"),
            (self._check_slippage_drift, "Slippage drift"),
            (self._check_rate_limit_hits, "Rate limit hits"),
            (self._check_ws_gaps, "WebSocket gaps"),
        ]

        for check_fn, check_name in checks:
            triggered, reason = check_fn()
            if triggered:
                return self._trip(reason)

        return True, None

    def _check_daily_loss(self) -> Tuple[bool, str]:
        if self.equity_day_start <= 0:
            return False, ""
        daily_return = (self.equity_now / self.equity_day_start) - 1
        if daily_return <= -self.cfg.max_daily_loss:
            return True, f"Daily loss: {daily_return:.2%}"
        return False, ""

    def _check_drawdown(self) -> Tuple[bool, str]:
        if self.equity_peak <= 0:
            return False, ""
        drawdown = 1 - (self.equity_now / self.equity_peak)
        if drawdown >= self.cfg.max_drawdown:
            return True, f"Max drawdown: {drawdown:.2%}"
        return False, ""

    def _check_consecutive_losses(self) -> Tuple[bool, str]:
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return True, f"Consecutive losses: {self.consecutive_losses}"
        return False, ""

    def _check_latency(self) -> Tuple[bool, str]:
        p95 = self.latency.p95()
        if self.cfg.latency_baseline_ms and self.cfg.latency_baseline_ms > 0:
            if p95 > self.cfg.latency_baseline_ms * 2:
                return True, f"Latency p95: {p95:.0f}ms (2x baseline)"
        if p95 > self.cfg.latency_p95_threshold_ms:
            return True, f"Latency p95: {p95:.0f}ms (threshold: {self.cfg.latency_p95_threshold_ms:.0f}ms)"
        return False, ""

    def _check_reject_rate(self) -> Tuple[bool, str]:
        reject_rate = self.execution.reject_rate()
        if reject_rate > self.cfg.reject_rate_threshold:
            return True, f"Reject rate: {reject_rate:.2%}"
        return False, ""

    def _check_slippage_drift(self) -> Tuple[bool, str]:
        drift = self.execution.slippage_drift()
        if abs(drift) > self.cfg.slippage_drift_threshold_bps:
            direction = "higher" if drift > 0 else "lower"
            return True, f"Slippage {direction}: {drift:.1f}bps"
        return False, ""

    def _check_rate_limit_hits(self) -> Tuple[bool, str]:
        if self.execution.rate_limit_hits >= self.cfg.rate_limit_threshold:
            return True, f"Rate limit hits: {self.execution.rate_limit_hits}"
        return False, ""

    def _check_ws_gaps(self) -> Tuple[bool, str]:
        if self.execution.ws_gaps >= self.cfg.ws_gap_threshold:
            return True, f"WebSocket gaps: {self.execution.ws_gaps}"
        return False, ""

    def _trip(self, reason: str) -> Tuple[bool, str]:
        if self.state != BreakerState.OPEN:
            self.state = BreakerState.OPEN
            self.trip_reason = reason
            self.trip_time = datetime.now()
            logger.critical(f"CIRCUIT BREAKER TRIPPED: {reason}")
        return False, reason

    def _can_attempt_recovery(self) -> bool:
        if not self.trip_time:
            return False
        elapsed = datetime.now() - self.trip_time
        return elapsed >= timedelta(minutes=self.cfg.recovery_cooldown_minutes)

    def _recovery_remaining_minutes(self) -> int:
        if not self.trip_time:
            return 0
        elapsed = datetime.now() - self.trip_time
        remaining = timedelta(minutes=self.cfg.recovery_cooldown_minutes) - elapsed
        return max(0, int(remaining.total_seconds() / 60))

    def get_status(self) -> Dict:
        daily_return = 0.0
        if self.equity_day_start > 0:
            daily_return = float((self.equity_now / self.equity_day_start) - 1)
        drawdown = 0.0
        if self.equity_peak > 0:
            drawdown = float(1 - (self.equity_now / self.equity_peak))
        return {
            "state": self.state.value,
            "trip_reason": self.trip_reason,
            "trip_time": self.trip_time.isoformat() if self.trip_time else None,
            "equity": {
                "now": float(self.equity_now),
                "day_start": float(self.equity_day_start),
                "peak": float(self.equity_peak),
                "daily_return": daily_return,
                "drawdown": drawdown,
            },
            "trades": {
                "consecutive_losses": self.consecutive_losses,
                "recovery_tests": self.recovery_test_count,
                "recovery_success": self.recovery_success_count,
            },
            "health": {
                "latency_p95_ms": self.latency.p95(),
                "latency_mean_ms": self.latency.mean(),
                "reject_rate": self.execution.reject_rate(),
                "slippage_drift_bps": self.execution.slippage_drift(),
                "rate_limit_hits": self.execution.rate_limit_hits,
                "ws_gaps": self.execution.ws_gaps,
            },
        }
