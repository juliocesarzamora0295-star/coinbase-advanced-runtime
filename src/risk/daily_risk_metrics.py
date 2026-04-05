"""
DailyRiskMetrics — métricas de riesgo diario deterministas.

Separa claramente:
    drawdown_pct  = (equity_peak - equity_current) / equity_peak
    daily_pnl     = equity_current - equity_day_start
    daily_pnl_pct = daily_pnl / equity_day_start

Ningún valor depende de heurísticas sobre fills o timestamps de trades.
La única entrada es equity_current en cada tick.

Uso típico:
    tracker = DailyRiskTracker.from_equity(initial_equity)
    ...
    metrics = tracker.update(portfolio_snapshot.equity)
    risk_snap = RiskSnapshot(
        equity=metrics.equity_current,
        drawdown_pct=metrics.drawdown_pct,
        day_pnl_pct=metrics.daily_pnl_pct,
        ...
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

_MS_PER_DAY = 24 * 60 * 60 * 1000


def _utc_day_start_ms(ts_ms: int) -> int:
    """Calcular el timestamp UTC de las 00:00:00.000 del día que contiene ts_ms."""
    return ts_ms - (ts_ms % _MS_PER_DAY)


@dataclass(frozen=True)
class DailyRiskMetrics:
    """
    Snapshot inmutable de métricas de riesgo diario.

    Todos los valores en QUOTE currency (ej: USD para BTC-USD).
    drawdown y daily_pnl son deterministas: dependen exclusivamente de
    equity_day_start, equity_peak y equity_current.
    """

    equity_day_start: Decimal  # Equity al inicio del día UTC 00:00
    equity_peak: Decimal       # Equity máximo registrado desde inicio del día
    equity_current: Decimal    # Equity en el momento de este snapshot
    day_start_ts_ms: int       # Timestamp UTC del inicio del día (ms)

    @property
    def daily_pnl(self) -> Decimal:
        """PnL absoluto del día: equity_current - equity_day_start."""
        return self.equity_current - self.equity_day_start

    @property
    def daily_pnl_pct(self) -> Decimal:
        """
        PnL del día como fracción de equity_day_start.

        Retorna 0 si equity_day_start <= 0 (fail-closed: no divide por cero).
        """
        if self.equity_day_start <= Decimal("0"):
            return Decimal("0")
        return self.daily_pnl / self.equity_day_start

    @property
    def drawdown_pct(self) -> Decimal:
        """
        Drawdown desde el pico del día.

        drawdown = (equity_peak - equity_current) / equity_peak

        Acotado a [0, 1]. Si equity_peak <= 0, retorna 0 (fail-closed).
        Si equity_current > equity_peak (no debería ocurrir en flujo normal),
        retorna 0 en lugar de un valor negativo.
        """
        if self.equity_peak <= Decimal("0"):
            return Decimal("0")
        dd = (self.equity_peak - self.equity_current) / self.equity_peak
        return max(dd, Decimal("0"))

    @property
    def is_at_peak(self) -> bool:
        """True si equity_current == equity_peak (en el máximo del día)."""
        return self.equity_current >= self.equity_peak


class DailyRiskTracker:
    """
    Tracker mutable de métricas de riesgo diario.

    Mantiene equity_day_start y equity_peak para el día UTC actual.
    Detecta rollover de día automáticamente en cada llamada a update().

    Invariantes:
    - equity_peak >= equity_day_start siempre.
    - Al detectar rollover, equity_day_start y equity_peak se reinician
      con el equity_current del primer tick del nuevo día.
    - now_ms es inyectable para permitir tests deterministas sin mock de reloj.
    """

    def __init__(
        self,
        equity_day_start: Decimal,
        day_start_ts_ms: Optional[int] = None,
    ) -> None:
        if equity_day_start < Decimal("0"):
            raise ValueError(
                f"equity_day_start no puede ser negativo: {equity_day_start}"
            )
        self._equity_day_start: Decimal = equity_day_start
        self._equity_peak: Decimal = equity_day_start
        self._day_start_ts_ms: int = (
            day_start_ts_ms
            if day_start_ts_ms is not None
            else _utc_day_start_ms(int(datetime.now(tz=timezone.utc).timestamp() * 1000))
        )

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def update(
        self,
        equity_current: Decimal,
        now_ms: Optional[int] = None,
    ) -> DailyRiskMetrics:
        """
        Actualizar con equity actual y retornar snapshot de métricas del día.

        Si se detecta rollover de día UTC:
          - equity_day_start ← equity_current
          - equity_peak ← equity_current
          - day_start_ts_ms ← inicio del nuevo día

        Args:
            equity_current: Equity actual del portfolio en QUOTE.
            now_ms: Timestamp en ms (None = UTC now). Inyectable para tests.

        Returns:
            DailyRiskMetrics inmutable.
        """
        ts = (
            now_ms
            if now_ms is not None
            else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        )
        today_start = _utc_day_start_ms(ts)

        if today_start > self._day_start_ts_ms:
            # Rollover: primer tick del nuevo día — reiniciar baseline
            self._equity_day_start = equity_current
            self._equity_peak = equity_current
            self._day_start_ts_ms = today_start
        elif equity_current > self._equity_peak:
            # Nuevo pico en el día actual
            self._equity_peak = equity_current

        return DailyRiskMetrics(
            equity_day_start=self._equity_day_start,
            equity_peak=self._equity_peak,
            equity_current=equity_current,
            day_start_ts_ms=self._day_start_ts_ms,
        )

    @property
    def equity_day_start(self) -> Decimal:
        return self._equity_day_start

    @property
    def equity_peak(self) -> Decimal:
        return self._equity_peak

    @property
    def day_start_ts_ms(self) -> int:
        return self._day_start_ts_ms

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_equity(
        cls,
        equity: Decimal,
        now_ms: Optional[int] = None,
    ) -> "DailyRiskTracker":
        """
        Inicializar tracker con equity actual como equity_day_start.

        Llamar al arranque del runtime o al inicio de cada sesión de trading.

        Args:
            equity: Equity inicial (de TradeLedger o PortfolioSnapshot).
            now_ms: Timestamp en ms (None = UTC now). Inyectable para tests.
        """
        ts = (
            now_ms
            if now_ms is not None
            else int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        )
        day_start = _utc_day_start_ms(ts)
        return cls(equity_day_start=equity, day_start_ts_ms=day_start)
