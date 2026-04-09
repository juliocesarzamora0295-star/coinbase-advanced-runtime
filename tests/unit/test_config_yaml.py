"""
Tests para config YAML efectivo (P0 FIX) y validación de startup.
"""

import sys

import pytest

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

from src.config import (
    Config,
    ConfigValidationError,
    MonitoringConfig,
    RiskConfig,
    SymbolConfig,
    TradingConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    trading: TradingConfig | None = None,
    risk: RiskConfig | None = None,
    symbols: list | None = None,
) -> Config:
    """
    Construir Config sin tocar disco ni variables de entorno.

    Usa Config.__new__ para omitir __post_init__ y asigna directamente
    los sub-configs, luego llama validate_config() explícitamente.
    """
    c = Config.__new__(Config)
    c.trading = trading or TradingConfig(
        risk_per_trade_pct=0.01,
        max_notional_per_symbol=1000.0,
        max_position_pct=0.20,
    )
    c.risk = risk or RiskConfig(max_daily_loss=0.05, max_drawdown=0.15)
    c.symbols = (
        symbols
        if symbols is not None
        else [SymbolConfig(symbol="BTC-USD", enabled=True, strategies=["sma_crossover"])]
    )
    return c


# ---------------------------------------------------------------------------
# Tests previos: sub-configs individuales
# ---------------------------------------------------------------------------


class TestConfigYAMLEffective:
    """Tests para config YAML efectivo."""

    def test_trading_config_defaults(self):
        cfg = TradingConfig()
        assert cfg.dry_run is True
        assert cfg.observe_only is True
        assert cfg.max_position_pct == 0.20

    def test_trading_config_overridable(self):
        cfg = TradingConfig(dry_run=False, observe_only=False, max_position_pct=0.30)
        assert cfg.dry_run is False
        assert cfg.observe_only is False
        assert cfg.max_position_pct == 0.30

    def test_risk_config_defaults(self):
        cfg = RiskConfig()
        assert cfg.max_daily_loss == 0.05
        assert cfg.max_drawdown == 0.15
        assert cfg.max_consecutive_losses == 3

    def test_risk_config_overridable(self):
        cfg = RiskConfig(
            max_daily_loss=0.10, max_drawdown=0.25, max_consecutive_losses=5, max_position_pct=0.30
        )
        assert cfg.max_daily_loss == 0.10
        assert cfg.max_drawdown == 0.25
        assert cfg.max_consecutive_losses == 5
        assert cfg.max_position_pct == 0.30

    def test_monitoring_config_defaults(self):
        cfg = MonitoringConfig()
        assert cfg.log_level == "INFO"
        assert cfg.latency_p95_threshold_ms == 500.0
        assert cfg.reject_rate_threshold == 0.03
        assert cfg.slippage_drift_threshold_bps == 10.0

    def test_monitoring_config_overridable(self):
        cfg = MonitoringConfig(
            log_level="DEBUG",
            latency_p95_threshold_ms=1000.0,
            reject_rate_threshold=0.05,
            slippage_drift_threshold_bps=20.0,
        )
        assert cfg.log_level == "DEBUG"
        assert cfg.latency_p95_threshold_ms == 1000.0
        assert cfg.reject_rate_threshold == 0.05
        assert cfg.slippage_drift_threshold_bps == 20.0


