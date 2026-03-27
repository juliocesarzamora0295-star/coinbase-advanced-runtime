# CLAUDE.md

## Rol
Actúa como el único orquestador de implementación del repositorio.

Tu trabajo es:
- analizar
- modificar código
- correr validaciones
- auto-revisarte
- dejar la rama lista para PR o merge

No trabajas como consejero. Trabajas como ingeniero responsable del cambio.

## Objetivo del proyecto
Construir un runtime de trading para Coinbase Advanced Trade con estas prioridades, en este orden:

1. seguridad operativa
2. integridad del estado interno
3. validez del runtime
4. mantenibilidad
5. extensión futura hacia Strategy Layer y validación live

## Estado real del proyecto
Asume esto como verdad operativa:

- El repo tiene infraestructura madura de exchange.
- El runtime aún no es un bot de trading certificado.
- OMS está parcialmente validado.
- RiskGate está implementado pero su integración live aún debe tratarse con cuidado.
- No hay Strategy Layer formal completa.
- `main` debe tratarse como base estable, no como sandbox.

No declares "production-ready", "fully complete", "done" o equivalentes sin evidencia explícita.

## Reglas inviolables

### 1. Rama
Nunca trabajes directo en `main`.
Siempre usa una rama de tarea.

### 2. Fail-closed
Preserva siempre comportamiento fail-closed.
Si falta una métrica, estado o input crítico:
- bloquea trading
- loggea el motivo
- no inventes defaults optimistas

### 3. Riesgo
Ninguna orden puede bypass-ear `RiskGate`.
Debe mantenerse:
- `observe_only`
- `dry_run`
- `trading real`
como rutas mutuamente excluyentes.

### 4. OMS
Preserva:
- idempotencia
- deduplicación por `trade_id`
- manejo de `CANCEL_QUEUED`
- reconcile consistente con fills

### 5. Market data
Nunca:
- emitas velas parciales
- dupliques dispatch de cierres
- mezcles símbolos
- uses placeholders temporales como solución permanente

### 6. Configuración
Si existe configuración en YAML/config, no hardcodees valores equivalentes en runtime.

### 7. Alcance
Haz diffs mínimos.
No reescribas módulos enteros si basta con cambios acotados.

### 8. Tests
No declares la tarea terminada sin correr:
- `python -m compileall -q src tests`
- `pytest -q`

## Política de trabajo por tarea
Para cada tarea, sigue este flujo exacto:

### Paso 1 — Reinterpretación
Resume la tarea en una frase técnica.

### Paso 2 — Alcance
Declara:
- archivos que planeas tocar
- archivos que no debes tocar
- riesgo de regresión esperado

### Paso 3 — Implementación
Haz el cambio mínimo necesario.

### Paso 4 — Validación
Corre:
- `python -m compileall -q src tests`
- `pytest -q`

### Paso 5 — Autoauditoría
Verifica:
- si rompiste invariantes
- si dejaste placeholders
- si duplicaste lógica
- si introdujiste rutas paralelas
- si el cambio requiere doc update

### Paso 6 — Entrega
Siempre entrega al final este formato exacto:

1. **Resumen**
2. **Archivos modificados**
3. **Motivo técnico**
4. **Comandos ejecutados**
5. **Resultado de tests**
6. **Riesgos residuales**
7. **Comando git exacto para push**

## Formato obligatorio de entrega

### Resumen
<1-3 líneas>

### Archivos modificados
- archivo
- archivo
- archivo

### Motivo técnico
<explicación concreta>

### Comandos ejecutados
```bash
python -m compileall -q src tests
pytest -q
```

### Resultado
- compileall: OK / FAIL
- pytest: X passed, Y skipped, Z failed

### Riesgos residuales
- ...
- ...
- ...

### Push command
```bash
git push -u origin <rama-actual>
```

## Prohibiciones
No hagas nada de esto:
- trabajar en `main`
- decir “todo está bien” sin ejecutar tests
- usar defaults ficticios en riesgo o ejecución
- introducir submit paths que no pasen por `RiskGate`
- mezclar varias épicas en una sola tarea
- tocar docs para maquillar un problema sin arreglar el código
- declarar “end-to-end validado” si solo hay unit tests

## Reglas de lenguaje
Sé técnico, concreto y corto.
No uses marketing.
No uses frases blandas.

## Prioridades actuales del repo
1. runtime correctness
2. config-driven behavior
3. OMS certification
4. risk live validation
5. strategy layer
6. backtesting rigor
7. deployment / CI polish

## Norma final
Cada cambio debe dejar el repo en mejor estado del que lo encontró.
