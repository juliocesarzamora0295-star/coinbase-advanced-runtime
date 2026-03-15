"""
Paper Engine - Motor de simulación de trading para Fortress v4.

Adaptado de GuardianBot con mejoras:
- Uso de Decimal para precisión financiera
- Integración con TradeLedger de Fortress
- Soporte para reduce_only y position_side
"""
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any

logger = logging.getLogger("PaperEngine")


@dataclass
class PaperFill:
    """Fill generado por el paper engine."""
    status: str
    trade_id: str
    order_id: str
    symbol: str
    side: str  # "buy" | "sell"
    position_side: str  # "LONG" | "SHORT"
    reduce_only: bool
    amount: Decimal
    price: Decimal
    fee_cost: Decimal
    fee_currency: str
    timestamp: str


class PaperEngine:
    """
    Motor de paper trading que simula la ejecución de órdenes.
    
    Características:
    - Matching de órdenes limit contra bid/ask
    - Ejecución inmediata de market orders
    - Fees configurables (maker/taker)
    - Tracking de órdenes abiertas
    """

    def __init__(
        self,
        maker_fee: Decimal = Decimal("0.0002"),
        taker_fee: Decimal = Decimal("0.0004"),
    ) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.open_orders: Dict[str, Dict[str, Any]] = {}
        logger.info(f"PaperEngine initialized: maker={maker_fee}, taker={taker_fee}")

    def _generate_trade_id(self, order_id: str) -> str:
        """Generar ID único para fill."""
        return f"paper_{int(time.time() * 1000)}_{order_id}"

    def _create_fill(
        self,
        order: Dict[str, Any],
        amount: Decimal,
        price: Decimal,
        fee_rate: Decimal,
    ) -> PaperFill:
        """Crear un fill de paper trading."""
        fee = amount * price * fee_rate
        return PaperFill(
            status="filled",
            trade_id=self._generate_trade_id(order["client_id"]),
            order_id=order["client_id"],
            symbol=order["symbol"],
            side=order["side"],
            position_side=order.get("position_side", "LONG"),
            reduce_only=bool(order.get("reduce_only", False)),
            amount=amount,
            price=price,
            fee_cost=fee,
            fee_currency=order.get("fee_currency", "USD"),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def submit_order(
        self,
        intent: Dict[str, Any],
        bid: Decimal,
        ask: Decimal,
        bid_size: Decimal = Decimal("1e9"),
        ask_size: Decimal = Decimal("1e9"),
    ) -> Dict[str, Any]:
        """
        Enviar orden al paper engine.
        
        Args:
            intent: Datos de la orden (client_id, symbol, side, type, amount, price)
            bid: Precio bid actual
            ask: Precio ask actual
            bid_size: Tamaño disponible en bid
            ask_size: Tamaño disponible en ask
            
        Returns:
            Dict con status: "filled", "open", o "rejected"
        """
        oid = intent["client_id"]
        side = intent["side"]
        typ = intent["type"]
        amt = Decimal(str(intent["amount"]))
        px = Decimal(str(intent.get("price") or "0"))

        if amt <= 0:
            return {"status": "rejected", "reason": "amount<=0", "order_id": oid}

        # Market order: ejecución inmediata
        if typ == "market":
            exec_px = ask if side == "buy" else bid
            if exec_px <= 0:
                return {"status": "rejected", "reason": "bid/ask<=0", "order_id": oid}
            fill = self._create_fill(intent, amt, exec_px, self.taker_fee)
            return {
                "status": "filled",
                "order_id": oid,
                "fill": fill,
            }

        # Validar limit order
        if typ != "limit":
            return {"status": "rejected", "reason": "bad_type", "order_id": oid}
        if px <= 0:
            return {"status": "rejected", "reason": "limit_price<=0", "order_id": oid}

        # Matching inmediato si el precio cruza el spread
        if side == "buy" and ask > 0 and px >= ask:
            return {
                "status": "filled",
                "order_id": oid,
                "fill": self._create_fill(intent, amt, ask, self.taker_fee),
            }
        if side == "sell" and bid > 0 and px <= bid:
            return {
                "status": "filled",
                "order_id": oid,
                "fill": self._create_fill(intent, amt, bid, self.taker_fee),
            }

        # Guardar orden abierta
        self.open_orders[oid] = {
            "client_id": oid,
            "symbol": intent["symbol"],
            "side": side,
            "type": "limit",
            "amount": amt,
            "price": px,
            "position_side": intent.get("position_side", "LONG"),
            "reduce_only": bool(intent.get("reduce_only", False)),
            "fee_currency": intent.get("fee_currency", "USD"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return {"status": "open", "order_id": oid}

    def on_tick(
        self,
        symbol: str,
        bid: Decimal,
        ask: Decimal,
        bid_size: Decimal = Decimal("1e9"),
        ask_size: Decimal = Decimal("1e9"),
    ) -> List[PaperFill]:
        """
        Procesar tick de mercado y ejecutar órdenes que se crucen.
        
        Args:
            symbol: Símbolo a procesar
            bid: Precio bid actual
            ask: Precio ask actual
            bid_size: Tamaño disponible
            ask_size: Tamaño disponible
            
        Returns:
            Lista de fills generados
        """
        fills: List[PaperFill] = []
        executed: List[str] = []

        for oid, order in list(self.open_orders.items()):
            if order["symbol"] != symbol:
                continue

            side = order["side"]
            px = Decimal(str(order["price"]))
            amt = Decimal(str(order["amount"]))

            # Buy limit: ejecutar si precio >= ask
            if side == "buy" and ask > 0 and px >= ask:
                fills.append(self._create_fill(order, amt, ask, self.maker_fee))
                executed.append(oid)
            # Sell limit: ejecutar si precio <= bid
            elif side == "sell" and bid > 0 and px <= bid:
                fills.append(self._create_fill(order, amt, bid, self.maker_fee))
                executed.append(oid)

        for oid in executed:
            self.open_orders.pop(oid, None)

        return fills

    def cancel_order(self, order_id: str) -> bool:
        """Cancelar orden abierta."""
        if order_id in self.open_orders:
            self.open_orders.pop(order_id, None)
            return True
        return False

    def get_open_orders(self, symbol: Optional[str] = None) -> Dict[str, Dict]:
        """Obtener órdenes abiertas."""
        if symbol:
            return {k: v for k, v in self.open_orders.items() if v["symbol"] == symbol}
        return dict(self.open_orders)

    def force_fill(
        self,
        order_id: str,
        symbol: str,
        price: Decimal,
    ) -> Optional[PaperFill]:
        """
        Forzar ejecución de una orden (para testing).
        
        Args:
            order_id: ID de la orden a ejecutar
            symbol: Símbolo
            price: Precio de ejecución
            
        Returns:
            Fill o None si no existe la orden
        """
        order = self.open_orders.get(order_id)
        if not order or order.get("symbol") != symbol:
            return None

        side = order["side"]
        order_price = Decimal(str(order["price"]))
        amount = Decimal(str(order["amount"]))

        # Verificar que el precio cruza
        bid = price
        ask = price

        if side == "buy" and order_price >= ask:
            fill = self._create_fill(order, amount, ask, self.maker_fee)
            self.open_orders.pop(order_id, None)
            return fill
        elif side == "sell" and order_price <= bid:
            fill = self._create_fill(order, amount, bid, self.maker_fee)
            self.open_orders.pop(order_id, None)
            return fill

        return None
