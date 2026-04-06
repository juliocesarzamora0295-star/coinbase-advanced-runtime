"""
HistoricalDataFeed — carga datos OHLCV y los emite como barras.

Formato CSV esperado: timestamp,open,high,low,close,volume
timestamp puede ser epoch ms, epoch seconds, o ISO-8601.
"""

import csv
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator, List


@dataclass(frozen=True)
class Bar:
    """Barra OHLCV individual."""

    timestamp_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def _parse_timestamp(raw: str) -> int:
    """Parse timestamp to epoch ms."""
    raw = raw.strip()
    # Try epoch ms (> 1e12)
    try:
        val = int(float(raw))
        if val > 1_000_000_000_000:
            return val
        # epoch seconds
        return val * 1000
    except ValueError:
        pass
    # ISO-8601
    from datetime import datetime

    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


class HistoricalDataFeed:
    """
    Carga datos OHLCV desde CSV y los emite como iterador de Bar.

    No tiene dependencias de red ni de módulos live.
    """

    def __init__(self, bars: List[Bar]) -> None:
        self._bars = sorted(bars, key=lambda b: b.timestamp_ms)

    @classmethod
    def from_csv(cls, path: str | Path) -> "HistoricalDataFeed":
        """
        Cargar desde archivo CSV.

        Columnas esperadas: timestamp, open, high, low, close, volume
        """
        bars: List[Bar] = []
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bars.append(
                    Bar(
                        timestamp_ms=_parse_timestamp(row["timestamp"]),
                        open=Decimal(row["open"]),
                        high=Decimal(row["high"]),
                        low=Decimal(row["low"]),
                        close=Decimal(row["close"]),
                        volume=Decimal(row.get("volume", "0")),
                    )
                )
        return cls(bars)

    @classmethod
    def from_bars(cls, bars: List[Bar]) -> "HistoricalDataFeed":
        """Crear desde lista de barras directamente."""
        return cls(bars)

    def __iter__(self) -> Iterator[Bar]:
        return iter(self._bars)

    def __len__(self) -> int:
        return len(self._bars)
