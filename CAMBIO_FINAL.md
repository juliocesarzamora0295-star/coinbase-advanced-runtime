# CAMBIO_FINAL

## Último checkpoint estable
Checkpoint preservado con:
- runtime de riesgo conectado a config
- modos de ejecución explícitos
- comportamiento fail-closed reforzado
- documentación alineada con el estado real

## Archivos clave tocados
- `src/main.py`
- `src/config.py`
- `configs/symbols.yaml`
- `src/accounting/ledger.py`
- `src/execution/orders.py`
- `README.md`

## Resultado
- compileall: OK
- pytest: 83 passed, 14 skipped

## Estado correcto del repo
- OMS: parcialmente validado
- RiskGate: implementado, no certificado end-to-end
- Runtime: estable
- Strategy Layer: pendiente
