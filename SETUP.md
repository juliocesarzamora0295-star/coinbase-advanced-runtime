# Fortress v4 - Setup Guide

## Estructura de Directorios

```
E:\Proyectos\BotsDeTrading\
├── fortress_v4\                  # REPO (Git) - Solo código
│   ├── src\                      # Código fuente
│   ├── tests\                    # Tests
│   ├── configs\                  # Configs versionables
│   ├── pyproject.toml            # Dependencias
│   ├── README.md
│   └── .gitignore
│
├── fortress_runtime\             # RUNTIME (NO Git) - Datos/logs
│   ├── data\raw\                 # Datos de mercado crudos
│   ├── data\processed\           # Datos procesados
│   ├── runs\                     # Resultados de backtests
│   ├── reports\                  # Reportes
│   ├── logs\                     # Logs
│   ├── cache\                    # Cache
│   └── state\                    # Estado persistente (SQLite)
│
└── fortress_secrets\             # SECRETS (NO Git) - Keys/env
    └── .env                      # Variables de entorno
```

## Instalación Rápida (Windows)

### 1. Crear estructura de directorios

```powershell
# Crear directorios
$base = "E:\Proyectos\BotsDeTrading"
New-Item -ItemType Directory -Force -Path "$base\fortress_v4"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\data\raw"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\data\processed"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\runs"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\reports"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\logs"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\cache"
New-Item -ItemType Directory -Force -Path "$base\fortress_runtime\state"
New-Item -ItemType Directory -Force -Path "$base\fortress_secrets"

# Configurar variables de entorno (User)
[Environment]::SetEnvironmentVariable("FORTRESS_REPO", "$base\fortress_v4", "User")
[Environment]::SetEnvironmentVariable("FORTRESS_RUNTIME", "$base\fortress_runtime", "User")
[Environment]::SetEnvironmentVariable("FORTRESS_SECRETS", "$base\fortress_secrets", "User")
```

### 2. Clonar/instalar código

```powershell
cd E:\Proyectos\BotsDeTrading\fortress_v4

# Si usas Git:
git clone <tu-repo> .

# O copiar archivos manualmente
```

### 3. Instalar dependencias

```powershell
cd E:\Proyectos\BotsDeTrading\fortress_v4
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 4. Configurar credenciales

```powershell
cd E:\Proyectos\BotsDeTrading\fortress_secrets
copy .env.example .env
notepad .env  # Editar con tus credenciales
```

Obtener credenciales desde: https://portal.cdp.coinbase.com/

### 5. Verificar instalación

```powershell
# Recargar variables de entorno
$env:FORTRESS_REPO = "E:\Proyectos\BotsDeTrading\fortress_v4"
$env:FORTRESS_RUNTIME = "E:\Proyectos\BotsDeTrading\fortress_runtime"
$env:FORTRESS_SECRETS = "E:\Proyectos\BotsDeTrading\fortress_secrets"

# Ejecutar tests unitarios
cd E:\Proyectos\BotsDeTrading\fortress_v4
pytest tests/unit/ -v

# Ejecutar tests de integración (requiere credenciales)
pytest tests/integration/ -v
```

## Instalación Rápida (Linux/Mac)

```bash
# Crear directorios
export BASE="$HOME/fortress"
mkdir -p $BASE/fortress_v4
mkdir -p $BASE/fortress_runtime/{data/{raw,processed},runs,reports,logs,cache,state}
mkdir -p $BASE/fortress_secrets

# Configurar variables de entorno (agregar a ~/.bashrc o ~/.zshrc)
echo 'export FORTRESS_REPO="'$BASE'/fortress_v4"' >> ~/.bashrc
echo 'export FORTRESS_RUNTIME="'$BASE'/fortress_runtime"' >> ~/.bashrc
echo 'export FORTRESS_SECRETS="'$BASE'/fortress_secrets"' >> ~/.bashrc
source ~/.bashrc

# Instalar dependencias
cd $BASE/fortress_v4
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Configurar credenciales
cd $BASE/fortress_secrets
cp .env.example .env
nano .env  # Editar con tus credenciales

# Verificar
pytest tests/unit/ -v
```

## Uso

### Ejecutar bot

```bash
# Desde cualquier lugar (con variables de entorno configuradas)
fortress

# O directamente
cd $FORTRESS_REPO
python -m src.main
```

### Ejecutar tests

```bash
# Todos los tests
pytest tests/ -v

# Solo unitarios (no requieren credenciales)
pytest tests/unit/ -v

# Solo integración (requieren credenciales)
pytest tests/integration/ -v

# Con cobertura
pytest tests/ --cov=src --cov-report=html
```

### Formatear código

```bash
black src/ tests/
ruff check src/ tests/
mypy src/
```

## Troubleshooting

### Error: "FORTRESS_REPO no configurado"

Asegúrate de que las variables de entorno estén configuradas:

```powershell
# Windows
[Environment]::GetEnvironmentVariable("FORTRESS_REPO", "User")
[Environment]::GetEnvironmentVariable("FORTRESS_RUNTIME", "User")
[Environment]::GetEnvironmentVariable("FORTRESS_SECRETS", "User")

# Si están vacías, configurarlas:
[Environment]::SetEnvironmentVariable("FORTRESS_REPO", "E:\Proyectos\BotsDeTrading\fortress_v4", "User")
[Environment]::SetEnvironmentVariable("FORTRESS_RUNTIME", "E:\Proyectos\BotsDeTrading\fortress_runtime", "User")
[Environment]::SetEnvironmentVariable("FORTRESS_SECRETS", "E:\Proyectos\BotsDeTrading\fortress_secrets", "User")
```

### Error: "No module named 'src'"

Asegúrate de instalar el paquete en modo editable:

```bash
cd $FORTRESS_REPO
pip install -e ".[dev]"
```

### Error: "COINBASE_KEY_NAME no configurado"

Crea el archivo `.env` en `fortress_secrets/` con tus credenciales.

## Estructura del Código

```
src/
├── config.py              # Configuración centralizada (rutas desde env)
├── core/
│   ├── jwt_auth.py        # Autenticación JWT (P0: uri en payload)
│   ├── coinbase_exchange.py  # Cliente REST (P0: wrappers correctos)
│   ├── coinbase_websocket.py # Cliente WS (P0: ISO-8601, heartbeat gaps)
│   └── quantization.py    # Cuantización (P0: quote_increment)
├── execution/
│   ├── idempotency.py     # Sistema de idempotencia
│   └── orders.py          # Ejecutor de órdenes
├── accounting/
│   └── ledger.py          # Ledger con Decimal (P1: fees en base)
├── risk/
│   └── circuit_breaker.py # Circuit breaker (P1: callback a fills)
└── main.py                # Entry point
```

## Correcciones P0/P1 Implementadas

### P0 (Críticos)
- ✅ JWT REST: uri en payload (no headers)
- ✅ Order response parsing: wrappers correctos
- ✅ WebSocket: ISO-8601 timestamp parsing
- ✅ Heartbeat_counter gap detection
- ✅ Quantization: quote_size usa quote_increment

### P1 (Importantes)
- ✅ Timeframe: usa canal candles nativo
- ✅ Estrategias determinísticas
- ✅ Breakout sin lookahead
- ✅ Circuit breaker cableado a fills
- ✅ Ledger: fees en base ajustan qty
