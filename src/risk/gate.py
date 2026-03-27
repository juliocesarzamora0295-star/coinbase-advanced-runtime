"""
Risk Gate - Pre-trade risk checks.

Evalúa si una orden puede ser ejecutada dado el estado actual del runtime.

Invariantes:
- RiskDecision.allowed=False bloquea la orden incondicionalmente.
- hard_max_qty es un cap, no una sugerencia. Riesgo impone límites, no propone negocio.
- CircuitBreaker es un INPUT de RiskGate (no una puerta paralela).
  Pipeline único: Signal → RiskGate(breaker_state) → allowed/blocked.
- Fail-closed: si falta equity, position, market_price → blocked siempre.
- Ninguna orden puede bypass-ear este gate.
"""
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

logger = logging.getLogger("RiskGate")

# IDs canónicos para blocking_rule_ids
RULE_CIRCUIT_BREAKER_OPEN = "CIRCUIT_BREAKER_OPEN"
RULE_EQUITY_ZERO_OR_MISSING = "EQUITY_ZERO_OR_MISSING"
RULE_DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
RULE_MAX_DRAWDOWN = "MAX_DRAWDOWN"
RULE_MAX_ORDERS_PER_MINUTE = "MAX_ORDERS_PER_MINUTE"
RULE_SELL_NO_POSITION = "SELL_NO_POSITION"
RULE_TARGET_QTY_ZERO = "TARGET_QTY_ZERO"
RULE_MAX_NOTIONAL_EXCEEDED = "MAX_NOTIONAL_EXCEEDED"
RULE_KILL_SWITCH = "KILL_SWITCH"


@dataclass(frozen=True)
class RiskDecision:
    """
    Decisión del risk gate. Inmutable.

    hard_max_qty y hard_max_notional son CAPS, no sugerencias.
    RiskGate impone límites. El dimensionamiento es responsabilidad de PositionSizer.
    """

    allowed: bool
    reason: str
    hard_max_qty: Decimal
    hard_max_notional: Decimal
    reduce_only: bool
    blocking_rule_ids: tuple[str, ...]


@dataclass
class RiskLimits:
    """Límites de riesgo configurables."""

    max_position_pct: Decimal = Decimal("0.20")           # 20% del equity
    max_notional_per_symbol: Decimal = Decimal("10000")   # $10k por símbolo
    max_orders_per_minute: int = 10
    max_daily_loss_pct: Decimal = Decimal("0.05")         # 5%
    max_drawdown_pct: Decimal = Decimal("0.15")           # 15%


@dataclass
class RiskSnapshot:
    """Snapshot de estado de riesgo actual."""

    equity: Decimal
    position_qty: Decimal
    day_pnl_pct: Decimal     # PnL diario como fracción del equity
    drawdown_pct: Decimal    # Drawdown actual como fracción
    orders_last_minute: int = 0


def _blocked(reason: str, *rule_ids: str) -> RiskDecision:
    """Helper: retornar RiskDecision bloqueada."""
    return RiskDecision(
        allowed=False,
        reason=reason,
        hard_max_qty=Decimal("0"),
        hard_max_notional=Decimal("0"),
        reduce_only=False,
        blocking_rule_ids=tuple(rule_ids),
    )


