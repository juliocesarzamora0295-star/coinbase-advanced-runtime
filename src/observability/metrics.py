"""
Colector de métricas para shadow-live y observabilidad operativa.

Métricas mínimas obligatorias según plan:
- open_orders_count
- reconcile_lag_ms
- ws_gap_count
- duplicate_fill_count
- order_reject_rate
- ledger_equity
- unrealized_pnl
- drawdown_pct
- circuit_breaker_state
- riskgate_rejection_reason_count (por tipo de razón)
- spread_used_count
- spread_stale_count
- signal_count_per_symbol
- sizing_blocked_count

Implementación:
- Colector en memoria, sin dependencias externas.
- flush() escribe JSON por línea a logger (rotación diaria vía logging config).
- Thread-safe: todas las operaciones sobre primitivas Python son GIL-safe.
"""

import json
import logging
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Dict

logger = logging.getLogger("Metrics")


@dataclass
class RuntimeMetrics:
    """Snapshot de métricas del runtime en un instante."""

    # OMS
    open_orders_count: int = 0
    reconcile_lag_ms: float = 0.0

    # WebSocket
    ws_gap_count: int = 0

    # Fills / Ledger
    duplicate_fill_count: int = 0
    ledger_equity: float = 0.0
    unrealized_pnl: float = 0.0
    drawdown_pct: float = 0.0

    # Orders
    order_reject_rate: float = 0.0
    order_total: int = 0
    order_rejected: int = 0

    # Risk
    circuit_breaker_state: str = "CLOSED"
    sizing_blocked_count: int = 0
    riskgate_rejection_reason_count: Dict[str, int] = field(default_factory=dict)

    # Market data
    spread_used_count: int = 0
    spread_stale_count: int = 0

    # Signals
    signal_count_per_symbol: Dict[str, int] = field(default_factory=dict)

    # Timestamp
    ts_ms: int = 0


def _make_key(name: str, labels: Dict[str, str] | None = None) -> str:
    """Build metric key from name + sorted labels."""
    if not labels:
        return name
    label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{label_str}}}"


