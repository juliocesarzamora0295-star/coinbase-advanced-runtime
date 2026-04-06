"""
Shadow Report — analyzes shadow run results and emits pass/fail certification.
"""

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from src.shadow_runner import ShadowRunResult


@dataclass(frozen=True)
class ShadowCriteria:
    """Configurable pass/fail thresholds."""

    max_drawdown_pct: float = 5.0
    min_sharpe: float = 0.0
    max_avg_latency_ms: float = 500.0
    max_crashes: int = 0
    min_trades: int = 1


@dataclass(frozen=True)
class ShadowReport:
    """Analyzed shadow run report."""

    passed: bool
    failures: List[str]
    sharpe_ratio: float
    max_drawdown_pct: float
    total_trades: int
    avg_latency_ms: float
    crashes: int
    equity_start: float
    equity_end: float
    return_pct: float
    duration_s: float
    total_ticks: int
    signals_total: int
    signals_blocked: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "passed": self.passed,
            "failures": self.failures,
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "total_trades": self.total_trades,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "crashes": self.crashes,
            "equity_start": round(self.equity_start, 2),
            "equity_end": round(self.equity_end, 2),
            "return_pct": round(self.return_pct, 4),
            "duration_s": round(self.duration_s, 2),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"Shadow Report: {status}",
            f"  Duration: {self.duration_s:.0f}s, Ticks: {self.total_ticks}",
            f"  Trades: {self.total_trades}, Signals: {self.signals_total} "
            f"(blocked: {self.signals_blocked})",
            f"  Equity: ${self.equity_start:,.2f} → ${self.equity_end:,.2f} "
            f"({self.return_pct:+.2%})",
            f"  Sharpe: {self.sharpe_ratio:.2f}, MaxDD: {self.max_drawdown_pct:.2%}",
            f"  Latency: {self.avg_latency_ms:.1f}ms, Crashes: {self.crashes}",
        ]
        if self.failures:
            lines.append(f"  Failures: {', '.join(self.failures)}")
        return "\n".join(lines)


def analyze_shadow_run(
    result: ShadowRunResult,
    criteria: Optional[ShadowCriteria] = None,
) -> ShadowReport:
    """
    Analyze a ShadowRunResult and produce a ShadowReport with pass/fail.

    Args:
        result: output from ShadowRunner.run()
        criteria: pass/fail thresholds
    """
    criteria = criteria or ShadowCriteria()
    failures: List[str] = []

    # Sharpe from equity samples
    returns = []
    for i in range(1, len(result.samples)):
        prev = result.samples[i - 1].equity
        curr = result.samples[i].equity
        if prev > 0:
            returns.append((curr - prev) / prev)

    sharpe = 0.0
    if len(returns) >= 2:
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean_r / std) * math.sqrt(252) if std > 0 else 0.0

    return_pct = 0.0
    if result.equity_start > 0:
        return_pct = (result.equity_end - result.equity_start) / result.equity_start

    # Apply criteria
    if result.max_drawdown_pct * 100 > criteria.max_drawdown_pct:
        failures.append(
            f"max_dd={result.max_drawdown_pct:.2%} > {criteria.max_drawdown_pct}%"
        )
    if sharpe < criteria.min_sharpe:
        failures.append(f"sharpe={sharpe:.2f} < {criteria.min_sharpe}")
    if result.avg_latency_ms > criteria.max_avg_latency_ms:
        failures.append(
            f"avg_latency={result.avg_latency_ms:.0f}ms > {criteria.max_avg_latency_ms}ms"
        )
    if result.crashes > criteria.max_crashes:
        failures.append(f"crashes={result.crashes} > {criteria.max_crashes}")
    if result.total_trades < criteria.min_trades:
        failures.append(f"trades={result.total_trades} < {criteria.min_trades}")

    return ShadowReport(
        passed=len(failures) == 0,
        failures=failures,
        sharpe_ratio=sharpe,
        max_drawdown_pct=result.max_drawdown_pct,
        total_trades=result.total_trades,
        avg_latency_ms=result.avg_latency_ms,
        crashes=result.crashes,
        equity_start=result.equity_start,
        equity_end=result.equity_end,
        return_pct=return_pct,
        duration_s=result.duration_s,
        total_ticks=result.total_ticks,
        signals_total=result.total_signals,
        signals_blocked=result.signals_blocked,
    )
