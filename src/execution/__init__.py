"""Execution module para Fortress v4."""

from src.execution.idempotency import IdempotencyStore, OrderRecord, OrderState, StoredIntent
from src.execution.order_planner import OrderIntent  # entidad canónica de dominio
from src.execution.orders import OrderExecutor, OrderResult

__all__ = [
    "IdempotencyStore",
    "OrderIntent",  # entidad canónica — de order_planner
    "StoredIntent",  # row de persistencia — de idempotency
    "OrderState",
    "OrderRecord",
    "OrderExecutor",
    "OrderResult",
]
