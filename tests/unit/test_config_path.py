"""
Test that FORTRESS_CONFIG env var controls which YAML config.py loads.
"""

import os
import tempfile
from unittest.mock import patch

import yaml


class TestFortressConfigEnvVar:

    def test_env_var_overrides_default_path(self):
        """FORTRESS_CONFIG env var → config.py loads that file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_yaml = os.path.join(tmpdir, "custom.yaml")
            with open(custom_yaml, "w") as f:
                yaml.dump({
                    "trading": {
                        "dry_run": True,
                        "observe_only": True,
                        "max_position_pct": 0.15,
                        "max_notional_per_symbol": 7777,
                    },
                    "risk": {
                        "max_daily_loss": 0.03,
                        "max_drawdown": 0.10,
                    },
                    "symbols": [
                        {"symbol": "SOL-USD", "enabled": True, "timeframe": "1h",
                         "strategies": ["ma_crossover"]},
                    ],
                }, f)

            with patch.dict(os.environ, {"FORTRESS_CONFIG": custom_yaml}):
                from src.config import Config, reset_config
                reset_config()
                cfg = Config()
                # Verify it loaded from custom file
                assert len(cfg.symbols) == 1
                assert cfg.symbols[0].symbol == "SOL-USD"
                assert cfg.trading.max_notional_per_symbol == 7777
                reset_config()

    def test_default_path_without_env_var(self):
        """Without FORTRESS_CONFIG → loads configs/symbols.yaml."""
        env = {k: v for k, v in os.environ.items() if k != "FORTRESS_CONFIG"}
        with patch.dict(os.environ, env, clear=True):
            from src.config import Config, reset_config
            reset_config()
            cfg = Config()
            # Should load the default symbols.yaml (BTC-USD, ETH-USD)
            symbols = [s.symbol for s in cfg.symbols]
            assert "BTC-USD" in symbols
            reset_config()

    def test_prod_config_validates(self):
        """prod_symbols.yaml passes validation."""
        from src.config_validator import validate_file
        result = validate_file("configs/prod_symbols.yaml")
        assert result.ok, f"Prod config invalid: {result.errors}"