class RiskGate:
    """
    Pre-trade risk gate.

    Valida órdenes antes de enviarlas al exchange.
    CircuitBreaker es un input, no una puerta paralela.

    Checks:
    1. Circuit breaker state
    2. Equity disponible
    3. Daily loss limit
    4. Max drawdown
    5. Max orders per minute
    6. Spot-only: SELL sin posición bloqueado
    7. Max notional por símbolo
    """

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._orders_this_minute: int = 0
        self._last_order_time: float = 0.0

    def evaluate(
        self,
        symbol: str,
        side: str,
        snapshot: RiskSnapshot,
        target_qty: Decimal,
        entry_ref: Decimal,
        breaker_state: str = "CLOSED",
        kill_switch: bool = False,
    ) -> RiskDecision:
        """
        Evaluar si una orden puede ser ejecutada.

        Args:
            symbol: Símbolo a operar.
            side: "BUY" o "SELL".
            snapshot: Snapshot de estado de riesgo actual.
            target_qty: Cantidad objetivo calculada por PositionSizer.
            entry_ref: Precio de referencia para cálculo de notional.
            breaker_state: Estado del CircuitBreaker ("CLOSED", "OPEN", "HALF_OPEN").

        Returns:
            RiskDecision. Si allowed=False, ninguna orden debe generarse.
        """
        equity = snapshot.equity
        position_qty = snapshot.position_qty
        side_upper = side.upper()

        # Check 0: Kill switch — bloqueo total manual
        if kill_switch:
            logger.warning(
                "RiskGate blocked: kill_switch=ON symbol=%s side=%s", symbol, side
            )
            return _blocked("Kill switch is active", RULE_KILL_SWITCH)

        # Check 1: Circuit Breaker (input externo, no puerta paralela)
        if breaker_state == "OPEN":
            logger.warning(
                "RiskGate blocked: circuit breaker OPEN symbol=%s side=%s", symbol, side
            )
            return _blocked(
                "Circuit breaker is OPEN", RULE_CIRCUIT_BREAKER_OPEN
            )

        # Check 2: Equity
        if equity is None or equity <= Decimal("0"):
            logger.warning(
                "RiskGate blocked: equity=%s symbol=%s", equity, symbol
            )
            return _blocked(
                f"Insufficient equity: {equity}", RULE_EQUITY_ZERO_OR_MISSING
            )

        # Check 3: Daily loss limit
        if snapshot.day_pnl_pct <= -self.limits.max_daily_loss_pct:
            logger.warning(
                "RiskGate blocked: daily loss %s symbol=%s", snapshot.day_pnl_pct, symbol
            )
            return _blocked(
                f"Daily loss limit reached: {snapshot.day_pnl_pct:.2%}",
                RULE_DAILY_LOSS_LIMIT,
            )

        # Check 4: Max drawdown
        if snapshot.drawdown_pct >= self.limits.max_drawdown_pct:
            logger.warning(
                "RiskGate blocked: drawdown %s symbol=%s", snapshot.drawdown_pct, symbol
            )
            return _blocked(
                f"Max drawdown reached: {snapshot.drawdown_pct:.2%}",
                RULE_MAX_DRAWDOWN,
            )

        # Check 5: Max orders per minute (snapshot externo + contador interno)
        now = time.time()
        if now - self._last_order_time > 60:
            self._orders_this_minute = 0

        observed_orders = max(self._orders_this_minute, snapshot.orders_last_minute)
        if observed_orders >= self.limits.max_orders_per_minute:
            logger.warning(
                "RiskGate blocked: orders/min=%s symbol=%s", observed_orders, symbol
            )
            return _blocked(
                f"Max orders per minute exceeded: {self.limits.max_orders_per_minute}",
                RULE_MAX_ORDERS_PER_MINUTE,
            )

        # Check 6: target_qty > 0
        if target_qty <= Decimal("0"):
            return _blocked(
                f"target_qty={target_qty} is zero or negative", RULE_TARGET_QTY_ZERO
            )

        # Check 7: Spot-only — SELL sin posición bloqueado
        if side_upper == "SELL" and position_qty <= Decimal("0"):
            logger.warning(
                "RiskGate blocked: SELL without position symbol=%s", symbol
            )
            return _blocked(
                "SELL without position (spot-only mode)", RULE_SELL_NO_POSITION
            )

        # Calcular hard_max_qty
        max_qty_by_equity = (equity * self.limits.max_position_pct) / entry_ref

        if side_upper == "BUY":
            # BUY: cap = espacio disponible hasta max_position
            available_qty = max(Decimal("0"), max_qty_by_equity - max(position_qty, Decimal("0")))
            reduce_only = False
        else:
            # SELL: solo puede vender posición existente (spot-only)
            available_qty = max(Decimal("0"), position_qty)
            reduce_only = True

        hard_max_qty = min(target_qty, available_qty)

        # Check 8: max_notional
        notional = hard_max_qty * entry_ref
        if notional > self.limits.max_notional_per_symbol:
            hard_max_qty = self.limits.max_notional_per_symbol / entry_ref
            notional = self.limits.max_notional_per_symbol
            if hard_max_qty <= Decimal("0"):
                return _blocked(
                    f"Max notional exceeded: {notional} > {self.limits.max_notional_per_symbol}",
                    RULE_MAX_NOTIONAL_EXCEEDED,
                )

        if hard_max_qty <= Decimal("0"):
            return _blocked(
                f"hard_max_qty=0 after applying all caps (side={side}, position={position_qty})",
                RULE_TARGET_QTY_ZERO,
            )

        # Aprobar y registrar
        self._orders_this_minute += 1
        self._last_order_time = now

        logger.debug(
            "RiskGate allowed: symbol=%s side=%s hard_max_qty=%s notional=%s",
            symbol, side, hard_max_qty, notional,
        )

        return RiskDecision(
            allowed=True,
            reason="Risk checks passed",
            hard_max_qty=hard_max_qty,
            hard_max_notional=notional,
            reduce_only=reduce_only,
            blocking_rule_ids=(),
        )

    def pre_order_check(
        self,
        *,
        equity: Decimal,
        price: Decimal,
        amount: Decimal,
        order_type: str,
        side: str,
        position_side: str = "LONG",
        reduce_only: bool = False,
    ) -> tuple[bool, str]:
        """
        Validación de formato/sanity de una orden concreta.

        Este método valida parámetros de orden ya formados.
        No calcula sizing ni computa hard caps.

        Returns:
            (ok, reason) — reason es "ok" si pasa, o el motivo del rechazo.
        """
        if equity <= 0:
            return False, "equity<=0"
        if amount <= 0:
            return False, "amount<=0"
        if price <= 0:
            return False, "price<=0"
        if order_type not in ("market", "limit"):
            return False, "bad_order_type"
        if side not in ("buy", "sell"):
            return False, "bad_side"
        if position_side not in ("LONG", "SHORT"):
            return False, "bad_position_side"

        if reduce_only:
            if position_side == "LONG" and side != "sell":
                return False, "reduce_only_LONG_requires_sell"
            if position_side == "SHORT" and side != "buy":
                return False, "reduce_only_SHORT_requires_buy"
        else:
            if position_side == "LONG" and side != "buy":
                return False, "open_LONG_requires_buy"
            if position_side == "SHORT" and side != "sell":
                return False, "open_SHORT_requires_sell"

        notional = amount * price
        if notional > self.limits.max_notional_per_symbol:
            return False, f"max_notional_exceeded:{notional}>{self.limits.max_notional_per_symbol}"

        return True, "ok"