# ---------------------------------------------------------------------------
# Tests: validate_config — fail-closed
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """Config.validate_config() es fail-closed: lanza ConfigValidationError."""

    # --- risk_per_trade_pct ---

    def test_zero_risk_per_trade_pct_raises(self):
        c = _make_config(trading=TradingConfig(risk_per_trade_pct=0.0))
        with pytest.raises(ConfigValidationError, match="risk_per_trade_pct"):
            c.validate_config()

    def test_negative_risk_per_trade_pct_raises(self):
        c = _make_config(trading=TradingConfig(risk_per_trade_pct=-0.01))
        with pytest.raises(ConfigValidationError, match="risk_per_trade_pct"):
            c.validate_config()

    # --- max_notional_per_symbol ---

    def test_zero_max_notional_raises(self):
        c = _make_config(trading=TradingConfig(max_notional_per_symbol=0.0))
        with pytest.raises(ConfigValidationError, match="max_notional_per_symbol"):
            c.validate_config()

    def test_negative_max_notional_raises(self):
        c = _make_config(trading=TradingConfig(max_notional_per_symbol=-100.0))
        with pytest.raises(ConfigValidationError, match="max_notional_per_symbol"):
            c.validate_config()

    # --- max_position_pct ---

    def test_zero_max_position_pct_raises(self):
        c = _make_config(trading=TradingConfig(max_position_pct=0.0))
        with pytest.raises(ConfigValidationError, match="max_position_pct"):
            c.validate_config()

    def test_negative_max_position_pct_raises(self):
        c = _make_config(trading=TradingConfig(max_position_pct=-0.1))
        with pytest.raises(ConfigValidationError, match="max_position_pct"):
            c.validate_config()

    def test_max_position_pct_over_one_raises(self):
        c = _make_config(trading=TradingConfig(max_position_pct=1.1))
        with pytest.raises(ConfigValidationError, match="max_position_pct"):
            c.validate_config()

    def test_max_position_pct_exactly_one_passes(self):
        c = _make_config(trading=TradingConfig(max_position_pct=1.0))
        c.validate_config()  # no debe lanzar

    # --- max_daily_loss ---

    def test_zero_max_daily_loss_raises(self):
        c = _make_config(risk=RiskConfig(max_daily_loss=0.0))
        with pytest.raises(ConfigValidationError, match="max_daily_loss"):
            c.validate_config()

    def test_max_daily_loss_over_one_raises(self):
        c = _make_config(risk=RiskConfig(max_daily_loss=1.1))
        with pytest.raises(ConfigValidationError, match="max_daily_loss"):
            c.validate_config()

    def test_max_daily_loss_exactly_one_passes(self):
        c = _make_config(risk=RiskConfig(max_daily_loss=1.0))
        c.validate_config()

    # --- max_drawdown ---

    def test_zero_max_drawdown_raises(self):
        c = _make_config(risk=RiskConfig(max_drawdown=0.0))
        with pytest.raises(ConfigValidationError, match="max_drawdown"):
            c.validate_config()

    def test_max_drawdown_over_one_raises(self):
        c = _make_config(risk=RiskConfig(max_drawdown=2.0))
        with pytest.raises(ConfigValidationError, match="max_drawdown"):
            c.validate_config()

    # --- símbolos enabled sin strategies ---

    def test_enabled_symbol_with_no_strategies_raises(self):
        syms = [SymbolConfig(symbol="BTC-USD", enabled=True, strategies=[])]
        c = _make_config(symbols=syms)
        with pytest.raises(ConfigValidationError, match="BTC-USD"):
            c.validate_config()

    def test_disabled_symbol_with_no_strategies_passes(self):
        syms = [SymbolConfig(symbol="BTC-USD", enabled=False, strategies=[])]
        c = _make_config(symbols=syms)
        c.validate_config()  # disabled → no se valida

    def test_empty_symbols_list_passes(self):
        c = _make_config(symbols=[])
        c.validate_config()

    # --- múltiples errores reportados juntos ---

    def test_multiple_errors_in_single_raise(self):
        c = _make_config(
            trading=TradingConfig(risk_per_trade_pct=0.0, max_notional_per_symbol=0.0),
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            c.validate_config()
        msg = str(exc_info.value)
        assert "risk_per_trade_pct" in msg
        assert "max_notional_per_symbol" in msg

    # --- config válida pasa ---

    def test_valid_config_passes(self):
        c = _make_config()
        c.validate_config()  # no debe lanzar

    def test_configvalidationerror_is_valueerror(self):
        """ConfigValidationError es subclase de ValueError para compatibilidad."""
        assert issubclass(ConfigValidationError, ValueError)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
