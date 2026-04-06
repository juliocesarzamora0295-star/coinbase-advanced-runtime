# Shadow Live Checklist — Fortress v4

## Pre-vuelo (antes de arrancar)

### Credenciales
- [ ] `prod.env` existe en `fortress_secrets/` con `COINBASE_API_KEY` y `COINBASE_API_SECRET` reales
- [ ] Las credenciales son de **API key read+trade** (no solo read)
- [ ] `FORTRESS_CONFIG=configs/prod_symbols.yaml` apunta al config correcto

### Config
- [ ] `observe_only: true` en `prod_symbols.yaml`
- [ ] `dry_run: false` (observe_only ya previene ejecución)
- [ ] `smoke_test_mode: false`
- [ ] `max_cycles: 0` (sin límite — corre indefinido)
- [ ] Un solo símbolo habilitado (`BTC-USD`)

### Estructura de directorios
- [ ] `fortress_runtime/logs/` existe
- [ ] `fortress_runtime/state/` existe
- [ ] `fortress_runtime/data/raw/` existe

### Validación pre-arranque
- [ ] `python -m src.config_validator configs/prod_symbols.yaml` — OK
- [ ] `pytest tests/unit/ -q` — 0 failed
- [ ] Tests de integración con credenciales reales:
  - [ ] `pytest tests/integration/test_coinbase_integration.py -v` — al menos auth pasa

---

## Arranque

```bash
source fortress_secrets/prod.env
export FORTRESS_CONFIG=configs/prod_symbols.yaml
python -m src.main
```

### Verificar en los primeros 60 segundos
- [ ] Log muestra `ESTADO: SHADOW MODE`
- [ ] Log muestra `MODO: OBSERVE_ONLY`
- [ ] `JWT Auth initialized` aparece
- [ ] `Circuit Breaker initialized` aparece
- [ ] `Risk Gate initialized` aparece
- [ ] WebSocket conecta sin errores
- [ ] Primer `HEALTH_CHECK` muestra `overall: HEALTHY`

---

## Monitoreo continuo (primeras 4 horas)

### Cada 15 minutos
- [ ] `HEALTH_CHECK` sigue `HEALTHY`
- [ ] No hay `ERROR` ni `CRITICAL` en logs
- [ ] WebSocket no se ha desconectado
- [ ] Circuit breaker sigue `CLOSED`
- [ ] Kill switch sigue `OFF`

### Cada hora
- [ ] Contar `OBSERVE ONLY:` entries en log — debe crecer (señales generándose)
- [ ] Verificar que candles se cierran correctamente (log entries de cierre de vela)
- [ ] No hay `OMS DEGRADED` en logs
- [ ] Memoria del proceso no crece sin control (`ps aux | grep python`)
- [ ] No hay `EXECUTION_REPORT ESTIMATED` recurrente

### Al final de 4 horas
- [ ] Total de señales generadas > 0
- [ ] Cero órdenes enviadas a exchange (verify en Coinbase dashboard)
- [ ] Cero errores no recuperados
- [ ] Health check nunca degradó a UNHEALTHY

---

## Criterios de éxito (24h shadow)

- [ ] Runtime corrió 24h sin crash
- [ ] WebSocket reconectó automáticamente si hubo desconexión
- [ ] Circuit breaker no trippeó por falsos positivos
- [ ] Candles cerraron correctamente en cada hora
- [ ] Señales de estrategia se generaron consistentemente
- [ ] Cero órdenes reales en Coinbase dashboard
- [ ] Memoria estable (no crece más de 50MB sobre baseline)
- [ ] Logs no muestran warnings repetitivos no explicados

---

## Criterios de STOP inmediato

Si ves cualquiera de estos, mata el proceso:

1. **`CIRCUIT BREAKER TRIPPED`** — investigar causa antes de reiniciar
2. **`OMS DEGRADED`** recurrente — algo no reconcilia
3. **Orden real aparece en Coinbase dashboard** — observe_only falló
4. **`CRITICAL`** en logs — error no recuperable
5. **Memoria crece >500MB** — probable memory leak
6. **WebSocket no reconecta en >5 min** — conectividad rota
7. **Kill switch se activó solo** — algo tripeó el kill switch

### Cómo matar el proceso
```bash
# Opción 1: Graceful
Ctrl+C

# Opción 2: Si no responde
kill -SIGTERM <pid>

# Opción 3: Emergencia
kill -9 <pid>
```

---

## Post-shadow: decisión de avanzar a dry_run

Solo si TODOS los criterios de éxito de 24h se cumplen:

1. Cambiar en `prod_symbols.yaml`:
   ```yaml
   observe_only: false
   dry_run: true
   ```
2. Repetir el mismo checklist de monitoreo
3. Verificar que OMS registra órdenes simuladas correctamente
4. Verificar que RiskGate bloquea cuando debe

Solo después de dry_run exitoso se considera `dry_run: false`.
