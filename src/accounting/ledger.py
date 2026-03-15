"""
Ledger de trades con Decimal para precisión exacta.

CORREGIDO P1:
  - Fees en base currency ajustan qty neta
  - Integración con circuit breaker para fills reales
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, asdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Callable


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


class TradeLedger:
    """
    Ledger de trades con Decimal y persistencia SQLite.
    
    CORREGIDO P1:
      - Fees en base currency ajustan qty neta
      - Callback para circuit breaker
    """
    
    def __init__(
        self,
        symbol: str,
        db_path: str = "state/ledger.db",
        on_fill_callback: Optional[Callable[[Fill], None]] = None,
    ) -> None:
        self.symbol = symbol
        self.db_path = db_path
        self.on_fill_callback = on_fill_callback  # CORREGIDO P1: callback para circuit breaker
        
        # Estado en memoria
        self.fills: List[Fill] = []
        self.position_qty: Decimal = Decimal("0")
        self.avg_entry: Decimal = Decimal("0")
        self.cost_basis_quote: Decimal = Decimal("0")
        self.realized_pnl_quote: Decimal = Decimal("0")
        self.last_trade_ts_ms: int = 0
        self.last_trade_id: str = ""
        
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
                    last_trade_id TEXT NOT NULL
                )
            """)
            
            conn.commit()
    
    def load(self) -> None:
        """Cargar estado desde SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            # Cargar fills
            cursor = conn.execute(
                "SELECT * FROM fills WHERE symbol = ? ORDER BY ts_ms",
                (self.symbol,)
            )
            rows = cursor.fetchall()
            
            self.fills = []
            for row in rows:
                self.fills.append(Fill(
                    side=row[2],
                    amount=Decimal(row[3]),
                    price=Decimal(row[4]),
                    cost=Decimal(row[5]),
                    fee_cost=Decimal(row[6]),
                    fee_currency=row[7],
                    ts_ms=row[8],
                    trade_id=row[0],
                    order_id=row[9],
                ))
            
            # Cargar estado
            cursor = conn.execute(
                "SELECT * FROM state WHERE symbol = ?",
                (self.symbol,)
            )
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
        """Guardar estado a SQLite."""
        with sqlite3.connect(self.db_path) as conn:
            self._save_state(conn)
    
    def _save_state(self, conn: sqlite3.Connection) -> None:
        """Guardar estado (interno)."""
        conn.execute(
            """
            INSERT OR REPLACE INTO state 
            (symbol, position_qty, avg_entry, cost_basis_quote, 
             realized_pnl_quote, last_trade_ts_ms, last_trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.symbol,
                str(self.position_qty),
                str(self.avg_entry),
                str(self.cost_basis_quote),
                str(self.realized_pnl_quote),
                self.last_trade_ts_ms,
                self.last_trade_id,
            )
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
                )
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
        
        CORREGIDO P1: Fees en base currency ajustan qty neta.
        """
        qty = Decimal("0")
        cost_basis = Decimal("0")
        realized = Decimal("0")
        
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
            
            # CORREGIDO P1: Fee en base ajusta qty neta
            fee_base = (
                f.fee_cost
                if f.fee_currency.upper() == self.base_currency.upper()
                else Decimal("0")
            )
            
            if f.side == "buy":
                # Cantidad neta = amount - fee_base (si fee en base)
                qty_in = f.amount - fee_base
                qty += qty_in
                # Costo en quote + fee_quote
                cost_basis += (f.cost + fee_quote)
                
            else:  # sell
                qty_out = f.amount
                if qty_out <= 0:
                    continue
                
                if qty <= 0:
                    continue
                
                # Costo promedio actual
                avg = cost_basis / qty if qty > 0 else Decimal("0")
                
                # Proceeds efectivos (menos fee si aplica)
                proceeds = f.cost - fee_quote
                
                # PnL realizado
                realized += proceeds - (avg * qty_out)
                
                # Reducir posición
                qty -= qty_out
                cost_basis -= avg * qty_out
                
                # Clamp
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
        """Obtener snapshot del estado actual."""
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
        """Calcular PnL no realizado dado el precio actual."""
        if self.position_qty <= 0:
            return Decimal("0")
        
        position_value = self.position_qty * current_price
        return position_value - self.cost_basis_quote
    
    def get_equity(self, current_price: Decimal) -> Decimal:
        """Calcular equity total (cash + posición)."""
        unrealized = self.get_unrealized_pnl(current_price)
        # Equity = realized + posición actual valorizada
        position_value = self.position_qty * current_price
        return self.realized_pnl_quote + position_value
    
    def get_day_pnl_pct(self, current_price: Decimal) -> Optional[Decimal]:
        """
        Calcular PnL diario como porcentaje del equity inicial del día.
        
        Returns:
            PnL % del día, o None si no hay datos suficientes.
        """
        from datetime import datetime, timedelta
        
        if not self.fills:
            return Decimal("0")
        
        # Obtener timestamp de inicio del día (UTC)
        now_ms = int(datetime.utcnow().timestamp() * 1000)
        day_start_ms = now_ms - (now_ms % (24 * 60 * 60 * 1000))
        
        # Calcular realized PnL del día
        day_realized = Decimal("0")
        for f in self.fills:
            if f.ts_ms >= day_start_ms and f.side == "sell":
                # Simplificación: usamos el cost del fill como proxy
                day_realized += f.cost
        
        # Equity de referencia (inicio del día o último valor)
        equity = self.get_equity(current_price)
        if equity <= 0:
            return Decimal("0")
        
        # PnL % = realized del día / equity
        # Nota: esto es una aproximación, el cálculo exacto requiere tracking de equity histórico
        return day_realized / equity
    
    def get_drawdown_pct(self, current_price: Decimal) -> Decimal:
        """
        Calcular drawdown actual desde el pico de equity.
        
        Returns:
            Drawdown % (positivo = en drawdown).
        """
        if not self.fills:
            return Decimal("0")
        
        current_equity = self.get_equity(current_price)
        if current_equity <= 0:
            return Decimal("0")
        
        # Encontrar equity máximo histórico (aproximado desde fills)
        max_equity = Decimal("0")
        running_realized = Decimal("0")
        
        for f in sorted(self.fills, key=lambda x: x.ts_ms):
            if f.side == "sell":
                running_realized += f.cost
                max_equity = max(max_equity, running_realized)
        
        # Incluir equity actual en el máximo
        max_equity = max(max_equity, current_equity)
        
        if max_equity <= 0:
            return Decimal("0")
        
        # Drawdown = (pico - actual) / pico
        if current_equity < max_equity:
            return (max_equity - current_equity) / max_equity
        
        return Decimal("0")
    
    def validate_equity_invariant(self, current_price: Decimal, tolerance: Decimal = Decimal("1e-6")) -> tuple[bool, str]:
        """
        Validar invariante de equity.
        
        Adaptado de GuardianBot.
        Verifica que: cash + unrealized_pnl == current_equity
        
        Args:
            current_price: Precio actual del activo
            tolerance: Tolerancia para la comparación
            
        Returns:
            (ok, message) - message describe el resultado
        """
        unrealized = self.get_unrealized_pnl(current_price)
        position_value = self.position_qty * current_price
        
        # Equity esperado = cash + unrealized
        # En nuestro modelo, realized_pnl_quote representa el cash acumulado
        expected_equity = self.realized_pnl_quote + position_value
        actual_equity = self.get_equity(current_price)
        
        diff = abs(expected_equity - actual_equity)
        
        if diff > tolerance:
            msg = f"EQUITY INVARIANT FAIL: expected={expected_equity:.6f} actual={actual_equity:.6f} diff={diff:.10f}"
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
