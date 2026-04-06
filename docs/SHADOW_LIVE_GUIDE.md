# Guía paso a paso — Shadow Live (CMD)

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

## Paso 2: Preparar el entorno

Crea un archivo `env.bat` en la raíz del repo con esto:

```cmd
@echo off
set BASE=E:\Proyectos\BotsDeTrading
set FORTRESS_REPO=%BASE%\fortress_v4
set FORTRESS_RUNTIME=%BASE%\fortress_runtime
set FORTRESS_SECRETS=%BASE%\fortress_secrets
set FORTRESS_CONFIG=%BASE%\fortress_v4\configs\prod_symbols.yaml
```

Crea un archivo `load_env.bat` en la raíz del repo con esto:

```cmd
@echo off
for /f "usebackq tokens=1,* delims==" %%A in ("%FORTRESS_SECRETS%\prod.env") do (
    set "%%A=%%B"
)
echo Credenciales cargadas.
```

Ahora ejecuta en CMD:

```cmd
REM Crear directorios
mkdir "E:\Proyectos\BotsDeTrading\fortress_runtime\logs" 2>nul
mkdir "E:\Proyectos\BotsDeTrading\fortress_runtime\state" 2>nul
mkdir "E:\Proyectos\BotsDeTrading\fortress_runtime\data\raw" 2>nul
mkdir "E:\Proyectos\BotsDeTrading\fortress_secrets" 2>nul

REM Instalar dependencias
cd /d E:\Proyectos\BotsDeTrading\fortress_v4
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -e ".[dev]"
```

---

## Paso 3: Crear archivo de credenciales

Abre Notepad y crea `E:\Proyectos\BotsDeTrading\fortress_secrets\prod.env` con:

```
COINBASE_API_KEY=organizations/TU-ORG-ID/apiKeys/TU-KEY-ID
COINBASE_API_SECRET=-----BEGIN EC PRIVATE KEY-----\nTU_CLAVE_AQUI\n-----END EC PRIVATE KEY-----
FORTRESS_CONFIG=configs/prod_symbols.yaml
```

Sin comillas alrededor de los valores.

---

## Paso 4: Verificar config

```cmd
cd /d E:\Proyectos\BotsDeTrading\fortress_v4
call env.bat
call .venv\Scripts\activate.bat
call load_env.bat

python -m src.config_validator configs/prod_symbols.yaml
python -c "import yaml; c=yaml.safe_load(open('configs/prod_symbols.yaml')); print('observe_only:', c['trading']['observe_only'])"
```

Debes ver: `observe_only: True`

---

## Paso 5: Verificar conectividad con Coinbase

```cmd
pytest tests/integration/test_coinbase_integration.py::TestAuthentication -v
```

Si pasa `test_list_accounts` tus credenciales funcionan.

---

## Paso 6: Arrancar shadow live

```cmd
cd /d E:\Proyectos\BotsDeTrading\fortress_v4
call env.bat
call .venv\Scripts\activate.bat
call load_env.bat

python -m src.main
```

Verifica los primeros logs segun `docs/SHADOW_LIVE_CHECKLIST.md`.

**Dejalo corriendo. No cierres esta terminal.**

---

## Paso 7: Claude Code monitorea (en OTRA terminal CMD)

Abre otra ventana CMD:

```cmd
cd /d E:\Proyectos\BotsDeTrading\fortress_v4
call env.bat
call .venv\Scripts\activate.bat
claude
```

Dentro de Claude Code, pega el prompt de `docs/CLAUDE_CODE_MONITOR_PROMPT.md`.

---

## Emergencia: Parar todo

### Desde la terminal del bot
```
Ctrl+C
```

### Desde otra terminal CMD
```cmd
REM Ver procesos python
tasklist /fi "imagename eq python.exe"

REM Matar por PID
taskkill /pid NUMERO_DE_PID /f

REM Matar todos los python (cuidado si tienes otros)
taskkill /im python.exe /f
```

### Kill switch (sin matar el proceso)
```cmd
cd /d E:\Proyectos\BotsDeTrading\fortress_v4
call .venv\Scripts\activate.bat
python -c "from src.risk.kill_switch import KillSwitch, KillSwitchMode; ks = KillSwitch(); ks.activate(KillSwitchMode.BLOCK_NEW, 'manual stop', 'operator'); print(ks.state)"
```
