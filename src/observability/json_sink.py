"""
JSON Line Sink — escribe métricas a archivo con rotación por tamaño.

Formato: una línea JSON por evento, con timestamp y labels.
Rotación: cuando el archivo supera max_bytes, se rota a .1, .2, etc.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("JSONLineSink")


class JSONLineSink:
    """
    Sink que escribe métricas como JSON lines a archivo con rotación.

    Cada línea: {"ts": ..., "name": ..., "value": ..., "labels": {...}}
    Rotación automática por tamaño (default 10MB, max 5 backups).
    """

    def __init__(
        self,
        path: str = "logs/metrics.jsonl",
        max_bytes: int = 10 * 1024 * 1024,  # 10MB
        backup_count: int = 5,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._ensure_dir()
        self._file = open(self.path, "a")
        # Register atexit to flush/close on shutdown (H5)
        import atexit
        atexit.register(self.close)

    def _ensure_dir(self) -> None:
        dir_path = os.path.dirname(self.path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

    def write(
        self,
        name: str,
        value: Any,
        metric_type: str = "gauge",
        labels: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Escribir una métrica como JSON line.

        Args:
            name: nombre de la métrica (e.g. "orders.submitted")
            value: valor numérico
            metric_type: "counter", "gauge", "histogram"
            labels: labels opcionales (e.g. {"symbol": "BTC-USD"})
        """
        record = {
            "ts": int(time.time() * 1000),
            "name": name,
            "value": _serialize_value(value),
            "type": metric_type,
        }
        if labels:
            record["labels"] = labels

        line = json.dumps(record, default=str) + "\n"
        self._file.write(line)
        self._file.flush()

        self._maybe_rotate()

    def write_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Escribir snapshot completo como JSON line."""
        record = {
            "ts": int(time.time() * 1000),
            "name": "_snapshot",
            "type": "snapshot",
            "data": {k: _serialize_value(v) for k, v in snapshot.items()},
        }
        line = json.dumps(record, default=str) + "\n"
        self._file.write(line)
        self._file.flush()
        self._maybe_rotate()

    def _maybe_rotate(self) -> None:
        """Rotar si el archivo supera max_bytes."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return

        if size < self.max_bytes:
            return

        self._file.close()

        # Rotate: .5 → delete, .4 → .5, ..., .1 → .2, current → .1
        for i in range(self.backup_count, 0, -1):
            src = f"{self.path}.{i}" if i > 0 else self.path
            dst = f"{self.path}.{i + 1}" if i < self.backup_count else None

            if i == self.backup_count:
                # Delete oldest
                if os.path.exists(f"{self.path}.{i}"):
                    os.remove(f"{self.path}.{i}")
            else:
                src_path = f"{self.path}.{i}"
                dst_path = f"{self.path}.{i + 1}"
                if os.path.exists(src_path):
                    os.rename(src_path, dst_path)

        # Rename current → .1
        if os.path.exists(self.path):
            os.rename(self.path, f"{self.path}.1")

        self._file = open(self.path, "a")
        logger.debug("Metrics file rotated: %s", self.path)

    def close(self) -> None:
        """Cerrar el archivo."""
        if self._file and not self._file.closed:
            self._file.close()

    def __del__(self) -> None:
        self.close()


def _serialize_value(v: Any) -> Any:
    """Serializar valores para JSON."""
    from decimal import Decimal

    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, dict):
        return {k: _serialize_value(val) for k, val in v.items()}
    return v
