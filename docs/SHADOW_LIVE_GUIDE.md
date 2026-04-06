# Guía paso a paso — Shadow Live

## Lo que necesitas antes de empezar

1. **Python 3.11+** instalado en tu máquina
2. **Credenciales de Coinbase Advanced Trade API** (key + secret)
3. **Claude Code** instalado (para monitoreo automático)
4. **El repo clonado** con la rama `fix/baseline-determinism` mergeada a main

---

## Paso 1: Obtener credenciales de Coinbase

1. Ve a https://portal.cdp.coinbase.com/
2. Crea un API key con permisos de **Trade** (read + trade)
3. Descarga o copia:
   - `API Key Name` — se ve como `organizations/xxx/apiKeys/yyy`
   - `API Secret` — es una EC private key en formato PEM

---

## Paso 2: Preparar el entorno (Windows PowerShell)

```powershell
# 1. Variables de entorno
$base = "E:\Proyectos\BotsDeTrading"

$env:FORTRESS_REPO = "$base\fortress_v4"
$env:FORTRESS_RUNTIME = "$base\fortress_runtime"
$env:FORTRESS_SECRETS = "$base\fortress_secrets"
$env:FORTRESS_CONFIG = "$base\fortress_v4\configs\prod_symbols.yaml"

# 2. Crear directorios si no existen
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\logs"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\state"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\data\raw"
New-Item -ItemType Directory -Force -Path "$base\fortress_secrets"

# 3. Activar virtual environment
cd $env:FORTRESS_REPO
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

---

## Paso 3: Crear archivo de credenciales

Crea el archivo `E:\Proyectos\BotsDeTrading\fortress_secrets\prod.env`:

```
COINBASE_API_KEY="organizations/TU-ORG-ID/apiKeys/TU-KEY-ID"
COINBASE_API_SECRET="-----BEGIN EC PRIVATE KEY-----\nTU_CLAVE_AQUI\n-----END EC PRIVATE KEY-----"
FORTRESS_CONFIG="configs/prod_symbols.yaml"
```

**IMPORTANTE**: El secret tiene saltos de línea. En Windows, asegúrate de que el `\n` se interprete correctamente. Si tienes problemas, pon la clave completa en una sola línea con `\n` literal entre cada parte.

---

## Paso 4: Verificar config

```powershell
cd $env:FORTRESS_REPO

# Cargar credenciales (Windows no tiene 'source', hay que hacerlo manual)
Get-Content "$env:FORTRESS_SECRETS\prod.env" | ForEach-Object {
    if ($_ -match '^([^#][^=]+)=(.*)$') {
        $key = $matches[1].Trim()
        $val = $matches[2].Trim().Trim('"')
        [Environment]::SetEnvironmentVariable($key, $val, "Process")
    }
}

# Validar config
python -m src.config_validator configs/prod_symbols.yaml

# Verificar que observe_only está en true
python -c "import yaml; c=yaml.safe_load(open('configs/prod_symbols.yaml')); print('observe_only:', c['trading']['observe_only'])"
```

Debes ver: `observe_only: True`

---

## Paso 5: Verificar conectividad con Coinbase

```powershell
pytest tests/integration/test_coinbase_integration.py::TestAuthentication -v
```

Si pasa `test_list_accounts` → tus credenciales funcionan.

---

## Paso 6: Arrancar shadow live

```powershell
cd $env:FORTRESS_REPO
python -m src.main
```

Verifica los primeros logs según el checklist en `docs/SHADOW_LIVE_CHECKLIST.md`.

**Déjalo corriendo.** No cierres la terminal.

---

## Paso 7: Claude Code monitorea (en OTRA terminal)

Abre otra terminal PowerShell y ejecuta Claude Code apuntando al repo:

```powershell
cd E:\Proyectos\BotsDeTrading\fortress_v4
claude
```

Dentro de Claude Code, pega el prompt de monitoreo que está en `docs/CLAUDE_CODE_MONITOR_PROMPT.md`.

---

## Emergencia: Parar todo

### Desde la terminal del bot
```
Ctrl+C
```

### Desde Claude Code
Claude Code puede ejecutar:
```bash
# Encontrar el proceso
Get-Process python | Where-Object {$_.CommandLine -like "*src.main*"}

# Matarlo
Stop-Process -Name python -Force
```

### Kill switch (sin matar el proceso)
```python
python -c "
from src.risk.kill_switch import KillSwitch, KillSwitchMode
ks = KillSwitch()
ks.activate(KillSwitchMode.BLOCK_NEW, 'manual stop', 'operator')
print(ks.state)
"
```
