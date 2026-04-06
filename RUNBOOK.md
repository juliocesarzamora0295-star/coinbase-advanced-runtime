# Runbook — Fortress v4 Operations

## Start

```bash
# 1. Verify config
python -m src.config_validator configs/prod_symbols.yaml

# 2. Source credentials
source prod.env

# 3. Start runtime
python -m src.main
```

Expected startup log:
```
ESTADO: SHADOW MODE  (or LIVE MODE)
MODO: OBSERVE_ONLY ...  (or LIVE TRADING ...)
Key Name: ******...****
JWT Auth initialized (credentials from: env)
Circuit Breaker initialized
Risk Gate initialized
```

## Stop

```bash
# Graceful: Ctrl+C or kill -SIGTERM <pid>
# The runtime will stop processing signals.
# SQLite state is durable — no data loss on clean shutdown.
```

## Kill Switch

### Activate (block new orders)
```python
# From Python shell or script:
from src.risk.kill_switch import KillSwitch, KillSwitchMode
ks = KillSwitch(db_path="state/kill_switch.db")
ks.activate(KillSwitchMode.BLOCK_NEW, "manual intervention", "operator")
```

### Activate (cancel + flatten)
```python
ks.activate(KillSwitchMode.CANCEL_AND_FLATTEN, "emergency", "operator")
```

### Clear
```python
ks.clear("operator")
```

### Check status
```python
print(ks.state)
print(ks.get_log(limit=10))
```

## Incident Response

### OMS Degraded
1. Check logs for: `OMS DEGRADED:`
2. Identify cause: orphan order, fill fetch failure, or divergence
3. If orphan: verify the order on Coinbase dashboard
4. If fill fetch: check network/API connectivity
5. Recovery: system auto-clears after 3 clean external reconciles
6. Manual override: restart runtime (state is durable)

### Circuit Breaker Open
1. Check logs for: `CIRCUIT BREAKER TRIPPED:`
2. Identify trip reason: daily loss, drawdown, latency, WS gap, etc.
3. Wait for recovery cooldown (default 30 min)
4. Verify underlying condition resolved
5. Breaker auto-transitions: OPEN → HALF_OPEN → CLOSED

### Execution Telemetry Estimated
1. Check logs for: `EXECUTION_REPORT ESTIMATED:`
2. This means pending metadata was missing (restart edge case)
3. Verify: `telemetry.fallback_estimated` metric count
4. If recurring: check PendingReportStore DB integrity

### Connection Lost
1. Check WebSocket connectivity
2. Check REST API accessibility
3. Circuit breaker should trip on WS gap
4. Restart runtime if connection doesn't recover

## Backup

```bash
# Daily backup of state and logs
cp -r state/ backup/state_$(date +%Y%m%d)/
cp -r logs/ backup/logs_$(date +%Y%m%d)/
```

## Health Check

The runtime logs health check every 60 seconds:
```
HEALTH_CHECK {"overall": "HEALTHY", "components": [...]}
```

Components checked:
- OMS: ready/degraded
- Circuit breaker: closed/open
- Kill switch: off/active
- Ledger: equity > 0
- Pending reports: count
- Reconcile: staleness
