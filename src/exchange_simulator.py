"""
Exchange Simulator — mimics Coinbase Advanced Trade API for shadow testing.

Deterministic when seeded. Generates realistic orderbook, fills with
slippage, simulated latency, and maintains order state.
"""

import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ExchangeSimulator")

ZERO = Decimal("0")


@dataclass
class SimulatedOrder:
    """Order tracked by the simulator."""

    order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: Decimal
    price: Optional[Decimal]
    order_type: str
    status: str  # PENDING, FILLED, CANCELLED
    fill_price: Decimal = ZERO
    fill_qty: Decimal = ZERO
    created_ms: int = 0


class ExchangeSimulator:
    """
    Simulates Coinbase Advanced Trade API for shadow testing.

    Features:
    - Synthetic orderbook with configurable spread
    - Fill simulation with slippage
    - Configurable API latency
    - Deterministic with seed
    - JSON Lines operation log
    """

    def __init__(
        self,
        initial_price: float = 50000.0,
        spread_pct: float = 0.02,
        slippage_bps: float = 2.0,
        latency_ms: float = 100.0,
        initial_balances: Optional[Dict[str, float]] = None,
        seed: int = 42,
        log_path: Optional[str] = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._price = initial_price
        self._spread_pct = spread_pct / 100.0
        self._slippage_bps = slippage_bps
        self._latency_ms = latency_ms
        self._order_counter = 0
        self._orders: Dict[str, SimulatedOrder] = {}
        self._balances: Dict[str, float] = initial_balances or {
            "USD": 10000.0, "BTC": 0.0, "ETH": 0.0,
        }
        self._log_path = log_path
        self._log_file = None
        if log_path:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            self._log_file = open(log_path, "a")

        # Price history for random walk
        self._price_history: List[float] = [initial_price]
        self._tick_count = 0

    def _log_op(self, op: str, data: Dict[str, Any]) -> None:
        if self._log_file:
            record = {"ts": int(time.time() * 1000), "op": op, **data}
            self._log_file.write(json.dumps(record, default=str) + "\n")
            self._log_file.flush()

    def _simulate_latency(self) -> None:
        """Simulate API latency (jittered)."""
        jitter = self._rng.uniform(0.5, 1.5)
        delay = (self._latency_ms * jitter) / 1000.0
        time.sleep(delay)

    def _advance_price(self) -> None:
        """Random walk price evolution."""
        self._tick_count += 1
        ret = self._rng.gauss(0, 0.001)  # ~0.1% per tick
        self._price *= (1 + ret)
        self._price_history.append(self._price)

    # ── Public API (mimics exchange) ──

    def get_ticker(self, symbol: str = "BTC-USD") -> Dict[str, Any]:
        """Get current price. Advances price on each call."""
        self._simulate_latency()
        self._advance_price()
        bid = self._price * (1 - self._spread_pct / 2)
        ask = self._price * (1 + self._spread_pct / 2)
        result = {
            "symbol": symbol,
            "price": str(round(self._price, 2)),
            "bid": str(round(bid, 2)),
            "ask": str(round(ask, 2)),
            "spread_pct": round(self._spread_pct * 100, 4),
            "timestamp_ms": int(time.time() * 1000),
        }
        self._log_op("ticker", result)
        return result

    def get_balances(self) -> List[Dict[str, Any]]:
        """Get account balances."""
        self._simulate_latency()
        return [
            {"currency": k, "available_balance": {"value": str(v)}}
            for k, v in self._balances.items()
        ]

    def place_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Place an order. Market orders fill immediately with slippage.
        Limit orders rest until explicitly filled.
        """
        if qty <= 0:
            return {"order_id": "", "client_order_id": client_order_id,
                    "status": "REJECTED", "fill_price": "0", "fill_qty": "0",
                    "error": "invalid_qty"}
        if side.upper() not in ("BUY", "SELL"):
            return {"order_id": "", "client_order_id": client_order_id,
                    "status": "REJECTED", "fill_price": "0", "fill_qty": "0",
                    "error": "invalid_side"}

        self._simulate_latency()
        self._order_counter += 1
        order_id = f"sim-{self._order_counter:06d}"

        order = SimulatedOrder(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side.upper(),
            qty=Decimal(str(qty)),
            price=Decimal(str(price)) if price else None,
            order_type=order_type.upper(),
            status="PENDING",
            created_ms=int(time.time() * 1000),
        )

        if order_type.upper() == "MARKET":
            # Fill immediately with slippage
            slip_mult = self._slippage_bps / 10000.0
            if side.upper() == "BUY":
                fill_price = self._price * (1 + self._spread_pct / 2 + slip_mult)
            else:
                fill_price = self._price * (1 - self._spread_pct / 2 - slip_mult)

            order.fill_price = Decimal(str(round(fill_price, 2)))
            order.fill_qty = order.qty
            order.status = "FILLED"

            # Check sufficient balance before fill
            notional = float(order.fill_qty) * float(order.fill_price)
            base = symbol.split("-")[0] if "-" in symbol else symbol
            quote = symbol.split("-")[1] if "-" in symbol else "USD"

            if side.upper() == "BUY":
                available = self._balances.get(quote, 0)
                if available < notional:
                    order.status = "REJECTED"
                    self._orders[order_id] = order
                    result = {"order_id": order_id, "client_order_id": client_order_id,
                              "status": "REJECTED", "fill_price": "0", "fill_qty": "0",
                              "error": "insufficient_funds"}
                    self._log_op("reject", result)
                    return result
                self._balances[quote] = available - notional
                self._balances[base] = self._balances.get(base, 0) + float(order.fill_qty)
            else:
                available = self._balances.get(base, 0)
                if available < float(order.fill_qty):
                    order.status = "REJECTED"
                    self._orders[order_id] = order
                    result = {"order_id": order_id, "client_order_id": client_order_id,
                              "status": "REJECTED", "fill_price": "0", "fill_qty": "0",
                              "error": "insufficient_funds"}
                    self._log_op("reject", result)
                    return result
                self._balances[quote] = self._balances.get(quote, 0) + notional
                self._balances[base] = available - float(order.fill_qty)

        self._orders[order_id] = order

        result = {
            "order_id": order_id,
            "client_order_id": client_order_id,
            "status": order.status,
            "fill_price": str(order.fill_price),
            "fill_qty": str(order.fill_qty),
        }
        self._log_op("place_order", result)
        return result

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a pending order."""
        self._simulate_latency()
        order = self._orders.get(order_id)
        if not order:
            return {"success": False, "error": "order not found"}
        if order.status != "PENDING":
            return {"success": False, "error": f"order status is {order.status}"}
        order.status = "CANCELLED"
        self._log_op("cancel", {"order_id": order_id})
        return {"success": True, "order_id": order_id}

    def get_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get orders, optionally filtered by status."""
        self._simulate_latency()
        orders = list(self._orders.values())
        if status:
            orders = [o for o in orders if o.status == status.upper()]
        return [
            {
                "order_id": o.order_id,
                "client_order_id": o.client_order_id,
                "symbol": o.symbol,
                "side": o.side,
                "qty": str(o.qty),
                "status": o.status,
                "fill_price": str(o.fill_price),
                "fill_qty": str(o.fill_qty),
            }
            for o in orders
        ]

    def close(self) -> None:
        """Close log file."""
        if self._log_file:
            self._log_file.close()
            self._log_file = None
