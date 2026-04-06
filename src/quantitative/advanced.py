"""
Advanced quantitative analytics — extended metrics, walk-forward,
robustness checks, anti-leakage controls.
"""

import math
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, List, Optional, Tuple

ZERO = Decimal("0")


# ──────────────────────────────────────────────
# C) Extended metrics
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class ExtendedMetrics:
    """Institutional-grade performance metrics."""

    sortino_ratio: float
    calmar_ratio: float
    skewness: float
    kurtosis: float
    max_consecutive_losses: int
    recovery_factor: float  # total_pnl / max_drawdown_abs
    time_in_market_pct: float  # fraction of bars with open position
    turnover_rate: float  # trades per bar


def compute_extended_metrics(
    returns: List[float],
    trades_per_bar: List[bool],
    total_pnl: float,
    max_drawdown_abs: float,
    trade_pnls: List[float],
    periods_per_year: int = 252,
) -> ExtendedMetrics:
    """
    Compute extended metrics from return series and trade data.

    Args:
        returns: list of per-period returns (floats)
        trades_per_bar: list of bool (True if position was open that bar)
        total_pnl: total PnL in quote currency
        max_drawdown_abs: max drawdown in absolute terms (quote)
        trade_pnls: list of PnL per trade
        periods_per_year: annualization factor
    """
    n = len(returns)

    # Sortino ratio (downside deviation only)
    mean_r = sum(returns) / n if n > 0 else 0.0
    downside = [min(0.0, r - mean_r) ** 2 for r in returns]
    downside_dev = math.sqrt(sum(downside) / max(n - 1, 1))
    sortino = (mean_r / downside_dev * math.sqrt(periods_per_year)) if downside_dev > 0 else 0.0

    # Calmar ratio: annualized return / max drawdown
    ann_return = mean_r * periods_per_year if n > 0 else 0.0
    calmar = ann_return / max_drawdown_abs if max_drawdown_abs > 0 else 0.0

    # Skewness & kurtosis
    skew = _skewness(returns)
    kurt = _kurtosis(returns)

    # Max consecutive losses
    max_consec = _max_consecutive_losses(trade_pnls)

    # Recovery factor
    recovery = total_pnl / max_drawdown_abs if max_drawdown_abs > 0 else 0.0

    # Time in market
    in_market = sum(1 for t in trades_per_bar if t)
    time_pct = in_market / len(trades_per_bar) if trades_per_bar else 0.0

    # Turnover
    n_trades = len(trade_pnls)
    turnover = n_trades / n if n > 0 else 0.0

    return ExtendedMetrics(
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        skewness=skew,
        kurtosis=kurt,
        max_consecutive_losses=max_consec,
        recovery_factor=recovery,
        time_in_market_pct=time_pct,
        turnover_rate=turnover,
    )


def _skewness(data: List[float]) -> float:
    n = len(data)
    if n < 3:
        return 0.0
    mean = sum(data) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in data) / (n - 1))
    if std == 0:
        return 0.0
    return (n / ((n - 1) * (n - 2))) * sum(((x - mean) / std) ** 3 for x in data)


def _kurtosis(data: List[float]) -> float:
    n = len(data)
    if n < 4:
        return 0.0
    mean = sum(data) / n
    std = math.sqrt(sum((x - mean) ** 2 for x in data) / (n - 1))
    if std == 0:
        return 0.0
    m4 = sum(((x - mean) / std) ** 4 for x in data) / n
    return m4 - 3.0  # excess kurtosis


def _max_consecutive_losses(pnls: List[float]) -> int:
    max_streak = 0
    current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


# ──────────────────────────────────────────────
# A) Walk-forward OOS validation
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class WalkForwardWindow:
    """Result of one walk-forward window."""

    window_idx: int
    train_bars: int
    test_bars: int
    train_pnl: float
    test_pnl: float
    train_trades: int
    test_trades: int
    oos_degradation: float  # (train_pnl - test_pnl) / abs(train_pnl) if train > 0


@dataclass(frozen=True)
class WalkForwardResult:
    """Aggregated walk-forward result."""

    windows: List[WalkForwardWindow]
    total_oos_pnl: float
    total_is_pnl: float
    avg_oos_degradation: float
    overfitting_detected: bool  # True if avg degradation > threshold


