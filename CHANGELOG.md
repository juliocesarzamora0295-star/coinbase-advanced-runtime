# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.1.0] - 2024-03-16

### Added (from GuardianBot - Experimental)
- **PaperEngine**: Basic paper trading simulation (used in dry_run mode)
- **GemProtocol**: Strategy validation framework (code present, not wired to pipeline)
- **SmaCrossoverStrategy**: SMA crossover strategy (code present, not integrated)
- **pre_order_check**: Pre-trade validation method in RiskGate (not called in pipeline)
- **Equity invariant validation**: `validate_equity_invariant()` in ledger (used in smoke mode)
- **Dedup check**: `dedup_check()` in ledger (code present, not actively used)
- **Smoke test mode**: `smoke_test_mode` and `max_cycles` in TradingConfig (functional)

### Changed
- TradingConfig: added `smoke_test_mode` and `max_cycles` fields
- config.py: parse `smoke_test_mode` and `max_cycles` from YAML
- symbols.yaml: added smoke test configuration
- main.py: smoke test mode functional with cycle limiting
- Ledger: added validation methods (used in smoke mode)

## [4.0.0] - 2024-03-16

### Added
- Config-driven RiskLimits: all risk parameters now loaded from YAML config
- Fail-closed risk evaluation: trading blocked if any risk input is unavailable
- Separate execution modes: `observe_only`, `dry_run`, and live trading
- Real risk metrics from ledger: `day_pnl_pct`, `drawdown_pct`
- Order rate tracking: `orders_last_minute` per executor
- WebSocket callback routing by `(channel, product_id)` to prevent cross-contamination
- Per-product sequence tracking for gap detection
- `CANCEL_QUEUED` state support in idempotency store

### Changed
- RiskLimits construction: now from `self.config` instead of hardcoded values
- RiskSnapshot inputs: no longer defaults to zeros, uses real ledger metrics
- Execution path: invariant modes with clear priority (observe > dry-run > live)
- Documentation: honest status for OMS and RiskGate (not "functional" but "implemented")

### Fixed
- WS routing bug: callbacks now filtered by product_id
- Sequence tracking bug: per-product instead of global
- Idempotency consistency: `CANCEL_QUEUED` now in `get_pending_or_open()`

### Infrastructure
- Coinbase Advanced Trade API v3 integration (REST + WebSocket)
- JWT authentication with ES256
- SQLite-based idempotency and ledger
- Market data service with 5m candle resampling
- Circuit breaker with health monitoring

## [4.0.0-alpha] - 2024-03-01

### Added
- Initial migration from Binance to Coinbase Advanced Trade API
- JWT authentication (REST with URI, WS without)
- WebSocket feeds: candles, market_trades, level2, heartbeats, user
- Order executor with idempotency guarantees
- Trade ledger with fee-adjusted PnL tracking
- Basic risk gate structure

### Known Limitations
- OMS reconciliation not end-to-end certified
- RiskGate not validated with live execution
- No live trading validation
- GemProtocol requires pandas/statsmodels for full functionality
