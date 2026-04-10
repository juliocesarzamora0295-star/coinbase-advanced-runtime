"""
Noop Strategy — returns no signals, for testing infrastructure.
"""

from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.strategy.base import Strategy


class NoopStrategy(Strategy):
    """Strategy that never generates signals. Useful for testing infrastructure."""

    def __init__(self, symbol: str, config: Optional[Dict[str, Any]] = None):
        super().__init__(symbol, config)

    @property
    def name(self) -> str:
        return "noop"

    @property
    def warmup_bars(self) -> int:
        return 0

    def generate_signals(self, *, mid: Decimal, bar_timestamp: Any = None) -> List:
        return []
