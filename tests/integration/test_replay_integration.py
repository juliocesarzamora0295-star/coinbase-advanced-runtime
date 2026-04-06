"""
Replay integration test — alimenta señales mock al pipeline completo.

Verifica que signal → sizing → risk gate → execution → ledger funciona
end-to-end sin Coinbase API.
"""

from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger
from src.execution.execution_report import build_execution_report
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import (
    OrderIntent,
    OrderNotAllowedError,
    OrderPlanner,
    RiskDecisionInput,
)
from src.observability import get_collector, reset_collector
from src.oms.reconcile import OMSReconcileService
from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskVerdict
from src.risk.kill_switch import KillSwitch, KillSwitchMode
from src.risk.position_sizer import PositionSizer, SymbolConstraints
from src.simulation.paper_engine import PaperEngine


CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("Infinity"),
    min_notional=Decimal("1"),
)


def _run_signal(
    *,
    side: str,
    price: Decimal,
    ledger: TradeLedger,
    gate: RiskGate,
    sizer: PositionSizer,
    planner: OrderPlanner,
    breaker: CircuitBreaker,
    ks: KillSwitch,
    oms: OMSReconcileService,
    engine: PaperEngine,
    signal_id: str = "sig-1",
) -> dict:
    """Run one signal through the full pipeline. Returns result dict."""
    # Gates
    if ks.state.blocks_new_orders:
        return {"blocked": "kill_switch"}
    if not oms.is_ready():
        return {"blocked": "oms_not_ready"}
    ok, reason = breaker.check_before_trade()
    if not ok:
        return {"blocked": f"breaker: {reason}"}

    # Equity
    equity = ledger.get_equity(price)
    breaker.update_equity(equity)

    # Sizing
    sizing = sizer.compute(
        symbol="BTC-USD",
        equity=equity,
        entry_price=price,
        notional_pct=Decimal("0.10"),  # 10% of equity
        constraints=CONSTRAINTS,
        max_notional=Decimal("10000"),
    )
    if sizing.target_qty <= Decimal("0"):
        return {"blocked": "sizing_zero"}

    # Risk gate
    snap = RiskSnapshot(
        equity=equity,
        position_qty=ledger.position_qty,
        day_pnl_pct=Decimal("0"),
        drawdown_pct=Decimal("0"),
        symbol="BTC-USD",
        side=side,
        target_qty=sizing.target_qty,
        entry_ref=price,
        breaker_state=breaker.state.value.upper(),
    )
    verdict = gate.evaluate(snap)
    if not verdict.allowed:
        return {"blocked": f"riskgate: {verdict.reason}"}

    # OrderPlanner
    risk_input = RiskDecisionInput(
        allowed=verdict.allowed,
        hard_max_qty=verdict.hard_max_qty,
        hard_max_notional=verdict.hard_max_notional,
        reduce_only=verdict.reduce_only,
        reason=verdict.reason,
    )
    intent = planner.plan(
        signal_id=signal_id,
        strategy_id="test-strategy",
        symbol="BTC-USD",
        side=side,
        sizing=sizing,
        risk=risk_input,
        constraints=CONSTRAINTS,
    )
    if not intent.viable:
        return {"blocked": "not_viable"}

    # Execute via PaperEngine
    bid = price * Decimal("0.999")
    ask = price * Decimal("1.001")
    paper_intent = {
        "client_id": intent.client_order_id,
        "symbol": "BTC-USD",
        "side": side.lower(),
        "type": "market",
        "amount": intent.final_qty,
    }
    result = engine.submit_order(paper_intent, bid, ask)
    if result.get("status") != "filled":
        return {"blocked": "paper_engine_reject"}

    fill = result["fill"]

    # Apply fill to ledger
    ledger_fill = Fill(
        side=fill.side,
        amount=fill.amount,
        price=fill.price,
        cost=fill.amount * fill.price,
        fee_cost=fill.fee_cost,
        fee_currency=fill.fee_currency,
        ts_ms=1_700_000_000_000,
        trade_id=fill.trade_id,
        order_id=fill.order_id,
    )
    ledger.add_fill(ledger_fill)

    # ExecutionReport
    report = build_execution_report(
        client_order_id=intent.client_order_id,
        symbol="BTC-USD",
        side=side,
        expected_price=price,
        fill_price=fill.price,
        requested_qty=intent.final_qty,
        filled_qty=fill.amount,
        latency_ms=0.0,
        outcome="FILLED",
    )

    # Feed to breaker
    breaker.record_slippage(float(report.slippage_bps))
    breaker.record_execution_result(True)

    return {
        "filled": True,
        "qty": fill.amount,
        "price": fill.price,
        "slippage_bps": report.slippage_bps,
        "quality": report.fill_quality_score,
    }


