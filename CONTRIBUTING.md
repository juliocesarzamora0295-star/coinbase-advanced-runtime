# CONTRIBUTING

## Reglas de contribución

### 1. Nunca trabajar en `main`
Toda contribución debe vivir en una rama:
- `feat/...`
- `fix/...`
- `docs/...`
- `refactor/...`

### 2. Validación obligatoria
Antes de declarar una tarea terminada:
```bash
python -m compileall -q src tests
pytest -q
```

### 3. Invariantes obligatorias
- Fail-closed siempre
- Ningún submit bypass-ea `RiskGate`
- `observe_only`, `dry_run` y trading real son mutuamente excluyentes
- No velas parciales
- No dispatch duplicado
- No mezcla de símbolos en pipelines de señal
- No hardcodes si existe config

### 4. Cambios mínimos
No reescribas módulos enteros si bastan diffs acotados.

### 5. Formato de entrega
Toda contribución debe incluir:
1. resumen
2. archivos modificados
3. motivo técnico
4. comandos ejecutados
5. resultado de tests
6. riesgos residuales
7. comando git exacto para push

### 6. Honestidad documental
No marcar como “funcional” algo que solo está validado por unit tests.
No marcar como “end-to-end” algo que no fue certificado live/restart-safe.
