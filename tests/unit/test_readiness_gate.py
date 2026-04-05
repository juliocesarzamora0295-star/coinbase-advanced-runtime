"""
Tests para RuntimeReadinessGate.

Verifica:
- RuntimeReadinessGate.ready = False hasta que los tres flags estén en True
- _execute_order bloqueado (retorna sin ejecutar) cuando not ready
- _execute_order pasa cuando ready=True
- Flags se activan independientemente
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from src.main import RuntimeReadinessGate


class TestRuntimeReadinessGateLogic:
    def test_not_ready_by_default(self):
        gate = RuntimeReadinessGate()
        assert gate.ready is False

    def test_all_false_not_ready(self):
        gate = RuntimeReadinessGate(
            oms_bootstrap_complete=False,
            ws_connected=False,
            initial_reconcile_clean=False,
        )
        assert gate.ready is False

    def test_only_oms_complete_not_ready(self):
        gate = RuntimeReadinessGate(oms_bootstrap_complete=True)
        assert gate.ready is False

    def test_only_ws_connected_not_ready(self):
        gate = RuntimeReadinessGate(ws_connected=True)
        assert gate.ready is False

    def test_only_reconcile_clean_not_ready(self):
        gate = RuntimeReadinessGate(initial_reconcile_clean=True)
        assert gate.ready is False

    def test_oms_and_ws_not_ready(self):
        gate = RuntimeReadinessGate(oms_bootstrap_complete=True, ws_connected=True)
        assert gate.ready is False

    def test_all_true_ready(self):
        gate = RuntimeReadinessGate(
            oms_bootstrap_complete=True,
            ws_connected=True,
            initial_reconcile_clean=True,
        )
        assert gate.ready is True

    def test_status_str_pending(self):
        gate = RuntimeReadinessGate()
        s = gate.status_str()
        assert "PENDING" in s
        assert "oms_bootstrap" in s
        assert "ws" in s
        assert "reconcile" in s

    def test_status_str_all_ok(self):
        gate = RuntimeReadinessGate(
            oms_bootstrap_complete=True,
            ws_connected=True,
            initial_reconcile_clean=True,
        )
        s = gate.status_str()
        assert "PENDING" not in s
        assert s.count("OK") == 3

    def test_pending_bootstrap_tracking(self):
        gate = RuntimeReadinessGate()
        gate._pending_bootstrap.add("BTC-USD")
        gate._pending_bootstrap.add("ETH-USD")
        gate._pending_bootstrap.discard("BTC-USD")
        assert "BTC-USD" not in gate._pending_bootstrap
        assert "ETH-USD" in gate._pending_bootstrap


class TestExecuteOrderReadinessGate:
    """
    Verifica que _execute_order respeta el readiness gate.

    Se testea con un TradingBot mínimo (sin credenciales reales) usando
    config OBSERVE_ONLY para evitar tocar exchange ni paper engine.
    """

    def _make_bot(self, ready: bool):
        """Instanciar TradingBot sin inicializar (evita credenciales)."""
        from src.config import RuntimeMode, TradingConfig
        from src.main import TradingBot

        bot = TradingBot.__new__(TradingBot)
        # Inyectar config mínima
        cfg = MagicMock()
        cfg.trading = TradingConfig(runtime_mode=RuntimeMode.OBSERVE_ONLY)
        bot.config = cfg
        # Inyectar readiness gate
        bot._readiness = RuntimeReadinessGate(
            oms_bootstrap_complete=ready,
            ws_connected=ready,
            initial_reconcile_clean=ready,
        )
        bot._lock = __import__("threading").Lock()
        bot.current_prices = {}
        bot.paper_engine = None
        bot.ledgers = {}
        bot.executors = {}
        return bot

    def _make_intent(self):
        from decimal import Decimal

        from src.execution.order_planner import OrderIntent

        return OrderIntent(
            client_order_id="a" * 32,
            signal_id="sig-001",
            strategy_id="ma_crossover",
            symbol="BTC-USD",
            side="BUY",
            final_qty=Decimal("0.001"),
            order_type="MARKET",
            price=None,
            reduce_only=False,
            viable=True,
            planner_version="1.0",
        )

    def test_order_blocked_when_not_ready(self, caplog):
        import logging

        bot = self._make_bot(ready=False)
        intent = self._make_intent()

        with caplog.at_level(logging.WARNING):
            bot._execute_order(intent, Decimal("50000"))

        assert any("ORDER BLOCKED" in r.message for r in caplog.records)

    def test_order_not_blocked_when_ready(self, caplog):
        import logging

        bot = self._make_bot(ready=True)
        intent = self._make_intent()

        with caplog.at_level(logging.INFO):
            bot._execute_order(intent, Decimal("50000"))

        # En OBSERVE_ONLY no hay "ORDER BLOCKED"
        assert not any("ORDER BLOCKED" in r.message for r in caplog.records)
        # El log OBSERVE_ONLY debe aparecer
        assert any("OBSERVE_ONLY" in r.message for r in caplog.records)

    def test_blocked_order_does_not_reach_paper_engine(self):
        bot = self._make_bot(ready=False)
        paper_mock = MagicMock()
        bot.paper_engine = paper_mock
        intent = self._make_intent()

        bot._execute_order(intent, Decimal("50000"))

        paper_mock.submit_order.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
