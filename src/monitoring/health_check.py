"""
Health check — verifies state of all system components.

Returns structured JSON with component status.
Detects stale data (no updates for N seconds).

Health file output:
  HealthFileWriter writes SystemHealth to a JSON file on disk.
  Docker/k8s can probe this file for container health:
    HEALTHCHECK CMD python -c "import json,sys,time; h=json.load(open('/tmp/health.json')); sys.exit(0 if h['overall']!='UNHEALTHY' and time.time()*1000-h['timestamp_ms']<180000 else 1)"
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("HealthCheck")


@dataclass
class ComponentHealth:
    """Health status of a single component."""

    name: str
    healthy: bool
    status: str  # "OK", "DEGRADED", "DOWN", "STALE"
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SystemHealth:
    """Aggregated system health."""

    overall: str  # "HEALTHY", "DEGRADED", "UNHEALTHY"
    components: list  # List[ComponentHealth]
    timestamp_ms: int = 0
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "timestamp_ms": self.timestamp_ms,
            "uptime_seconds": self.uptime_seconds,
            "components": [
                {
                    "name": c.name,
                    "healthy": c.healthy,
                    "status": c.status,
                    "details": c.details,
                }
                for c in self.components
            ],
        }

    def log_json(self) -> None:
        logger.info("HEALTH_CHECK %s", json.dumps(self.to_dict(), default=str))


class HealthChecker:
    """
    Checks health of all system components.

    Stale detection: if last_update_ts is older than stale_threshold_s,
    the component is marked STALE.
    """

    def __init__(self, stale_threshold_s: float = 120.0) -> None:
        self.stale_threshold_s = stale_threshold_s
        self._start_time = time.time()

    def check(
        self,
        *,
        oms_ready: Optional[bool] = None,
        oms_degraded: Optional[bool] = None,
        breaker_state: Optional[str] = None,
        kill_switch_active: Optional[bool] = None,
        kill_switch_mode: Optional[str] = None,
        ledger_equity: Optional[float] = None,
        pending_reports_count: Optional[int] = None,
        last_reconcile_ts: Optional[float] = None,
        last_fill_ts: Optional[float] = None,
        ws_connected: Optional[bool] = None,
    ) -> SystemHealth:
        """
        Run health check across all components.

        Pass None for unavailable metrics (component not initialized).
        """
        now = time.time()
        components: list = []

        # OMS
        if oms_ready is not None:
            if oms_degraded:
                components.append(ComponentHealth("oms", False, "DEGRADED"))
            elif oms_ready:
                components.append(ComponentHealth("oms", True, "OK"))
            else:
                components.append(ComponentHealth("oms", False, "DOWN", {"reason": "not bootstrapped"}))

        # Circuit breaker
        if breaker_state is not None:
            healthy = breaker_state.upper() == "CLOSED"
            components.append(ComponentHealth(
                "circuit_breaker", healthy,
                "OK" if healthy else breaker_state.upper(),
            ))

        # Kill switch
        if kill_switch_active is not None:
            components.append(ComponentHealth(
                "kill_switch",
                not kill_switch_active,
                "OFF" if not kill_switch_active else f"ACTIVE:{kill_switch_mode}",
            ))

        # Ledger
        if ledger_equity is not None:
            healthy = ledger_equity > 0
            components.append(ComponentHealth(
                "ledger", healthy,
                "OK" if healthy else "ZERO_EQUITY",
                {"equity": ledger_equity},
            ))

        # Pending reports
        if pending_reports_count is not None:
            components.append(ComponentHealth(
                "pending_reports", True, "OK",
                {"count": pending_reports_count},
            ))

        # Reconcile staleness
        if last_reconcile_ts is not None:
            age = now - last_reconcile_ts
            stale = age > self.stale_threshold_s
            components.append(ComponentHealth(
                "reconcile",
                not stale,
                "STALE" if stale else "OK",
                {"age_seconds": round(age, 1)},
            ))

        # WebSocket
        if ws_connected is not None:
            components.append(ComponentHealth(
                "websocket", ws_connected,
                "OK" if ws_connected else "DISCONNECTED",
            ))

        # Overall
        all_healthy = all(c.healthy for c in components)
        any_down = any(c.status in ("DOWN", "DISCONNECTED") for c in components)

        if any_down:
            overall = "UNHEALTHY"
        elif all_healthy:
            overall = "HEALTHY"
        else:
            overall = "DEGRADED"

        return SystemHealth(
            overall=overall,
            components=components,
            timestamp_ms=int(now * 1000),
            uptime_seconds=round(now - self._start_time, 1),
        )


class HealthFileWriter:
    """
    Writes SystemHealth to a JSON file on disk for external probing.

    Docker/k8s HEALTHCHECK can read this file to determine container health.
    Writes atomically (write to temp, rename) to prevent partial reads.
    """

    def __init__(self, path: str = "/tmp/health.json") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, health: SystemHealth) -> None:
        """Write health status to file atomically."""
        data = health.to_dict()
        tmp_path = self._path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, default=str)
            tmp_path.replace(self._path)
        except Exception as exc:
            logger.error("Failed to write health file: %s", exc)

    @staticmethod
    def check_file(path: str = "/tmp/health.json", max_age_ms: int = 180_000) -> bool:
        """
        Read health file and return True if healthy and fresh.

        Used by Docker HEALTHCHECK or monitoring probes.

        Args:
            path: Path to health JSON file
            max_age_ms: Maximum age in milliseconds before considering stale

        Returns:
            True if file exists, overall != UNHEALTHY, and timestamp is fresh.
        """
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("overall") == "UNHEALTHY":
                return False
            age_ms = time.time() * 1000 - data.get("timestamp_ms", 0)
            return age_ms < max_age_ms
        except Exception:
            return False
