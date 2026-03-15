# Fortress v4 - Coinbase Advanced Trade Integration

**ESTADO: Infraestructura madura + componentes experimentales de strategy/paper/validation**

Componente de infraestructura para Coinbase Advanced Trade API v3.

## Estado Actual

| Componente | Estado | Notas |
|------------|--------|-------|
| API Client (REST) | ✅ Funcional | JWT con query params, wrappers, retry correctos |
| WebSocket | ✅ Funcional | candles (5m), market_trades, level2, heartbeats |
| Cuantización | ✅ Funcional | BUY floor, SELL ceil con Decimal |
| Idempotencia | ✅ Funcional | SQLite durable |
| Ledger | ✅ Funcional | PnL tracking con fees + equity invariant validation |
| Circuit Breaker | ⚠️ Básico | Health monitoring, sin pre-trade gate real |
| **Strategy Layer** | ⚠️ **PARCIAL** | estrategia demo (naive MA); SmaCrossoverStrategy no integrada |
| **Paper Engine** | ⚠️ **BÁSICO** | simulación simple en dry_run, sin partial fills ni size-aware matching |
| **GemProtocol** | ⚠️ **NO INTEGRADO** | código presente, sin wiring en pipeline |
| **Smoke Tests** | ⚠️ **PARCIAL** | smoke_test_mode y max_cycles funcionan, validaciones básicas |
| **OMS Reconciliation** | ⚠️ **PARCIALMENTE VALIDADO** | unit tests sólidos; falta certify end-to-end |
| **Position Sizing** | ⚠️ **PARCIAL** | En RiskGate, sin integración en main |
| **Timeframe/Resampling** | ✅ **Funcional** | Resampling 5m -> 1h/4h con filtrado de barras |
| **Risk Gate** | ⚠️ **IMPLEMENTADO, NO CERTIFICADO** | lógica correcta; métricas reales; falta validación end-to-end |

### P0 Absorbidos (infraestructura base)
- ✅ JWT firma path + query params
- ✅ heartbeat_counter convertido de string a int
- ✅ heartbeats sin product_ids
- ✅ MARKET order -> OPEN_PENDING (no FILLED)
- ✅ Canal user: una suscripción con todos los product_ids
- ✅ Resampling 5m -> 1h/4h UTC

### P1 Implementados
- ✅ OMS reconcile: REST fills, deduplicación, CANCEL_QUEUED
- ✅ Risk Gate: métricas reales desde ledger (day_pnl, drawdown), fail-closed
- ✅ Config YAML manda: RiskLimits desde config, no hardcodes
- ✅ Modos separados: observe_only / dry_run / trading real con invariantes claras

### P1 Pendientes (para bot real)
- ⚠️ Bootstrap de órdenes abiertas
- ⚠️ Position sizing integrado en main
- ⚠️ Strategy layer
- ⚠️ Feature engineering
- ⚠️ Certificación end-to-end de RiskGate con datos live
- ⚠️ Validación de OMS reconcile en restart real

## Características Implementadas

### Core (Sólido)
- **JWT Authentication**: ES256 con uri en payload (REST) y sin uri (WS)
- **WebSocket**: Canal `candles` (buckets 5m), market_trades, level2, heartbeats
- **Idempotencia**: SQLite durable para 1:1 intent_id ↔ client_order_id
- **Cuantización**: Side-aware con Decimal (corregido para SELL/TP)
- **Ledger**: Tracking de PnL con fees en base currency
- **Circuit Breaker**: Monitoreo básico de latencia, reject rate, WS gaps

### Experimental (Sin integración completa)
- **Paper Engine**: Simulación básica en dry_run (sin partial fills, sin size-aware matching)
- **Ledger Validations**: `validate_equity_invariant()`, `dedup_check()`, `get_stats()` - usados en smoke mode
- **Smoke Tests**: `smoke_test_mode` y `max_cycles` funcionan desde YAML config
- **GemProtocol**: Código presente pero sin wiring en pipeline (no se usa en runtime)
- **SmaCrossoverStrategy**: Código presente pero no integrada (se usa naive MA)

## Estructura

```
fortress_v4/              # REPO (Git) - Solo código
├── src/                  # Código fuente
│   ├── accounting/       # Ledger, PnL tracking
│   ├── core/             # Exchange, WS, JWT, quantization
│   ├── execution/        # Orders, idempotency
│   ├── marketdata/       # MarketDataService, SignalEngine
│   ├── oms/              # Reconcile service
│   ├── risk/             # RiskGate, CircuitBreaker
│   ├── simulation/       # PaperEngine (NEW)
│   ├── strategy/         # SMA Crossover, Strategy base (NEW)
│   └── validation/       # GemProtocol (NEW)
├── tests/                # Tests unitarios e integración
├── configs/              # Configs versionables
├── scripts/              # Scripts de utilidad
├── pyproject.toml        # Dependencias
└── README.md

fortress_runtime/         # RUNTIME (NO Git) - Datos/logs
├── data/raw/             # Datos de mercado
├── runs/                 # Resultados de backtests
├── reports/              # Reportes
├── logs/                 # Logs
├── cache/                # Cache
└── state/                # Estado persistente (SQLite)

fortress_secrets/         # SECRETS (NO Git) - Keys/env
└── .env                  # Variables de entorno
```

