# fortress_v4

Runtime de trading para Coinbase Advanced Trade con foco en seguridad operativa, integridad de estado, mantenibilidad y evolución controlada hacia OMS certificado, validación live de riesgo y Strategy Layer.

## Estado actual
- Infraestructura de exchange: madura
- OMS Reconciliation: parcialmente validado
- Risk Gate: implementado, no certificado end-to-end
- Market data runtime: estable a nivel base
- Strategy Layer: no implementada formalmente
- Trading live: no certificado

## Objetivo inmediato
No crecer features arbitrariamente. Primero cerrar:
1. runtime correctness
2. config-driven behavior
3. OMS certification
4. risk live validation
5. strategy layer

## Componentes principales
- `src/core/`: exchange, websocket, auth, cuantización
- `src/execution/`: idempotencia y órdenes
- `src/accounting/`: ledger
- `src/oms/`: reconcile
- `src/risk/`: gate y circuit breaker
- `src/marketdata/`: ingestión y cierre de velas
- `src/strategy/`: base y estrategia demo
- `src/simulation/`: paper engine
- `src/validation/`: protocolos

## Reglas del runtime
- Fail-closed siempre
- Ningún submit puede bypass-ear `RiskGate`
- `observe_only`, `dry_run` y trading real son mutuamente excluyentes
- No se aceptan velas parciales ni dispatch duplicado
- No se permiten hardcodes si existe config equivalente

## Validación mínima obligatoria
```bash
python -m compileall -q src tests
pytest -q
```

## Estado honesto
Este repo no es todavía un bot de trading operativo para producción.
Es una base madura de infraestructura y runtime controlado.
