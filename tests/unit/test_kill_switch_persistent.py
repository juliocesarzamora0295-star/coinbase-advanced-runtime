"""
Tests para kill switch persistente.

Invariantes testeadas:
- 3 modos: BLOCK_NEW, CANCEL_OPEN, CANCEL_AND_FLATTEN
- Persistencia en DB: estado sobrevive reinicios
- activate + clear cycle
- State properties: is_active, blocks_new_orders, should_cancel_open, should_flatten
- Log de activaciones
- Solo clear() desactiva — no hay auto-recovery
"""

import os
import tempfile

from src.risk.kill_switch import KillSwitch, KillSwitchMode, KillSwitchState


class TestKillSwitchModes:
    """3 modos del kill switch."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "ks.db")
        self.ks = KillSwitch(db_path=self.db_path)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_initial_state_is_off(self):
        assert not self.ks.is_active
        assert self.ks.state.mode == KillSwitchMode.OFF

    def test_block_new_mode(self):
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "test", "manual")
        s = self.ks.state
        assert s.is_active
        assert s.blocks_new_orders
        assert not s.should_cancel_open
        assert not s.should_flatten

    def test_cancel_open_mode(self):
        self.ks.activate(KillSwitchMode.CANCEL_OPEN, "test", "manual")
        s = self.ks.state
        assert s.is_active
        assert s.blocks_new_orders
        assert s.should_cancel_open
        assert not s.should_flatten

    def test_cancel_and_flatten_mode(self):
        self.ks.activate(KillSwitchMode.CANCEL_AND_FLATTEN, "emergency", "manual")
        s = self.ks.state
        assert s.is_active
        assert s.blocks_new_orders
        assert s.should_cancel_open
        assert s.should_flatten

    def test_activate_off_clears(self):
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "test", "manual")
        assert self.ks.is_active
        self.ks.activate(KillSwitchMode.OFF, "clearing", "manual")
        assert not self.ks.is_active


class TestKillSwitchPersistence:
    """Estado persiste en DB tras reinicio."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "ks.db")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_state_survives_restart(self):
        ks1 = KillSwitch(db_path=self.db_path)
        ks1.activate(KillSwitchMode.CANCEL_OPEN, "network issue", "breaker")

        # Simulate restart
        ks2 = KillSwitch(db_path=self.db_path)
        assert ks2.is_active
        assert ks2.state.mode == KillSwitchMode.CANCEL_OPEN
        assert ks2.state.reason == "network issue"
        assert ks2.state.activated_by == "breaker"

    def test_clear_persists(self):
        ks1 = KillSwitch(db_path=self.db_path)
        ks1.activate(KillSwitchMode.BLOCK_NEW, "test", "manual")
        ks1.clear("admin")

        ks2 = KillSwitch(db_path=self.db_path)
        assert not ks2.is_active
        assert ks2.state.mode == KillSwitchMode.OFF


class TestKillSwitchClearCycle:
    """activate + clear cycle."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "ks.db")
        self.ks = KillSwitch(db_path=self.db_path)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_activate_then_clear(self):
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "test", "manual")
        assert self.ks.is_active

        self.ks.clear("admin")
        assert not self.ks.is_active
        assert self.ks.state.mode == KillSwitchMode.OFF

    def test_clear_when_already_off_is_noop(self):
        self.ks.clear("admin")
        assert not self.ks.is_active

    def test_upgrade_mode(self):
        """Can escalate from BLOCK_NEW to CANCEL_AND_FLATTEN."""
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "warning", "manual")
        assert not self.ks.state.should_cancel_open

        self.ks.activate(KillSwitchMode.CANCEL_AND_FLATTEN, "critical", "manual")
        assert self.ks.state.should_cancel_open
        assert self.ks.state.should_flatten


class TestKillSwitchLog:
    """Log de activaciones y desactivaciones."""

    def setup_method(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "ks.db")
        self.ks = KillSwitch(db_path=self.db_path)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_activation_logged(self):
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "test reason", "manual")
        log = self.ks.get_log()
        assert len(log) == 1
        assert log[0]["action"] == "ACTIVATE"
        assert log[0]["mode"] == "BLOCK_NEW"
        assert log[0]["reason"] == "test reason"

    def test_clear_logged(self):
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "test", "manual")
        self.ks.clear("admin")
        log = self.ks.get_log()
        assert len(log) == 2
        # Most recent first
        assert "CLEAR" in log[0]["action"]

    def test_multiple_events_tracked(self):
        self.ks.activate(KillSwitchMode.BLOCK_NEW, "first", "manual")
        self.ks.activate(KillSwitchMode.CANCEL_OPEN, "escalate", "breaker")
        self.ks.clear("admin")
        log = self.ks.get_log()
        assert len(log) == 3


class TestKillSwitchStateProperties:
    """KillSwitchState properties."""

    def test_off_state(self):
        s = KillSwitchState(mode=KillSwitchMode.OFF, reason="", activated_at=None, activated_by="")
        assert not s.is_active
        assert not s.blocks_new_orders
        assert not s.should_cancel_open
        assert not s.should_flatten

    def test_block_new_state(self):
        s = KillSwitchState(
            mode=KillSwitchMode.BLOCK_NEW, reason="test",
            activated_at="2024-01-01T00:00:00Z", activated_by="manual",
        )
        assert s.is_active
        assert s.blocks_new_orders
        assert not s.should_cancel_open
        assert not s.should_flatten

    def test_cancel_and_flatten_state(self):
        s = KillSwitchState(
            mode=KillSwitchMode.CANCEL_AND_FLATTEN, reason="emergency",
            activated_at="2024-01-01T00:00:00Z", activated_by="manual",
        )
        assert s.is_active
        assert s.blocks_new_orders
        assert s.should_cancel_open
        assert s.should_flatten
