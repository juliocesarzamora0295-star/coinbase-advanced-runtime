"""
Configuración centralizada de Fortress v4.

Usa variables de entorno para separar repo vs runtime vs secrets.

Variables requeridas:
  - FORTRESS_REPO: Ruta al repositorio (código)
  - FORTRESS_RUNTIME: Ruta a datos/logs/resultados
  - FORTRESS_SECRETS: Ruta a .env y keys

Ejemplo:
  FORTRESS_REPO=E:\\Proyectos\\BotsDeTrading\\fortress_v4
  FORTRESS_RUNTIME=E:\\Proyectos\\BotsDeTrading\\fortress_runtime
  FORTRESS_SECRETS=E:\\Proyectos\\BotsDeTrading\\fortress_secrets
"""

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv


class RuntimeMode(str, Enum):
    """
    Modo de ejecución del runtime.

    Rutas mutuamente excluyentes — no se puede activar más de una a la vez.

    OBSERVE_ONLY  — pipeline completo, nunca se envía orden ni a paper ni a exchange.
    PAPER         — pipeline completo + PaperEngine local, sin exchange real.
    SHADOW        — igual que PAPER pero emparejado con feed real para comparar.
    LIVE_CERT     — exchange real con límites mínimos de certificación.
    LIVE_PROD     — exchange real con límites de producción.
                    REQUIERE variable de entorno FORTRESS_LIVE_PROD_UNLOCK=1.
                    Sin esa variable, el runtime se niega a arrancar.
    """

    OBSERVE_ONLY = "observe_only"
    PAPER = "paper"
    SHADOW = "shadow"
    LIVE_CERT = "live_cert"
    LIVE_PROD = "live_prod"


def get_repo_path() -> Path:
    """Obtener ruta al repositorio (código)."""
    env_path = os.getenv("FORTRESS_REPO")
    if env_path:
        return Path(env_path)
    # Fallback: directorio del script
    return Path(__file__).parent.parent


def get_runtime_path() -> Path:
    """Obtener ruta a runtime (datos/logs/resultados)."""
    env_path = os.getenv("FORTRESS_RUNTIME")
    if env_path:
        return Path(env_path)
    # Fallback: hermano del repo
    return get_repo_path().parent / "fortress_runtime"


def get_secrets_path() -> Path:
    """Obtener ruta a secrets (.env, keys)."""
    env_path = os.getenv("FORTRESS_SECRETS")
    if env_path:
        return Path(env_path)
    # Fallback: hermano del repo
    return get_repo_path().parent / "fortress_secrets"


def load_env_file() -> None:
    """Cargar variables de entorno desde archivo .env."""
    secrets_path = get_secrets_path()
    env_file = secrets_path / ".env"

    if env_file.exists():
        load_dotenv(env_file)
    else:
        # Fallback: buscar en repo (para compatibilidad)
        repo_env = get_repo_path() / ".env"
        if repo_env.exists():
            load_dotenv(repo_env)


# Cargar .env al importar
load_env_file()


@dataclass
class CoinbaseConfig:
    """Configuración de Coinbase API."""

    key_name: str = field(default_factory=lambda: os.getenv("COINBASE_KEY_NAME", ""))
    key_secret: str = field(default_factory=lambda: os.getenv("COINBASE_KEY_SECRET", ""))
    issuer: str = field(default_factory=lambda: os.getenv("COINBASE_JWT_ISSUER", "cdp"))
    timeout: float = 30.0
    max_retries: int = 5

    @property
    def is_configured(self) -> bool:
        """Verificar que las credenciales están configuradas."""
        return bool(self.key_name and self.key_secret)


@dataclass
class TradingConfig:
    """Configuración de trading."""

    runtime_mode: RuntimeMode = RuntimeMode.OBSERVE_ONLY
    # Legacy booleans mantenidos para compatibilidad backward con tests y loaders.
    # El dispatch real usa runtime_mode. Estos se derivan del modo al cargar YAML.
    dry_run: bool = True
    observe_only: bool = True
    max_position_pct: float = 0.20
    max_notional_per_symbol: float = 10000.0
    max_orders_per_minute: int = 10
    smoke_test_mode: bool = False
    max_cycles: int = 0
    risk_per_trade_pct: float = 0.01  # fracción del equity por trade