def walk_forward_validate(
    bars: list,
    run_backtest_fn: Callable,
    train_size: int = 100,
    test_size: int = 50,
    overfitting_threshold: float = 0.5,
) -> WalkForwardResult:
    """
    Rolling walk-forward validation.

    Args:
        bars: full list of bars
        run_backtest_fn: callable(bars) -> (report, ledger)
        train_size: bars per training window
        test_size: bars per test window
        overfitting_threshold: avg OOS degradation above this = overfitting
    """
    windows: List[WalkForwardWindow] = []
    total_bars = len(bars)
    idx = 0
    window_num = 0

    while idx + train_size + test_size <= total_bars:
        train_bars = bars[idx : idx + train_size]
        test_bars = bars[idx + train_size : idx + train_size + test_size]

        train_report, _ = run_backtest_fn(train_bars)
        test_report, _ = run_backtest_fn(test_bars)

        train_pnl = float(train_report.total_pnl)
        test_pnl = float(test_report.total_pnl)

        degradation = 0.0
        if abs(train_pnl) > 0.01:
            degradation = (train_pnl - test_pnl) / abs(train_pnl)

        windows.append(WalkForwardWindow(
            window_idx=window_num,
            train_bars=len(train_bars),
            test_bars=len(test_bars),
            train_pnl=train_pnl,
            test_pnl=test_pnl,
            train_trades=train_report.total_trades,
            test_trades=test_report.total_trades,
            oos_degradation=degradation,
        ))

        idx += test_size  # roll forward by test_size
        window_num += 1

    total_oos = sum(w.test_pnl for w in windows)
    total_is = sum(w.train_pnl for w in windows)
    avg_deg = (
        sum(w.oos_degradation for w in windows) / len(windows)
        if windows else 0.0
    )

    return WalkForwardResult(
        windows=windows,
        total_oos_pnl=total_oos,
        total_is_pnl=total_is,
        avg_oos_degradation=avg_deg,
        overfitting_detected=avg_deg > overfitting_threshold,
    )


# ──────────────────────────────────────────────
# B) Robustness checks
# ──────────────────────────────────────────────


def monte_carlo_permutation_test(
    strategy_pnl: float,
    returns: List[float],
    n_permutations: int = 1000,
    seed: int = 42,
) -> Tuple[float, float]:
    """
    Monte Carlo permutation test for strategy alpha.

    Shuffle the return series N times, compute PnL each time.
    p-value = fraction of shuffled PnLs >= strategy PnL.

    Returns:
        (p_value, percentile_rank)
    """
    rng = random.Random(seed)
    shuffled_pnls = []
    for _ in range(n_permutations):
        shuffled = returns[:]
        rng.shuffle(shuffled)
        shuffled_pnls.append(sum(shuffled))

    better_count = sum(1 for sp in shuffled_pnls if sp >= strategy_pnl)
    p_value = better_count / n_permutations
    percentile = sum(1 for sp in shuffled_pnls if sp < strategy_pnl) / n_permutations

    return p_value, percentile


def bootstrap_confidence_interval(
    values: List[float],
    n_bootstrap: int = 1000,
    confidence: float = 0.95,
    seed: int = 42,
) -> Tuple[float, float, float]:
    """
    Bootstrap confidence interval for the mean.

    Returns:
        (mean, ci_lower, ci_upper)
    """
    if not values:
        return 0.0, 0.0, 0.0

    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(n_bootstrap):
        sample = [rng.choice(values) for _ in range(n)]
        means.append(sum(sample) / n)

    means.sort()
    alpha = (1 - confidence) / 2
    lo_idx = int(alpha * n_bootstrap)
    hi_idx = int((1 - alpha) * n_bootstrap) - 1

    return sum(values) / n, means[lo_idx], means[hi_idx]


# ──────────────────────────────────────────────
# D) Anti-leakage controls
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class LeakageCheckResult:
    """Result of anti-leakage validation."""

    clean: bool
    issues: List[str]


