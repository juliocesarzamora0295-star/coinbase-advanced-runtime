"""
Execution Report — métricas de microestructura por orden.

Cada orden completada (fill, reject, timeout) genera un ExecutionReport
con slippage, latencia, fill ratio y quality score.

Invariantes:
- slippage_bps con signo: positivo = desfavorable (pagaste más / recibiste menos)
- latency_ms: timestamp de fill - timestamp de envío
- fill_ratio: filled_qty / requested_qty (1.0 = full fill)
- fill_quality_score: métrica compuesta normalizada [0, 1] (1 = perfecto)
- ExecutionReport es inmutable (frozen)
"""

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("ExecutionReport")

ZERO = Decimal("0")


@dataclass(frozen=True)
class ExecutionReport:
    """
    Reporte de ejecución para una orden completada.

    Generado después de cada fill, reject o timeout.
    Alimenta CircuitBreaker con slippage y latencia.
    """

    # Identity
    client_order_id: str
    symbol: str
    side: str  # "BUY" or "SELL"

    # Prices
    expected_price: Decimal  # precio al momento del submit
    fill_price: Decimal  # precio promedio del fill (0 si reject/timeout)

    # Quantities
    requested_qty: Decimal
    filled_qty: Decimal  # puede ser < requested_qty (partial fill)

    # Metrics
    slippage_bps: Decimal  # basis points, con signo
    latency_ms: float  # envío → fill/reject
    fill_ratio: Decimal  # filled_qty / requested_qty
    fill_quality_score: Decimal  # compuesta [0, 1]

    # Outcome
    outcome: str  # "FILLED", "PARTIAL", "REJECTED", "TIMEOUT"

    # Telemetry quality
    estimated_slippage: bool = False  # True if expected_price was unavailable (fallback)

    def log_structured(self) -> None:
        """Log con campos parseables para observabilidad."""
        logger.info(
            "EXECUTION_REPORT "
            "client_order_id=%s symbol=%s side=%s outcome=%s "
            "expected_price=%s fill_price=%s "
            "requested_qty=%s filled_qty=%s fill_ratio=%s "
            "slippage_bps=%s latency_ms=%.1f quality=%s estimated_slippage=%s",
            self.client_order_id,
            self.symbol,
            self.side,
            self.outcome,
            self.expected_price,
            self.fill_price,
            self.requested_qty,
            self.filled_qty,
            self.fill_ratio,
            self.slippage_bps,
            self.latency_ms,
            self.fill_quality_score,
            self.estimated_slippage,
        )


def compute_slippage_bps(
    side: str,
    expected_price: Decimal,
    fill_price: Decimal,
) -> Decimal:
    """
    Calcular slippage en basis points con signo.

    Positivo = desfavorable:
    - BUY: fill_price > expected → slippage positivo (pagaste más)
    - SELL: fill_price < expected → slippage positivo (recibiste menos)

    Args:
        side: "BUY" or "SELL"
        expected_price: precio de referencia al submit
        fill_price: precio promedio de ejecución

    Returns:
        Slippage en bps. 0 si expected_price es 0.
    """
    if expected_price <= ZERO:
        return ZERO

    raw_diff = fill_price - expected_price

    if side.upper() == "BUY":
        # BUY: pagaste más = desfavorable = positivo
        slippage = raw_diff / expected_price
    else:
        # SELL: recibiste menos = desfavorable = positivo
        slippage = -raw_diff / expected_price

    return slippage * Decimal("10000")  # convert to bps


def compute_fill_ratio(requested_qty: Decimal, filled_qty: Decimal) -> Decimal:
    """
    Ratio de fill: filled / requested.

    Returns:
        Decimal en [0, 1]. 0 si requested es 0.
    """
    if requested_qty <= ZERO:
        return ZERO
    ratio = filled_qty / requested_qty
    return min(ratio, Decimal("1"))  # cap at 1 (no overfill)


def compute_fill_quality_score(
    slippage_bps: Decimal,
    latency_ms: float,
    fill_ratio: Decimal,
    *,
    max_acceptable_slippage_bps: Decimal = Decimal("20"),
    max_acceptable_latency_ms: float = 2000.0,
) -> Decimal:
    """
    Score compuesto de calidad de ejecución [0, 1].

    Fórmula: weighted average de 3 componentes:
    - slippage_score (40%): 1 - clamp(|slippage| / max_slippage, 0, 1)
    - latency_score (30%): 1 - clamp(latency / max_latency, 0, 1)
    - fill_score (30%): fill_ratio directamente

    Returns:
        Decimal en [0, 1]. 1 = perfect execution.
    """
    # Slippage score (40%)
    abs_slippage = abs(slippage_bps)
    if max_acceptable_slippage_bps > ZERO:
        slippage_normalized = min(abs_slippage / max_acceptable_slippage_bps, Decimal("1"))
    else:
        slippage_normalized = ZERO
    slippage_score = Decimal("1") - slippage_normalized

    # Latency score (30%)
    if max_acceptable_latency_ms > 0:
        latency_normalized = min(Decimal(str(latency_ms)) / Decimal(str(max_acceptable_latency_ms)), Decimal("1"))
    else:
        latency_normalized = ZERO
    latency_score = Decimal("1") - latency_normalized

    # Fill ratio score (30%)
    fill_score = fill_ratio

    # Weighted average
    quality = (
        slippage_score * Decimal("0.4")
        + latency_score * Decimal("0.3")
        + fill_score * Decimal("0.3")
    )
    return max(ZERO, min(quality, Decimal("1")))


def build_execution_report(
    *,
    client_order_id: str,
    symbol: str,
    side: str,
    expected_price: Decimal,
    fill_price: Decimal,
    requested_qty: Decimal,
    filled_qty: Decimal,
    latency_ms: float,
    outcome: str,
    estimated_slippage: bool = False,
) -> ExecutionReport:
    """
    Factory para construir ExecutionReport con métricas calculadas.

    Args:
        client_order_id: ID de la orden
        symbol: símbolo (e.g. "BTC-USD")
        side: "BUY" or "SELL"
        expected_price: precio de referencia al submit
        fill_price: precio promedio de ejecución (0 para reject/timeout)
        requested_qty: cantidad solicitada
        filled_qty: cantidad ejecutada (puede ser parcial)
        latency_ms: tiempo de envío a fill/reject/timeout
        outcome: "FILLED", "PARTIAL", "REJECTED", "TIMEOUT"

    Returns:
        ExecutionReport con todas las métricas calculadas.
    """
    slippage = compute_slippage_bps(side, expected_price, fill_price)
    ratio = compute_fill_ratio(requested_qty, filled_qty)
    quality = compute_fill_quality_score(slippage, latency_ms, ratio)

    return ExecutionReport(
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        expected_price=expected_price,
        fill_price=fill_price,
        requested_qty=requested_qty,
        filled_qty=filled_qty,
        slippage_bps=slippage,
        latency_ms=latency_ms,
        fill_ratio=ratio,
        fill_quality_score=quality,
        outcome=outcome,
        estimated_slippage=estimated_slippage,
    )
