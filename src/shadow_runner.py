"""
Shadow Runner — orchestrates shadow testing sessions.

Runs full pipeline: data fetch → signal → risk gate → order management,
all with simulated execution. Collects metrics and generates report.
"""

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.backtest.data_feed import Bar
from src.exchange_simulator import ExchangeSimulator
from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot

logger = logging.getLogger("ShadowRunner")


@dataclass
class ShadowMetricsSample:
    """Single metrics sample taken during shadow run."""

    timestamp_ms: int
    equity: float
    drawdown_pct: float
    total_trades: int
    latency_ms: float
    breaker_state: str


@dataclass
class ShadowRunResult:
    """Result of a complete shadow run."""

    duration_s: float
    total_ticks: int
    total_trades: int
    total_signals: int
    signals_blocked: int
    equity_start: float
    equity_end: float
    max_drawdown_pct: float
    avg_latency_ms: float
    breaker_trips: int
    crashes: int
    samples: List[ShadowMetricsSample] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class ShadowRunner:
    """
    Orchestrates shadow testing sessions.

    Runs the trading pipeline with simulated exchange data.
    All trades are paper — nothing touches real exchange.
    """

    def __init__(
        self,
        simulator: ExchangeSimulator,
        risk_limits: Optional[RiskLimits] = None,
        breaker_config: Optional[BreakerConfig] = None,
        initial_cash: float = 10000.0,
        signal_threshold: float = 0.001,
    ) -> None:
        self._sim = simulator
        self._risk_limits = risk_limits or RiskLimits()
        self._gate = RiskGate(self._risk_limits)
        self._breaker = CircuitBreaker(breaker_config or BreakerConfig())
        self._initial_cash = initial_cash
        self._cash = initial_cash
        self._position_qty = 0.0
        self._avg_entry = 0.0
        self._signal_threshold = signal_threshold
        self._equity_peak = initial_cash

        # Tracking
        self._trades: List[Dict[str, Any]] = []
        self._signals_total = 0
        self._signals_blocked = 0
        self._breaker_trips = 0
        self._latencies: List[float] = []
        self._errors: List[str] = []

        self._breaker.reset_day(Decimal(str(initial_cash)))

    def _get_equity(self, price: float) -> float:
        return self._cash + self._position_qty * price

    def _get_drawdown(self, price: float) -> float:
        eq = self._get_equity(price)
        if eq > self._equity_peak:
            self._equity_peak = eq
        if self._equity_peak <= 0:
            return 0.0
        return max(0.0, (self._equity_peak - eq) / self._equity_peak)

    def _generate_signal(self, prices: List[float]) -> Optional[str]:
        """Simple momentum signal from recent prices."""
        if len(prices) < 5:
            return None
        sma_fast = sum(prices[-3:]) / 3
        sma_slow = sum(prices[-5:]) / 5
        if sma_fast > sma_slow * (1 + self._signal_threshold):
            return "BUY"
        if sma_fast < sma_slow * (1 - self._signal_threshold):
            return "SELL"
        return None

    def run(self, duration_s: float = 60.0, tick_interval_s: float = 0.01) -> ShadowRunResult:
        """
        Execute shadow run for given duration.

        Args:
            duration_s: how long to run (seconds)
            tick_interval_s: time between price ticks
        """
        start = time.time()
        prices: List[float] = []
        samples: List[ShadowMetricsSample] = []
        ticks = 0
        crashes = 0
        sample_interval = max(1, int(duration_s / 60))  # ~60 samples

        while time.time() - start < duration_s:
            ticks += 1

            try:
                t0 = time.time()
                ticker = self._sim.get_ticker("BTC-USD")
                latency = (time.time() - t0) * 1000
                self._latencies.append(latency)

                price = float(ticker["price"])
                prices.append(price)

                # Update breaker equity
                equity = self._get_equity(price)
                self._breaker.update_equity(Decimal(str(equity)))

                # Check breaker
                breaker_ok, _ = self._breaker.check_before_trade()
                if not breaker_ok:
                    self._breaker_trips += 1

                # Generate signal
                signal = self._generate_signal(prices)
                if signal:
                    self._signals_total += 1

                    # Risk gate
                    snap = RiskSnapshot(
                        equity=Decimal(str(equity)),
                        position_qty=Decimal(str(self._position_qty)),
                        day_pnl_pct=Decimal("0"),
                        drawdown_pct=Decimal(str(self._get_drawdown(price))),
                        symbol="BTC-USD",
                        side=signal,
                        target_qty=Decimal(str(max(0.001, equity * 0.01 / price))),
                        entry_ref=Decimal(str(price)),
                        breaker_state=self._breaker.state.value.upper(),
                    )
                    verdict = self._gate.evaluate(snap)

                    if verdict.allowed:
                        qty = float(verdict.hard_max_qty)
                        if signal == "SELL" and self._position_qty <= 0:
                            pass  # can't sell if no position
                        elif signal == "SELL":
                            qty = min(qty, self._position_qty)
                            result = self._sim.place_order(
                                f"shadow-{ticks}", "BTC-USD", "SELL", qty,
                            )
                            if result.get("status") == "FILLED":
                                fill_price = float(result["fill_price"])
                                self._cash += qty * fill_price
                                self._position_qty -= qty
                                self._trades.append({
                                    "side": "SELL", "qty": qty,
                                    "price": fill_price, "tick": ticks,
                                })
                        elif signal == "BUY":
                            result = self._sim.place_order(
                                f"shadow-{ticks}", "BTC-USD", "BUY", qty,
                            )
                            if result.get("status") == "FILLED":
                                fill_price = float(result["fill_price"])
                                self._cash -= qty * fill_price
                                self._position_qty += qty
                                self._trades.append({
                                    "side": "BUY", "qty": qty,
                                    "price": fill_price, "tick": ticks,
                                })
                    else:
                        self._signals_blocked += 1

                # Sample metrics periodically
                if ticks % sample_interval == 0:
                    samples.append(ShadowMetricsSample(
                        timestamp_ms=int(time.time() * 1000),
                        equity=self._get_equity(price),
                        drawdown_pct=self._get_drawdown(price),
                        total_trades=len(self._trades),
                        latency_ms=latency,
                        breaker_state=self._breaker.state.value,
                    ))

            except Exception as e:
                crashes += 1
                self._errors.append(f"tick {ticks}: {e}")
                logger.error("Shadow tick %d error: %s", ticks, e)

            time.sleep(tick_interval_s)

        elapsed = time.time() - start
        final_price = prices[-1] if prices else self._initial_cash

        return ShadowRunResult(
            duration_s=elapsed,
            total_ticks=ticks,
            total_trades=len(self._trades),
            total_signals=self._signals_total,
            signals_blocked=self._signals_blocked,
            equity_start=self._initial_cash,
            equity_end=self._get_equity(final_price),
            max_drawdown_pct=max((s.drawdown_pct for s in samples), default=0.0),
            avg_latency_ms=sum(self._latencies) / len(self._latencies) if self._latencies else 0.0,
            breaker_trips=self._breaker_trips,
            crashes=crashes,
            samples=samples,
            errors=self._errors,
        )