**Regla de oro**: Git solo en `fortress_v4`. Todo lo que "crece" (CSV, resultados, logs) vive en `fortress_runtime`.

## Instalación Rápida

### Windows (PowerShell)

```powershell
# 1. Clonar repo
git clone <repo-url> E:\Proyectos\BotsDeTrading\fortress_v4
cd E:\Proyectos\BotsDeTrading\fortress_v4

# 2. Ejecutar setup
.\scripts\setup.ps1

# 3. Reiniciar terminal y continuar
pip install -e ".[dev]"

# 4. Configurar credenciales
notepad E:\Proyectos\BotsDeTrading\fortress_secrets\.env

# 5. Verificar
pytest tests/unit/ -v
```

### Linux/Mac

```bash
# 1. Clonar repo
git clone <repo-url> ~/fortress/fortress_v4
cd ~/fortress/fortress_v4

# 2. Ejecutar setup
./scripts/setup.sh

# 3. Recargar shell
source ~/.bashrc  # o ~/.zshrc

# 4. Instalar dependencias
pip install -e ".[dev]"

# 5. Configurar credenciales
nano ~/fortress/fortress_secrets/.env

# 6. Verificar
pytest tests/unit/ -v
```

## Configuración

### Variables de Entorno

```bash
# Requeridas
FORTRESS_REPO="E:\Proyectos\BotsDeTrading\fortress_v4"
FORTRESS_RUNTIME="E:\Proyectos\BotsDeTrading\fortress_runtime"
FORTRESS_SECRETS="E:\Proyectos\BotsDeTrading\fortress_secrets"

# En fortress_secrets/.env
COINBASE_KEY_NAME="organizations/{org_id}/apiKeys/{key_id}"
COINBASE_KEY_SECRET="-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----"
COINBASE_JWT_ISSUER="cdp"
```

### Configuración de Símbolos

Editar `configs/symbols.yaml`:

```yaml
symbols:
  - symbol: "BTC-USD"
    enabled: true
    timeframe: "1h"
    strategies:
      - "ma_crossover"
      - "breakout"
```

## Uso

### Ejecutar Bot (Modo Observación)

```bash
# Desde cualquier lugar (con variables de entorno)
fortress

# O directamente
python -m src.main
```

**IMPORTANTE**: El bot arranca en modo **OBSERVACIÓN**. No ejecuta estrategias ni órdenes.
Para convertirlo en un bot de trading real, debes implementar:
- Feature engineering
- Signal generation
- Position sizing conectado a equity
- Risk checks pre-trade
- OMS reconciliation completa

### Tests

```bash
# Unitarios (83 passed, 0 skipped - no requieren credenciales)
pytest tests/unit/ -v

# Integración (⚠️ REQUIERE FLAG EXPLÍCITO - toca API real)
export COINBASE_RUN_LIVE_TESTS=1
pytest tests/integration/ -v

# Con cobertura
pytest tests/ --cov=src --cov-report=html
```

### Formatear Código

```bash
black src/ tests/
ruff check src/ tests/
mypy src/
```

## Arquitectura Objetivo (NO implementada completa)

```
┌─────────────────────────────────────────────────────────┐
│                     FORTRESS V4                          │
├─────────────────────────────────────────────────────────┤
│  Estrategias  │  PENDIENTE  │  PENDIENTE  │  PENDIENTE │
├─────────────────────────────────────────────────────────┤
│  Risk Engine  │  Circuit Breaker  │  Risk Gate         │
│               │  ✅ Básico        │  ⚠️ Estructura     │
├─────────────────────────────────────────────────────────┤
│  OMS          │  Reconcile       │  Position Sizing    │
│               │  ⚠️ Estructura   │  ❌ No impl.        │
├─────────────────────────────────────────────────────────┤
│  Execution    │  Order Executor  │  Idempotencia       │
│               │  ✅ OK           │  ✅ OK              │
├─────────────────────────────────────────────────────────┤
│  Accounting   │  Trade Ledger  │  PnL Tracking         │
│               │  ✅ OK         │  ✅ OK                │
├─────────────────────────────────────────────────────────┤
│  Exchange     │  REST Client  │  WebSocket  │  JWT Auth│
│               │  ✅ OK        │  ✅ OK      │  ✅ OK   │
└─────────────────────────────────────────────────────────┘
```

**Leyenda**: ✅ Implementado | ⚠️ Parcial | ❌ No implementado

## Correcciones P0/P1

### P0 (Críticos) ✅
- JWT REST: uri en payload `"<METHOD> api.coinbase.com<PATH>"`
- Order response parsing: wrappers correctos
- WebSocket: ISO-8601 timestamp parsing
- Heartbeat_counter gap detection
- Decimal para cantidades/precios
- Quantization: quote_size usa quote_increment

### P1 (Importantes) ⚠️ Parcial
- ✅ Timeframe: usa canal `candles` nativo
- ✅ Resampling 5m -> 1h/4h con filtrado de barras
- ⚠️ OMS Reconcile: Estructura creada
- ⚠️ Risk Gate: Estructura creada
- ❌ Estrategias determinísticas
- ❌ Circuit breaker cableado a fills reales (parcial)
- ✅ Ledger: fees en base currency ajustan qty neta
- ✅ Cuantización side-aware

## Licencia

MIT License