class MetricsCollector:
    """
    Colector de métricas en memoria con flush periódico a log.

    Uso:
        collector = MetricsCollector()
        collector.record_signal("BTC-USD")
        collector.record_order_rejected("EQUITY_ZERO")
        collector.flush()  # escribe JSON a logger

    Diseño:
    - No depende de infraestructura externa (no Prometheus, no DataDog).
    - Formato: JSON por línea, compatible con cualquier log aggregator.
    - Flush periódico sugerido: cada 60 segundos o cada N eventos.
    """

    def __init__(self) -> None:
        self._metrics = RuntimeMetrics()
        self._order_total: int = 0
        self._order_rejected: int = 0
        self._riskgate_rejections: Dict[str, int] = defaultdict(int)
        self._signal_counts: Dict[str, int] = defaultdict(int)

        # Generic metric stores
        self._counters: Dict[str, int] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, list] = {}

    # ── OMS ──────────────────────────────────────────────

    def set_open_orders_count(self, count: int) -> None:
        self._metrics.open_orders_count = count

    def record_reconcile_lag(self, lag_ms: float) -> None:
        self._metrics.reconcile_lag_ms = lag_ms

    # ── WebSocket ─────────────────────────────────────────

    def record_ws_gap(self) -> None:
        self._metrics.ws_gap_count += 1

    # ── Fills ─────────────────────────────────────────────

    def record_duplicate_fill(self) -> None:
        self._metrics.duplicate_fill_count += 1

    # ── Ledger ────────────────────────────────────────────

    def set_ledger_equity(self, equity: Decimal) -> None:
        self._metrics.ledger_equity = float(equity)

    def set_unrealized_pnl(self, pnl: Decimal) -> None:
        self._metrics.unrealized_pnl = float(pnl)

    def set_drawdown_pct(self, drawdown: Decimal) -> None:
        self._metrics.drawdown_pct = float(drawdown)

    # ── Orders ────────────────────────────────────────────

    def record_order_submitted(self) -> None:
        self._order_total += 1
        self._update_reject_rate()

    def record_order_rejected(self, reason: str = "") -> None:
        self._order_rejected += 1
        self._order_total += 1
        self._update_reject_rate()
        if reason:
            self._riskgate_rejections[reason] += 1

    def _update_reject_rate(self) -> None:
        if self._order_total > 0:
            self._metrics.order_reject_rate = self._order_rejected / self._order_total
        self._metrics.order_total = self._order_total
        self._metrics.order_rejected = self._order_rejected

    # ── Risk ──────────────────────────────────────────────

    def set_circuit_breaker_state(self, state: str) -> None:
        """state: 'CLOSED' | 'OPEN' | 'HALF_OPEN'"""
        self._metrics.circuit_breaker_state = state

    def record_sizing_blocked(self) -> None:
        self._metrics.sizing_blocked_count += 1

    # ── Market data ───────────────────────────────────────

    def record_spread_used(self) -> None:
        self._metrics.spread_used_count += 1

    def record_spread_stale(self) -> None:
        self._metrics.spread_stale_count += 1

    # ── Signals ───────────────────────────────────────────

    def record_signal(self, symbol: str) -> None:
        self._signal_counts[symbol] += 1

    # ── Snapshot / Flush ──────────────────────────────────

    def snapshot(self) -> RuntimeMetrics:
        """Retornar snapshot inmutable del estado actual."""
        import copy

        snap = copy.deepcopy(self._metrics)
        snap.ts_ms = int(time.time() * 1000)
        snap.riskgate_rejection_reason_count = dict(self._riskgate_rejections)
        snap.signal_count_per_symbol = dict(self._signal_counts)
        return snap

    def flush(self) -> None:
        """
        Escribir snapshot actual como JSON a logger.

        Formato: una línea JSON por flush.
        Rotación diaria vía logging config del runtime.
        """
        snap = self.snapshot()
        snap_dict = asdict(snap)
        logger.info(json.dumps(snap_dict, default=str))

    # ── Generic API ────────────────────────────────

    def inc(self, name: str, delta: int = 1, labels: Dict[str, str] | None = None) -> None:
        """
        Incrementar un contador genérico.

        Args:
            name: nombre del contador (e.g. "orders.submitted")
            delta: cantidad a incrementar (default 1)
            labels: labels opcionales
        """
        key = _make_key(name, labels)
        self._counters[key] = self._counters.get(key, 0) + delta

    def gauge(self, name: str, value: float, labels: Dict[str, str] | None = None) -> None:
        """
        Establecer un gauge genérico.

        Args:
            name: nombre del gauge (e.g. "equity.current")
            value: valor actual
            labels: labels opcionales
        """
        key = _make_key(name, labels)
        self._gauges[key] = value

    def observe(self, name: str, value: float, labels: Dict[str, str] | None = None) -> None:
        """
        Registrar observación en un histograma genérico.

        Args:
            name: nombre del histograma (e.g. "latency.ms")
            value: valor observado
            labels: labels opcionales
        """
        key = _make_key(name, labels)
        if key not in self._histograms:
            self._histograms[key] = []
        self._histograms[key].append(value)

    def get_counter(self, name: str, labels: Dict[str, str] | None = None) -> int:
        """Obtener valor actual de un contador."""
        return self._counters.get(_make_key(name, labels), 0)

    def get_gauge(self, name: str, labels: Dict[str, str] | None = None) -> float:
        """Obtener valor actual de un gauge."""
        return self._gauges.get(_make_key(name, labels), 0.0)

    def get_histogram(self, name: str, labels: Dict[str, str] | None = None) -> list:
        """Obtener observaciones de un histograma."""
        return list(self._histograms.get(_make_key(name, labels), []))

    def generic_snapshot(self) -> Dict:
        """Snapshot de métricas genéricas (counters + gauges + histograms)."""
        result: Dict = {
            "ts_ms": int(time.time() * 1000),
            "counters": dict(self._counters),
            "gauges": dict(self._gauges),
            "histograms": {},
        }
        for key, values in self._histograms.items():
            if values:
                sorted_v = sorted(values)
                result["histograms"][key] = {
                    "count": len(values),
                    "min": sorted_v[0],
                    "max": sorted_v[-1],
                    "mean": sum(values) / len(values),
                    "p50": sorted_v[len(sorted_v) // 2],
                    "p95": sorted_v[int(len(sorted_v) * 0.95)],
                    "p99": sorted_v[min(int(len(sorted_v) * 0.99), len(sorted_v) - 1)],
                }
        return result

    def reset(self) -> None:
        """Reiniciar todos los contadores (para tests o reset diario)."""
        self.__init__()
