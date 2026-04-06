"""Observability module — métricas, sinks, registry global."""

from src.observability.json_sink import JSONLineSink
from src.observability.metrics import MetricsCollector, RuntimeMetrics

# Global registry singleton — accesible desde cualquier módulo
_global_collector: MetricsCollector | None = None


def get_collector() -> MetricsCollector:
    """Obtener instancia global del MetricsCollector."""
    global _global_collector
    if _global_collector is None:
        _global_collector = MetricsCollector()
    return _global_collector


def reset_collector() -> None:
    """Resetear instancia global (para tests)."""
    global _global_collector
    _global_collector = None


__all__ = [
    "MetricsCollector",
    "RuntimeMetrics",
    "JSONLineSink",
    "get_collector",
    "reset_collector",
]
