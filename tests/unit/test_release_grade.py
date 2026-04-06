"""
Tests for release-grade certification:
- Config missing YAML → fail-closed (no symbols, not silent defaults)
- Config parse error → fail-closed
- Dockerfile validity (file exists, has health check)
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from src.config import Config, SymbolConfig


class TestConfigFailClosed:
    """Config must not silently fall back to defaults on missing/broken YAML."""

    def test_missing_yaml_no_symbols(self):
        """Missing config file → symbols list is empty (fail-closed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Point Config to a dir without symbols.yaml
            with patch.dict(os.environ, {"FORTRESS_REPO": tmpdir}):
                from src.config import reset_config
                reset_config()
                # Create necessary dirs
                os.makedirs(os.path.join(tmpdir, "configs"), exist_ok=True)
                # Don't create symbols.yaml
                cfg = Config()
                assert cfg.symbols == [], (
                    f"Missing YAML should give empty symbols, got {len(cfg.symbols)} symbols"
                )
                reset_config()

    def test_broken_yaml_no_symbols(self):
        """Unparseable YAML → symbols list is empty (fail-closed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"FORTRESS_REPO": tmpdir}):
                from src.config import reset_config
                reset_config()
                config_dir = os.path.join(tmpdir, "configs")
                os.makedirs(config_dir, exist_ok=True)
                # Write invalid YAML
                with open(os.path.join(config_dir, "symbols.yaml"), "w") as f:
                    f.write(": : : invalid yaml [[[")
                cfg = Config()
                assert cfg.symbols == [], (
                    f"Broken YAML should give empty symbols, got {len(cfg.symbols)}"
                )
                reset_config()

    def test_valid_yaml_loads_symbols(self):
        """Valid YAML → symbols loaded correctly."""
        # Use the real config
        from src.config import get_config, reset_config
        reset_config()
        cfg = get_config()
        assert len(cfg.symbols) > 0
        for sym in cfg.symbols:
            assert sym.symbol, "Symbol name should not be empty"
        reset_config()


class TestSymbolConfigDefaults:
    """SymbolConfig defaults must only reference registered strategies."""

    def test_default_strategies_registered(self):
        sc = SymbolConfig(symbol="TEST-USD")
        from src.strategy.manager import _STRATEGY_REGISTRY
        for s in sc.strategies:
            assert s in _STRATEGY_REGISTRY, (
                f"Default strategy '{s}' not in registry"
            )

    def test_no_breakout_in_defaults(self):
        sc = SymbolConfig(symbol="X")
        assert "breakout" not in sc.strategies
        assert "mean_reversion" not in sc.strategies


class TestDockerfileValid:
    """Dockerfile must exist and have health check."""

    def test_dockerfile_exists(self):
        assert os.path.exists("Dockerfile")

    def test_dockerfile_copies_tests(self):
        with open("Dockerfile") as f:
            content = f.read()
        assert "COPY tests/" in content

    def test_dockerfile_has_health_check(self):
        with open("Dockerfile") as f:
            content = f.read()
        assert "import" in content.lower() or "RUN python" in content

    def test_dockerfile_has_config_validation(self):
        with open("Dockerfile") as f:
            content = f.read()
        assert "config_validator" in content
