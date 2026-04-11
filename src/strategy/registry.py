"""
Strategy Registry — config-driven strategy selection.

Maps strategy names (from YAML config) to strategy classes.
Single source of truth for available strategies.
"""

import logging
from typing import Dict, Type

from src.strategy.base import Strategy

logger = logging.getLogger("StrategyRegistry")

# Lazy imports to avoid circular dependencies
_REGISTRY: Dict[str, Type[Strategy]] = {}
_INITIALIZED = False


def _init_registry() -> None:
    """Populate registry with built-in strategies. Called once on first access."""
    global _INITIALIZED
    if _INITIALIZED:
        return

    from src.strategy.sma_crossover import SmaCrossoverStrategy
    from src.strategy.mean_reversion import MeanReversionStrategy
    from src.strategy.momentum_breakout import MomentumBreakoutStrategy
    from src.strategy.macd_strategy import MacdStrategy
    from src.strategy.rsi_divergence import RsiDivergenceStrategy
    from src.strategy.vwap_strategy import VwapStrategy
    from src.strategy.noop import NoopStrategy

    _REGISTRY.update({
        "ma_crossover": SmaCrossoverStrategy,
        "sma_crossover": SmaCrossoverStrategy,
        "mean_reversion": MeanReversionStrategy,
        "momentum_breakout": MomentumBreakoutStrategy,
        "macd": MacdStrategy,
        "macd_histogram": MacdStrategy,
        "rsi_divergence": RsiDivergenceStrategy,
        "vwap": VwapStrategy,
        "vwap_reversion": VwapStrategy,
        "noop": NoopStrategy,
    })
    _INITIALIZED = True


def register_strategy(name: str, cls: Type[Strategy]) -> None:
    """Register a custom strategy class under the given name."""
    _init_registry()
    _REGISTRY[name] = cls
    logger.info("Registered strategy '%s' -> %s", name, cls.__name__)


def get_strategy_class(name: str) -> Type[Strategy] | None:
    """Look up a strategy class by config name. Returns None if unknown."""
    _init_registry()
    return _REGISTRY.get(name)


def list_strategies() -> Dict[str, Type[Strategy]]:
    """Return a copy of the full registry."""
    _init_registry()
    return dict(_REGISTRY)
