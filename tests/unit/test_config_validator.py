"""
Tests for config validator.
"""

from src.config_validator import validate_config, validate_file


class TestConfigValidatorValid:

    def test_valid_config_passes(self):
        data = {
            "trading": {
                "dry_run": True,
                "observe_only": True,
                "max_position_pct": 0.20,
                "max_notional_per_symbol": 10000,
                "max_orders_per_minute": 10,
            },
            "risk": {
                "max_daily_loss": 0.05,
                "max_drawdown": 0.15,
            },
            "symbols": [
                {"symbol": "BTC-USD", "enabled": True},
            ],
        }
        result = validate_config(data)
        assert result.ok, f"Should pass: {result.errors}"

    def test_minimal_valid_config(self):
        data = {
            "trading": {"dry_run": True, "max_position_pct": 0.2, "max_notional_per_symbol": 1000},
            "risk": {"max_daily_loss": 0.05, "max_drawdown": 0.15},
            "symbols": [{"symbol": "BTC-USD"}],
        }
        result = validate_config(data)
        assert result.ok


class TestConfigValidatorErrors:

    def test_missing_trading_section(self):
        data = {"risk": {"max_daily_loss": 0.05}, "symbols": []}
        result = validate_config(data)
        assert not result.ok
        assert any("trading" in e for e in result.errors)

    def test_missing_risk_section(self):
        data = {"trading": {"dry_run": True}, "symbols": []}
        result = validate_config(data)
        assert not result.ok
        assert any("risk" in e for e in result.errors)

    def test_missing_symbols(self):
        data = {"trading": {"dry_run": True}, "risk": {"max_daily_loss": 0.05}}
        result = validate_config(data)
        assert not result.ok
        assert any("symbols" in e for e in result.errors)

    def test_position_pct_over_one(self):
        data = {
            "trading": {"max_position_pct": 1.5, "max_notional_per_symbol": 1000},
            "risk": {"max_daily_loss": 0.05, "max_drawdown": 0.15},
            "symbols": [{"symbol": "X"}],
        }
        result = validate_config(data)
        assert not result.ok
        assert any("max_position_pct" in e for e in result.errors)

    def test_zero_max_notional(self):
        data = {
            "trading": {"max_position_pct": 0.2, "max_notional_per_symbol": 0},
            "risk": {"max_daily_loss": 0.05, "max_drawdown": 0.15},
            "symbols": [{"symbol": "X"}],
        }
        result = validate_config(data)
        assert not result.ok

    def test_daily_loss_disabled(self):
        data = {
            "trading": {"max_position_pct": 0.2, "max_notional_per_symbol": 1000},
            "risk": {"max_daily_loss": 0, "max_drawdown": 0.15},
            "symbols": [{"symbol": "X"}],
        }
        result = validate_config(data)
        assert not result.ok

    def test_live_trading_without_risk_limits(self):
        data = {
            "trading": {
                "dry_run": False, "observe_only": False,
                "max_position_pct": 0.2, "max_notional_per_symbol": 1000,
            },
            "risk": {"max_daily_loss": 0, "max_drawdown": 0.15},
            "symbols": [{"symbol": "X"}],
        }
        result = validate_config(data)
        assert not result.ok
        assert any("DANGEROUS" in e for e in result.errors)

    def test_symbol_missing_name(self):
        data = {
            "trading": {"max_position_pct": 0.2, "max_notional_per_symbol": 1000},
            "risk": {"max_daily_loss": 0.05, "max_drawdown": 0.15},
            "symbols": [{"enabled": True}],
        }
        result = validate_config(data)
        assert not result.ok


class TestConfigValidatorWarnings:

    def test_aggressive_daily_loss_warns(self):
        data = {
            "trading": {"max_position_pct": 0.2, "max_notional_per_symbol": 1000},
            "risk": {"max_daily_loss": 0.6, "max_drawdown": 0.15},
            "symbols": [{"symbol": "X"}],
        }
        result = validate_config(data)
        assert any("aggressive" in w for w in result.warnings)

    def test_no_symbols_warns(self):
        data = {
            "trading": {"max_position_pct": 0.2, "max_notional_per_symbol": 1000},
            "risk": {"max_daily_loss": 0.05, "max_drawdown": 0.15},
            "symbols": [],
        }
        result = validate_config(data)
        assert any("No symbols" in w for w in result.warnings)


class TestConfigValidatorFile:

    def test_validate_existing_config(self):
        result = validate_file("configs/symbols.yaml")
        assert result.ok, f"Config should be valid: {result.errors}"

    def test_validate_missing_file(self):
        result = validate_file("nonexistent.yaml")
        assert not result.ok
        assert any("not found" in e for e in result.errors)
