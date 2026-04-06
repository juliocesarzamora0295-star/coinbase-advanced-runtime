"""
Tests para hardening operativo.

Cubre:
- Reconcile clean/dirty counter + auto-clear
- Reserved balances en ledger
- Available cash
- Equity con reserved descontado
"""

import os
import tempfile
from decimal import Decimal

from src.accounting.ledger import Fill, TradeLedger
from src.oms.reconcile import OMSReconcileService
from src.execution.idempotency import IdempotencyStore


# ──────────────────────────────────────────────
# Reconcile auto-clear
# ──────────────────────────────────────────────


class TestReconcileAutoRecovery:

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        idem_path = os.path.join(self.temp_dir, "idem.db")
        ledger_path = os.path.join(self.temp_dir, "ledger.db")
        self.oms = OMSReconcileService(
            idempotency=IdempotencyStore(db_path=idem_path),
            ledger=TradeLedger("BTC-USD", db_path=ledger_path),
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_clean_reconcile_increments_counter(self):
        self.oms.handle_user_event("snapshot", [])  # bootstrap
        assert self.oms.is_ready()
        self.oms.record_clean_reconcile()
        assert self.oms._consecutive_clean_reconciles == 1

    def test_dirty_reconcile_resets_counter(self):
        self.oms.handle_user_event("snapshot", [])
        self.oms.record_clean_reconcile()
        self.oms.record_clean_reconcile()
        self.oms.record_dirty_reconcile()
        assert self.oms._consecutive_clean_reconciles == 0

    def test_auto_clear_after_threshold(self):
        """Degradado se limpia después de N reconciles limpios."""
        self.oms.handle_user_event("snapshot", [])
        self.oms.report_divergence("test issue")
        assert self.oms.is_degraded()
        assert not self.oms.is_ready()

        # Threshold default = 3
        self.oms.record_clean_reconcile()
        self.oms.record_clean_reconcile()
        assert self.oms.is_degraded()  # still degraded

        self.oms.record_clean_reconcile()  # 3rd → auto-clear
        assert not self.oms.is_degraded()
        assert self.oms.is_ready()

    def test_auto_clear_reset_by_dirty(self):
        """Dirty reconcile reset el contador, no auto-clear."""
        self.oms.handle_user_event("snapshot", [])
        self.oms.report_divergence("issue")

        self.oms.record_clean_reconcile()
        self.oms.record_clean_reconcile()
        self.oms.record_dirty_reconcile()  # reset
        self.oms.record_clean_reconcile()
        self.oms.record_clean_reconcile()
        # Only 2 clean after reset — not enough
        assert self.oms.is_degraded()

    def test_custom_threshold(self):
        self.oms.handle_user_event("snapshot", [])
        self.oms.clean_reconcile_threshold = 1
        self.oms.report_divergence("issue")

        self.oms.record_clean_reconcile()  # 1 clean = threshold
        assert not self.oms.is_degraded()


# ──────────────────────────────────────────────
# Reserved balances
# ──────────────────────────────────────────────


class TestReservedBalances:

    def test_equity_with_no_reserved(self, tmp_path):
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        assert ledger.reserved_quote == Decimal("0")
        assert ledger.get_equity(Decimal("50000")) == Decimal("10000")

    def test_equity_reduced_by_reserved(self, tmp_path):
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.set_reserved(Decimal("2000"))
        # equity = 10000 - 2000 = 8000
        assert ledger.get_equity(Decimal("50000")) == Decimal("8000")

    def test_available_cash(self, tmp_path):
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.set_reserved(Decimal("3000"))
        assert ledger.get_available_cash() == Decimal("7000")

    def test_available_cash_never_negative(self, tmp_path):
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("1000"),
        )
        ledger.set_reserved(Decimal("5000"))
        assert ledger.get_available_cash() == Decimal("0")

    def test_reserved_with_position(self, tmp_path):
        ledger = TradeLedger(
            "BTC-USD", db_path=str(tmp_path / "l.db"),
            initial_cash=Decimal("10000"),
        )
        ledger.add_fill(Fill(
            side="buy", amount=Decimal("0.1"), price=Decimal("50000"),
            cost=Decimal("5000"), fee_cost=Decimal("0"),
            fee_currency="USD", ts_ms=1000, trade_id="t-1", order_id="o-1",
        ))
        # Cash = 5000, position = 0.1 BTC
        ledger.set_reserved(Decimal("1000"))
        # equity = 5000 + 0.1 * 50000 - 1000 = 9000
        assert ledger.get_equity(Decimal("50000")) == Decimal("9000")


# ──────────────────────────────────────────────
# Mutable default fix
# ──────────────────────────────────────────────


class TestBacktestMutableDefault:

    def test_sma_crossover_no_shared_state(self):
        """Without explicit position arg, no shared state between calls."""
        from src.backtest.run import sma_crossover_strategy
        from src.backtest.data_feed import Bar

        bar = Bar(
            timestamp_ms=1000, open=Decimal("100"), high=Decimal("101"),
            low=Decimal("99"), close=Decimal("100"), volume=Decimal("100"),
        )
        # Two calls without position= → each gets fresh []
        result1 = sma_crossover_strategy(bar, [])
        result2 = sma_crossover_strategy(bar, [])
        # Both should return None (not enough history) — key is no crash
        assert result1 is None
        assert result2 is None
