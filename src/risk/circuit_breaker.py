"""
Circuit Breaker expandido para Fortress v4 - Coinbase Edition.

CORREGIDO P1: Cableado a fills reales via callback.
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Deque, Dict, Optional, Tuple

from src.accounting.ledger import Fill

logger = logging.getLogger("CircuitBreaker")


class BreakerState(Enum):
    """Estados del circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class LatencyMetrics:
    """Métricas de latencia."""

    samples: Deque[float] = field(default_factory=lambda: deque(maxlen=100))

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
    """Métricas de ejecución para health monitoring."""

    total_requests: int = 0
    total_rejects: int = 0
    rate_limit_hits: int = 0
    ws_gaps: int = 0
    slippage_observations: Deque[float] = field(default_factory=lambda: deque(maxlen=50))
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
    """Configuración del circuit breaker."""

    # Equity limits
    max_daily_loss: float = 0.05
    max_drawdown: float = 0.15
    max_consecutive_losses: int = 3

    # Latency limits
    latency_p95_threshold_ms: float = 500.0
    latency_baseline_ms: Optional[float] = None

    # Reject rate limits
    reject_rate_threshold: float = 0.03

    # Slippage drift
    slippage_drift_threshold_bps: float = 10.0

    # Rate limit hits
    rate_limit_threshold: int = 5

    # WS gaps
    ws_gap_threshold: int = 1

    # Recovery
    recovery_cooldown_minutes: int = 30
    recovery_test_trades: int = 3


