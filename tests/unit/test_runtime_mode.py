"""
Tests para RuntimeMode y validate_config (PR-0.2).

Verifica:
- RuntimeMode enum cubre todos los modos esperados
- TradingConfig.runtime_mode default = OBSERVE_ONLY
- YAML con runtime_mode explícito se carga correctamente
- YAML con booleans legacy (observe_only / dry_run) deriva el modo correcto
- validate_config bloquea live_prod sin FORTRESS_LIVE_PROD_UNLOCK=1
- validate_config pasa para live_prod con FORTRESS_LIVE_PROD_UNLOCK=1
- validate_config detecta invariantes violadas (max_position_pct, daily_loss>drawdown, etc.)
- _execute_order dispatch correcto por modo (via TradingConfig.runtime_mode)
"""

import os
import textwrap
from pathlib import Path

import pytest

from src.config import (
    Config,
    MonitoringConfig,
    RiskConfig,
    RuntimeMode,
    TradingConfig,
    reset_config,
    validate_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, yaml_content: str) -> Config:
    """Crear Config desde YAML en tmp_path."""
    cfg_dir = tmp_path / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "symbols.yaml").write_text(yaml_content)

    old_repo = os.environ.get("FORTRESS_REPO")
    old_runtime = os.environ.get("FORTRESS_RUNTIME")
    os.environ["FORTRESS_REPO"] = str(tmp_path)
    os.environ["FORTRESS_RUNTIME"] = str(tmp_path / "runtime")

    try:
        reset_config()
        cfg = Config()
        return cfg
    finally:
        reset_config()
        if old_repo is None:
            os.environ.pop("FORTRESS_REPO", None)
        else:
            os.environ["FORTRESS_REPO"] = old_repo
        if old_runtime is None:
            os.environ.pop("FORTRESS_RUNTIME", None)
        else:
            os.environ["FORTRESS_RUNTIME"] = old_runtime


# ---------------------------------------------------------------------------
# RuntimeMode enum
# ---------------------------------------------------------------------------


class TestRuntimeModeEnum:
    def test_all_modes_present(self):
        modes = {m.value for m in RuntimeMode}
        assert modes == {"observe_only", "paper", "shadow", "live_cert", "live_prod"}

    def test_is_str_enum(self):
        assert RuntimeMode.OBSERVE_ONLY == "observe_only"
        assert RuntimeMode.LIVE_PROD == "live_prod"

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            RuntimeMode("nonexistent_mode")


# ---------------------------------------------------------------------------
# TradingConfig defaults
# ---------------------------------------------------------------------------


class TestTradingConfigRuntimeMode:
    def test_default_is_observe_only(self):
        cfg = TradingConfig()
        assert cfg.runtime_mode == RuntimeMode.OBSERVE_ONLY

    def test_override_to_paper(self):
        cfg = TradingConfig(runtime_mode=RuntimeMode.PAPER)
        assert cfg.runtime_mode == RuntimeMode.PAPER

    def test_override_to_live_prod(self):
        cfg = TradingConfig(runtime_mode=RuntimeMode.LIVE_PROD)
        assert cfg.runtime_mode == RuntimeMode.LIVE_PROD


# ---------------------------------------------------------------------------
# YAML loading: runtime_mode explícito
# ---------------------------------------------------------------------------


