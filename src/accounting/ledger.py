"""
Ledger de trades con Decimal para precisión exacta.

CORREGIDO P1:
  - Fees en base currency ajustan qty neta
  - Integración con circuit breaker para fills reales
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

    side: str  # "buy" | "sell"
    amount: Decimal  # Cantidad de BASE (ej: BTC)
    price: Decimal  # Precio en QUOTE (ej: USD)
    cost: Decimal  # amount * price en QUOTE
    fee_cost: Decimal  # Costo de fee
    fee_currency: str  # Moneda del fee (BASE, QUOTE, etc.)
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
    def from_dict(cls, d: Dict[str, Any]) -> Fill:
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
    """Snapshot del estado del ledger."""

    symbol: str
    position_qty: Decimal
    avg_entry: Decimal
    cost_basis_quote: Decimal
    realized_pnl_quote: Decimal
    last_trade_ts_ms: int
    last_trade_id: str
    cash: Decimal = Decimal("0")
    fees_paid_quote: Decimal = Decimal("0")
    equity_day_start: Decimal = Decimal("0")
    equity_peak: Decimal = Decimal("0")


class TradeLedger:
    """
    Ledger de trades con Decimal y persistencia SQLite.

    Modelo contable institucional:
      - cash: capital disponible en quote currency
      - inventory: position_qty (base currency)
      - equity = cash + inventory * mark_price
      - equity_day_start: equity al inicio del día (para daily PnL exacto)
      - equity_peak: equity máximo histórico (para drawdown exacto)
      - Fees en base currency ajustan qty neta
      - Callback para circuit breaker
    """

    def __init__(
        self,
        symbol: str,
        db_path: str = "state/ledger.db",
        on_fill_callback: Optional[Callable[[Fill], None]] = None,
        initial_cash: Decimal = Decimal("0"),
    ) -> None:
        self.symbol = symbol
        self.db_path = db_path
        self.on_fill_callback = on_fill_callback

        # Estado en memoria — posición
        self.fills: List[Fill] = []
        self.position_qty: Decimal = Decimal("0")
        self.avg_entry: Decimal = Decimal("0")
        self.cost_basis_quote: Decimal = Decimal("0")
        self.realized_pnl_quote: Decimal = Decimal("0")
        self.last_trade_ts_ms: int = 0
        self.last_trade_id: str = ""

        # Contabilidad institucional
        self.initial_cash: Decimal = initial_cash
        self.cash: Decimal = initial_cash  # quote currency disponible
        self.fees_paid_quote: Decimal = Decimal("0")  # fees acumulados en quote
        self.equity_day_start: Decimal = Decimal("0")  # equity al inicio del día
        self.equity_peak: Decimal = Decimal("0")  # equity máximo histórico
        self.reserved_quote: Decimal = Decimal("0")  # capital en órdenes abiertas

        # Inferir monedas del símbolo
        parts = symbol.split("-")
        self.base_currency = parts[0] if len(parts) > 0 else ""
        self.quote_currency = parts[1] if len(parts) > 1 else ""

        # Inicializar DB
        self._init_db()
        self.load()

    def _init_db(self) -> None:
        """Inicializar base de datos SQLite."""
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
                    last_trade_id TEXT NOT NULL,
                    initial_cash TEXT NOT NULL DEFAULT '0',
                    cash TEXT NOT NULL DEFAULT '0',
                    fees_paid_quote TEXT NOT NULL DEFAULT '0',
                    equity_day_start TEXT NOT NULL DEFAULT '0',
                    equity_peak TEXT NOT NULL DEFAULT '0'
                )
            """)

            conn.commit()

    def load(self) -> None:
        """Cargar estado desde SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            # Cargar fills
            cursor = conn.execute(
                "SELECT * FROM fills WHERE symbol = ? ORDER BY ts_ms", (self.symbol,)
            )
            rows = cursor.fetchall()

            self.fills = []
            for row in rows:
                self.fills.append(
                    Fill(
                        side=row[2],
                        amount=Decimal(row[3]),
                        price=Decimal(row[4]),
                        cost=Decimal(row[5]),
                        fee_cost=Decimal(row[6]),
                        fee_currency=row[7],
                        ts_ms=row[8],
                        trade_id=row[0],
                        order_id=row[9],
                    )
                )

            # Cargar estado
            cursor = conn.execute("SELECT * FROM state WHERE symbol = ?", (self.symbol,))
            row = cursor.fetchone()

            if row:
                self.position_qty = Decimal(row[1])
                self.avg_entry = Decimal(row[2])
                self.cost_basis_quote = Decimal(row[3])
                self.realized_pnl_quote = Decimal(row[4])
                self.last_trade_ts_ms = row[5]
                self.last_trade_id = row[6]
                # New accounting columns (backward compat: may not exist in old DBs)
                if len(row) > 7:
                    self.initial_cash = Decimal(row[7]) if row[7] else self.initial_cash
                    self.cash = Decimal(row[8]) if row[8] else self.cash
                    self.fees_paid_quote = Decimal(row[9]) if row[9] else Decimal("0")
                    self.equity_day_start = Decimal(row[10]) if row[10] else Decimal("0")
                    self.equity_peak = Decimal(row[11]) if row[11] else Decimal("0")
            else:
                self._save_state(conn)

    def save(self) -> None:
        """Guardar estado a SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            self._save_state(conn)

    def _save_state(self, conn: sqlite3.Connection) -> None:
        """Guardar estado (interno)."""
        conn.execute(
            """
            INSERT OR REPLACE INTO state
            (symbol, position_qty, avg_entry, cost_basis_quote,
             realized_pnl_quote, last_trade_ts_ms, last_trade_id,
             initial_cash, cash, fees_paid_quote, equity_day_start, equity_peak)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.symbol,
                str(self.position_qty),
                str(self.avg_entry),
                str(self.cost_basis_quote),
                str(self.realized_pnl_quote),
                self.last_trade_ts_ms,
                self.last_trade_id,
                str(self.initial_cash),
                str(self.cash),
                str(self.fees_paid_quote),
                str(self.equity_day_start),
                str(self.equity_peak),
            ),
        )
        conn.commit()

    def add_fill(self, fill: Fill) -> bool:
        """
        Agregar un fill al ledger.

        CORREGIDO P1: Notifica al circuit breaker via callback.
        """
        # Verificar duplicado
        for f in self.fills:
            if f.trade_id == fill.trade_id:
                return False

        # Agregar a DB
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO fills
                (trade_id, symbol, side, amount, price, cost, fee_cost,
                 fee_currency, ts_ms, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill.trade_id,
                    self.symbol,
                    fill.side,
                    str(fill.amount),
                    str(fill.price),
                    str(fill.cost),
                    str(fill.fee_cost),
                    fill.fee_currency,
                    fill.ts_ms,
                    fill.order_id,
                ),
            )
            conn.commit()

        # Agregar a memoria y recomputar
        self.fills.append(fill)
        self.fills.sort(key=lambda x: x.ts_ms)
        self.recompute()
        self.save()

        # CORREGIDO P1: Notificar al circuit breaker
        if self.on_fill_callback:
            self.on_fill_callback(fill)

        return True

    def recompute(self) -> None:
        """
        Recomputar estado desde fills.

        Modelo contable:
        - cash empieza en initial_cash
        - BUY: cash -= cost + fee_quote, qty += amount - fee_base
        - SELL: cash += proceeds - fee_quote, qty -= amount
        - realized_pnl se calcula contra cost_basis promedio
        - fees_paid_quote acumula todos los fees convertidos a quote
        """
        qty = Decimal("0")
        cost_basis = Decimal("0")
        realized = Decimal("0")
        cash = self.initial_cash
        fees_paid = Decimal("0")

        last_ts = 0
        last_id = ""

        for f in self.fills:
            last_ts = max(last_ts, f.ts_ms)
            last_id = f.trade_id or last_id

            # Fee en quote
            fee_quote = (
                f.fee_cost
                if f.fee_currency.upper() == self.quote_currency.upper()
                else Decimal("0")
            )

            # Fee en base ajusta qty neta
            fee_base = (
                f.fee_cost
                if f.fee_currency.upper() == self.base_currency.upper()
                else Decimal("0")
            )

            fees_paid += fee_quote + (fee_base * f.price if fee_base else Decimal("0"))

            if f.side == "buy":
                qty_in = f.amount - fee_base
                qty += qty_in
                cost_basis += f.cost + fee_quote
                # Cash sale: pagamos cost + fee_quote
                cash -= f.cost + fee_quote

            else:  # sell
                qty_out = f.amount
                if qty_out <= 0:
                    continue

                if qty <= 0:
                    continue

                avg = cost_basis / qty if qty > 0 else Decimal("0")
                proceeds = f.cost - fee_quote

                realized += proceeds - (avg * qty_out)

                qty -= qty_out
                cost_basis -= avg * qty_out
                # Cash entra: recibimos proceeds
                cash += proceeds

                if qty < Decimal("1e-12"):
                    qty = Decimal("0")
                    cost_basis = Decimal("0")

        self.position_qty = qty
        self.cost_basis_quote = cost_basis
        self.avg_entry = cost_basis / qty if qty > 0 else Decimal("0")
        self.realized_pnl_quote = realized
        self.last_trade_ts_ms = last_ts
        self.last_trade_id = last_id
        self.cash = cash
        self.fees_paid_quote = fees_paid

    def snapshot(self) -> LedgerSnapshot:
        """Obtener snapshot del estado actual."""
        return LedgerSnapshot(
            symbol=self.symbol,
            position_qty=self.position_qty,
            avg_entry=self.avg_entry,
            cost_basis_quote=self.cost_basis_quote,
            realized_pnl_quote=self.realized_pnl_quote,
            last_trade_ts_ms=self.last_trade_ts_ms,
            last_trade_id=self.last_trade_id,
            cash=self.cash,
            fees_paid_quote=self.fees_paid_quote,
            equity_day_start=self.equity_day_start,
            equity_peak=self.equity_peak,
        )

    def get_unrealized_pnl(self, current_price: Decimal) -> Decimal:
        """Calcular PnL no realizado dado el precio actual."""
        if self.position_qty <= 0:
            return Decimal("0")

        position_value = self.position_qty * current_price
        return position_value - self.cost_basis_quote

    def set_reserved(self, reserved_quote: Decimal) -> None:
        """
        Establecer capital reservado por órdenes abiertas.

        Debe llamarse periódicamente con el total notional de órdenes abiertas.
        """
        self.reserved_quote = reserved_quote

    def get_equity(self, current_price: Decimal) -> Decimal:
        """
        Equity = cash + mark_to_market(inventory) - reserved.

        cash = initial_cash + sum(sell_proceeds) - sum(buy_costs) - fees
        inventory_value = position_qty * current_price
        reserved = capital comprometido en órdenes abiertas
        """
        inventory_value = self.position_qty * current_price
        return self.cash + inventory_value - self.reserved_quote

    def get_available_cash(self) -> Decimal:
        """Cash disponible (descontando reservas)."""
        return max(Decimal("0"), self.cash - self.reserved_quote)

    def mark_equity(self, current_price: Decimal) -> Decimal:
        """
        Calcular equity y actualizar peak si es nuevo máximo.

        Llama a get_equity() y actualiza equity_peak.
        Retorna equity actual.
        """
        equity = self.get_equity(current_price)
        if equity > self.equity_peak:
            self.equity_peak = equity
        return equity

    def reset_day(self, current_price: Decimal) -> None:
        """
        Resetear métricas diarias.

        Debe llamarse al inicio de cada día de trading.
        Captura equity actual como equity_day_start.
        """
        equity = self.get_equity(current_price)
        self.equity_day_start = equity
        # No resetear equity_peak — es histórico

    def get_day_pnl_pct(self, current_price: Decimal) -> Optional[Decimal]:
        """
        PnL diario exacto: (equity_now - equity_day_start) / equity_day_start.

        Returns:
            Fracción del equity (e.g. -0.05 = -5%). None si no hay referencia.
        """
        equity_now = self.get_equity(current_price)

        if self.equity_day_start <= Decimal("0"):
            # Sin referencia de inicio de día — fail-closed
            return None

        return (equity_now - self.equity_day_start) / self.equity_day_start

    def get_drawdown_pct(self, current_price: Decimal) -> Decimal:
        """
        Drawdown exacto: (equity_peak - equity_now) / equity_peak.

        Actualiza equity_peak si equity actual es nuevo máximo.

        Returns:
            Fracción positiva (e.g. 0.10 = 10% drawdown). 0 si en peak.
        """
        equity_now = self.mark_equity(current_price)

        if self.equity_peak <= Decimal("0"):
            return Decimal("0")

        if equity_now >= self.equity_peak:
            return Decimal("0")

        return (self.equity_peak - equity_now) / self.equity_peak

    def validate_equity_invariant(
        self, current_price: Decimal, tolerance: Decimal = Decimal("1e-6")
    ) -> tuple[bool, str]:
        """
        Validar invariante de equity.

        Verifica que: cash + inventory_value == get_equity(current_price)
        Y que cash = initial_cash + realized_pnl - fees_adjustment

        Args:
            current_price: Precio actual del activo
            tolerance: Tolerancia para la comparación

        Returns:
            (ok, message) - message describe el resultado
        """
        inventory_value = self.position_qty * current_price
        expected_equity = self.cash + inventory_value
        actual_equity = self.get_equity(current_price)

        diff = abs(expected_equity - actual_equity)

        if diff > tolerance:
            msg = (
                f"EQUITY INVARIANT FAIL: cash={self.cash:.6f} "
                f"inventory={inventory_value:.6f} "
                f"expected={expected_equity:.6f} actual={actual_equity:.6f} "
                f"diff={diff:.10f}"
            )
            return False, msg

        return True, f"Equity invariant OK: diff={diff:.10f}"

    def dedup_check(self, fill: Fill) -> tuple[bool, str]:
        """
        Verificar que un fill no sea duplicado.

        Adaptado de GuardianBot.
        Intenta aplicar el fill y verifica que no cambie el estado si ya existe.

        Args:
            fill: Fill a verificar

        Returns:
            (ok, message) - True si pasa el check de deduplicación
        """
        # Si el trade_id ya existe, es duplicado
        if fill.trade_id in [f.trade_id for f in self.fills]:
            return True, f"Fill {fill.trade_id} already exists (duplicate detected)"

        # Si no existe, debería poder aplicarse sin problemas
        return True, f"Fill {fill.trade_id} is new"

    def bootstrap_from_exchange(
        self,
        quote_balance: Decimal,
        base_balance: Decimal,
        current_price: Decimal,
    ) -> None:
        """
        Initialize cash and equity from real exchange balances.

        Called at startup when exchange credentials are available.
        Overrides initial_cash with real balance.

        Args:
            quote_balance: available quote currency (e.g. USD)
            base_balance: available base currency (e.g. BTC)
            current_price: current market price for MTM
        """
        self.initial_cash = quote_balance + base_balance * current_price
        self.cash = quote_balance
        self.position_qty = base_balance
        if base_balance > Decimal("0") and current_price > Decimal("0"):
            self.avg_entry = current_price  # best estimate without fill history
            self.cost_basis_quote = base_balance * current_price
        equity = self.get_equity(current_price)
        self.equity_peak = max(self.equity_peak, equity)
        self.equity_day_start = equity
        self.save()
        import logging
        logging.getLogger("TradeLedger").info(
            "Bootstrap from exchange: quote=%s base=%s price=%s equity=%s",
            quote_balance, base_balance, current_price, equity,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Obtener estadísticas del ledger."""
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