@dataclass
class RiskConfig:
    """Configuración de riesgo."""

    max_daily_loss: float = 0.05
    max_drawdown: float = 0.15
    max_consecutive_losses: int = 3
    max_position_pct: float = 0.20


@dataclass
class MonitoringConfig:
    """Configuración de monitoreo."""

    log_level: str = "INFO"
    latency_p95_threshold_ms: float = 500.0
    reject_rate_threshold: float = 0.03
    slippage_drift_threshold_bps: float = 10.0


@dataclass
class SymbolConfig:
    """Configuración de un símbolo."""

    symbol: str
    enabled: bool = True
    timeframe: str = "1h"
    strategies: List[str] = field(default_factory=lambda: ["ma_crossover", "breakout"])


@dataclass
class PathsConfig:
    """Configuración de rutas."""

    repo: Path = field(default_factory=get_repo_path)
    runtime: Path = field(default_factory=get_runtime_path)
    secrets: Path = field(default_factory=get_secrets_path)

    @property
    def data_raw(self) -> Path:
        return self.runtime / "data" / "raw"

    @property
    def data_processed(self) -> Path:
        return self.runtime / "data" / "processed"

    @property
    def runs(self) -> Path:
        return self.runtime / "runs"

    @property
    def reports(self) -> Path:
        return self.runtime / "reports"

    @property
    def logs(self) -> Path:
        return self.runtime / "logs"

    @property
    def cache(self) -> Path:
        return self.runtime / "cache"

    @property
    def state(self) -> Path:
        """Directorio para estado persistente (ledgers, idempotencia)."""
        return self.runtime / "state"

    def ensure_directories(self) -> None:
        """Crear directorios si no existen."""
        for path in [
            self.data_raw,
            self.data_processed,
            self.runs,
            self.reports,
            self.logs,
            self.cache,
            self.state,
        ]:
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class Config:
    """Configuración global."""

    paths: PathsConfig = field(default_factory=PathsConfig)
    coinbase: CoinbaseConfig = field(default_factory=CoinbaseConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    symbols: List[SymbolConfig] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Inicialización posterior."""
        self.paths.ensure_directories()
        self._load_yaml_config()

    def _load_yaml_config(self) -> None:
        """Cargar configuración completa desde YAML."""
        symbols_file = self.paths.repo / "configs" / "symbols.yaml"

        if not symbols_file.exists():
            # Configuración por defecto
            self.symbols = [
                SymbolConfig(symbol="BTC-USD", enabled=True, timeframe="1h"),
                SymbolConfig(symbol="ETH-USD", enabled=True, timeframe="1h"),
            ]
            return

        try:
            with open(symbols_file, "r") as f:
                data = yaml.safe_load(f) or {}

            # P0 FIX: Cargar trading config
            trading_cfg = data.get("trading", {})
            raw_mode = trading_cfg.get("runtime_mode", None)
            if raw_mode is not None:
                try:
                    mode = RuntimeMode(raw_mode)
                except ValueError:
                    raise ValueError(
                        f"runtime_mode inválido en YAML: '{raw_mode}'. "
                        f"Valores permitidos: {[m.value for m in RuntimeMode]}"
                    )
            else:
                # Derivar modo de los booleans legacy para compatibilidad
                observe_only = trading_cfg.get("observe_only", True)
                dry_run = trading_cfg.get("dry_run", True)
                if observe_only:
                    mode = RuntimeMode.OBSERVE_ONLY
                elif dry_run:
                    mode = RuntimeMode.PAPER
                else:
                    mode = RuntimeMode.LIVE_CERT

            self.trading = TradingConfig(
                runtime_mode=mode,
                dry_run=mode in (RuntimeMode.PAPER, RuntimeMode.SHADOW),
                observe_only=mode == RuntimeMode.OBSERVE_ONLY,
                max_position_pct=trading_cfg.get("max_position_pct", 0.20),
                max_notional_per_symbol=trading_cfg.get("max_notional_per_symbol", 10000.0),
                max_orders_per_minute=trading_cfg.get("max_orders_per_minute", 10),
                smoke_test_mode=trading_cfg.get("smoke_test_mode", False),
                max_cycles=trading_cfg.get("max_cycles", 0),
                risk_per_trade_pct=trading_cfg.get("risk_per_trade_pct", 0.01),
            )

            # P0 FIX: Cargar risk config
            risk_cfg = data.get("risk", {})
            self.risk = RiskConfig(
                max_daily_loss=risk_cfg.get("max_daily_loss", 0.05),
                max_drawdown=risk_cfg.get("max_drawdown", 0.15),
                max_consecutive_losses=risk_cfg.get("max_consecutive_losses", 3),
                max_position_pct=risk_cfg.get(
                    "max_position_pct", trading_cfg.get("max_position_pct", 0.20)
                ),
            )

            # P0 FIX: Cargar monitoring config
            monitoring_cfg = data.get("monitoring", {})
            self.monitoring = MonitoringConfig(
                log_level=monitoring_cfg.get("log_level", "INFO"),
                latency_p95_threshold_ms=monitoring_cfg.get("latency_p95_threshold_ms", 500.0),
                reject_rate_threshold=monitoring_cfg.get("reject_rate_threshold", 0.03),
                slippage_drift_threshold_bps=monitoring_cfg.get(
                    "slippage_drift_threshold_bps", 10.0
                ),
            )

            # Cargar symbols
            self.symbols = []
            for item in data.get("symbols", []):
                self.symbols.append(
                    SymbolConfig(
                        symbol=item["symbol"],
                        enabled=item.get("enabled", True),
                        timeframe=item.get("timeframe", "1h"),
                        strategies=item.get("strategies", ["ma_crossover", "breakout"]),
                    )
                )
        except ValueError:
            # ValueError = configuración inválida explícita (ej: runtime_mode desconocido).
            # Fail-closed: no silenciar — propagar para que el runtime se niegue a arrancar.
            raise
        except Exception as e:
            print(f"Warning: Could not load symbols config: {e}")
            self.symbols = [
                SymbolConfig(symbol="BTC-USD", enabled=True, timeframe="1h"),
            ]


def validate_config(cfg: "Config") -> None:
    """
    Validar invariantes cross-sección de configuración.

    Lanza ValueError en la primera violación encontrada.
    El runtime debe llamar esto antes de arrancar.
    """
    t = cfg.trading
    r = cfg.risk

    # --- RuntimeMode: live_prod requiere unlock explícito ---
    if t.runtime_mode == RuntimeMode.LIVE_PROD:
        unlock = os.getenv("FORTRESS_LIVE_PROD_UNLOCK", "")
        if unlock != "1":
            raise ValueError(
                "runtime_mode=live_prod requiere FORTRESS_LIVE_PROD_UNLOCK=1. "
                "Esta variable debe setearse de forma explícita y consciente. "
                "El runtime no arranca en producción por accidente."
            )

    # --- Rango de posición ---
    if not (0 < t.max_position_pct <= 1.0):
        raise ValueError(f"trading.max_position_pct={t.max_position_pct} fuera de rango (0, 1]")
    if not (0 < r.max_position_pct <= 1.0):
        raise ValueError(f"risk.max_position_pct={r.max_position_pct} fuera de rango (0, 1]")

    # --- Riesgo: daily_loss ≤ drawdown ---
    if r.max_daily_loss > r.max_drawdown:
        raise ValueError(
            f"risk.max_daily_loss={r.max_daily_loss} > max_drawdown={r.max_drawdown}: "
            "una pérdida diaria no puede exceder el drawdown máximo permitido."
        )

    # --- Notional positivo ---
    if t.max_notional_per_symbol <= 0:
        raise ValueError(
            f"trading.max_notional_per_symbol={t.max_notional_per_symbol} debe ser > 0"
        )

    # --- Orders per minute positivo ---
    if t.max_orders_per_minute <= 0:
        raise ValueError(f"trading.max_orders_per_minute={t.max_orders_per_minute} debe ser > 0")

    # --- risk_per_trade_pct en rango razonable ---
    if not (0 < t.risk_per_trade_pct <= 0.10):
        raise ValueError(
            f"trading.risk_per_trade_pct={t.risk_per_trade_pct} fuera de rango (0, 0.10]. "
            "Riesgo por trade > 10% es inusualmente alto — confirmar configuración."
        )


# Instancia global
_config: Optional[Config] = None


def get_config() -> Config:
    """Obtener instancia global de configuración."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def reset_config() -> None:
    """Resetear configuración (útil para tests)."""
    global _config
    _config = None