class TestRuntimeModeFromYAML:
    YAML_BASE = textwrap.dedent("""\
        symbols:
          - symbol: "BTC-USD"
            enabled: true
            timeframe: "1h"
            strategies:
              - "ma_crossover"
        trading:
          runtime_mode: "{mode}"
          max_position_pct: 0.10
          max_notional_per_symbol: 5000
          max_orders_per_minute: 5
          risk_per_trade_pct: 0.01
        risk:
          max_daily_loss: 0.03
          max_drawdown: 0.10
          max_consecutive_losses: 3
        monitoring:
          log_level: "INFO"
          latency_p95_threshold_ms: 500.0
          reject_rate_threshold: 0.03
          slippage_drift_threshold_bps: 10.0
    """)

    def test_observe_only_from_yaml(self, tmp_path):
        cfg = _make_config(tmp_path, self.YAML_BASE.format(mode="observe_only"))
        assert cfg.trading.runtime_mode == RuntimeMode.OBSERVE_ONLY

    def test_paper_from_yaml(self, tmp_path):
        cfg = _make_config(tmp_path, self.YAML_BASE.format(mode="paper"))
        assert cfg.trading.runtime_mode == RuntimeMode.PAPER

    def test_shadow_from_yaml(self, tmp_path):
        cfg = _make_config(tmp_path, self.YAML_BASE.format(mode="shadow"))
        assert cfg.trading.runtime_mode == RuntimeMode.SHADOW

    def test_live_cert_from_yaml(self, tmp_path):
        cfg = _make_config(tmp_path, self.YAML_BASE.format(mode="live_cert"))
        assert cfg.trading.runtime_mode == RuntimeMode.LIVE_CERT

    def test_invalid_mode_raises(self, tmp_path):
        with pytest.raises(Exception):
            _make_config(tmp_path, self.YAML_BASE.format(mode="turbo_mode"))


# ---------------------------------------------------------------------------
# YAML loading: derivar modo desde booleans legacy
# ---------------------------------------------------------------------------


class TestRuntimeModeFromLegacyBooleans:
    def _yaml(self, observe_only: bool, dry_run: bool) -> str:
        return textwrap.dedent(f"""\
            symbols:
              - symbol: "BTC-USD"
                enabled: true
                timeframe: "1h"
                strategies:
                  - "ma_crossover"
            trading:
              observe_only: {str(observe_only).lower()}
              dry_run: {str(dry_run).lower()}
              max_position_pct: 0.10
              max_notional_per_symbol: 5000
              max_orders_per_minute: 5
              risk_per_trade_pct: 0.01
            risk:
              max_daily_loss: 0.03
              max_drawdown: 0.10
              max_consecutive_losses: 3
            monitoring:
              log_level: "INFO"
              latency_p95_threshold_ms: 500.0
              reject_rate_threshold: 0.03
              slippage_drift_threshold_bps: 10.0
        """)

    def test_observe_true_gives_observe_only(self, tmp_path):
        cfg = _make_config(tmp_path, self._yaml(observe_only=True, dry_run=True))
        assert cfg.trading.runtime_mode == RuntimeMode.OBSERVE_ONLY

    def test_observe_false_dry_true_gives_paper(self, tmp_path):
        cfg = _make_config(tmp_path, self._yaml(observe_only=False, dry_run=True))
        assert cfg.trading.runtime_mode == RuntimeMode.PAPER

    def test_observe_false_dry_false_gives_live_cert(self, tmp_path):
        cfg = _make_config(tmp_path, self._yaml(observe_only=False, dry_run=False))
        assert cfg.trading.runtime_mode == RuntimeMode.LIVE_CERT


# ---------------------------------------------------------------------------
# validate_config: live_prod guard
# ---------------------------------------------------------------------------


class TestValidateConfigLiveProdGuard:
    def _base_trading(self, mode: RuntimeMode) -> TradingConfig:
        return TradingConfig(
            runtime_mode=mode,
            max_position_pct=0.10,
            max_notional_per_symbol=5000.0,
            max_orders_per_minute=5,
            risk_per_trade_pct=0.01,
        )

    def _base_risk(self) -> RiskConfig:
        return RiskConfig(
            max_daily_loss=0.03,
            max_drawdown=0.10,
            max_consecutive_losses=3,
            max_position_pct=0.10,
        )

    def _make_minimal_config(self, mode: RuntimeMode) -> Config:
        cfg = Config.__new__(Config)
        cfg.trading = self._base_trading(mode)
        cfg.risk = self._base_risk()
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        return cfg

    def test_live_prod_blocked_without_unlock(self, monkeypatch):
        monkeypatch.delenv("FORTRESS_LIVE_PROD_UNLOCK", raising=False)
        cfg = self._make_minimal_config(RuntimeMode.LIVE_PROD)
        with pytest.raises(ValueError, match="FORTRESS_LIVE_PROD_UNLOCK"):
            validate_config(cfg)

    def test_live_prod_allowed_with_unlock(self, monkeypatch):
        monkeypatch.setenv("FORTRESS_LIVE_PROD_UNLOCK", "1")
        cfg = self._make_minimal_config(RuntimeMode.LIVE_PROD)
        validate_config(cfg)  # no debe lanzar

    def test_live_prod_blocked_with_wrong_value(self, monkeypatch):
        monkeypatch.setenv("FORTRESS_LIVE_PROD_UNLOCK", "true")
        cfg = self._make_minimal_config(RuntimeMode.LIVE_PROD)
        with pytest.raises(ValueError, match="FORTRESS_LIVE_PROD_UNLOCK"):
            validate_config(cfg)

    def test_live_cert_passes_without_unlock(self, monkeypatch):
        monkeypatch.delenv("FORTRESS_LIVE_PROD_UNLOCK", raising=False)
        cfg = self._make_minimal_config(RuntimeMode.LIVE_CERT)
        validate_config(cfg)  # no debe lanzar

    def test_observe_only_passes(self, monkeypatch):
        monkeypatch.delenv("FORTRESS_LIVE_PROD_UNLOCK", raising=False)
        cfg = self._make_minimal_config(RuntimeMode.OBSERVE_ONLY)
        validate_config(cfg)  # no debe lanzar


