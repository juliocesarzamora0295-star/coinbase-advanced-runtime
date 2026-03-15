"""
Gem Protocol - Validación robusta de estrategias de trading.

Adaptado de GuardianBot para Fortress v4:
- Uso de Decimal para precisión
- Integración con TradeLedger
- Métricas de riesgo mejoradas
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Any

import numpy as np
import pandas as pd

try:
    from statsmodels.tsa.stattools import adfuller  # type: ignore
    _HAS_ADF = True
except Exception:
    _HAS_ADF = False


@dataclass
class GemConfig:
    """Configuración para validación GemProtocol."""
    commissions: Decimal = Decimal("0.001")
    slippage: Decimal = Decimal("0.0005")
    risk_free_rate: Decimal = Decimal("0.02")

    max_dd_threshold: Decimal = Decimal("0.25")
    recovery_factor_threshold: Decimal = Decimal("1.5")

    hurst_trend: float = 0.55
    hurst_meanrev: float = 0.45
    vr_trend: float = 1.05
    vr_meanrev: float = 0.95

    stress_periods: Dict[str, Tuple[str, str]] = field(default_factory=lambda: {
        "covid_crisis": ("2020-02-15", "2020-04-15"),
        "luna_collapse": ("2022-05-01", "2022-06-15"),
        "ftx_collapse": ("2022-11-01", "2022-12-15"),
        "bear_2022": ("2022-01-01", "2022-12-31"),
    })

    bootstrap_runs: int = 300
    bootstrap_block: int = 20
    signal_flip_rate: float = 0.03
    min_points: int = 200

    direction: str = "long"
    init_cash: Decimal = Decimal("10000.0")
    freq_per_year: float = 252.0


def _max_drawdown(equity: pd.Series) -> Decimal:
    """Calcular máximo drawdown."""
    peak = equity.cummax()
    drawdown = (equity / peak) - 1.0
    return Decimal(str(drawdown.min()))


def _sharpe(returns: pd.Series, rf: Decimal, freq: float) -> Decimal:
    """Calcular ratio de Sharpe."""
    r = returns.dropna()
    if len(r) < 2:
        return Decimal("0")
    rf_per = (1.0 + float(rf)) ** (1.0 / freq) - 1.0
    excess = r - rf_per
    mu = float(excess.mean())
    sd = float(excess.std(ddof=1))
    if sd <= 0:
        return Decimal("0")
    return Decimal(str((mu / sd) * math.sqrt(freq)))


def _calmar(cagr: Decimal, max_dd_abs: Decimal) -> Decimal:
    """Calcular ratio de Calmar."""
    if max_dd_abs <= 0:
        return Decimal("inf")
    return cagr / max_dd_abs


def _cagr(equity: pd.Series, freq: float) -> Decimal:
    """Calcular CAGR."""
    e = equity.dropna()
    if len(e) < 2:
        return Decimal("0")
    years = len(e) / freq
    if years <= 0:
        return Decimal("0")
    return Decimal(str((e.iloc[-1] / e.iloc[0]) ** (1.0 / years) - 1.0))


def _hurst_exponent(x: np.ndarray, max_lag: int = 100) -> float:
    """Calcular exponente de Hurst."""
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if len(x) < 500:
        return float("nan")

    lags = np.arange(2, max_lag)
    tau = []
    for lag in lags:
        diff = x[lag:] - x[:-lag]
        tau.append(np.sqrt(np.var(diff)))
    tau = np.asarray(tau)
    if np.any(tau <= 0):
        return float("nan")
    poly = np.polyfit(np.log(lags), np.log(tau), 1)
    return float(2.0 * poly[0])


def _variance_ratio(returns: np.ndarray, k: int = 10) -> float:
    """Calcular variance ratio."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < (k * 50):
        return float("nan")
    var1 = np.var(r, ddof=1)
    if var1 <= 0:
        return float("nan")
    rk = np.add.reduceat(r, np.arange(0, len(r) - (len(r) % k), k))
    vark = np.var(rk, ddof=1)
    return float(vark / (k * var1))


