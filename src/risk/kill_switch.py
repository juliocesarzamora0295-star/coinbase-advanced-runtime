"""
Kill switch persistente para runtime de trading.

3 modos:
- BLOCK_NEW: no se emiten nuevas órdenes
- CANCEL_OPEN: cancela órdenes abiertas (no cierra posiciones)
- CANCEL_AND_FLATTEN: cancela abiertas + cierra posiciones al mercado

Persistido en SQLite para sobrevivir reinicios.
Activación/desactivación vía API programática.

Invariantes:
- Kill switch activo siempre bloquea nuevas órdenes (BLOCK_NEW es el mínimo)
- Estado persiste en DB — restart no limpia el kill switch
- Solo clear_kill_switch() desactiva — nunca auto-recovery
- Cada activación y desactivación se loguea con timestamp y razón
"""

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

logger = logging.getLogger("KillSwitch")


class KillSwitchMode(Enum):
    """Modos del kill switch."""

    OFF = "OFF"
    BLOCK_NEW = "BLOCK_NEW"  # Solo bloquea nuevas órdenes
    CANCEL_OPEN = "CANCEL_OPEN"  # Bloquea nuevas + cancela abiertas
    CANCEL_AND_FLATTEN = "CANCEL_AND_FLATTEN"  # Todo lo anterior + cierra posiciones


@dataclass(frozen=True)
class KillSwitchState:
    """Estado inmutable del kill switch."""

    mode: KillSwitchMode
    reason: str
    activated_at: Optional[str]  # ISO-8601
    activated_by: str  # "manual", "breaker", "oms", etc.

    @property
    def is_active(self) -> bool:
        return self.mode != KillSwitchMode.OFF

    @property
    def blocks_new_orders(self) -> bool:
        return self.mode in (
            KillSwitchMode.BLOCK_NEW,
            KillSwitchMode.CANCEL_OPEN,
            KillSwitchMode.CANCEL_AND_FLATTEN,
        )

    @property
    def should_cancel_open(self) -> bool:
        return self.mode in (
            KillSwitchMode.CANCEL_OPEN,
            KillSwitchMode.CANCEL_AND_FLATTEN,
        )

    @property
    def should_flatten(self) -> bool:
        return self.mode == KillSwitchMode.CANCEL_AND_FLATTEN


class KillSwitch:
    """
    Kill switch persistente.

    Estado almacenado en SQLite — sobrevive reinicios.
    Solo `clear_kill_switch()` desactiva. No hay auto-recovery.
    """

    def __init__(self, db_path: str = "state/kill_switch.db") -> None:
        self.db_path = db_path
        self._init_db()
        self._state: KillSwitchState = self._load_state()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kill_switch (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    mode TEXT NOT NULL DEFAULT 'OFF',
                    reason TEXT NOT NULL DEFAULT '',
                    activated_at TEXT,
                    activated_by TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kill_switch_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    action TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    activated_by TEXT NOT NULL
                )
            """)
            # Ensure row exists
            conn.execute("""
                INSERT OR IGNORE INTO kill_switch (id, mode, reason, activated_at, activated_by)
                VALUES (1, 'OFF', '', NULL, '')
            """)
            conn.commit()

    def _load_state(self) -> KillSwitchState:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT mode, reason, activated_at, activated_by FROM kill_switch WHERE id = 1"
            ).fetchone()
            if row:
                return KillSwitchState(
                    mode=KillSwitchMode(row[0]),
                    reason=row[1] or "",
                    activated_at=row[2],
                    activated_by=row[3] or "",
                )
            return KillSwitchState(
                mode=KillSwitchMode.OFF, reason="", activated_at=None, activated_by=""
            )

    def _persist(self, state: KillSwitchState, action: str) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE kill_switch
                SET mode = ?, reason = ?, activated_at = ?, activated_by = ?
                WHERE id = 1
                """,
                (state.mode.value, state.reason, state.activated_at, state.activated_by),
            )
            conn.execute(
                """
                INSERT INTO kill_switch_log (timestamp, action, mode, reason, activated_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now, action, state.mode.value, state.reason, state.activated_by),
            )
            conn.commit()

    # ──────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────

    @property
    def state(self) -> KillSwitchState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state.is_active

    def activate(
        self,
        mode: KillSwitchMode,
        reason: str,
        activated_by: str = "manual",
    ) -> KillSwitchState:
        """
        Activar kill switch.

        Args:
            mode: Modo de kill switch (BLOCK_NEW, CANCEL_OPEN, CANCEL_AND_FLATTEN)
            reason: Razón de la activación
            activated_by: Quién activó ("manual", "breaker", "oms", etc.)

        Returns:
            Nuevo estado del kill switch.
        """
        if mode == KillSwitchMode.OFF:
            return self.clear(activated_by)

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        new_state = KillSwitchState(
            mode=mode,
            reason=reason,
            activated_at=now,
            activated_by=activated_by,
        )
        self._state = new_state
        self._persist(new_state, "ACTIVATE")

        logger.critical(
            "KILL SWITCH ACTIVATED: mode=%s reason=%s by=%s",
            mode.value,
            reason,
            activated_by,
        )
        return new_state

    def clear(self, cleared_by: str = "manual") -> KillSwitchState:
        """
        Desactivar kill switch.

        Returns:
            Nuevo estado (OFF).
        """
        new_state = KillSwitchState(
            mode=KillSwitchMode.OFF,
            reason="",
            activated_at=None,
            activated_by="",
        )
        self._state = new_state
        self._persist(new_state, f"CLEAR(by={cleared_by})")

        logger.info("KILL SWITCH CLEARED by=%s", cleared_by)
        return new_state

    def get_log(self, limit: int = 50) -> list:
        """Obtener historial de activaciones/desactivaciones."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT timestamp, action, mode, reason, activated_by "
                "FROM kill_switch_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "timestamp": r[0],
                    "action": r[1],
                    "mode": r[2],
                    "reason": r[3],
                    "activated_by": r[4],
                }
                for r in rows
            ]
