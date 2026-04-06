"""
Alert manager — multi-backend alerting with rate limiting.

Backends: console, file (JSON lines), webhook (POST), email (SMTP).
Rate limited per alert type to prevent spam.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("AlertManager")


class AlertLevel(IntEnum):
    """Alert severity levels."""

    INFO = 0
    WARNING = 1
    CRITICAL = 2
    EMERGENCY = 3


@dataclass(frozen=True)
class Alert:
    """Immutable alert event."""

    level: AlertLevel
    source: str  # component that raised the alert
    message: str
    timestamp_ms: int
    details: Dict[str, Any] = field(default_factory=dict)


class AlertBackend:
    """Base class for alert backends."""

    def send(self, alert: Alert) -> bool:
        raise NotImplementedError


class ConsoleBackend(AlertBackend):
    """Always-on console logging backend."""

    def send(self, alert: Alert) -> bool:
        level_map = {
            AlertLevel.INFO: logger.info,
            AlertLevel.WARNING: logger.warning,
            AlertLevel.CRITICAL: logger.critical,
            AlertLevel.EMERGENCY: logger.critical,
        }
        log_fn = level_map.get(alert.level, logger.info)
        log_fn(
            "ALERT [%s] %s: %s",
            alert.level.name, alert.source, alert.message,
        )
        return True


class FileBackend(AlertBackend):
    """JSON lines file backend with rotation."""

    def __init__(self, path: str = "logs/alerts.jsonl", max_bytes: int = 5_000_000) -> None:
        self.path = path
        self.max_bytes = max_bytes
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def send(self, alert: Alert) -> bool:
        try:
            record = {
                "ts": alert.timestamp_ms,
                "level": alert.level.name,
                "source": alert.source,
                "message": alert.message,
                "details": alert.details,
            }
            with open(self.path, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
            # Basic rotation
            if os.path.getsize(self.path) > self.max_bytes:
                backup = f"{self.path}.1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.rename(self.path, backup)
            return True
        except Exception as e:
            logger.error("FileBackend error: %s", e)
            return False


class WebhookBackend(AlertBackend):
    """POST alerts to a webhook URL (Slack, Discord, PagerDuty)."""

    def __init__(self, url: str, min_level: AlertLevel = AlertLevel.WARNING) -> None:
        self.url = url
        self.min_level = min_level

    def send(self, alert: Alert) -> bool:
        if alert.level < self.min_level:
            return True  # filtered, not an error
        try:
            import urllib.request
            payload = json.dumps({
                "text": f"[{alert.level.name}] {alert.source}: {alert.message}",
                "level": alert.level.name,
                "source": alert.source,
                "details": alert.details,
            }).encode()
            req = urllib.request.Request(
                self.url, data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as e:
            logger.error("WebhookBackend error: %s", e)
            return False


class AlertManager:
    """
    Central alert manager with rate limiting.

    Rate limit: same (source, message) pair suppressed for cooldown_seconds.
    """

    def __init__(
        self,
        backends: Optional[List[AlertBackend]] = None,
        cooldown_seconds: float = 300.0,
    ) -> None:
        self.backends = backends or [ConsoleBackend()]
        self.cooldown_seconds = cooldown_seconds
        self._last_sent: Dict[str, float] = {}
        self._alert_count = 0

    def alert(
        self,
        level: AlertLevel,
        source: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Send alert to all backends. Rate limited.

        Returns True if alert was sent (not suppressed).
        """
        now = time.time()
        key = f"{source}:{message}"

        # Rate limit check
        last = self._last_sent.get(key, 0.0)
        if now - last < self.cooldown_seconds:
            return False  # suppressed

        alert_obj = Alert(
            level=level,
            source=source,
            message=message,
            timestamp_ms=int(now * 1000),
            details=details or {},
        )

        sent = False
        for backend in self.backends:
            try:
                if backend.send(alert_obj):
                    sent = True
            except Exception as e:
                logger.error("Backend %s failed: %s", type(backend).__name__, e)

        if sent:
            self._last_sent[key] = now
            self._alert_count += 1

        return sent

    def heartbeat(self, source: str = "system") -> None:
        """Send periodic heartbeat (INFO level, not rate limited for heartbeat)."""
        now = int(time.time() * 1000)
        hb = Alert(
            level=AlertLevel.INFO,
            source=source,
            message="heartbeat",
            timestamp_ms=now,
            details={"alert_count": self._alert_count},
        )
        for backend in self.backends:
            try:
                backend.send(hb)
            except Exception:
                pass

    @property
    def total_alerts(self) -> int:
        return self._alert_count