class TestReplayBuySellCycle:
    """BUY → SELL cycle through full pipeline."""

    def test_buy_then_sell(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"), initial_cash=Decimal("10000"))
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        gate = RiskGate(RiskLimits())
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=ledger,
        )
        oms.handle_user_event("snapshot", [])
        sizer = PositionSizer()
        planner = OrderPlanner()
        engine = PaperEngine(taker_fee=Decimal("0"))

        common = dict(
            ledger=ledger, gate=gate, sizer=sizer, planner=planner,
            breaker=breaker, ks=ks, oms=oms, engine=engine,
        )

        # BUY at 50000
        r1 = _run_signal(side="BUY", price=Decimal("50000"), signal_id="buy-1", **common)
        assert r1.get("filled"), f"BUY blocked: {r1}"
        assert ledger.position_qty > Decimal("0")

        # SELL at 51000 — sell entire position
        # Override target_qty to match position for exact close
        sell_snap = RiskSnapshot(
            equity=ledger.get_equity(Decimal("51000")),
            position_qty=ledger.position_qty,
            day_pnl_pct=Decimal("0"),
            drawdown_pct=Decimal("0"),
            symbol="BTC-USD",
            side="SELL",
            target_qty=ledger.position_qty,
            entry_ref=Decimal("51000"),
            breaker_state=breaker.state.value.upper(),
        )
        verdict = gate.evaluate(sell_snap)
        assert verdict.allowed, f"SELL blocked: {verdict.reason}"

        sell_qty = min(ledger.position_qty, verdict.hard_max_qty)
        paper_sell = {
            "client_id": "sell-close",
            "symbol": "BTC-USD",
            "side": "sell",
            "type": "market",
            "amount": sell_qty,
        }
        sell_result = engine.submit_order(
            paper_sell,
            bid=Decimal("51000") * Decimal("0.999"),
            ask=Decimal("51000") * Decimal("1.001"),
        )
        assert sell_result["status"] == "filled"
        sell_fill = sell_result["fill"]
        ledger.add_fill(Fill(
            side=sell_fill.side,
            amount=sell_fill.amount,
            price=sell_fill.price,
            cost=sell_fill.amount * sell_fill.price,
            fee_cost=sell_fill.fee_cost,
            fee_currency=sell_fill.fee_currency,
            ts_ms=1_700_000_001_000,
            trade_id=sell_fill.trade_id,
            order_id=sell_fill.order_id,
        ))

        # Position should be closed (or near zero)
        assert ledger.position_qty <= Decimal("0.00001")

        # Verify equity increased (sold at higher price)
        final_equity = ledger.get_equity(Decimal("51000"))
        assert final_equity > Decimal("10000")


class TestReplayKillSwitchBlocks:
    """Kill switch blocks signal in replay."""

    def test_kill_switch_blocks_buy(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"), initial_cash=Decimal("10000"))
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        gate = RiskGate(RiskLimits())
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        ks.activate(KillSwitchMode.BLOCK_NEW, "test", "ci")
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=ledger,
        )
        oms.handle_user_event("snapshot", [])

        r = _run_signal(
            side="BUY", price=Decimal("50000"),
            ledger=ledger, gate=gate, sizer=PositionSizer(),
            planner=OrderPlanner(), breaker=breaker, ks=ks, oms=oms,
            engine=PaperEngine(),
        )
        assert r.get("blocked") == "kill_switch"
        assert ledger.position_qty == Decimal("0")


class TestReplayBreakerTrip:
    """Breaker trips during replay."""

    def test_breaker_open_blocks(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"), initial_cash=Decimal("10000"))
        breaker = CircuitBreaker(BreakerConfig(ws_gap_threshold=1))
        breaker.reset_day(Decimal("10000"))
        breaker.execution.record_ws_gap()  # trip breaker

        gate = RiskGate(RiskLimits())
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=ledger,
        )
        oms.handle_user_event("snapshot", [])

        r = _run_signal(
            side="BUY", price=Decimal("50000"),
            ledger=ledger, gate=gate, sizer=PositionSizer(),
            planner=OrderPlanner(), breaker=breaker, ks=ks, oms=oms,
            engine=PaperEngine(),
        )
        assert "breaker" in r.get("blocked", "")


class TestReplayOmsNotReady:
    """OMS not ready blocks replay."""

    def test_oms_not_bootstrapped_blocks(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"), initial_cash=Decimal("10000"))
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        gate = RiskGate(RiskLimits())
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=ledger,
        )
        # No bootstrap — OMS not ready

        r = _run_signal(
            side="BUY", price=Decimal("50000"),
            ledger=ledger, gate=gate, sizer=PositionSizer(),
            planner=OrderPlanner(), breaker=breaker, ks=ks, oms=oms,
            engine=PaperEngine(),
        )
        assert r.get("blocked") == "oms_not_ready"


class TestReplayExecutionReport:
    """ExecutionReport generated from fills."""

    def test_report_has_real_slippage(self, tmp_path):
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"), initial_cash=Decimal("10000"))
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        gate = RiskGate(RiskLimits())
        ks = KillSwitch(db_path=str(tmp_path / "ks.db"))
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=ledger,
        )
        oms.handle_user_event("snapshot", [])

        r = _run_signal(
            side="BUY", price=Decimal("50000"),
            ledger=ledger, gate=gate, sizer=PositionSizer(),
            planner=OrderPlanner(), breaker=breaker, ks=ks, oms=oms,
            engine=PaperEngine(taker_fee=Decimal("0")),
        )
        assert r.get("filled")
        # PaperEngine fills at ask (price * 1.001) — slippage is real
        assert "slippage_bps" in r
        assert "quality" in r