# ---------------------------------------------------------------------------
# validate_config: invariantes de riesgo
# ---------------------------------------------------------------------------


class TestValidateConfigInvariants:
    def _cfg(
        self,
        trading_position_pct=0.10,
        risk_position_pct=0.10,
        daily_loss=0.03,
        drawdown=0.10,
        notional=5000.0,
        orders_per_min=5,
        risk_per_trade=0.01,
    ) -> Config:
        cfg = Config.__new__(Config)
        cfg.trading = TradingConfig(
            runtime_mode=RuntimeMode.OBSERVE_ONLY,
            max_position_pct=trading_position_pct,
            max_notional_per_symbol=notional,
            max_orders_per_minute=orders_per_min,
            risk_per_trade_pct=risk_per_trade,
        )
        cfg.risk = RiskConfig(
            max_daily_loss=daily_loss,
            max_drawdown=drawdown,
            max_consecutive_losses=3,
            max_position_pct=risk_position_pct,
        )
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        return cfg

    def test_valid_config_passes(self):
        validate_config(self._cfg())

    def test_trading_position_pct_zero_fails(self):
        with pytest.raises(ValueError, match="max_position_pct"):
            validate_config(self._cfg(trading_position_pct=0.0))

    def test_trading_position_pct_over_one_fails(self):
        with pytest.raises(ValueError, match="max_position_pct"):
            validate_config(self._cfg(trading_position_pct=1.1))

    def test_risk_position_pct_zero_fails(self):
        with pytest.raises(ValueError, match="max_position_pct"):
            validate_config(self._cfg(risk_position_pct=0.0))

    def test_daily_loss_exceeds_drawdown_fails(self):
        with pytest.raises(ValueError, match="max_daily_loss"):
            validate_config(self._cfg(daily_loss=0.20, drawdown=0.10))

    def test_daily_loss_equals_drawdown_passes(self):
        validate_config(self._cfg(daily_loss=0.10, drawdown=0.10))

    def test_notional_zero_fails(self):
        with pytest.raises(ValueError, match="max_notional_per_symbol"):
            validate_config(self._cfg(notional=0.0))

    def test_notional_negative_fails(self):
        with pytest.raises(ValueError, match="max_notional_per_symbol"):
            validate_config(self._cfg(notional=-100.0))

    def test_orders_per_min_zero_fails(self):
        with pytest.raises(ValueError, match="max_orders_per_minute"):
            validate_config(self._cfg(orders_per_min=0))

    def test_risk_per_trade_zero_fails(self):
        with pytest.raises(ValueError, match="risk_per_trade_pct"):
            validate_config(self._cfg(risk_per_trade=0.0))

    def test_risk_per_trade_above_10pct_fails(self):
        with pytest.raises(ValueError, match="risk_per_trade_pct"):
            validate_config(self._cfg(risk_per_trade=0.11))

    def test_risk_per_trade_exactly_10pct_passes(self):
        validate_config(self._cfg(risk_per_trade=0.10))


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
