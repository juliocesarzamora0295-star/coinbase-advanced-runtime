# fortress_v4

Runtime de trading para Coinbase Advanced Trade con foco en seguridad operativa, integridad de estado, mantenibilidad y evolución controlada hacia OMS certificado, validación live de riesgo y Strategy Layer.

## Estado actual
- Infraestructura de exchange: madura
- OMS Reconciliation: validado (27+ tests — fills, orphans, dedup, degradation)
- Risk Gate: implementado con exposure check, circuit breaker, kill switch
- Circuit Breaker: CLOSED→OPEN→HALF_OPEN→CLOSED lifecycle validado
- Market data runtime: estable, multi-timeframe nativo
- Strategy Layer: base ABC, registry config-driven, SMA crossover, selector regime-aware
- Backtest framework: engine, synthetic data, risk adapter, strategy adapter, segmented runner
- Health check: HealthChecker + HealthFileWriter (Docker HEALTHCHECK)
- Graceful shutdown: SIGINT/SIGTERM handlers, state logging, 10s timeout
- Trading live: no certificado — observe_only y dry_run validados

## Test coverage
- 1136+ tests passing, 14 skipped (Coinbase API integration tests)
- Unit, integration, quantitative, property-based tests

## Objetivo inmediato
1. risk live validation (RiskGate + CircuitBreaker en entorno real)
2. runtime correctness (mantener invariantes)
3. strategy iteration (nuevas estrategias vía registry + backtest)
4. multi-symbol live operation
5. deployment / CI polish

## Componentes principales
- `src/core/`: exchange, websocket, auth, cuantización
- `src/execution/`: idempotencia, órdenes, order_planner
- `src/accounting/`: ledger con SQLite
- `src/oms/`: reconcile (bootstrap, orphan detection, dedup)
- `src/risk/`: gate, circuit breaker, kill switch, position sizer, adaptive sizer
- `src/marketdata/`: ingestión, cierre de velas, multi-timeframe
- `src/strategy/`: base ABC, registry, SMA crossover, noop, selector, MTF filter
- `src/backtest/`: engine, data_feed, data_downloader, synthetic_data, risk_adapter, strategy_adapter, data_replay, report
- `src/monitoring/`: health_check, alert_manager, metrics
- `src/simulation/`: paper engine
- `src/validation/`: protocolos

## Signal pipeline
```
Signal → OMS readiness → KillSwitch → CircuitBreaker → PositionSizer → RiskGate → Exposure Check → OrderPlanner → Executor
```

## Execution modes
| Mode | observe_only | dry_run | Behavior |
|------|-------------|---------|----------|
| Observe | true | * | Log signals only, no execution |
| Dry run | false | true | PaperEngine simulation |
| Live | false | false | Real orders to Coinbase |

Default config is safe: `observe_only=true`, `dry_run=true`.

## Reglas del runtime
- Fail-closed siempre
- Ningún submit puede bypass-ear `RiskGate`
- `observe_only`, `dry_run` y trading real son mutuamente excluyentes
- No se aceptan velas parciales ni dispatch duplicado
- No se permiten hardcodes si existe config equivalente

## Backtest
```bash
# Run backtest with default SMA crossover
python -m src.backtest.run --data data.csv --cash 10000

# Segmented runner across market regimes
python -m src.backtest.segmented_runner --symbol BTC-USD --use-risk --use-full-adaptive
```

## Validación mínima obligatoria
```bash
python -m compileall -q src tests
pytest -q
```

## Estado honesto
Este repo no es todavía un bot de trading operativo para producción.
Es una base madura de infraestructura y runtime controlado con validación extensiva.
