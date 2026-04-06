"""
Mock exchange integration test — simulates WebSocket + API without credentials.

Verifies the full data flow: market data → signal → order pipeline
using mocked exchange responses.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from src.accounting.ledger import Fill, TradeLedger
from src.execution.execution_report import build_execution_report
from src.execution.idempotency import IdempotencyStore, OrderState
from src.execution.order_planner import (
    OrderIntent,
    OrderPlanner,
    RiskDecisionInput,
)
from src.observability import get_collector, reset_collector
from src.oms.reconcile import OMSReconcileService
from src.risk.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot
from src.risk.kill_switch import KillSwitch
from src.risk.position_sizer import PositionSizer, SizingMode, SymbolConstraints
from src.simulation.paper_engine import PaperEngine


CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.00001"),
    min_qty=Decimal("0.00001"),
    max_qty=Decimal("Infinity"),
    min_notional=Decimal("1"),
)


class TestMockExchangeFullPipeline:
    """Simulate full exchange interaction with mocks."""

    def test_ws_snapshot_bootstraps_oms(self, tmp_path):
        """WebSocket snapshot event bootstraps OMS and enables trading."""
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        oms = OMSReconcileService(
            idempotency=idem,
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        assert not oms.is_ready()

        # Pre-populate idempotency store with known orders
        for i in range(3):
            intent = OrderIntent(
                client_order_id=f"c-{i}",
                signal_id="test", strategy_id="test",
                symbol="BTC-USD", side="BUY",
                final_qty=Decimal("0.01"), order_type="LIMIT",
                price=Decimal("50000"), reduce_only=False,
                post_only=False, viable=True, planner_version="test",
            )
            idem.save_intent(intent, OrderState.OPEN_RESTING)

        # Simulate WS snapshot (< 50 orders = bootstrap complete)
        ws_snapshot = [
            {"order_id": f"o-{i}", "client_order_id": f"c-{i}",
             "product_id": "BTC-USD", "status": "OPEN", "number_of_fills": "0"}
            for i in range(3)
        ]
        oms.handle_user_event("snapshot", ws_snapshot)
        assert oms.is_ready()

    def test_ws_fill_event_updates_ledger(self, tmp_path):
        """WebSocket fill event → OMS reconcile → ledger updated."""
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"),
                             initial_cash=Decimal("10000"))
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))

        # Pre-populate order
        intent = OrderIntent(
            client_order_id="ws-fill-client",
            signal_id="sig-1", strategy_id="strat-1",
            symbol="BTC-USD", side="BUY",
            final_qty=Decimal("0.1"), order_type="MARKET",
            price=None, reduce_only=False, post_only=False,
            viable=True, planner_version="test",
        )
        idem.save_intent(intent, OrderState.OPEN_PENDING)

        # Mock fill fetcher (simulates REST list_fills)
        mock_fetcher = MagicMock(return_value=[{
            "trade_id": "fill-ws-1",
            "side": "BUY",
            "size": "0.1",
            "price": "50050",
            "commission": "5",
            "trade_time": "2024-06-01T12:00:00Z",
        }])

        fill_applied = []

        def on_fill(cid, price, qty, sym, side):
            fill_applied.append((cid, price, qty))

        oms = OMSReconcileService(
            idempotency=idem, ledger=ledger,
            fill_fetcher=mock_fetcher,
            on_fill_applied=on_fill,
        )
        oms.handle_user_event("snapshot", [])  # bootstrap

        # Simulate WS update with fill
        ws_update = {
            "order_id": "ex-ws-1",
            "client_order_id": "ws-fill-client",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }
        oms.handle_user_event("update", [ws_update])

        # Verify fill applied
        assert len(fill_applied) == 1
        assert fill_applied[0][0] == "ws-fill-client"
        assert ledger.position_qty == Decimal("0.1")

    def test_ws_orphan_degrades_oms(self, tmp_path):
        """Unknown order in WS → OMS degraded → trading blocked."""
        oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=str(tmp_path / "idem.db")),
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
        )
        oms.handle_user_event("snapshot", [])
        assert oms.is_ready()

        # Orphan order arrives from exchange
        orphan = {
            "order_id": "unknown-ex-1",
            "client_order_id": "unknown-client-1",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }
        oms.handle_user_event("update", [orphan])
        assert oms.is_degraded()
        assert not oms.is_ready()

    def test_rest_api_error_in_fill_fetch_degrades(self, tmp_path):
        """REST API error when fetching fills → OMS degraded."""
        idem = IdempotencyStore(db_path=str(tmp_path / "idem.db"))
        intent = OrderIntent(
            client_order_id="api-err-client",
            signal_id="sig", strategy_id="strat",
            symbol="BTC-USD", side="BUY",
            final_qty=Decimal("0.1"), order_type="MARKET",
            price=None, reduce_only=False, post_only=False,
            viable=True, planner_version="test",
        )
        idem.save_intent(intent, OrderState.OPEN_PENDING)

        failing_fetcher = MagicMock(side_effect=ConnectionError("API timeout"))
        oms = OMSReconcileService(
            idempotency=idem,
            ledger=TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db")),
            fill_fetcher=failing_fetcher,
        )
        oms.handle_user_event("snapshot", [])

        ws_update = {
            "order_id": "ex-err-1",
            "client_order_id": "api-err-client",
            "product_id": "BTC-USD",
            "status": "FILLED",
            "number_of_fills": "1",
        }
        oms.handle_user_event("update", [ws_update])
        assert oms.is_degraded()


class TestMockExchangeSignalToOrder:
    """Signal → sizing → risk → order → fill, all with mocks."""

    def test_full_signal_to_fill_cycle(self, tmp_path):
        """Complete cycle: signal generates order, paper engine fills, ledger updates."""
        reset_collector()
        ledger = TradeLedger("BTC-USD", db_path=str(tmp_path / "l.db"),
                             initial_cash=Decimal("10000"))
        breaker = CircuitBreaker(BreakerConfig())
        breaker.reset_day(Decimal("10000"))
        gate = RiskGate(RiskLimits())
        sizer = PositionSizer()
        planner = OrderPlanner()
        engine = PaperEngine(taker_fee=Decimal("0"))

        # 1. Sizing
        sizing = sizer.compute(
            symbol="BTC-USD", equity=Decimal("10000"),
            entry_price=Decimal("50000"),
            notional_pct=Decimal("0.05"),
            constraints=CONSTRAINTS, max_notional=Decimal("10000"),
        )
        assert sizing.target_qty > Decimal("0")
        assert sizing.sizing_mode == SizingMode.NOTIONAL

        # 2. Risk gate
        snap = RiskSnapshot(
            equity=Decimal("10000"), position_qty=Decimal("0"),
            day_pnl_pct=Decimal("0"), drawdown_pct=Decimal("0"),
            symbol="BTC-USD", side="BUY",
            target_qty=sizing.target_qty, entry_ref=Decimal("50000"),
        )
        verdict = gate.evaluate(snap)
        assert verdict.allowed

        # 3. Order planner
        risk_input = RiskDecisionInput(
            allowed=True, hard_max_qty=verdict.hard_max_qty,
            hard_max_notional=verdict.hard_max_notional,
            reduce_only=False, reason="ok",
        )
        intent = planner.plan(
            signal_id="mock-sig-1", strategy_id="mock-strat",
            symbol="BTC-USD", side="BUY", sizing=sizing,
            risk=risk_input, constraints=CONSTRAINTS,
        )
        assert intent.viable

        # 4. Execute via paper engine
        result = engine.submit_order({
            "client_id": intent.client_order_id,
            "symbol": "BTC-USD", "side": "buy",
            "type": "market", "amount": intent.final_qty,
        }, bid=Decimal("49900"), ask=Decimal("50100"))
        assert result["status"] == "filled"

        # 5. Apply fill to ledger
        fill = result["fill"]
        ledger.add_fill(Fill(
            side=fill.side, amount=fill.amount, price=fill.price,
            cost=fill.amount * fill.price, fee_cost=fill.fee_cost,
            fee_currency=fill.fee_currency, ts_ms=1000,
            trade_id=fill.trade_id, order_id=fill.order_id,
        ))
        assert ledger.position_qty > Decimal("0")

        # 6. ExecutionReport with real fill data
        report = build_execution_report(
            client_order_id=intent.client_order_id,
            symbol="BTC-USD", side="BUY",
            expected_price=Decimal("50000"),
            fill_price=fill.price,
            requested_qty=intent.final_qty,
            filled_qty=fill.amount,
            latency_ms=0.0, outcome="FILLED",
        )
        assert report.outcome == "FILLED"
        assert report.slippage_bps != Decimal("0")  # paper engine fills at ask

        # 7. Feed to breaker
        breaker.record_slippage(float(report.slippage_bps))
        breaker.record_execution_result(True)
        assert breaker.state == BreakerState.CLOSED

        reset_collector()