def check_lookahead_bias(
    bars: list,
    strategy_fn: Callable,
) -> LeakageCheckResult:
    """
    Detect look-ahead bias by running strategy with truncated history.

    The strategy should only see bars up to current index.
    If adding future bars changes past decisions, there's leakage.
    """
    issues = []

    # Run with full data
    full_signals = []
    history: list = []
    for bar in bars:
        sig = strategy_fn(bar, history)
        full_signals.append(sig)
        history.append(bar)

    # Run with only first half
    half = len(bars) // 2
    half_signals = []
    history2: list = []
    for bar in bars[:half]:
        sig = strategy_fn(bar, history2)
        half_signals.append(sig)
        history2.append(bar)

    # Compare: first-half signals should be identical
    for i in range(half):
        fs = full_signals[i]
        hs = half_signals[i]
        if fs is None and hs is None:
            continue
        if (fs is None) != (hs is None):
            issues.append(f"bar {i}: signal differs (full={fs}, half={hs})")
            break
        if fs and hs and (fs.side != hs.side or fs.qty != hs.qty):
            issues.append(f"bar {i}: signal differs")
            break

    return LeakageCheckResult(clean=len(issues) == 0, issues=issues)


def check_data_snooping(
    n_parameters: int,
    n_data_points: int,
    threshold_ratio: float = 10.0,
) -> LeakageCheckResult:
    """
    Check for data snooping risk.

    Rule of thumb: need at least N*threshold_ratio data points per parameter.
    """
    issues = []
    required = n_parameters * threshold_ratio
    if n_data_points < required:
        issues.append(
            f"Data snooping risk: {n_parameters} params with {n_data_points} points "
            f"(need {required:.0f} for ratio {threshold_ratio})"
        )
    return LeakageCheckResult(clean=len(issues) == 0, issues=issues)


# ──────────────────────────────────────────────
# Integrated certification protocol
# ──────────────────────────────────────────────


@dataclass(frozen=True)
class CertificationResult:
    """Result of full strategy certification."""

    passed: bool
    metrics_pass: bool
    metrics_failures: List[str]
    walk_forward_pass: bool
    overfitting_detected: bool
    oos_degradation: float
    leakage_clean: bool
    leakage_issues: List[str]
    total_oos_pnl: float
    total_is_pnl: float


def certify_strategy(
    bars: list,
    run_backtest_fn: Callable,
    *,
    train_size: int = 100,
    test_size: int = 50,
    min_sharpe: float = 0.0,
    max_drawdown: float = 0.50,
    min_trades: int = 5,
    min_profit_factor: float = 0.5,
    max_consecutive_losses: int = 20,
    overfitting_threshold: float = 0.5,
    n_parameters: int = 2,
    strategy_fn: Optional[Callable] = None,
) -> CertificationResult:
    """
    Integrated certification: walk-forward + metrics + anti-leakage.

    Returns CertificationResult with combined pass/fail.
    """
    from src.quantitative.metrics import TradeRecord, compute_metrics

    # 1. Walk-forward OOS
    wf = walk_forward_validate(
        bars, run_backtest_fn,
        train_size=train_size, test_size=test_size,
        overfitting_threshold=overfitting_threshold,
    )

    # 2. Full-sample metrics
    full_report, full_ledger = run_backtest_fn(bars)
    trades = [
        TradeRecord(pnl=t.pnl, duration_ms=t.duration_ms)
        for t in full_ledger.trades
    ]
    metrics = compute_metrics(trades, full_ledger.equity_curve, full_ledger.initial_cash)
    metrics_ok, metrics_failures = metrics.passes_certification(
        min_sharpe=min_sharpe, max_drawdown=max_drawdown,
        min_trades=min_trades, min_profit_factor=min_profit_factor,
        max_consecutive_losses=max_consecutive_losses,
    )

    # 3. Anti-leakage
    snooping = check_data_snooping(n_parameters=n_parameters, n_data_points=len(bars))
    leakage_issues = list(snooping.issues)
    if strategy_fn is not None:
        lookahead = check_lookahead_bias(bars, strategy_fn)
        leakage_issues.extend(lookahead.issues)
    leakage_clean = len(leakage_issues) == 0

    # Combined
    wf_pass = not wf.overfitting_detected and len(wf.windows) > 0
    passed = metrics_ok and wf_pass and leakage_clean

    return CertificationResult(
        passed=passed, metrics_pass=metrics_ok,
        metrics_failures=metrics_failures,
        walk_forward_pass=wf_pass,
        overfitting_detected=wf.overfitting_detected,
        oos_degradation=wf.avg_oos_degradation,
        leakage_clean=leakage_clean,
        leakage_issues=leakage_issues,
        total_oos_pnl=wf.total_oos_pnl,
        total_is_pnl=wf.total_is_pnl,
    )
