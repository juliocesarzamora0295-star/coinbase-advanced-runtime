# Observe-Only Validation Checklist

Post-run analysis guide for `scripts/observe_only_runner.py` structured logs.

Log file location: `logs/observe_only_<timestamp>.json` (one JSON object per line).

---

## 1. WebSocket Connectivity

**What to verify:** WS connects, authenticates, and maintains heartbeat.

| Check | Log filter | Pass criteria |
|-------|-----------|---------------|
| Market WS connected | `category=ws, event=started` | Present within 10s of `session.start` |
| No connection failures | `category=ws, event=connect_failed` | Absent |
| No gaps detected | `category=ws, event=gap_detected` | Absent (or justified by known network event) |
| Session ran full duration | `category=session, event=duration_reached` | `elapsed_s` >= configured duration |

**Command to check:**
```bash
grep '"category":"ws"' logs/observe_only_*.json | jq .
```

---

## 2. Candle Aggregation

**What to verify:** 5m candles arrive from Coinbase and are correctly ingested.

| Check | Log filter | Pass criteria |
|-------|-----------|---------------|
| 5m candles ingested | `category=candle, event=ingest_5m` | At least 1 per 5 minutes of runtime |
| Candle closed events | `category=candle, event=closed` | At least 1 per configured timeframe interval |
| OHLCV values valid | `event=closed` fields | open, high, low, close > 0; high >= low |
| No duplicate timestamps | `event=ingest_5m, timestamp_ms` | Unique per symbol |

**Command to check:**
```bash
grep '"event":"ingest_5m"' logs/observe_only_*.json | jq '{ts: .ts, symbol: .symbol, close: .close}'
grep '"event":"closed"' logs/observe_only_*.json | jq '{ts: .ts, symbol: .symbol, ohlc: [.open, .high, .low, .close]}'
```

**Note:** With `timeframe: 5m`, expect a `candle.closed` event roughly every 5 minutes.
With `timeframe: 1h`, you need 60+ minutes of runtime to see one. Use 5m for quick validation.

---

## 3. RiskGate Evaluation

**What to verify:** RiskGate evaluates signals correctly; all orders blocked in observe_only.

| Check | Log filter | Pass criteria |
|-------|-----------|---------------|
| Observe-only blocks logged | `category=execution, event=observe_only_block` | Present if signals were generated |
| Circuit breaker checks | `category=risk, event=circuit_breaker_check` | Present with `state=closed` |
| No real order submissions | `category=execution, event=order_submitted` | Absent |

**Command to check:**
```bash
grep '"observe_only_block"' logs/observe_only_*.json | jq '{symbol: .symbol, side: .side, qty: .qty}'
grep '"circuit_breaker_check"' logs/observe_only_*.json | jq '{allowed: .allowed, state: .state}'
```

---

## 4. OMS State Machine

**What to verify:** OMS initializes, bootstraps, and reports health even without fills.

| Check | Log filter | Pass criteria |
|-------|-----------|---------------|
| OMS snapshot present | `category=snapshot, event=oms` | At least 1 per symbol |
| Bootstrap complete | snapshot.oms `bootstrap_complete=true` | True after first snapshot |
| Not degraded | snapshot.oms `degraded=false` | False throughout |
| No orphan incidents | grep for `OMS DEGRADED` in console output | Absent |

**Command to check:**
```bash
grep '"event":"oms"' logs/observe_only_*.json | jq '{symbol: .symbol, bootstrap: .bootstrap_complete, degraded: .degraded}'
```

---

## 5. Kill Switch and Circuit Breaker

**What to verify:** Respond correctly to live conditions; no spurious trips.

| Check | Log filter | Pass criteria |
|-------|-----------|---------------|
| Kill switch off at start | `category=risk, event=kill_switch_state` | `active=false` |
| CB stays closed | `category=snapshot, event=circuit_breaker` | `state=closed` throughout |
| No spurious trips | `state=open` in any CB snapshot | Absent (unless justified) |
| Latency p95 reasonable | snapshot.circuit_breaker `health.latency_p95_ms` | < 500ms |

**Command to check:**
```bash
grep '"event":"circuit_breaker"' logs/observe_only_*.json | jq '{state: .state, latency_p95: .health.latency_p95_ms, ws_gaps: .health.ws_gaps}'
```

---

## 6. Telemetry Completeness

**What to verify:** Enough data to diagnose issues post-run.

| Check | Log filter | Pass criteria |
|-------|-----------|---------------|
| Session start/complete pair | `category=session` | Both `start` and `complete` present |
| Periodic snapshots | `category=snapshot` | At least 1 per 30s of runtime |
| Prices received | `session.complete` → `prices_received` | `true` |
| Metrics captured | `category=snapshot, event=metrics` | Non-empty `data` |
| Ledger state captured | `category=snapshot, event=ledger` | At least 1 per symbol |

**Command to check:**
```bash
grep '"category":"session"' logs/observe_only_*.json | jq '{event: .event, elapsed_s: .elapsed_s}'
grep '"event":"metrics"' logs/observe_only_*.json | wc -l
```

---

## Quick Full Validation (copy-paste)

```bash
# Run a 5-minute session
python scripts/observe_only_runner.py --duration 300

# Find latest log
LOG=$(ls -t logs/observe_only_*.json | head -1)

# Check all categories
echo "=== Session ==="
grep '"category":"session"' "$LOG" | jq -c '{event: .event, elapsed_s: .elapsed_s}'

echo "=== WebSocket ==="
grep '"category":"ws"' "$LOG" | jq -c '{event: .event}'

echo "=== Candles ingested ==="
grep '"ingest_5m"' "$LOG" | wc -l

echo "=== Candles closed ==="
grep '"event":"closed"' "$LOG" | jq -c '{symbol: .symbol, close: .close, ts: .timestamp_ms}'

echo "=== Observe-only blocks ==="
grep '"observe_only_block"' "$LOG" | jq -c '{symbol: .symbol, side: .side}'

echo "=== Circuit breaker ==="
grep '"event":"circuit_breaker"' "$LOG" | jq -c '{state: .state}' | sort | uniq -c

echo "=== OMS ==="
grep '"event":"oms"' "$LOG" | jq -c '{symbol: .symbol, bootstrap: .bootstrap_complete, degraded: .degraded}'

echo "=== Final summary ==="
grep '"event":"complete"' "$LOG" | jq .
```

---

## Findings Log

Use this section to record findings from each validation run:

| Date | Duration | Candles | Signals | Issues |
|------|----------|---------|---------|--------|
| _yyyy-mm-dd_ | _Xs_ | _N_ | _N_ | _description_ |
