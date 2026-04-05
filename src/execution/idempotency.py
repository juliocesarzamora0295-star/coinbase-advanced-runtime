"""
Sistema de idempotencia con almacén durable (SQLite).

Garantiza 1:1 entre client_order_id y exchange_order_id.
Usa OrderIntent canónico de order_planner como fuente única.

Invariantes:
- save_intent NO sobrescribe un intent existente (INSERT estricto)
- save_intent retorna False si el intent ya existe (idempotente)
- client_order_id es UNIQUE — dos intents no pueden compartir el mismo
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, auto
from typing import List, Optional

from src.execution.order_planner import OrderIntent


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


@dataclass
class OrderRecord:
    """Registro completo de orden en el sistema de idempotencia."""

    client_order_id: str
    exchange_order_id: Optional[str]
    state: OrderState
    intent: OrderIntent
    created_at: datetime
    updated_at: datetime
    error_message: Optional[str] = None

    # Compat: alias para código que aún use intent_id
    @property
    def intent_id(self) -> str:
        return self.client_order_id

    @property
    def is_terminal(self) -> bool:
        """Verificar si el estado es terminal."""
        return self.state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.FAILED,
        )

    @property
    def is_active(self) -> bool:
        """Verificar si la orden está activa."""
        return self.state in (
            OrderState.NEW,
            OrderState.OPEN_RESTING,
            OrderState.OPEN_PENDING,
            OrderState.CANCEL_QUEUED,
        )


class IntentAlreadyExistsError(Exception):
    """Raised cuando se intenta guardar un intent que ya existe con force=True context."""

    pass


class IdempotencyStore:
    """
    Almacén durable de intents con SQLite.

    Garantiza que un client_order_id siempre mapea a un único intent.
    save_intent es idempotente: si el intent ya existe, retorna False.
    """

    def __init__(self, db_path: str = "state/idempotency.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Inicializar base de datos SQLite."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_intents (
                    client_order_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    qty TEXT NOT NULL,
                    price TEXT,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    post_only INTEGER NOT NULL DEFAULT 0,
                    exchange_order_id TEXT,
                    state TEXT NOT NULL,
                    error_message TEXT,
                    created_ts_ms INTEGER NOT NULL,
                    updated_ts_ms INTEGER NOT NULL
                )
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

    def save_intent(
        self, intent: OrderIntent, state: OrderState = OrderState.NEW
    ) -> bool:
        """
        Guardar un nuevo intent. INSERT estricto — no sobrescribe.

        Returns:
            True si se insertó exitosamente.
            False si el client_order_id ya existe (idempotente, no error).
        """
        now = int(datetime.now().timestamp() * 1000)

        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO order_intents
                    (client_order_id, signal_id, strategy_id, product_id, side,
                     order_type, qty, price, reduce_only, post_only,
                     exchange_order_id, state, error_message,
                     created_ts_ms, updated_ts_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        intent.client_order_id,
                        intent.signal_id,
                        intent.strategy_id,
                        intent.symbol,
                        intent.side,
                        intent.order_type,
                        str(intent.final_qty),
                        str(intent.price) if intent.price else None,
                        1 if intent.reduce_only else 0,
                        1 if intent.post_only else 0,
                        None,
                        state.name,
                        None,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                # client_order_id already exists — idempotent, not an error
                return False

    def get_by_client_order_id(self, client_order_id: str) -> Optional[OrderRecord]:
        """Buscar registro por client_order_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM order_intents WHERE client_order_id = ?",
                (client_order_id,),
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_record(row)
            return None

    # Compat alias
    def get_by_intent_id(self, intent_id: str) -> Optional[OrderRecord]:
        """Alias de compatibilidad — busca por client_order_id."""
        return self.get_by_client_order_id(intent_id)

    def get_by_exchange_order_id(self, exchange_order_id: str) -> Optional[OrderRecord]:
        """Buscar registro por exchange_order_id."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM order_intents WHERE exchange_order_id = ?",
                (exchange_order_id,),
            )
            row = cursor.fetchone()
            if row:
                return self._row_to_record(row)
            return None

    def update_state(
        self,
        client_order_id: str,
        state: OrderState,
        exchange_order_id: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Actualizar estado de un intent por client_order_id."""
        now = int(datetime.now().timestamp() * 1000)

        with sqlite3.connect(self.db_path) as conn:
            if exchange_order_id:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, exchange_order_id = ?, updated_ts_ms = ?
                    WHERE client_order_id = ?
                    """,
                    (state.name, exchange_order_id, now, client_order_id),
                )
            elif error_message:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, error_message = ?, updated_ts_ms = ?
                    WHERE client_order_id = ?
                    """,
                    (state.name, error_message, now, client_order_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE order_intents
                    SET state = ?, updated_ts_ms = ?
                    WHERE client_order_id = ?
                    """,
                    (state.name, now, client_order_id),
                )
            conn.commit()

    def get_pending_or_open(self) -> List[OrderRecord]:
        """Obtener órdenes pendientes o abiertas."""
        active_states = [
            OrderState.NEW.name,
            OrderState.OPEN_RESTING.name,
            OrderState.OPEN_PENDING.name,
            OrderState.CANCEL_QUEUED.name,
        ]

        with sqlite3.connect(self.db_path) as conn:
            placeholders = ",".join(["?"] * len(active_states))
            cursor = conn.execute(
                f"SELECT * FROM order_intents WHERE state IN ({placeholders})",
                active_states,
            )
            return [self._row_to_record(row) for row in cursor.fetchall()]

    def cleanup_old(self, days: int = 30) -> int:
        """Eliminar registros antiguos (más de N días)."""
        cutoff = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM order_intents WHERE updated_ts_ms < ?", (cutoff,)
            )
            conn.commit()
            return cursor.rowcount

    def _row_to_record(self, row) -> OrderRecord:
        """Convertir fila de SQLite a OrderRecord."""
        # Columns: client_order_id, signal_id, strategy_id, product_id, side,
        #          order_type, qty, price, reduce_only, post_only,
        #          exchange_order_id, state, error_message,
        #          created_ts_ms, updated_ts_ms
        intent = OrderIntent(
            client_order_id=row[0],
            signal_id=row[1],
            strategy_id=row[2],
            symbol=row[3],
            side=row[4],
            final_qty=Decimal(row[6]),
            order_type=row[5],
            price=Decimal(row[7]) if row[7] else None,
            reduce_only=bool(row[8]),
            post_only=bool(row[9]),
            viable=True,  # si está persistido, fue viable
            planner_version="recovered",
        )

        return OrderRecord(
            client_order_id=row[0],
            exchange_order_id=row[10],
            state=OrderState[row[11]],
            intent=intent,
            created_at=datetime.fromtimestamp(row[13] / 1000),
            updated_at=datetime.fromtimestamp(row[14] / 1000),
            error_message=row[12],
        )
