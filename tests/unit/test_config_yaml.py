"""
Tests para config YAML efectivo (P0 FIX).

Valida que cambiar trading, risk, monitoring en YAML cambie el comportamiento real.
"""

import sys

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.config import MonitoringConfig, RiskConfig, TradingConfig


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


def _make_config_from_yaml(tmp_path, yaml_data: dict):
    """Helper: escribe un YAML en tmp_path/configs/symbols.yaml y carga Config."""
    import os

    import yaml

    from src.config import Config, reset_config

    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    (configs_dir / "symbols.yaml").write_text(yaml.dump(yaml_data))

    reset_config()
    old_repo = os.environ.get("FORTRESS_REPO")
    os.environ["FORTRESS_REPO"] = str(tmp_path)
    try:
        cfg = Config()
    finally:
        if old_repo is None:
            os.environ.pop("FORTRESS_REPO", None)
        else:
            os.environ["FORTRESS_REPO"] = old_repo
        reset_config()
    return cfg


class TestRiskCfgMaxPositionPct:
    """
    Verifica que RiskConfig.max_position_pct lee de risk_cfg, no de trading_cfg.

    Bug corregido: config.py usaba trading_cfg.get() para un campo de RiskConfig,
    ignorando el valor definido en la sección 'risk:' del YAML.
    """

    def test_risk_max_position_pct_reads_from_risk_section(self, tmp_path):
        """risk.max_position_pct se toma de sección 'risk:', no de 'trading:'."""
        cfg = _make_config_from_yaml(
            tmp_path,
            {
                "trading": {"max_position_pct": 0.30, "dry_run": True, "observe_only": True},
                "risk": {"max_position_pct": 0.10, "max_daily_loss": 0.05, "max_drawdown": 0.15},
            },
        )
        # Debe leer 0.10 de risk, no 0.30 de trading
        assert cfg.risk.max_position_pct == 0.10
        assert cfg.trading.max_position_pct == 0.30

    def test_risk_max_position_pct_default_when_absent(self, tmp_path):
        """risk.max_position_pct usa default 0.20 cuando no está en YAML."""
        cfg = _make_config_from_yaml(
            tmp_path,
            {
                "trading": {"max_position_pct": 0.25, "dry_run": True, "observe_only": True},
                "risk": {"max_daily_loss": 0.05, "max_drawdown": 0.15},
            },
        )
        # No definido en risk: → default 0.20, no 0.25 de trading
        assert cfg.risk.max_position_pct == 0.20


class TestValidateConfig:
    """Tests para validate_config() — invariantes cross-sección."""

    def _cfg(self, tmp_path, overrides: dict = None):
        base = {
            "trading": {
                "dry_run": True,
                "observe_only": True,
                "max_position_pct": 0.20,
                "risk_per_trade_pct": 0.01,
            },
            "risk": {
                "max_position_pct": 0.20,
                "max_daily_loss": 0.05,
                "max_drawdown": 0.15,
            },
        }
        if overrides:
            for section, vals in overrides.items():
                base.setdefault(section, {}).update(vals)
        return _make_config_from_yaml(tmp_path, base)

    def test_valid_config_passes(self, tmp_path):
        """Config válida no lanza excepción."""
        from src.config import validate_config

        validate_config(self._cfg(tmp_path))

    def test_risk_max_position_pct_zero_raises(self, tmp_path):
        """risk.max_position_pct=0 → ValueError."""
        import pytest

        from src.config import validate_config

        with pytest.raises(ValueError, match="risk.max_position_pct"):
            validate_config(self._cfg(tmp_path, {"risk": {"max_position_pct": 0.0}}))

    def test_trading_max_position_pct_above_one_raises(self, tmp_path):
        """trading.max_position_pct > 1 → ValueError."""
        import pytest

        from src.config import validate_config

        with pytest.raises(ValueError, match="trading.max_position_pct"):
            validate_config(self._cfg(tmp_path, {"trading": {"max_position_pct": 1.5}}))

    def test_daily_loss_exceeds_drawdown_raises(self, tmp_path):
        """risk.max_daily_loss > risk.max_drawdown → ValueError."""
        import pytest

        from src.config import validate_config

        with pytest.raises(ValueError, match="max_daily_loss"):
            validate_config(
                self._cfg(tmp_path, {"risk": {"max_daily_loss": 0.20, "max_drawdown": 0.10}})
            )

    def test_risk_per_trade_pct_zero_raises(self, tmp_path):
        """trading.risk_per_trade_pct=0 → ValueError."""
        import pytest

        from src.config import validate_config

        with pytest.raises(ValueError, match="risk_per_trade_pct"):
            validate_config(self._cfg(tmp_path, {"trading": {"risk_per_trade_pct": 0.0}}))


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
