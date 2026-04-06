"""
Persistent store for pending execution metadata.

Survives process restarts so that fills arriving after restart
can be reconciled with the original expected_price for real slippage.
"""

import os
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass(frozen=True)
class PendingReport:
    """Metadata stored at submit time, consumed at fill time."""

    client_order_id: str
    symbol: str
    side: str
    expected_price: Decimal
    requested_qty: Decimal
    submit_latency_ms: float
    submit_ts_ms: int


class PendingReportStore:
    """
    SQLite-backed store for pending execution metadata.

    Write at submit → read at fill → delete after report generated.
    """

    def __init__(self, db_path: str = "state/pending_reports.db") -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_reports (
                    client_order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    expected_price TEXT NOT NULL,
                    requested_qty TEXT NOT NULL,
                    submit_latency_ms REAL NOT NULL,
                    submit_ts_ms INTEGER NOT NULL
                )
            """)
            conn.commit()

    def save(self, report: PendingReport) -> None:
        """Persist pending report at submit time."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO pending_reports
                (client_order_id, symbol, side, expected_price,
                 requested_qty, submit_latency_ms, submit_ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report.client_order_id,
                    report.symbol,
                    report.side,
                    str(report.expected_price),
                    str(report.requested_qty),
                    report.submit_latency_ms,
                    report.submit_ts_ms,
                ),
            )
            conn.commit()

    def load(self, client_order_id: str) -> Optional[PendingReport]:
        """Load pending report for fill reconciliation."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM pending_reports WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if row:
                return PendingReport(
                    client_order_id=row[0],
                    symbol=row[1],
                    side=row[2],
                    expected_price=Decimal(row[3]),
                    requested_qty=Decimal(row[4]),
                    submit_latency_ms=row[5],
                    submit_ts_ms=row[6],
                )
            return None

    def delete(self, client_order_id: str) -> None:
        """Remove completed pending report."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM pending_reports WHERE client_order_id = ?",
                (client_order_id,),
            )
            conn.commit()

    def count(self) -> int:
        """Number of pending reports in store."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT COUNT(*) FROM pending_reports").fetchone()
            return row[0] if row else 0

    def cleanup_stale(self, max_age_ms: int = 86400000) -> int:
        """Remove pending reports older than max_age_ms (default 24h)."""
        cutoff = int(time.time() * 1000) - max_age_ms
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM pending_reports WHERE submit_ts_ms < ?",
                (cutoff,),
            )
            conn.commit()
            return cursor.rowcount
