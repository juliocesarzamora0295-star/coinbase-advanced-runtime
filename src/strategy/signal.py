"""
Contrato de dominio: Signal

Una Signal representa la intención de trading generada por una estrategia.

Invariantes:
- Signal NO fija qty ejecutable final. Eso es responsabilidad de PositionSizer + OrderPlanner.
- bar_timestamp corresponde al timestamp del bucket CERRADO/CONFIRMADO por el stream.
  No se valida contra el reloj local (evita fallo por clock skew en replay/latency).
- Signal es inmutable (frozen=True).
- emitted_at es el timestamp interno del runtime en el momento de emisión.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping


@dataclass(frozen=True)
class Signal:
    """
    Señal de trading generada por una estrategia.

    No contiene qty ni price. Sizing y planificación de orden son responsabilidad
    de PositionSizer y OrderPlanner respectivamente.
    """

    signal_id: str
    symbol: str
    direction: Literal["BUY", "SELL"]
    strength: Decimal  # rango [0.0, 1.0]
    strategy_id: str
    bar_timestamp: datetime  # timestamp del bucket confirmado por el stream
    emitted_at: datetime  # timestamp de emisión del runtime (UTC)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"Invalid direction: {self.direction!r}. Must be 'BUY' or 'SELL'")
        if not (Decimal("0") <= self.strength <= Decimal("1")):
            raise ValueError(f"strength must be in [0.0, 1.0], got {self.strength}")
        if not self.signal_id:
            raise ValueError("signal_id must not be empty")
        if not self.symbol:
            raise ValueError("symbol must not be empty")
        if not self.strategy_id:
            raise ValueError("strategy_id must not be empty")
        if self.bar_timestamp.tzinfo is None:
            raise ValueError("bar_timestamp must be timezone-aware")
        if self.emitted_at.tzinfo is None:
            raise ValueError("emitted_at must be timezone-aware")


def make_signal(
    *,
    symbol: str,
    direction: Literal["BUY", "SELL"],
    strength: Decimal,
    strategy_id: str,
    bar_timestamp: datetime,
    metadata: Mapping[str, Any] | None = None,
) -> Signal:
    """
    Factory para Signal con signal_id y emitted_at auto-generados.

    bar_timestamp debe ser el timestamp del bucket confirmado por el stream,
    pasado explícitamente por la estrategia. No se genera aquí.
    """
    return Signal(
        signal_id=str(uuid.uuid4()),
        symbol=symbol,
        direction=direction,
        strength=strength,
        strategy_id=strategy_id,
        bar_timestamp=bar_timestamp,
        emitted_at=datetime.now(tz=timezone.utc),
        metadata=dict(metadata) if metadata else {},
    )
