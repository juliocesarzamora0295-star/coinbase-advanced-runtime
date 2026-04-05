# fortress_v4

Runtime de trading para Coinbase Advanced Trade con foco en seguridad operativa, integridad de estado, mantenibilidad y evolución controlada hacia OMS certificado, validación live de riesgo y Strategy Layer.

## Estado actual

| Componente | Estado |
|---|---|
| Infraestructura de exchange | Madura |
| OMS Reconciliation | Parcialmente validado |
| Risk Gate | Implementado, integración live pendiente de certificación |
| Market data runtime | Estable |
| Strategy Layer | Solo `ma_crossover` implementado y registrado |
| Trading live | **BLOQUEADO** — no certificado para producción |

## Modos de ejecución

Los tres modos son **mutuamente excluyentes**. Prioridad: `observe_only` > `dry_run` > live.

| Modo | `observe_only` | `dry_run` | Comportamiento |
|---|---|---|---|
| Observación | `true` | cualquiera | Pipeline completo (señales + riesgo), **sin ejecución**. Ni PaperEngine ni exchange. |
| Simulación | `false` | `true` | Pipeline completo + ejecución simulada vía PaperEngine. Sin exchange real. |
| Live | `false` | `false` | **BLOQUEADO**. Requiere certificación OMS + riesgo live antes de activar. |

> **`observe_only` no bloquea la generación de señales.** El pipeline corre completo —
> market data, estrategia, RiskGate — pero la orden se loggea y se descarta.

## Objetivo inmediato

Cerrar en orden:

1. ✅ runtime correctness
2. ✅ config-driven behavior
3. OMS certification
4. risk live validation
5. strategy layer

## Componentes principales

- `src/core/` — exchange, websocket, auth, cuantización
- `src/execution/` — idempotencia y órdenes
- `src/accounting/` — ledger
- `src/oms/` — reconcile
- `src/risk/` — gate y circuit breaker
- `src/marketdata/` — ingestión y cierre de velas
- `src/strategy/` — base y estrategia `ma_crossover`
- `src/simulation/` — paper engine
- `src/validation/` — protocolos de backtest

## Estrategias disponibles

Solo `ma_crossover` (alias `sma_crossover`) está implementada y registrada.

`breakout` y `mean_reversion` **no existen** en el código — cualquier referencia en config produce warning y se omite.

## Reglas del runtime

- Fail-closed siempre: si falta equity, posición o precio → bloquear, loggear, no inventar defaults
- Ningún submit puede bypass-ear `RiskGate`
- `observe_only`, `dry_run` y live son mutuamente excluyentes (ver tabla arriba)
- No se aceptan velas parciales ni dispatch duplicado
- No se permiten hardcodes si existe config equivalente en YAML

## Validación mínima obligatoria

```bash
python -m compileall -q src tests
pytest -q
```

## Estado honesto

Este repo **no es todavía un bot de trading operativo para producción**.  
Es una base madura de infraestructura y runtime controlado.  
El modo live está desactivado hasta que OMS y risk live estén certificados.
