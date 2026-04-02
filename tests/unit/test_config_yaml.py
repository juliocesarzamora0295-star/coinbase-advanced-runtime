"""
Tests para config YAML efectivo (P0 FIX).

Valida que cambiar trading, risk, monitoring en YAML cambie el comportamiento real.
"""

import sys

import pytest

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.config import MonitoringConfig, RiskConfig, SymbolConfig, TradingConfig, validate_config


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


class TestConfigStartupValidation:
    """validate_config es fail-closed: lanza ValueError ante misconfiguration silenciosa."""

    _VALID_SYMBOLS = [SymbolConfig(symbol="BTC-USD", enabled=True)]

    def _valid_trading(self, **overrides) -> TradingConfig:
        base = dict(risk_per_trade_pct=0.01, max_notional_per_symbol=1000.0)
        base.update(overrides)
        return TradingConfig(**base)

    def _valid_risk(self, **overrides) -> RiskConfig:
        base = dict(max_daily_loss=0.05, max_drawdown=0.15)
        base.update(overrides)
        return RiskConfig(**base)

    # --- risk_per_trade_pct ---

    def test_zero_risk_per_trade_pct_raises(self):
        with pytest.raises(ValueError, match="risk_per_trade_pct"):
            validate_config(
                self._valid_trading(risk_per_trade_pct=0.0),
                self._valid_risk(),
                self._VALID_SYMBOLS,
            )

    def test_negative_risk_per_trade_pct_raises(self):
        with pytest.raises(ValueError, match="risk_per_trade_pct"):
            validate_config(
                self._valid_trading(risk_per_trade_pct=-0.01),
                self._valid_risk(),
                self._VALID_SYMBOLS,
            )

    # --- max_notional_per_symbol ---

    def test_zero_max_notional_raises(self):
        with pytest.raises(ValueError, match="max_notional_per_symbol"):
            validate_config(
                self._valid_trading(max_notional_per_symbol=0.0),
                self._valid_risk(),
                self._VALID_SYMBOLS,
            )

    def test_negative_max_notional_raises(self):
        with pytest.raises(ValueError, match="max_notional_per_symbol"):
            validate_config(
                self._valid_trading(max_notional_per_symbol=-100.0),
                self._valid_risk(),
                self._VALID_SYMBOLS,
            )

    # --- max_daily_loss ---

    def test_zero_max_daily_loss_raises(self):
        with pytest.raises(ValueError, match="max_daily_loss"):
            validate_config(
                self._valid_trading(),
                self._valid_risk(max_daily_loss=0.0),
                self._VALID_SYMBOLS,
            )

    def test_max_daily_loss_over_one_raises(self):
        with pytest.raises(ValueError, match="max_daily_loss"):
            validate_config(
                self._valid_trading(),
                self._valid_risk(max_daily_loss=1.1),
                self._VALID_SYMBOLS,
            )

    # --- max_drawdown ---

    def test_zero_max_drawdown_raises(self):
        with pytest.raises(ValueError, match="max_drawdown"):
            validate_config(
                self._valid_trading(),
                self._valid_risk(max_drawdown=0.0),
                self._VALID_SYMBOLS,
            )

    def test_max_drawdown_over_one_raises(self):
        with pytest.raises(ValueError, match="max_drawdown"):
            validate_config(
                self._valid_trading(),
                self._valid_risk(max_drawdown=2.0),
                self._VALID_SYMBOLS,
            )

    # --- symbols enabled ---

    def test_no_enabled_symbols_raises(self):
        disabled_only = [SymbolConfig(symbol="BTC-USD", enabled=False)]
        with pytest.raises(ValueError, match="ningún símbolo"):
            validate_config(self._valid_trading(), self._valid_risk(), disabled_only)

    def test_empty_symbols_list_passes(self):
        # Lista vacía no levanta — es estado válido (sin símbolos aún configurados)
        validate_config(self._valid_trading(), self._valid_risk(), [])

    # --- válido ---

    def test_valid_config_passes(self):
        validate_config(self._valid_trading(), self._valid_risk(), self._VALID_SYMBOLS)

    def test_boundary_values_pass(self):
        # max_daily_loss=1.0 y max_drawdown=1.0 son válidos (límite superior inclusivo)
        validate_config(
            self._valid_trading(risk_per_trade_pct=0.001, max_notional_per_symbol=0.01),
            self._valid_risk(max_daily_loss=1.0, max_drawdown=1.0),
            self._VALID_SYMBOLS,
        )

    def test_multiple_errors_reported_together(self):
        """Un ValueError debe listar todos los problemas, no solo el primero."""
        with pytest.raises(ValueError) as exc_info:
            validate_config(
                self._valid_trading(risk_per_trade_pct=0.0, max_notional_per_symbol=0.0),
                self._valid_risk(),
                self._VALID_SYMBOLS,
            )
        msg = str(exc_info.value)
        assert "risk_per_trade_pct" in msg
        assert "max_notional_per_symbol" in msg


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
