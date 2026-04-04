"""
Tests para config YAML efectivo (P0 FIX).

Valida que cambiar trading, risk, monitoring en YAML cambie el comportamiento real.
"""

import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.config import Config, MonitoringConfig, PathsConfig, RiskConfig, TradingConfig


class TestConfigYAMLEffective:
    """Tests para config YAML efectivo."""

    def test_trading_config_defaults(self):
        """
        P0 FIX: TradingConfig debe tener valores por defecto razonables.
        """
        cfg = TradingConfig()

        # Verificar valores por defecto
        assert cfg.dry_run is True, f"Expected dry_run=True by default, got {cfg.dry_run}"
        assert (
            cfg.observe_only is True
        ), f"Expected observe_only=True by default, got {cfg.observe_only}"
        assert (
            cfg.max_position_pct == 0.20
        ), f"Expected max_position_pct=0.20 by default, got {cfg.max_position_pct}"

    def test_trading_config_overridable(self):
        """
        P0 FIX: TradingConfig debe permitir override de valores.
        """
        cfg = TradingConfig(dry_run=False, observe_only=False, max_position_pct=0.30)

        assert cfg.dry_run is False, f"Expected dry_run=False, got {cfg.dry_run}"
        assert cfg.observe_only is False, f"Expected observe_only=False, got {cfg.observe_only}"
        assert (
            cfg.max_position_pct == 0.30
        ), f"Expected max_position_pct=0.30, got {cfg.max_position_pct}"

    def test_risk_config_defaults(self):
        """
        P0 FIX: RiskConfig debe tener valores por defecto razonables.
        """
        cfg = RiskConfig()

        assert (
            cfg.max_daily_loss == 0.05
        ), f"Expected max_daily_loss=0.05 by default, got {cfg.max_daily_loss}"
        assert (
            cfg.max_drawdown == 0.15
        ), f"Expected max_drawdown=0.15 by default, got {cfg.max_drawdown}"
        assert (
            cfg.max_consecutive_losses == 3
        ), f"Expected max_consecutive_losses=3 by default, got {cfg.max_consecutive_losses}"

    def test_risk_config_overridable(self):
        """
        P0 FIX: RiskConfig debe permitir override de valores.
        """
        cfg = RiskConfig(
            max_daily_loss=0.10, max_drawdown=0.25, max_consecutive_losses=5, max_position_pct=0.30
        )

        assert cfg.max_daily_loss == 0.10, f"Expected max_daily_loss=0.10, got {cfg.max_daily_loss}"
        assert cfg.max_drawdown == 0.25, f"Expected max_drawdown=0.25, got {cfg.max_drawdown}"
        assert (
            cfg.max_consecutive_losses == 5
        ), f"Expected max_consecutive_losses=5, got {cfg.max_consecutive_losses}"
        assert (
            cfg.max_position_pct == 0.30
        ), f"Expected max_position_pct=0.30, got {cfg.max_position_pct}"

    def test_monitoring_config_defaults(self):
        """
        P0 FIX: MonitoringConfig debe tener valores por defecto razonables.
        """
        cfg = MonitoringConfig()

        assert cfg.log_level == "INFO", f"Expected log_level=INFO by default, got {cfg.log_level}"
        assert (
            cfg.latency_p95_threshold_ms == 500.0
        ), f"Expected latency_p95_threshold_ms=500.0 by default, got {cfg.latency_p95_threshold_ms}"
        assert (
            cfg.reject_rate_threshold == 0.03
        ), f"Expected reject_rate_threshold=0.03 by default, got {cfg.reject_rate_threshold}"
        assert (
            cfg.slippage_drift_threshold_bps == 10.0
        ), f"Expected slippage_drift_threshold_bps=10.0 by default, got {cfg.slippage_drift_threshold_bps}"

    def test_monitoring_config_overridable(self):
        """
        P0 FIX: MonitoringConfig debe permitir override de valores.
        """
        cfg = MonitoringConfig(
            log_level="DEBUG",
            latency_p95_threshold_ms=1000.0,
            reject_rate_threshold=0.05,
            slippage_drift_threshold_bps=20.0,
        )

        assert cfg.log_level == "DEBUG", f"Expected log_level=DEBUG, got {cfg.log_level}"
        assert (
            cfg.latency_p95_threshold_ms == 1000.0
        ), f"Expected latency_p95_threshold_ms=1000.0, got {cfg.latency_p95_threshold_ms}"
        assert (
            cfg.reject_rate_threshold == 0.05
        ), f"Expected reject_rate_threshold=0.05, got {cfg.reject_rate_threshold}"
        assert (
            cfg.slippage_drift_threshold_bps == 20.0
        ), f"Expected slippage_drift_threshold_bps=20.0, got {cfg.slippage_drift_threshold_bps}"


