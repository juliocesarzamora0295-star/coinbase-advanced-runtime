"""
Tests for HealthFileWriter — file-based health check probe.
"""

import json
import os
import time

from src.monitoring.health_check import (
    ComponentHealth,
    HealthChecker,
    HealthFileWriter,
    SystemHealth,
)


class TestHealthFileWriter:
    """Verify health file write and read."""

    def test_write_creates_file(self, tmp_path):
        """write() creates the health JSON file."""
        path = str(tmp_path / "health.json")
        writer = HealthFileWriter(path=path)

        health = SystemHealth(
            overall="HEALTHY",
            components=[ComponentHealth("oms", True, "OK")],
            timestamp_ms=int(time.time() * 1000),
            uptime_seconds=10.0,
        )
        writer.write(health)

        assert os.path.exists(path)
        with open(path) as f:
            data = json.load(f)
        assert data["overall"] == "HEALTHY"
        assert len(data["components"]) == 1

    def test_write_overwrites(self, tmp_path):
        """Subsequent writes overwrite the file."""
        path = str(tmp_path / "health.json")
        writer = HealthFileWriter(path=path)

        h1 = SystemHealth(overall="HEALTHY", components=[], timestamp_ms=1000)
        writer.write(h1)

        h2 = SystemHealth(overall="DEGRADED", components=[], timestamp_ms=2000)
        writer.write(h2)

        with open(path) as f:
            data = json.load(f)
        assert data["overall"] == "DEGRADED"
        assert data["timestamp_ms"] == 2000

    def test_check_file_healthy(self, tmp_path):
        """check_file returns True for fresh healthy file."""
        path = str(tmp_path / "health.json")
        writer = HealthFileWriter(path=path)

        health = SystemHealth(
            overall="HEALTHY",
            components=[],
            timestamp_ms=int(time.time() * 1000),
        )
        writer.write(health)

        assert HealthFileWriter.check_file(path=path) is True

    def test_check_file_unhealthy(self, tmp_path):
        """check_file returns False for UNHEALTHY status."""
        path = str(tmp_path / "health.json")
        writer = HealthFileWriter(path=path)

        health = SystemHealth(
            overall="UNHEALTHY",
            components=[],
            timestamp_ms=int(time.time() * 1000),
        )
        writer.write(health)

        assert HealthFileWriter.check_file(path=path) is False

    def test_check_file_stale(self, tmp_path):
        """check_file returns False for stale file (old timestamp)."""
        path = str(tmp_path / "health.json")
        writer = HealthFileWriter(path=path)

        # Timestamp from 10 minutes ago
        old_ts = int((time.time() - 600) * 1000)
        health = SystemHealth(overall="HEALTHY", components=[], timestamp_ms=old_ts)
        writer.write(health)

        # Default max_age_ms is 180000 (3 minutes)
        assert HealthFileWriter.check_file(path=path) is False

    def test_check_file_missing(self, tmp_path):
        """check_file returns False for missing file."""
        path = str(tmp_path / "nonexistent.json")
        assert HealthFileWriter.check_file(path=path) is False

    def test_check_file_degraded_is_ok(self, tmp_path):
        """DEGRADED is not UNHEALTHY — check_file returns True."""
        path = str(tmp_path / "health.json")
        writer = HealthFileWriter(path=path)

        health = SystemHealth(
            overall="DEGRADED",
            components=[],
            timestamp_ms=int(time.time() * 1000),
        )
        writer.write(health)

        assert HealthFileWriter.check_file(path=path) is True

    def test_integration_with_health_checker(self, tmp_path):
        """HealthChecker → HealthFileWriter round-trip."""
        path = str(tmp_path / "health.json")
        checker = HealthChecker()
        writer = HealthFileWriter(path=path)

        health = checker.check(
            oms_ready=True,
            breaker_state="CLOSED",
            kill_switch_active=False,
            kill_switch_mode="NORMAL",
            ws_connected=True,
        )
        writer.write(health)

        assert HealthFileWriter.check_file(path=path) is True

        with open(path) as f:
            data = json.load(f)
        assert data["overall"] == "HEALTHY"
        assert len(data["components"]) == 4  # oms, breaker, kill_switch, websocket
