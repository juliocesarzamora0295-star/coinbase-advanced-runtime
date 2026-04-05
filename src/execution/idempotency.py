"""
Sistema de idempotencia con almacén durable (SQLite).

Garantiza 1:1 entre intent_id y client_order_id.
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, auto
from typing import Any, Dict, List, Optional


class OrderState(Enum):
    """Estados de una orden."""

    NEW = auto()
    OPEN_RESTING = auto()
    OPEN_PENDING = auto()
    CANCEL_QUEUED = auto()  # Estado documentado por Coinbase
    FILLED = auto()
    CANCELLED = auto()
    EXPIRED = auto()
    FAILED = auto()
    # Estados de reconciliación de fills (post-FILLED)
    RECONCILE_PENDING = auto()   # Exchange FILLED, fills pendientes de aplicar al ledger
    RECONCILE_CONFLICT = auto()  # Exchange FILLED, fills en ledger no convergen con exchange
    RECONCILE_RESOLVED = auto()  # Exchange FILLED, todos los fills confirmados en ledger


@dataclass
class OrderIntent:
    """Intención de orden antes de enviar a exchange."""

    intent_id: str
    client_order_id: str
    product_id: str
    side: str
    order_type: str
    qty: Decimal
    price: Optional[Decimal]
    stop_price: Optional[Decimal]
    post_only: bool
    created_ts_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "product_id": self.product_id,
            "side": self.side,
            "order_type": self.order_type,
            "qty": str(self.qty),
            "price": str(self.price) if self.price else None,
            "stop_price": str(self.stop_price) if self.stop_price else None,
            "post_only": self.post_only,
            "created_ts_ms": self.created_ts_ms,
        }


@dataclass
class OrderRecord:
    """Registro completo de orden en el sistema de idempotencia."""

    intent_id: str
    client_order_id: str
    exchange_order_id: Optional[str]
    state: OrderState
    intent: OrderIntent
    created_at: datetime
    updated_at: datetime
    error_message: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        """
        Verificar si el estado es terminal limpio.

        RECONCILE_CONFLICT y RECONCILE_PENDING NO son terminales:
        el ledger no ha convergido y el estado requiere resolución.
        RECONCILE_RESOLVED sí es terminal: todos los fills confirmados.
        """
        return self.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.FAILED,
            OrderState.RECONCILE_RESOLVED,
        )

    @property
    def is_active(self) -> bool:
        """Verificar si la orden está activa (incluyendo pendiente de reconcile)."""
        return self.state in (
            OrderState.NEW,
            OrderState.OPEN_RESTING,
            OrderState.OPEN_PENDING,
            OrderState.CANCEL_QUEUED,
            OrderState.RECONCILE_PENDING,
        )

    @property
    def is_ledger_conflict(self) -> bool:
        """Fill count del exchange no converge con fills aplicados al ledger."""
        return self.state == OrderState.RECONCILE_CONFLICT


class IdempotencyStore:
    """
    Almacén durable de intents con SQLite.

    Garantiza que un intent_id siempre mapea al mismo client_order_id.
    """

    def __init__(self, db_path: str = "state/idempotency.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Inicializar base de datos SQLite."""
        # Crear directorio si no existe
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    exchange_order_id TEXT,
                    product_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    price TEXT,
                    stop_price TEXT,
                    post_only INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    error_message TEXT,
                    created_ts_ms INTEGER NOT NULL,
                    updated_ts_ms INTEGER NOT NULL
                )
            """)

            # Índices para búsquedas eficientes
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_client_order_id
                ON order_intents(client_order_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_exchange_order_id
                ON order_intents(exchange_order_id)
            """)

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_state
                ON order_intents(state)
            """)

            conn.commit()

    def save_intent(self, intent: OrderIntent, state: OrderState = OrderState.NEW) -> None:
        """Guardar un nuevo intent."""
        now = int(datetime.now().timestamp() * 1000)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO order_intents
                (intent_id, client_order_id, exchange_order_id, product_id, side,
                 order_type, qty, price, stop_price, post_only, state, error_message,
                 created_ts_ms, updated_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent.intent_id,
                    intent.client_order_id,
                    None,
                    intent.product_id,
                    intent.side,
                    intent.order_type,
                    str(intent.qty),
                    str(intent.price) if intent.price else None,
                    str(intent.stop_price) if intent.stop_price else None,
                    1 if intent.post_only else 0,
                    state.name,
                    None,
                    intent.created_ts_ms,
                    now,
                ),
            )
            conn.commit()

    def get_by_intent_id(self, intent_id: str) -> Optional[OrderRecord]:
        """Buscar registro por intent_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT * FROM order_intents WHERE intent_id = ?", (intent_id,))
            row = cursor.fetchone()

            if row:
                return self._row_to_record(row)
            return None

    def get_by_client_order_id(self, client_order_id: str) -> Optional[OrderRecord]:
        """Buscar registro por client_order_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?", (client_order_id,)
            )
            row = cursor.fetchone()

            if row:
                return self._row_to_record(row)
            return None

    def get_by_exchange_order_id(self, exchange_order_id: str) -> Optional[OrderRecord]:
        """Buscar registro por exchange_order_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM order_intents WHERE exchange_order_id = ?", (exchange_order_id,)
            )
            row = cursor.fetchone()

            if row:
                return self._row_to_record(row)
            return None

    def update_state(
        self,
        intent_id: str,
        state: OrderState,
        exchange_order_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Actualizar estado de un intent."""
        now = int(datetime.now().timestamp() * 1000)

        with sqlite3.connect(self.db_path) as conn:
            if exchange_order_id:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, exchange_order_id = ?, updated_ts_ms = ?
                    WHERE intent_id = ?
                    """,
                    (state.name, exchange_order_id, now, intent_id),
                )
            elif error_message:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, error_message = ?, updated_ts_ms = ?
                    WHERE intent_id = ?
                    """,
                    (state.name, error_message, now, intent_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, updated_ts_ms = ?
                    WHERE intent_id = ?
                    """,
                    (state.name, now, intent_id),
                )
            conn.commit()

    def get_pending_or_open(self) -> List[OrderRecord]:
        """Obtener órdenes pendientes o abiertas."""
        # Incluir CANCEL_QUEUED y RECONCILE_PENDING para consistencia con is_active
        active_states = [
            OrderState.NEW.name,
            OrderState.OPEN_RESTING.name,
            OrderState.OPEN_PENDING.name,
            OrderState.CANCEL_QUEUED.name,
            OrderState.RECONCILE_PENDING.name,
        ]

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join(["?"] * len(active_states))
            cursor = conn.execute(
                f"SELECT * FROM order_intents WHERE state IN ({placeholders})", active_states
            )

            return [self._row_to_record(row) for row in cursor.fetchall()]

    def cleanup_old(self, days: int = 30) -> int:
        """Eliminar registros antiguos (más de N días)."""
        cutoff = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM order_intents WHERE updated_ts_ms < ?", (cutoff,))
            conn.commit()
            return cursor.rowcount

    def _row_to_record(self, row: sqlite3.Row) -> OrderRecord:
        """Convertir fila de SQLite a OrderRecord."""
        intent = OrderIntent(
            intent_id=row[0],
            client_order_id=row[1],
            product_id=row[3],
            side=row[4],
            order_type=row[5],
            qty=Decimal(row[6]),
            price=Decimal(row[7]) if row[7] else None,
            stop_price=Decimal(row[8]) if row[8] else None,
            post_only=bool(row[9]),
            created_ts_ms=row[12],
        )

        return OrderRecord(
            intent_id=row[0],
            client_order_id=row[1],
            exchange_order_id=row[2],
            state=OrderState[row[10]],
            intent=intent,
            created_at=datetime.fromtimestamp(row[12] / 1000),
            updated_at=datetime.fromtimestamp(row[13] / 1000),
            error_message=row[11],
        )