class TestRiskMaxPositionPctSource:
    """Verifica que risk.max_position_pct se lea de la sección 'risk', no de 'trading'."""

    def _make_config_with_yaml(self, yaml_data: dict) -> Config:
        """Crear Config apuntando a un YAML temporal."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            configs_dir = repo / "configs"
            configs_dir.mkdir()
            symbols_file = configs_dir / "symbols.yaml"
            symbols_file.write_text(yaml.dump(yaml_data))

            # Patch paths para que apunten al tmpdir
            paths = PathsConfig(repo=repo, runtime=repo / "runtime", secrets=repo / "secrets")
            paths.ensure_directories()

            cfg = Config.__new__(Config)
            cfg.paths = paths
            cfg.coinbase = Config.__dataclass_fields__["coinbase"].default_factory()
            cfg.trading = TradingConfig()
            cfg.risk = RiskConfig()
            cfg.monitoring = MonitoringConfig()
            cfg.symbols = []
            cfg._load_yaml_config()
            return cfg

    def test_risk_max_position_pct_from_risk_section(self):
        """risk.max_position_pct debe leerse de 'risk', no de 'trading'."""
        data = {
            "trading": {"max_position_pct": 0.20},
            "risk": {"max_position_pct": 0.10},
        }
        cfg = self._make_config_with_yaml(data)
        assert (
            cfg.risk.max_position_pct == 0.10
        ), f"Expected risk.max_position_pct=0.10 (from risk section), got {cfg.risk.max_position_pct}"
        # trading.max_position_pct debe seguir siendo su propio valor
        assert cfg.trading.max_position_pct == 0.20

    def test_risk_max_position_pct_independent_of_trading(self):
        """Cuando trading.max_position_pct difiere, risk.max_position_pct no se contamina."""
        data = {
            "trading": {"max_position_pct": 0.50},
            "risk": {"max_position_pct": 0.15},
        }
        cfg = self._make_config_with_yaml(data)
        assert cfg.risk.max_position_pct == 0.15
        assert cfg.trading.max_position_pct == 0.50

    def test_risk_max_position_pct_default_when_absent(self):
        """Si risk.max_position_pct no está en YAML, usa el default de RiskConfig (0.20)."""
        data = {
            "trading": {"max_position_pct": 0.30},
            "risk": {"max_daily_loss": 0.05},
        }
        cfg = self._make_config_with_yaml(data)
        assert cfg.risk.max_position_pct == 0.20  # default de RiskConfig


class TestValidateConfig:
    """Tests para validate_config (validación cruzada)."""

    def test_valid_config_passes(self):
        """Config válida no lanza excepción."""
        cfg = Config.__new__(Config)
        cfg.paths = PathsConfig.__new__(PathsConfig)
        cfg.trading = TradingConfig(max_position_pct=0.20)
        cfg.risk = RiskConfig(max_daily_loss=0.05, max_drawdown=0.15, max_position_pct=0.20)
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        cfg.validate_config()  # no debe lanzar

    def test_risk_position_pct_zero_raises(self):
        """risk.max_position_pct=0 debe fallar."""
        cfg = Config.__new__(Config)
        cfg.paths = PathsConfig.__new__(PathsConfig)
        cfg.trading = TradingConfig(max_position_pct=0.20)
        cfg.risk = RiskConfig(max_position_pct=0.0)
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        try:
            cfg.validate_config()
            assert False, "Debió lanzar ValueError"
        except ValueError as e:
            assert "risk.max_position_pct" in str(e)

    def test_trading_position_pct_exceeds_one_raises(self):
        """trading.max_position_pct > 1.0 debe fallar."""
        cfg = Config.__new__(Config)
        cfg.paths = PathsConfig.__new__(PathsConfig)
        cfg.trading = TradingConfig(max_position_pct=1.5)
        cfg.risk = RiskConfig(max_position_pct=0.20)
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        try:
            cfg.validate_config()
            assert False, "Debió lanzar ValueError"
        except ValueError as e:
            assert "trading.max_position_pct" in str(e)

    def test_daily_loss_exceeds_drawdown_raises(self):
        """risk.max_daily_loss > risk.max_drawdown debe fallar."""
        cfg = Config.__new__(Config)
        cfg.paths = PathsConfig.__new__(PathsConfig)
        cfg.trading = TradingConfig(max_position_pct=0.20)
        cfg.risk = RiskConfig(max_daily_loss=0.20, max_drawdown=0.10, max_position_pct=0.20)
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        try:
            cfg.validate_config()
            assert False, "Debió lanzar ValueError"
        except ValueError as e:
            assert "max_daily_loss" in str(e)
            assert "max_drawdown" in str(e)


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
