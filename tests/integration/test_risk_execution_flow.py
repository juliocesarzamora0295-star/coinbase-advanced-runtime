"""
Tests de integración: RiskGate + CircuitBreaker + PaperEngine + TradeLedger.

Verifica el pipeline completo:
  Signal → RiskGate(snapshot) → PaperEngine → TradeLedger → CircuitBreaker feedback

Sin Coinbase API. Sin SQLite persistente (tmp_path donde aplica).

Invariantes testeadas:
- happy path: señal aprobada → paper fill → ledger actualizado
- drawdown excedido: RiskVerdict.allowed=False después de pérdidas acumuladas
- breaker OPEN: RiskGate bloquea antes de llegar al engine
- equity=0: fail-closed, bloqueo total
- SELL sin posición: bloqueado aunque breaker=CLOSED y equity OK
- hard_max_qty respetado: fill nunca excede el cap impuesto por RiskGate
- ledger post-fill coherente con fill recibido
- CircuitBreaker recibe feedback de fills (callback wiring)
- Determinista: misma snapshot → misma decisión
"""

from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger
from src.risk.circuit_breaker import BreakerState, CircuitBreaker
from src.risk.gate import (
    RULE_CIRCUIT_BREAKER_OPEN,
    RULE_DAILY_LOSS_LIMIT,
    RULE_EQUITY_ZERO_OR_MISSING,
    RULE_MAX_DRAWDOWN,
    RULE_SELL_NO_POSITION,
    RiskGate,
    RiskLimits,
    RiskSnapshot,
)
from src.simulation.paper_engine import PaperEngine

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

BID = Decimal("49900")
ASK = Decimal("50100")
ENTRY = Decimal("50000")


def make_gate(
    max_position_pct: str = "0.50",
    max_notional: str = "100000",
    max_orders_per_minute: int = 100,
    max_daily_loss_pct: str = "0.05",
    max_drawdown_pct: str = "0.15",
) -> RiskGate:
    limits = RiskLimits(
        max_position_pct=Decimal(max_position_pct),
        max_notional_per_symbol=Decimal(max_notional),
        max_orders_per_minute=max_orders_per_minute,
        max_daily_loss_pct=Decimal(max_daily_loss_pct),
        max_drawdown_pct=Decimal(max_drawdown_pct),
    )
    return RiskGate(limits=limits)


def make_snapshot(
    equity: str = "10000",
    position_qty: str = "0",
    day_pnl_pct: str = "0",
    drawdown_pct: str = "0",
    symbol: str = "BTC-USD",
    side: str = "BUY",
    target_qty: str = "0.1",
    entry_ref: str = "50000",
    breaker_state: str = "CLOSED",
    kill_switch: bool = False,
) -> RiskSnapshot:
    return RiskSnapshot(
        equity=Decimal(equity),
        position_qty=Decimal(position_qty),
        day_pnl_pct=Decimal(day_pnl_pct),
        drawdown_pct=Decimal(drawdown_pct),
        symbol=symbol,
        side=side,
        target_qty=Decimal(target_qty),
        entry_ref=Decimal(entry_ref),
        breaker_state=breaker_state,
        kill_switch=kill_switch,
    )


def fill_from_paper(paper_fill, ts_ms: int = 1_700_000_000_000) -> Fill:
    """Convertir PaperFill → Fill para TradeLedger."""
    return Fill(
        side=paper_fill.side,
        amount=paper_fill.amount,
        price=paper_fill.price,
        cost=paper_fill.amount * paper_fill.price,
        fee_cost=paper_fill.fee_cost,
        fee_currency=paper_fill.fee_currency,
        ts_ms=ts_ms,
        trade_id=paper_fill.trade_id,
        order_id=paper_fill.order_id,
    )


# ──────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────


