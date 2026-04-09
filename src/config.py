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
from pathlib import Path
from typing import Any, List, Optional

import yaml
from dotenv import load_dotenv


class ConfigValidationError(ValueError):
    """
    Lanzada en startup cuando la configuración tiene valores que harían
    que el bot arrancara silenciosamente sin operar o con riesgo mal definido.

    Hereda de ValueError para compatibilidad con manejadores genéricos.
    """

    pass


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

    dry_run: bool = True
    observe_only: bool = True
    max_position_pct: float = 0.20
    max_notional_per_symbol: float = 10000.0
    max_orders_per_minute: int = 10
    smoke_test_mode: bool = False
    max_cycles: int = 0
    notional_pct: float = 0.01  # fracción del equity como notional por trade
    initial_cash: float = 10000.0  # capital inicial en quote currency
    sizing_mode: str = "NOTIONAL"  # NOTIONAL or RISK_BASED


@dataclass
class RiskConfig:
    """Configuración de riesgo."""

    max_daily_loss: float = 0.05
    max_drawdown: float = 0.15
    max_consecutive_losses: int = 3
    max_position_pct: float = 0.20
    max_total_exposure_pct: float = 0.80  # 80% max across all symbols


_VALID_SIZING_MODES = {"NOTIONAL", "RISK_BASED"}


def _validate_sizing_mode(value: str) -> str:
    """Validate sizing_mode from config. Fail-closed on invalid."""
    upper = value.upper().strip()
    if upper in _VALID_SIZING_MODES:
        return upper
    import logging
    logging.getLogger("Config").warning(
        "Invalid sizing_mode '%s'. Valid: %s. Falling back to NOTIONAL.",
        value, _VALID_SIZING_MODES,
    )
    return "NOTIONAL"


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
    strategies: List[str] = field(default_factory=lambda: ["ma_crossover"])


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


def _extract_strategy_names(strategies: List[Any]) -> List[str]:
    """Extraer nombres de strategies desde lista mixta de str o dict."""
    names: List[str] = []
    for entry in strategies:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name", "")
            if name:
                names.append(name)
    return names


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
        self.validate_config()

    def validate_config(self) -> None:
        """
        Validar coherencia de configuración. Fail-closed en startup.

        Lanza ConfigValidationError listando todos los errores encontrados
        si algún valor crítico haría que el bot arrancara sin operar o con
        parámetros de riesgo fuera de rango.

        Invariantes:
        - risk_per_trade_pct > 0       (0 → PositionSizer retorna qty=0 siempre)
        - max_notional_per_symbol > 0  (0 → toda orden bloqueada por cap)
        - max_position_pct en (0, 1]   (fuera de rango → cap de posición inoperable)
        - max_daily_loss en (0, 1]     (fuera de rango → RiskGate inoperable)
        - max_drawdown en (0, 1]       (fuera de rango → RiskGate inoperable)
        - símbolos enabled tienen strategies no vacías
        - símbolos enabled tienen al menos una strategy implementada en el registry
        """
        errors: List[str] = []

        if self.trading.risk_per_trade_pct <= 0:
            errors.append(
                f"trading.risk_per_trade_pct={self.trading.risk_per_trade_pct} "
                "debe ser > 0 (con 0 el PositionSizer retorna qty=0 para toda señal)"
            )

        if self.trading.max_notional_per_symbol <= 0:
            errors.append(
                f"trading.max_notional_per_symbol={self.trading.max_notional_per_symbol} "
                "debe ser > 0 (con 0 toda orden queda bloqueada por cap de notional)"
            )

        if not (0 < self.trading.max_position_pct <= 1.0):
            errors.append(
                f"trading.max_position_pct={self.trading.max_position_pct} " "debe estar en (0, 1]"
            )

        if not (0 < self.risk.max_daily_loss <= 1.0):
            errors.append(f"risk.max_daily_loss={self.risk.max_daily_loss} " "debe estar en (0, 1]")

        if not (0 < self.risk.max_drawdown <= 1.0):
            errors.append(f"risk.max_drawdown={self.risk.max_drawdown} " "debe estar en (0, 1]")

        for s in self.symbols:
            if not s.enabled:
                continue
            if not s.strategies:
                errors.append(
                    f"símbolo '{s.symbol}' está enabled pero no tiene strategies definidas"
                )
                continue
            # Importación lazy para evitar dependencia circular en nivel de módulo
            from src.strategy.manager import _STRATEGY_REGISTRY  # noqa: PLC0415

            names = _extract_strategy_names(s.strategies)
            implementable = [n for n in names if _STRATEGY_REGISTRY.get(n) is not None]
            if names and not implementable:
                errors.append(
                    f"símbolo '{s.symbol}': ninguna strategy implementada en registry "
                    f"(configuradas: {names!r}, implementadas: "
                    f"{[k for k, v in _STRATEGY_REGISTRY.items() if v is not None]!r})"
                )

        if errors:
            raise ConfigValidationError(
                "Configuración inválida — corrige antes de arrancar:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

    def _load_yaml_config(self) -> None:
        """Cargar configuración completa desde YAML.

        Path resolution order:
        1. FORTRESS_CONFIG env var (explicit path to YAML)
        2. Default: {repo}/configs/symbols.yaml
        """
        env_config = os.getenv("FORTRESS_CONFIG")
        if env_config:
            symbols_file = Path(env_config)
        else:
            symbols_file = self.paths.repo / "configs" / "symbols.yaml"

        if not symbols_file.exists():
            import logging
            logging.getLogger("Config").error(
                "Config file not found: %s — no symbols loaded (fail-closed)", symbols_file
            )
            self.symbols = []
            return

        try:
            with open(symbols_file, "r") as f:
                data = yaml.safe_load(f) or {}

            # P0 FIX: Cargar trading config
            trading_cfg = data.get("trading", {})
            self.trading = TradingConfig(
                dry_run=trading_cfg.get("dry_run", True),
                observe_only=trading_cfg.get("observe_only", True),
                max_position_pct=trading_cfg.get("max_position_pct", 0.20),
                max_notional_per_symbol=trading_cfg.get("max_notional_per_symbol", 10000.0),
                max_orders_per_minute=trading_cfg.get("max_orders_per_minute", 10),
                smoke_test_mode=trading_cfg.get("smoke_test_mode", False),
                max_cycles=trading_cfg.get("max_cycles", 0),
                notional_pct=trading_cfg.get(
                    "notional_pct",
                    trading_cfg.get("risk_per_trade_pct", 0.01),  # backward compat from YAML
                ),
                initial_cash=trading_cfg.get("initial_cash", 10000.0),
                sizing_mode=_validate_sizing_mode(trading_cfg.get("sizing_mode", "NOTIONAL")),
            )

            # P0 FIX: Cargar risk config
            risk_cfg = data.get("risk", {})
            self.risk = RiskConfig(
                max_daily_loss=risk_cfg.get("max_daily_loss", 0.05),
                max_drawdown=risk_cfg.get("max_drawdown", 0.15),
                max_consecutive_losses=risk_cfg.get("max_consecutive_losses", 3),
                max_position_pct=trading_cfg.get("max_position_pct", 0.20),
                max_total_exposure_pct=risk_cfg.get("max_total_exposure_pct", 0.80),
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
                        strategies=item.get("strategies", ["ma_crossover"]),
                    )
                )
        except Exception as e:
            import logging
            logging.getLogger("Config").error(
                "Failed to load config %s: %s — no symbols loaded (fail-closed)",
                symbols_file, e,
            )
            self.symbols = []


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
