"""
Extracted: Spot Ledger with VWAP and PnL from Kimi_Agent fortress_v4.

Origin: Kimi_Agent_Especificacion API Coinbase/fortress_v4/src/accounting/ledger.py
Reason: Minimal viable ledger — the original version before institutional accounting
        was added to coinbase-advanced-runtime. Useful as a reference for the core
        VWAP/cost-basis algorithm without cash-tracking complexity.

Differences vs src/accounting/ledger.py (canonical):
  - No initial_cash, cash, fees_paid_quote, equity_day_start, equity_peak tracking
  - No reserved_quote or set_reserved()
  - No bootstrap_from_exchange()
  - No mark_equity() or reset_day()
  - LedgerSnapshot lacks cash/fees/equity fields
  - get_equity() uses realized_pnl + position_value (not cash-based)
  - get_day_pnl_pct() approximates daily PnL from fills (not equity_day_start)
  - get_drawdown_pct() reconstructs peak from running_realized (not tracked peak)
  - Same core recompute() VWAP algorithm with fee_base/fee_quote handling

Verdict: canonical version is strictly superior for production. This version is
         useful as documentation of the minimal VWAP accounting model.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Fill:
    """Registro normalizado de un fill (trade)."""
    side: str
    amount: Decimal
    price: Decimal
    cost: Decimal
    fee_cost: Decimal
    fee_currency: str
    ts_ms: int
    trade_id: str
    order_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "amount": str(self.amount),
            "price": str(self.price),
            "cost": str(self.cost),
            "fee_cost": str(self.fee_cost),
            "fee_currency": self.fee_currency,
            "ts_ms": self.ts_ms,
            "trade_id": self.trade_id,
            "order_id": self.order_id,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Fill":
        return cls(
            side=d["side"],
            amount=Decimal(d["amount"]),
            price=Decimal(d["price"]),
            cost=Decimal(d["cost"]),
            fee_cost=Decimal(d["fee_cost"]),
            fee_currency=d["fee_currency"],
            ts_ms=d["ts_ms"],
            trade_id=d["trade_id"],
            order_id=d["order_id"],
        )


@dataclass
class LedgerSnapshot:
    """Snapshot del estado del ledger (minimal version, no cash tracking)."""
    symbol: str
    position_qty: Decimal
    avg_entry: Decimal
    cost_basis_quote: Decimal
    realized_pnl_quote: Decimal
    last_trade_ts_ms: int
    last_trade_id: str


class TradeLedger:
    """
    Minimal spot ledger with VWAP and PnL tracking.

    This is the pre-institutional version: no cash tracking, no reserved_quote,
    no bootstrap_from_exchange. Core algorithm is identical to canonical version.

    TODO: fortress-v4 integration — if you need cash-aware equity, use
          src/accounting/ledger.py instead. This version is for reference only.
    """

    def __init__(
        self,
        symbol: str,
        db_path: str = "state/extracted_ledger.db",
        on_fill_callback: Optional[Callable[["Fill"], None]] = None,
    ) -> None:
        self.symbol = symbol
        self.db_path = db_path
        self.on_fill_callback = on_fill_callback

        self.fills: List[Fill] = []
        self.position_qty: Decimal = Decimal("0")
        self.avg_entry: Decimal = Decimal("0")
        self.cost_basis_quote: Decimal = Decimal("0")
        self.realized_pnl_quote: Decimal = Decimal("0")
        self.last_trade_ts_ms: int = 0
        self.last_trade_id: str = ""

        parts = symbol.split("-")
        self.base_currency = parts[0] if len(parts) > 0 else ""
        self.quote_currency = parts[1] if len(parts) > 1 else ""

        self._init_db()
        self.load()

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fills (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    price TEXT NOT NULL,
                    cost TEXT NOT NULL,
                    fee_cost TEXT NOT NULL,
                    fee_currency TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    order_id TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    symbol TEXT PRIMARY KEY,
                    position_qty TEXT NOT NULL,
                    avg_entry TEXT NOT NULL,
                    cost_basis_quote TEXT NOT NULL,
                    realized_pnl_quote TEXT NOT NULL,
                    last_trade_ts_ms INTEGER NOT NULL,
                    last_trade_id TEXT NOT NULL
                )
            """)
            conn.commit()

    def load(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT * FROM fills WHERE symbol = ? ORDER BY ts_ms",
                (self.symbol,)
            )
            rows = cursor.fetchall()
            self.fills = []
            for row in rows:
                self.fills.append(Fill(
                    side=row[2], amount=Decimal(row[3]), price=Decimal(row[4]),
                    cost=Decimal(row[5]), fee_cost=Decimal(row[6]),
                    fee_currency=row[7], ts_ms=row[8], trade_id=row[0],
                    order_id=row[9],
                ))

            cursor = conn.execute("SELECT * FROM state WHERE symbol = ?", (self.symbol,))
            row = cursor.fetchone()
            if row:
                self.position_qty = Decimal(row[1])
                self.avg_entry = Decimal(row[2])
                self.cost_basis_quote = Decimal(row[3])
                self.realized_pnl_quote = Decimal(row[4])
                self.last_trade_ts_ms = row[5]
                self.last_trade_id = row[6]
            else:
                self._save_state(conn)

    def save(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            self._save_state(conn)

    def _save_state(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """INSERT OR REPLACE INTO state
            (symbol, position_qty, avg_entry, cost_basis_quote,
             realized_pnl_quote, last_trade_ts_ms, last_trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (self.symbol, str(self.position_qty), str(self.avg_entry),
             str(self.cost_basis_quote), str(self.realized_pnl_quote),
             self.last_trade_ts_ms, self.last_trade_id),
        )
        conn.commit()

    def add_fill(self, fill: Fill) -> bool:
        for f in self.fills:
            if f.trade_id == fill.trade_id:
                return False

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO fills
                (trade_id, symbol, side, amount, price, cost, fee_cost,
                 fee_currency, ts_ms, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fill.trade_id, self.symbol, fill.side, str(fill.amount),
                 str(fill.price), str(fill.cost), str(fill.fee_cost),
                 fill.fee_currency, fill.ts_ms, fill.order_id),
            )
            conn.commit()

        self.fills.append(fill)
        self.fills.sort(key=lambda x: x.ts_ms)
        self.recompute()
        self.save()

        # TODO: fortress-v4 integration — callback wires to CircuitBreaker.on_fill()
        if self.on_fill_callback:
            self.on_fill_callback(fill)

        return True

    def recompute(self) -> None:
        """
        Core VWAP algorithm. Identical to canonical version's core logic,
        but without cash/fees_paid tracking.
        """
        qty = Decimal("0")
        cost_basis = Decimal("0")
        realized = Decimal("0")
        last_ts = 0
        last_id = ""

        for f in self.fills:
            last_ts = max(last_ts, f.ts_ms)
            last_id = f.trade_id or last_id

            fee_quote = (
                f.fee_cost
                if f.fee_currency.upper() == self.quote_currency.upper()
                else Decimal("0")
            )
            fee_base = (
                f.fee_cost
                if f.fee_currency.upper() == self.base_currency.upper()
                else Decimal("0")
            )

            if f.side == "buy":
                qty_in = f.amount - fee_base
                qty += qty_in
                cost_basis += (f.cost + fee_quote)
            else:  # sell
                qty_out = f.amount
                if qty_out <= 0 or qty <= 0:
                    continue
                avg = cost_basis / qty if qty > 0 else Decimal("0")
                proceeds = f.cost - fee_quote
                realized += proceeds - (avg * qty_out)
                qty -= qty_out
                cost_basis -= avg * qty_out
                if qty < Decimal("1e-12"):
                    qty = Decimal("0")
                    cost_basis = Decimal("0")

        self.position_qty = qty
        self.cost_basis_quote = cost_basis
        self.avg_entry = cost_basis / qty if qty > 0 else Decimal("0")
        self.realized_pnl_quote = realized
        self.last_trade_ts_ms = last_ts
        self.last_trade_id = last_id

    def snapshot(self) -> LedgerSnapshot:
        return LedgerSnapshot(
            symbol=self.symbol,
            position_qty=self.position_qty,
            avg_entry=self.avg_entry,
            cost_basis_quote=self.cost_basis_quote,
            realized_pnl_quote=self.realized_pnl_quote,
            last_trade_ts_ms=self.last_trade_ts_ms,
            last_trade_id=self.last_trade_id,
        )

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        if self.position_qty <= 0:
            return Decimal("0")
        position_value = self.position_qty * current_price
        return position_value - self.cost_basis_quote

    def get_equity(self, current_price: Decimal) -> Decimal:
        """Equity = realized + position_value (no cash model)."""
        position_value = self.position_qty * current_price
        return self.realized_pnl_quote + position_value

    def get_stats(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_fills": len(self.fills),
            "position_qty": str(self.position_qty),
            "avg_entry": str(self.avg_entry),
            "realized_pnl": str(self.realized_pnl_quote),
            "cost_basis": str(self.cost_basis_quote),
            "last_trade_id": self.last_trade_id,
            "last_trade_ts": self.last_trade_ts_ms,
        }
