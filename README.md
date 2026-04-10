# fortress_v4

Runtime de trading para Coinbase Advanced Trade con foco en seguridad operativa, integridad de estado, mantenibilidad y evolución controlada hacia OMS certificado, validación live de riesgo y Strategy Layer.

## Estado actual
- Infraestructura de exchange: madura
- OMS Reconciliation: validado (27+ tests — fills, orphans, dedup, degradation)
- Risk Gate: implementado con exposure check, circuit breaker, kill switch
- Circuit Breaker: CLOSED->OPEN->HALF_OPEN->CLOSED lifecycle validado
- Market data runtime: estable, multi-timeframe nativo, 40+ horas de streaming sin desconexiones
- Strategy Layer: base ABC, registry config-driven, SMA crossover, selector regime-aware
- Backtest framework: engine, synthetic data, risk adapter, strategy adapter, segmented runner
- Health check: HealthChecker + HealthFileWriter (Docker HEALTHCHECK)
- Graceful shutdown: SIGINT/SIGTERM handlers, state logging, 10s timeout
- Dry-run PaperEngine: validado con `--fresh` mode (estado aislado, cash simulado)
- Restart resilience: 3/3 ciclos pasados (SQLite integrity, ledger consistency, kill switch, idempotency)
- Trading live: no certificado — observe_only y dry_run validados, live canary config lista

## Test coverage
- 1136+ tests passing, 14 skipped (Coinbase API integration tests)
- Unit, integration, quantitative, property-based tests
- Stability report: [`docs/stability_report.md`](docs/stability_report.md)

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

## Live validation scripts

```bash
# Observe-only (no orders, just log signals)
python scripts/observe_only_runner.py --duration 300

# Dry-run with PaperEngine simulation
python scripts/dry_run_runner.py --config config/dry_run.yaml --duration 600

# Dry-run with isolated state (no exchange bootstrap)
python scripts/dry_run_runner.py --config config/dry_run.yaml --duration 600 --fresh --initial-cash 1000

# Aggressive dry-run (SMA 5/13, tight risk limits)
python scripts/dry_run_runner.py --config config/dry_run_aggressive.yaml --duration 3600 --fresh

# Restart resilience test (3 cycles)
python scripts/stability_test.py --cycles 3 --duration 120 --fresh

# Analyze log output
python scripts/analyze_dry_run.py logs/dry_run_*.json --format markdown
```

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

## Configs disponibles

| Config | Modo | Descripcion |
|--------|------|-------------|
| `config/observe_only.yaml` | observe_only | Solo observa, no ejecuta |
| `config/dry_run.yaml` | dry_run | PaperEngine, SMA 20/50 |
| `config/dry_run_aggressive.yaml` | dry_run | SMA 5/13, limites estrictos |
| `config/live_canary.yaml` | live | $1/trade, $5 daily cap |

## Estado honesto
Este repo no es todavia un bot de trading operativo para produccion.
Es una base madura de infraestructura y runtime controlado con validacion extensiva.
Dry-run con PaperEngine validado. Live canary config lista para primer test real.