class CircuitBreaker:
    """
    Circuit breaker expandido con monitoreo de salud de ejecución.

    CORREGIDO P1: Cableado a fills reales via callback.
    """

    def __init__(self, config: BreakerConfig):
        self.cfg = config
        self.state = BreakerState.CLOSED
        self.trip_reason: Optional[str] = None
        self.trip_time: Optional[datetime] = None

        # Equity metrics
        self.equity_day_start: Decimal = Decimal("0")
        self.equity_peak: Decimal = Decimal("0")
        self.equity_now: Decimal = Decimal("0")
        self.consecutive_losses: int = 0
        self.day_start: datetime = datetime.now()
        self.last_trade_time: Optional[datetime] = None

        # Execution health metrics
        self.latency = LatencyMetrics()
        self.execution = ExecutionMetrics()

        # Recovery tracking
        self.recovery_test_count: int = 0
        self.recovery_success_count: int = 0

    def on_fill(self, fill: Fill) -> None:
        """
        CORREGIDO P1: Callback para fills reales del ledger.

        Args:
            fill: Fill recibido del ledger
        """
        self.last_trade_time = datetime.now()
        logger.debug(f"Circuit breaker received fill: {fill.trade_id} {fill.side}")

    def get_fill_callback(self) -> Callable[[Fill], None]:
        """Retorna el callback para registrar en el ledger."""
        return self.on_fill

    def reset_day(self, equity: Decimal) -> None:
        """Resetear métricas diarias."""
        self.day_start = datetime.now()
        self.equity_day_start = equity
        self.equity_peak = equity
        self.equity_now = equity
        self.consecutive_losses = 0

        self.latency.clear()
        self.execution = ExecutionMetrics()

        logger.info(f"Day reset: equity=${equity:,.2f}")

    def update_equity(self, equity: Decimal) -> None:
        """Actualizar equity actual."""
        self.equity_now = equity

        if equity > self.equity_peak:
            self.equity_peak = equity

    def record_trade_pnl(self, pnl: Decimal) -> None:
        """
        Registrar PnL de un trade (desde el risk engine).

        CORREGIDO P1: Este método debe ser llamado por el risk engine
        después de que el ledger procesa un fill.
        """
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
        """Registrar latencia de una operación."""
        self.latency.add(latency_ms)

        if self.cfg.latency_baseline_ms is None and len(self.latency.samples) >= 20:
            self.cfg.latency_baseline_ms = self.latency.mean()

    def record_execution_result(self, success: bool) -> None:
        """Registrar resultado de ejecución."""
        self.execution.record_request(success)

    def record_rate_limit_hit(self) -> None:
        """Registrar hit de rate limit."""
        self.execution.record_rate_limit_hit()

    def record_ws_gap(self) -> None:
        """Registrar gap en WebSocket."""
        self.execution.record_ws_gap()
        logger.warning("WebSocket gap detected by circuit breaker")

    def record_slippage(self, slippage_bps: float) -> None:
        """Registrar slippage observado."""
        self.execution.record_slippage(slippage_bps)

    def check_before_trade(self) -> Tuple[bool, Optional[str]]:
        """Verificar si se permite trading."""
        # Auto-reset diario
        if datetime.now().date() > self.day_start.date():
            self.reset_day(self.equity_now)

        # Estado OPEN: verificar cooldown
        if self.state == BreakerState.OPEN:
            if self._can_attempt_recovery():
                self.state = BreakerState.HALF_OPEN
                self.recovery_test_count = 0
                self.recovery_success_count = 0
                # Limpiar métricas de ejecución: HALF_OPEN comienza con ventana limpia.
                # El cooldown period es para que la condición subyacente se resuelva.
                self.execution = ExecutionMetrics()
                self.latency.clear()
                logger.info("Circuit breaker -> HALF_OPEN (testing recovery)")
            else:
                remaining = self._recovery_remaining_minutes()
                return False, f"OPEN: {self.trip_reason} (recovery in {remaining}m)"

        # Estado HALF_OPEN: verificar recuperación
        if self.state == BreakerState.HALF_OPEN:
            if self.recovery_success_count >= self.cfg.recovery_test_trades:
                self.state = BreakerState.CLOSED
                self.trip_reason = None
                self.trip_time = None
                logger.info("Circuit breaker recovered -> CLOSED")

        # Verificar límites
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
        """Verificar pérdida diaria."""
        if self.equity_day_start <= 0:
            return False, ""

        daily_return = (self.equity_now / self.equity_day_start) - 1
        if daily_return <= -self.cfg.max_daily_loss:
            return True, f"Daily loss: {daily_return:.2%}"
        return False, ""

    def _check_drawdown(self) -> Tuple[bool, str]:
        """Verificar drawdown máximo."""
        if self.equity_peak <= 0:
            return False, ""

        drawdown = 1 - (self.equity_now / self.equity_peak)
        if drawdown >= self.cfg.max_drawdown:
            return True, f"Max drawdown: {drawdown:.2%}"
        return False, ""

    def _check_consecutive_losses(self) -> Tuple[bool, str]:
        """Verificar pérdidas consecutivas."""
        if self.consecutive_losses >= self.cfg.max_consecutive_losses:
            return True, f"Consecutive losses: {self.consecutive_losses}"
        return False, ""

    def _check_latency(self) -> Tuple[bool, str]:
        """Verificar latencia p95."""
        p95 = self.latency.p95()

        if self.cfg.latency_baseline_ms and self.cfg.latency_baseline_ms > 0:
            if p95 > self.cfg.latency_baseline_ms * 2:
                return True, f"Latency p95: {p95:.0f}ms (2x baseline)"

        if p95 > self.cfg.latency_p95_threshold_ms:
            return (
                True,
                f"Latency p95: {p95:.0f}ms (threshold: {self.cfg.latency_p95_threshold_ms:.0f}ms)",
            )

        return False, ""

    def _check_reject_rate(self) -> Tuple[bool, str]:
        """Verificar tasa de rechazos."""
        reject_rate = self.execution.reject_rate()
        if reject_rate > self.cfg.reject_rate_threshold:
            return True, f"Reject rate: {reject_rate:.2%}"
        return False, ""

    def _check_slippage_drift(self) -> Tuple[bool, str]:
        """Verificar drift de slippage."""
        drift = self.execution.slippage_drift()
        if abs(drift) > self.cfg.slippage_drift_threshold_bps:
            direction = "higher" if drift > 0 else "lower"
            return True, f"Slippage {direction}: {drift:.1f}bps"
        return False, ""

    def _check_rate_limit_hits(self) -> Tuple[bool, str]:
        """Verificar hits de rate limit."""
        if self.execution.rate_limit_hits >= self.cfg.rate_limit_threshold:
            return True, f"Rate limit hits: {self.execution.rate_limit_hits}"
        return False, ""

    def _check_ws_gaps(self) -> Tuple[bool, str]:
        """Verificar gaps en WebSocket."""
        if self.execution.ws_gaps >= self.cfg.ws_gap_threshold:
            return True, f"WebSocket gaps: {self.execution.ws_gaps}"
        return False, ""

    def force_open(self, reason: str) -> None:
        """
        Forzar apertura del circuit breaker desde fuera.

        Usado por: OMS degraded, kill switch, external monitoring.
        """
        self._trip(reason)

    def _trip(self, reason: str) -> Tuple[bool, str]:
        """Disparar circuit breaker."""
        if self.state != BreakerState.OPEN:
            self.state = BreakerState.OPEN
            self.trip_reason = reason
            self.trip_time = datetime.now()
            logger.critical(f"CIRCUIT BREAKER TRIPPED: {reason}")
        return False, reason

    def _can_attempt_recovery(self) -> bool:
        """Verificar si puede intentar recuperación."""
        if not self.trip_time:
            return False
        elapsed = datetime.now() - self.trip_time
        return elapsed >= timedelta(minutes=self.cfg.recovery_cooldown_minutes)

    def _recovery_remaining_minutes(self) -> int:
        """Minutos restantes para recuperación."""
        if not self.trip_time:
            return 0
        elapsed = datetime.now() - self.trip_time
        remaining = timedelta(minutes=self.cfg.recovery_cooldown_minutes) - elapsed
        return max(0, int(remaining.total_seconds() / 60))

    def get_status(self) -> Dict:
        """Obtener estado completo del circuit breaker."""
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
