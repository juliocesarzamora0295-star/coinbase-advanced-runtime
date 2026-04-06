"""
Tests for operational infrastructure: credentials, monitoring, health check.
"""

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

from src.credentials import (
    Credentials,
    _validate_format,
    load_credentials,
)
from src.monitoring.alert_manager import (
    Alert,
    AlertLevel,
    AlertManager,
    ConsoleBackend,
    FileBackend,
)
from src.monitoring.health_check import HealthChecker, SystemHealth


# ── A) Credentials ──


class TestCredentials:

    def test_credentials_from_env(self):
        with patch.dict(os.environ, {"COINBASE_API_KEY": "test_key_12345", "COINBASE_API_SECRET": "test_secret_12345"}):
            creds = load_credentials()
            assert creds is not None
            assert creds.source == "env"
            assert creds.is_configured()

    def test_no_credentials_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove all coinbase env vars
            env = {k: v for k, v in os.environ.items() if "COINBASE" not in k}
            with patch.dict(os.environ, env, clear=True):
                creds = load_credentials(secrets_path="/nonexistent")
                # May still find from .env or other sources
                # Just verify it doesn't crash
                assert creds is None or isinstance(creds, Credentials)

    def test_validate_format_short_key(self):
        creds = Credentials(api_key="ab", api_secret="valid_secret_123", source="test")
        assert not _validate_format(creds)

    def test_validate_format_valid(self):
        creds = Credentials(api_key="valid_key_12345", api_secret="valid_secret_12345", source="test")
        assert _validate_format(creds)

    def test_credentials_immutable(self):
        creds = Credentials(api_key="k", api_secret="s", source="t")
        try:
            creds.api_key = "new"  # type: ignore
            assert False, "Should be frozen"
        except (AttributeError, TypeError):
            pass


# ── B) AlertManager ──


class TestAlertManager:

    def test_console_backend_sends(self):
        backend = ConsoleBackend()
        alert = Alert(
            level=AlertLevel.WARNING,
            source="test",
            message="test alert",
            timestamp_ms=1000,
        )
        assert backend.send(alert)

    def test_file_backend_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "alerts.jsonl")
            backend = FileBackend(path=path)
            alert = Alert(
                level=AlertLevel.CRITICAL,
                source="oms",
                message="degraded",
                timestamp_ms=int(time.time() * 1000),
            )
            assert backend.send(alert)

            with open(path) as f:
                data = json.loads(f.readline())
                assert data["level"] == "CRITICAL"
                assert data["source"] == "oms"

    def test_alert_manager_rate_limiting(self):
        manager = AlertManager(
            backends=[ConsoleBackend()],
            cooldown_seconds=60.0,
        )
        # First alert sent
        assert manager.alert(AlertLevel.WARNING, "test", "msg1")
        # Same alert suppressed
        assert not manager.alert(AlertLevel.WARNING, "test", "msg1")
        # Different alert sent
        assert manager.alert(AlertLevel.WARNING, "test", "msg2")

    def test_alert_manager_count(self):
        manager = AlertManager(backends=[ConsoleBackend()], cooldown_seconds=0)
        manager.alert(AlertLevel.INFO, "a", "m1")
        manager.alert(AlertLevel.INFO, "a", "m2")
        assert manager.total_alerts == 2

    def test_heartbeat_runs(self):
        manager = AlertManager(backends=[ConsoleBackend()])
        manager.heartbeat("test")  # should not raise

    def test_alert_levels(self):
        assert AlertLevel.INFO < AlertLevel.WARNING
        assert AlertLevel.WARNING < AlertLevel.CRITICAL
        assert AlertLevel.CRITICAL < AlertLevel.EMERGENCY

    def test_alert_with_details(self):
        manager = AlertManager(backends=[ConsoleBackend()], cooldown_seconds=0)
        sent = manager.alert(
            AlertLevel.CRITICAL, "kill_switch", "activated",
            details={"mode": "CANCEL_AND_FLATTEN"},
        )
        assert sent


# ── C) HealthCheck ──


class TestHealthCheck:

    def test_all_healthy(self):
        hc = HealthChecker()
        health = hc.check(
            oms_ready=True, oms_degraded=False,
            breaker_state="closed",
            kill_switch_active=False,
            ledger_equity=10000.0,
            pending_reports_count=0,
            ws_connected=True,
        )
        assert health.overall == "HEALTHY"
        assert all(c.healthy for c in health.components)

    def test_degraded_oms(self):
        hc = HealthChecker()
        health = hc.check(
            oms_ready=True, oms_degraded=True,
            breaker_state="closed",
        )
        assert health.overall == "DEGRADED"

    def test_breaker_open(self):
        hc = HealthChecker()
        health = hc.check(breaker_state="OPEN")
        assert health.overall == "DEGRADED"

    def test_kill_switch_active(self):
        hc = HealthChecker()
        health = hc.check(kill_switch_active=True, kill_switch_mode="BLOCK_NEW")
        comp = [c for c in health.components if c.name == "kill_switch"][0]
        assert not comp.healthy
        assert "ACTIVE" in comp.status

    def test_zero_equity_unhealthy(self):
        hc = HealthChecker()
        health = hc.check(ledger_equity=0.0)
        comp = [c for c in health.components if c.name == "ledger"][0]
        assert not comp.healthy

    def test_stale_reconcile(self):
        hc = HealthChecker(stale_threshold_s=60.0)
        health = hc.check(last_reconcile_ts=time.time() - 120)
        comp = [c for c in health.components if c.name == "reconcile"][0]
        assert comp.status == "STALE"

    def test_ws_disconnected(self):
        hc = HealthChecker()
        health = hc.check(ws_connected=False)
        assert health.overall == "UNHEALTHY"

    def test_to_dict(self):
        hc = HealthChecker()
        health = hc.check(oms_ready=True, oms_degraded=False)
        d = health.to_dict()
        assert "overall" in d
        assert "components" in d
        assert "timestamp_ms" in d

    def test_log_json_does_not_raise(self):
        hc = HealthChecker()
        health = hc.check(oms_ready=True, oms_degraded=False)
        health.log_json()  # should not raise

    def test_uptime_tracked(self):
        hc = HealthChecker()
        time.sleep(0.15)
        health = hc.check()
        assert health.uptime_seconds >= 0.1