def _align_inputs(
    price: pd.Series, entries: pd.Series, exits: pd.Series
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Alinear inputs para backtest."""
    if not isinstance(price.index, pd.DatetimeIndex):
        raise ValueError("price must have DatetimeIndex")
    price = price.astype(float).dropna()
    entries = entries.reindex(price.index).fillna(False).astype(bool)
    exits = exits.reindex(price.index).fillna(False).astype(bool)
    return price, entries, exits


def _backtest_long_only(
    price: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    *,
    init_cash: Decimal,
    fee: Decimal,
    slip: Decimal,
) -> Dict[str, Any]:
    """Backtest long-only con fees y slippage."""
    idx = price.index
    pos = 0
    cash = float(init_cash)
    qty = 0.0
    equity = np.zeros(len(price), dtype=float)

    trades: List[Dict] = []
    entry_px = None
    entry_t = None

    for i, (t, px) in enumerate(zip(idx, price.values)):
        if pos == 0 and entries.iloc[i]:
            exec_px = px * (1.0 + float(slip))
            if exec_px > 0 and cash > 0:
                qty = cash / exec_px
                cost = cash * float(fee)
                cash = cash - cost
                pos = 1
                entry_px = exec_px
                entry_t = t

        elif pos == 1 and exits.iloc[i]:
            exec_px = px * (1.0 - float(slip))
            if exec_px > 0 and qty > 0:
                gross = qty * exec_px
                cost = gross * float(fee)
                cash = gross - cost
                if entry_px is not None:
                    ret = (exec_px - entry_px) / entry_px
                    trades.append({
                        "entry_time": entry_t,
                        "exit_time": t,
                        "entry_px": float(entry_px),
                        "exit_px": float(exec_px),
                        "return": float(ret),
                    })
                qty = 0.0
                pos = 0
                entry_px = None
                entry_t = None

        equity[i] = cash if pos == 0 else (qty * px)

    equity_s = pd.Series(equity, index=idx, name="equity")
    rets = equity_s.pct_change().fillna(0.0)

    mdd = _max_drawdown(equity_s)
    mdd_abs = abs(mdd)

    cagr = _cagr(equity_s, freq=252.0)
    sharpe = _sharpe(rets, rf=Decimal("0"), freq=252.0)
    calmar = _calmar(cagr, mdd_abs)

    total_return = Decimal(str(equity_s.iloc[-1] / equity_s.iloc[0] - 1.0))

    if mdd_abs > 0:
        recovery_factor = total_return / mdd_abs
    else:
        recovery_factor = Decimal("inf") if total_return > 0 else Decimal("-inf")

    tr = pd.DataFrame(trades)
    ntr = len(tr)
    if ntr > 0:
        wins = (tr["return"] > 0).sum()
        win_rate = float(wins / ntr)
        gross_win = tr.loc[tr["return"] > 0, "return"].sum()
        gross_loss = tr.loc[tr["return"] < 0, "return"].sum()
        profit_factor = float(gross_win / abs(gross_loss)) if gross_loss < 0 else float("inf")

        if ntr >= 10 and float(tr["return"].std(ddof=1)) > 0:
            sqn = float(tr["return"].mean() / tr["return"].std(ddof=1) * math.sqrt(ntr))
        else:
            sqn = 0.0
    else:
        win_rate = 0.0
        profit_factor = 0.0
        sqn = 0.0

    return {
        "equity": equity_s,
        "returns": rets,
        "trades": tr,
        "total_return": total_return,
        "max_drawdown": mdd,
        "max_drawdown_abs": mdd_abs,
        "cagr": cagr,
        "sharpe": sharpe,
        "calmar": calmar,
        "recovery_factor": recovery_factor,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "sqn": sqn,
        "total_trades": int(ntr),
    }


class GemProtocol:
    """
    Protocolo de validación de estrategias Gem.
    
    Realiza análisis completo de una estrategia:
    1. Análisis de régimen (Hurst, Variance Ratio, ADF)
    2. Backtest base con métricas de riesgo
    3. Stress testing en períodos de crisis
    4. Testing adversarial (bootstrap, jackknife, noise)
    5. Decisión final con recomendaciones
    """

    def __init__(
        self,
        price: pd.Series,
        entries: pd.Series,
        exits: pd.Series,
        config: Optional[Dict] = None,
    ) -> None:
        self.cfg = GemConfig(**(config or {}))
        self.price, self.entries, self.exits = _align_inputs(price, entries, exits)

        if len(self.price) < self.cfg.min_points:
            raise ValueError(
                f"min_points insuficiente: {len(self.price)} < {self.cfg.min_points}"
            )

        if self.cfg.direction != "long":
            raise ValueError("Con entries/exits únicos solo se soporta direction='long'")

        self.regime: Dict = {}
        self.base: Dict = {}
        self.stress: Dict[str, Dict] = {}
        self.adv: Dict = {}
        self.decision: Dict = {}

    def run(self) -> Dict:
        """Ejecutar validación completa."""
        self._analyze_regime()
        self._run_base_backtest()
        self._run_stress()
        self._run_adversarial()
        self._decide()
        return self.decision

    def _analyze_regime(self) -> None:
        """Analizar régimen del mercado."""
        px = self.price
        ret = np.log(px / px.shift(1)).dropna().values

        hurst = _hurst_exponent(ret, max_lag=100)
        variance_ratio = _variance_ratio(ret, k=10)

        adf_p = None
        if _HAS_ADF:
            try:
                adf_p = float(adfuller(np.asarray(ret, dtype=float), autolag="AIC")[1])
            except Exception:
                adf_p = None

        regime = "UNKNOWN"
        if (
            np.isfinite(hurst)
            and hurst >= self.cfg.hurst_trend
            and (np.isfinite(variance_ratio) and variance_ratio >= self.cfg.vr_trend)
        ):
            regime = "TREND"
        elif (
            np.isfinite(hurst)
            and hurst <= self.cfg.hurst_meanrev
            and (np.isfinite(variance_ratio) and variance_ratio <= self.cfg.vr_meanrev)
        ):
            regime = "MEAN_REVERSION"
        else:
            regime = "RANDOM_WALK"

        self.regime = {
            "hurst": float(hurst) if np.isfinite(hurst) else None,
            "variance_ratio": float(variance_ratio) if np.isfinite(variance_ratio) else None,
            "adf_pvalue_returns": adf_p,
            "current_regime": regime,
        }

    def _run_base_backtest(self) -> None:
        """Ejecutar backtest base."""
        self.base = _backtest_long_only(
            self.price,
            self.entries,
            self.exits,
            init_cash=self.cfg.init_cash,
            fee=self.cfg.commissions,
            slip=self.cfg.slippage,
        )

    def _run_stress(self) -> None:
        """Ejecutar stress testing."""
        for name, (start, end) in self.cfg.stress_periods.items():
            mask = (self.price.index >= pd.Timestamp(start)) & (
                self.price.index <= pd.Timestamp(end)
            )
            if mask.sum() < 50:
                continue
            p = self.price.loc[mask]
            en = self.entries.loc[mask]
            ex = self.exits.loc[mask]
            res = _backtest_long_only(
                p,
                en,
                ex,
                init_cash=self.cfg.init_cash,
                fee=self.cfg.commissions * Decimal("1.5"),
                slip=self.cfg.slippage * Decimal("2.0"),
            )
            self.stress[name] = {
                "total_return": float(res["total_return"]),
                "max_drawdown_abs": float(res["max_drawdown_abs"]),
                "recovery_factor": float(res["recovery_factor"]),
                "win_rate": res["win_rate"],
                "total_trades": res["total_trades"],
            }

    def _run_adversarial(self) -> None:
        """Ejecutar testing adversarial."""
        eq = self.base["equity"]
        r = eq.pct_change().dropna().values
        block = self.cfg.bootstrap_block
        n = len(r)

        def sample_path() -> pd.Series:
            out = []
            while len(out) < n:
                start = np.random.randint(0, max(1, n - block))
                out.extend(r[start : start + block].tolist())
            out = np.array(out[:n], dtype=float)
            e = np.empty(n + 1, dtype=float)
            e[0] = float(self.cfg.init_cash)
            e[1:] = e[0] * np.cumprod(1.0 + out)
            return pd.Series(e, index=eq.index)

        rf_list = []
        dd_list = []
        for _ in range(self.cfg.bootstrap_runs):
            e = sample_path()
            dd = abs(_max_drawdown(e))
            tr = float(e.iloc[-1] / e.iloc[0] - 1.0)
            rf = tr / float(dd) if dd > 0 else (float("inf") if tr > 0 else float("-inf"))
            rf_list.append(rf)
            dd_list.append(float(dd))

        en = self.entries.copy()
        ex = self.exits.copy()
        length = len(en)
        k = int(length * self.cfg.signal_flip_rate)
        if k > 0:
            idx = np.random.choice(np.arange(length), size=k, replace=False)
            en.iloc[idx] = ~en.iloc[idx]
            idx2 = np.random.choice(np.arange(length), size=k, replace=False)
            ex.iloc[idx2] = ~ex.iloc[idx2]
        noisy = _backtest_long_only(
            self.price,
            en,
            ex,
            init_cash=self.cfg.init_cash,
            fee=self.cfg.commissions,
            slip=self.cfg.slippage,
        )

        years = sorted(set(self.price.index.year))
        worst = None
        for year in years:
            mask = self.price.index.year != year
            if mask.sum() < self.cfg.min_points:
                continue
            res = _backtest_long_only(
                self.price.loc[mask],
                self.entries.loc[mask],
                self.exits.loc[mask],
                init_cash=self.cfg.init_cash,
                fee=self.cfg.commissions,
                slip=self.cfg.slippage,
            )
            rf = res["recovery_factor"]
            if (worst is None) or (rf < worst["rf"]):
                worst = {
                    "dropped_year": int(year),
                    "rf": float(rf),
                    "mdd": float(res["max_drawdown_abs"]),
                    "ret": float(res["total_return"]),
                }

        self.adv = {
            "bootstrap_rf_p05": float(np.nanpercentile(rf_list, 5)),
            "bootstrap_rf_p50": float(np.nanpercentile(rf_list, 50)),
            "bootstrap_dd_p95": float(np.nanpercentile(dd_list, 95)),
            "noisy_recovery_factor": float(noisy["recovery_factor"]),
            "noisy_max_dd": float(noisy["max_drawdown_abs"]),
            "jackknife_worst": worst,
        }

    def _decide(self) -> None:
        """Tomar decisión final."""
        issues = []
        recs = []

        rf = float(self.base["recovery_factor"])
        mdd = float(self.base["max_drawdown_abs"])

        passed = True

        if rf < float(self.cfg.recovery_factor_threshold):
            passed = False
            issues.append(
                f"Recovery Factor bajo: {rf:.2f} < {self.cfg.recovery_factor_threshold}"
            )

        if mdd > float(self.cfg.max_dd_threshold):
            passed = False
            issues.append(
                f"Drawdown alto: {mdd*100:.1f}% > {float(self.cfg.max_dd_threshold)*100:.1f}%"
            )

        for name, s in self.stress.items():
            if s["max_drawdown_abs"] > 0.35:
                passed = False
                issues.append(
                    f"Crisis DD catastrófico {name}: {s['max_drawdown_abs']*100:.1f}%"
                )

        if self.adv["bootstrap_rf_p05"] < 0.5:
            issues.append(
                f"Bootstrap RF p05 débil: {self.adv['bootstrap_rf_p05']:.2f} (fragilidad)"
            )
        if self.adv["noisy_recovery_factor"] < rf * 0.7:
            issues.append("Alta sensibilidad a ruido de señales (RF cae >30%)")

        if self.regime.get("current_regime") == "RANDOM_WALK":
            recs.append(
                "Régimen aleatorio: reduce frecuencia/operar solo con filtros de volatilidad/tendencia."
            )
        if rf < 0:
            recs.append("RF negativo: edge inexistente. Cambiar lógica, no parámetros.")

        self.decision = {
            "passed": bool(passed),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "regime": self.regime,
            "base": {
                "total_return": float(self.base["total_return"]),
                "max_drawdown_abs": float(self.base["max_drawdown_abs"]),
                "recovery_factor": float(self.base["recovery_factor"]),
                "sharpe": float(self.base["sharpe"]),
                "calmar": float(self.base["calmar"]),
                "win_rate": self.base["win_rate"],
                "profit_factor": self.base["profit_factor"],
                "sqn": self.base["sqn"],
                "total_trades": self.base["total_trades"],
            },
            "stress": self.stress,
            "adversarial": self.adv,
            "issues": issues,
            "recommendations": recs,
        }

    def to_json(self) -> str:
        """Exportar decisión a JSON."""
        return json.dumps(self.decision, indent=2, ensure_ascii=False)