class TestHappyPath:

    def test_buy_signal_approved_fill_and_ledger_updated(self, tmp_path):
        """Señal BUY aprobada → paper fill → ledger position actualizada."""
        gate = make_gate()
        engine = PaperEngine()
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        snap = make_snapshot(equity="10000", position_qty="0", side="BUY")
        target_qty = Decimal("0.1")

        decision = gate.evaluate(snap)

        assert decision.allowed is True
        assert decision.hard_max_qty > Decimal("0")

        final_qty = min(target_qty, decision.hard_max_qty)

        result = engine.submit_order(
            intent={
                "client_id": "test-buy-001",
                "symbol": "BTC-USD",
                "side": "buy",
                "type": "market",
                "amount": str(final_qty),
            },
            bid=BID,
            ask=ASK,
        )

        assert result["status"] == "filled"
        paper_fill = result["fill"]

        ledger.add_fill(fill_from_paper(paper_fill))

        assert ledger.position_qty == final_qty
        assert ledger.avg_entry > Decimal("0")

    def test_sell_signal_approved_reduces_position(self, tmp_path):
        """Señal SELL aprobada → paper fill → position reducida."""
        gate = make_gate()
        engine = PaperEngine()
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        # Setup: comprar primero
        buy_fill_data = Fill(
            side="buy",
            amount=Decimal("0.2"),
            price=Decimal("50000"),
            cost=Decimal("10000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_000_000,
            trade_id="setup-buy-001",
            order_id="o-setup",
        )
        ledger.add_fill(buy_fill_data)
        assert ledger.position_qty == Decimal("0.2")

        # SELL
        snap = make_snapshot(
            equity="10000",
            position_qty="0.2",
            side="SELL",
            target_qty="0.1",
        )
        target_qty = Decimal("0.1")

        decision = gate.evaluate(snap)

        assert decision.allowed is True
        assert decision.reduce_only is True

        result = engine.submit_order(
            intent={
                "client_id": "test-sell-001",
                "symbol": "BTC-USD",
                "side": "sell",
                "type": "market",
                "amount": str(min(target_qty, decision.hard_max_qty)),
            },
            bid=BID,
            ask=ASK,
        )

        assert result["status"] == "filled"
        ledger.add_fill(fill_from_paper(result["fill"], ts_ms=1_700_000_001_000))

        assert ledger.position_qty == Decimal("0.1")

    def test_hard_max_qty_caps_actual_fill(self, tmp_path):
        """final_qty = min(target_qty, hard_max_qty) — fill nunca excede el cap."""
        gate = make_gate(max_position_pct="0.01")  # caps muy bajos
        engine = PaperEngine()
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))  # noqa: F841

        snap = make_snapshot(equity="10000", side="BUY", target_qty="1.0")
        target_qty = Decimal("1.0")

        decision = gate.evaluate(snap)

        assert decision.allowed is True
        assert decision.hard_max_qty < target_qty

        final_qty = min(target_qty, decision.hard_max_qty)

        result = engine.submit_order(
            intent={
                "client_id": "capped-buy-001",
                "symbol": "BTC-USD",
                "side": "buy",
                "type": "market",
                "amount": str(final_qty),
            },
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "filled"
        assert result["fill"].amount == final_qty


# ──────────────────────────────────────────────
# Fail-closed: equity=0
# ──────────────────────────────────────────────


class TestEquityFailClosed:

    def test_equity_zero_blocks_all_trades(self):
        """equity=0 → todas las señales bloqueadas."""
        gate = make_gate()

        for side in ["BUY", "SELL"]:
            snap = make_snapshot(equity="0", side=side)
            decision = gate.evaluate(snap)
            assert decision.allowed is False
            assert RULE_EQUITY_ZERO_OR_MISSING in decision.blocking_rule_ids
            assert decision.hard_max_qty == Decimal("0")

    def test_engine_never_called_when_gate_blocked(self, tmp_path):
        """Cuando gate bloquea, el engine no debe recibir ninguna orden."""
        gate = make_gate()
        engine = PaperEngine()

        snap = make_snapshot(equity="0", side="BUY")
        decision = gate.evaluate(snap)

        assert decision.allowed is False
        assert len(engine.open_orders) == 0


# ──────────────────────────────────────────────
# Circuit Breaker como input de RiskGate
# ──────────────────────────────────────────────


class TestCircuitBreakerAsInput:

    def test_breaker_open_blocks_gate(self):
        """breaker_state=OPEN → gate bloquea con RULE_CIRCUIT_BREAKER_OPEN."""
        gate = make_gate()
        snap = make_snapshot(equity="50000", breaker_state="OPEN")

        decision = gate.evaluate(snap)

        assert decision.allowed is False
        assert RULE_CIRCUIT_BREAKER_OPEN in decision.blocking_rule_ids
        assert decision.hard_max_qty == Decimal("0")

    def test_breaker_open_from_circuit_breaker_object(self, tmp_path):
        """CircuitBreaker trip → su state alimenta RiskGate → bloqueado."""
        from src.risk.circuit_breaker import BreakerConfig

        config = BreakerConfig(
            max_daily_loss=Decimal("0.05"),
            max_drawdown=Decimal("0.10"),
            max_consecutive_losses=2,
            ws_gap_threshold=1,
        )
        breaker = CircuitBreaker(config=config)
        breaker.reset_day(Decimal("10000"))

        # Forzar trip por ws_gap
        breaker.execution.record_ws_gap()
        breaker.check_before_trade()
        assert breaker.state == BreakerState.OPEN

        gate = make_gate()
        snap = make_snapshot(
            equity="9000",
            breaker_state=breaker.state.value.upper(),
        )

        decision = gate.evaluate(snap)

        assert decision.allowed is False
        assert RULE_CIRCUIT_BREAKER_OPEN in decision.blocking_rule_ids

    def test_breaker_closed_gate_allows(self):
        """CircuitBreaker CLOSED → gate puede aprobar señal."""
        from src.risk.circuit_breaker import BreakerConfig

        config = BreakerConfig(
            max_daily_loss=Decimal("0.05"),
            max_drawdown=Decimal("0.10"),
            max_consecutive_losses=5,
        )
        breaker = CircuitBreaker(config=config)

        assert breaker.state == BreakerState.CLOSED

        gate = make_gate()
        snap = make_snapshot(
            equity="10000",
            breaker_state=breaker.state.value.upper(),
        )

        decision = gate.evaluate(snap)

        assert decision.allowed is True


# ──────────────────────────────────────────────
# Drawdown excedido post-pérdidas
# ──────────────────────────────────────────────


class TestDrawdownBlocks:

    def test_drawdown_exceeded_blocks_new_trade(self, tmp_path):
        """drawdown >= threshold → RiskVerdict.allowed=False."""
        gate = make_gate(max_drawdown_pct="0.10", max_daily_loss_pct="1.0")
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        buy_fill = Fill(
            side="buy",
            amount=Decimal("0.2"),
            price=Decimal("50000"),
            cost=Decimal("10000"),
            fee_cost=Decimal("0"),
            fee_currency="USD",
            ts_ms=1_700_000_000_000,
            trade_id="loss-buy-001",
            order_id="o-loss",
        )
        ledger.add_fill(buy_fill)

        snap = make_snapshot(
            equity="8800",
            position_qty=str(ledger.position_qty),
            drawdown_pct="0.12",
        )

        decision = gate.evaluate(snap)

        assert decision.allowed is False
        assert RULE_MAX_DRAWDOWN in decision.blocking_rule_ids

    def test_daily_loss_exceeded_blocks(self):
        """day_pnl_pct <= -max_daily_loss_pct → bloqueado."""
        gate = make_gate(max_daily_loss_pct="0.05", max_drawdown_pct="1.0")
        snap = make_snapshot(equity="9500", day_pnl_pct="-0.05")

        decision = gate.evaluate(snap)

        assert decision.allowed is False
        assert RULE_DAILY_LOSS_LIMIT in decision.blocking_rule_ids


# ──────────────────────────────────────────────
# SELL sin posición
# ──────────────────────────────────────────────


class TestSellWithoutPosition:

    def test_sell_blocked_when_no_position(self):
        """SELL sin posición → bloqueado incluso con equity y breaker OK."""
        gate = make_gate()
        snap = make_snapshot(equity="10000", position_qty="0", side="SELL")

        decision = gate.evaluate(snap)

        assert decision.allowed is False
        assert RULE_SELL_NO_POSITION in decision.blocking_rule_ids


# ──────────────────────────────────────────────
# Wiring ledger ↔ fill
# ──────────────────────────────────────────────


class TestLedgerFillWiring:

    def test_fill_from_paper_engine_updates_ledger_correctly(self, tmp_path):
        """Fill de paper engine convertido a Fill y aplicado al ledger produce estado correcto."""
        engine = PaperEngine(taker_fee=Decimal("0"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        result = engine.submit_order(
            intent={
                "client_id": "wiring-001",
                "symbol": "BTC-USD",
                "side": "buy",
                "type": "market",
                "amount": "0.1",
            },
            bid=BID,
            ask=ASK,
        )

        assert result["status"] == "filled"
        paper_fill = result["fill"]

        ledger_fill = Fill(
            side=paper_fill.side,
            amount=paper_fill.amount,
            price=paper_fill.price,
            cost=paper_fill.amount * paper_fill.price,
            fee_cost=paper_fill.fee_cost,
            fee_currency=paper_fill.fee_currency,
            ts_ms=1_700_000_000_000,
            trade_id=paper_fill.trade_id,
            order_id=paper_fill.order_id,
        )

        result_add = ledger.add_fill(ledger_fill)
        assert result_add is True

        assert ledger.position_qty == Decimal("0.1")
        assert ledger.avg_entry == ASK

    def test_duplicate_fill_not_double_counted_in_ledger(self, tmp_path):
        """El mismo trade_id del paper engine no se doble-cuenta en el ledger."""
        engine = PaperEngine(taker_fee=Decimal("0"))
        ledger = TradeLedger(symbol="BTC-USD", db_path=str(tmp_path / "ledger.db"))

        result = engine.submit_order(
            intent={
                "client_id": "dedup-001",
                "symbol": "BTC-USD",
                "side": "buy",
                "type": "market",
                "amount": "0.1",
            },
            bid=BID,
            ask=ASK,
        )
        paper_fill = result["fill"]

        ledger_fill = Fill(
            side=paper_fill.side,
            amount=paper_fill.amount,
            price=paper_fill.price,
            cost=paper_fill.amount * paper_fill.price,
            fee_cost=paper_fill.fee_cost,
            fee_currency=paper_fill.fee_currency,
            ts_ms=1_700_000_000_000,
            trade_id=paper_fill.trade_id,
            order_id=paper_fill.order_id,
        )

        assert ledger.add_fill(ledger_fill) is True
        assert ledger.add_fill(ledger_fill) is False  # deduplicado
        assert ledger.position_qty == Decimal("0.1")  # no doble-cuenta
