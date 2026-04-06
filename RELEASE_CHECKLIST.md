# Release Checklist — Fortress v4 Coinbase Advanced Trade

## Pre-Release Verification

- [ ] `python -m pytest -q` → 0 failures
- [ ] `mypy src/` → 0 errors
- [ ] `python -m compileall -q src tests` → OK
- [ ] `python -m src.config_validator configs/prod_symbols.yaml` → PASSED
- [ ] `bash scripts/ci_smoke.sh` → PASSED
- [ ] Git tag created: `git tag -a v1.0.0-rc1 -m "Release candidate 1"`

## Configuration

- [ ] `configs/prod_symbols.yaml` reviewed and correct
- [ ] `prod.env` created from `prod.env.example` with real credentials
- [ ] Credentials validated: `COINBASE_API_KEY` and `COINBASE_API_SECRET` set
- [ ] `dry_run: false` and `observe_only: true` for shadow phase
- [ ] `initial_cash` matches real account balance
- [ ] `sizing_mode` set appropriately
- [ ] `max_total_exposure_pct` set conservatively (start with 0.50)
- [ ] Alert webhook URL configured

## Infrastructure

- [ ] Host/server provisioned with stable network
- [ ] Python 3.10+ installed
- [ ] Project installed: `pip install -e .`
- [ ] State directory writable: `state/`
- [ ] Logs directory writable: `logs/`
- [ ] SQLite databases: `state/ledger_*.db`, `state/idempotency_*.db`, `state/kill_switch.db`, `state/pending_reports.db`
- [ ] Backup script configured for `state/` and `logs/`
- [ ] Log rotation configured (or JSONLineSink handles it)

## Shadow Phase (24-72h minimum)

- [ ] Start with `observe_only: true`, `dry_run: false`
- [ ] Verify: OMS bootstrap complete (log: "OMS bootstrap complete")
- [ ] Verify: External reconcile running (log: every 300s)
- [ ] Verify: Health check running (log: "HEALTH_CHECK")
- [ ] Verify: Heartbeat active
- [ ] Verify: No orphan orders detected
- [ ] Verify: No OMS degradation
- [ ] Verify: Circuit breaker stays CLOSED
- [ ] Verify: Kill switch OFF
- [ ] Verify: `telemetry.fallback_estimated` metric = 0
- [ ] Test manual restart: stop → start → verify state recovery
- [ ] Test kill switch: activate BLOCK_NEW → verify signals blocked → clear
- [ ] Review: 24h of clean logs before proceeding

## Canary Live Phase (3-5 days)

- [ ] Set `observe_only: false`, `dry_run: false`
- [ ] Conservative limits: `notional_pct: 0.005`, `max_position_pct: 0.10`
- [ ] Single symbol only (BTC-USD)
- [ ] Daily review: fills, slippage, equity, drawdown
- [ ] Verify: Orders reconciled (OMS + ledger consistent)
- [ ] Verify: ExecutionReport slippage is REAL (not estimated)
- [ ] Verify: Kill switch CANCEL_OPEN tested with real order
- [ ] No S1 incidents in 3 consecutive days

## Scale Phase

- [ ] Increase `notional_pct` gradually (0.005 → 0.01 → 0.02)
- [ ] Add symbols one at a time
- [ ] Weekly post-trade review first month
- [ ] Monthly after that
