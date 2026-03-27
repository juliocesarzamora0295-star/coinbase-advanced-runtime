# PROJECT_SUMMARY

## Snapshot del proyecto
- Nombre: `fortress_v4`
- Exchange target: Coinbase Advanced Trade
- Estado: infraestructura madura + runtime controlado
- Suite validada: 83 passed, 14 skipped
- `main`: base estable
- Trabajo futuro: ramas separadas por épica

## Qué sí está hecho
- REST/WS/JWT
- cuantización
- idempotencia
- ledger
- OMS base
- RiskGate base
- config-driven runtime parcial
- market data runtime estable
- signal isolation

## Qué no está cerrado
- OMS end-to-end certification
- RiskGate live certification
- Strategy Layer formal
- trading live certificado
- portfolio layer formal

## Riesgo principal actual
Falsa sensación de completitud por suite verde. La base es fuerte, pero no debe tratarse como productiva.

## Siguiente prioridad
1. OMS certification
2. risk live validation
3. strategy layer
