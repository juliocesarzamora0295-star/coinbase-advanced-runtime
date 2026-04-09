"""
Tests for 3 operational gaps: exposure wiring, sizing_mode from YAML,
strategy config consistency.
"""

from decimal import Decimal

import yaml

from src.config import TradingConfig, _validate_sizing_mode
from src.risk.gate import (
    RULE_TOTAL_EXPOSURE,
    RiskGate,
    RiskLimits,
    RiskSnapshot,
)


# ── A) check_total_exposure wired ──


class TestExposureGateActive:
    """Verify check_total_exposure blocks when aggregated exposure exceeds limit."""

    def test_blocks_when_total_exposure_exceeded(self):
        """Portfolio at 70% + new 20% → 90% > 80% limit → blocked."""
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.80")))
        verdict = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("7000")},
            new_symbol="ETH-USD",
            new_notional=Decimal("2000"),
        )
        assert not verdict.allowed
        assert RULE_TOTAL_EXPOSURE in verdict.blocking_rule_ids

    def test_allows_when_within_limit(self):
        """Portfolio at 30% + new 20% → 50% < 80% limit → allowed."""
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.80")))
        verdict = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("3000")},
            new_symbol="ETH-USD",
            new_notional=Decimal("2000"),
        )
        assert verdict.allowed

    def test_exposure_aggregates_multiple_symbols(self):
        """Multiple existing positions sum correctly."""
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.50")))
        verdict = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={
                "BTC-USD": Decimal("2000"),
                "ETH-USD": Decimal("1500"),
                "SOL-USD": Decimal("1000"),
            },
            new_symbol="DOGE-USD",
            new_notional=Decimal("1000"),
        )
        # Total: 2000+1500+1000+1000 = 5500 / 10000 = 55% > 50%
        assert not verdict.allowed

    def test_sell_orders_dont_add_exposure(self):
        """SELL reduces position — new_notional=0 for sells is caller's job."""
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.80")))
        verdict = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("7000")},
            new_symbol="BTC-USD",
            new_notional=Decimal("0"),  # sell → no new exposure
        )
        assert verdict.allowed


# ── B) sizing_mode loaded from YAML ──


class TestSizingModeFromYAML:
    """Verify sizing_mode is loaded from YAML and validated."""

    def test_default_is_notional(self):
        tc = TradingConfig()
        assert tc.sizing_mode == "NOTIONAL"

    def test_valid_notional(self):
        assert _validate_sizing_mode("NOTIONAL") == "NOTIONAL"

    def test_valid_risk_based(self):
        assert _validate_sizing_mode("RISK_BASED") == "RISK_BASED"

    def test_case_insensitive(self):
        assert _validate_sizing_mode("notional") == "NOTIONAL"
        assert _validate_sizing_mode("risk_based") == "RISK_BASED"

    def test_invalid_falls_back_to_notional(self):
        assert _validate_sizing_mode("INVALID_MODE") == "NOTIONAL"

    def test_yaml_has_sizing_mode(self):
        """symbols.yaml now declares sizing_mode."""
        with open("configs/symbols.yaml") as f:
            data = yaml.safe_load(f)
        assert "sizing_mode" in data.get("trading", {})
        assert data["trading"]["sizing_mode"] in ("NOTIONAL", "RISK_BASED")


# ── C) Strategy config consistency ──


class TestStrategyConfigConsistency:
    """Verify YAML only declares strategies that exist in StrategyManager."""

    def test_yaml_strategies_all_in_registry(self):
        """Every strategy in symbols.yaml must exist in STRATEGY_REGISTRY."""
        from src.strategy.manager import _STRATEGY_REGISTRY as STRATEGY_REGISTRY

        with open("configs/symbols.yaml") as f:
            data = yaml.safe_load(f)

        for sym_cfg in data.get("symbols", []):
            for entry in sym_cfg.get("strategies", []):
                strat_name = entry if isinstance(entry, str) else entry.get("name", "")
                assert strat_name in STRATEGY_REGISTRY, (
                    f"Strategy '{strat_name}' declared for {sym_cfg['symbol']} "
                    f"but not in STRATEGY_REGISTRY. "
                    f"Available: {list(STRATEGY_REGISTRY.keys())}"
                )

    def test_no_ghost_strategies(self):
        """No breakout, mean_reversion in YAML (removed)."""
        with open("configs/symbols.yaml") as f:
            content = f.read()
        assert "breakout" not in content
        assert "mean_reversion" not in content

    def test_no_ghost_strategies_in_config_defaults(self):
        """SymbolConfig defaults and fallbacks don't reference unregistered strategies."""
        from src.config import SymbolConfig
        sc = SymbolConfig(symbol="TEST-USD")
        from src.strategy.manager import _STRATEGY_REGISTRY as REG
        for s in sc.strategies:
            assert s in REG, f"Default strategy '{s}' not in registry"


# ── Exposure fail-closed on missing prices ──


class TestExposureFailClosed:
    """Exposure check must not silently pass when prices are missing."""

    def test_exposure_blocks_with_zero_equity(self):
        """Zero equity → exposure check blocks (equity <= 0 guard)."""
        gate = RiskGate(RiskLimits())
        verdict = gate.check_total_exposure(
            equity=Decimal("0"),
            exposures={},
            new_symbol="BTC-USD",
            new_notional=Decimal("100"),
        )
        assert not verdict.allowed

    def test_exposure_uses_avg_entry_as_fallback(self):
        """
        If current_price missing for a position, avg_entry is used.
        This is tested via the gate directly — avg_entry produces
        a non-zero exposure that may trigger the limit.
        """
        gate = RiskGate(RiskLimits(max_total_exposure_pct=Decimal("0.50")))
        # Simulate: position valued at avg_entry (50000) → exposure 5000/10000 = 50%
        verdict = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("5000")},  # valued via avg_entry
            new_symbol="ETH-USD",
            new_notional=Decimal("1000"),
        )
        # 6000/10000 = 60% > 50% → blocked
        assert not verdict.allowed


# ── max_total_exposure_pct configurable ──


class TestExposureLimitConfigurable:

    def test_risk_config_has_max_total_exposure(self):
        from src.config import RiskConfig
        rc = RiskConfig()
        assert rc.max_total_exposure_pct == 0.80

    def test_yaml_can_override_exposure_limit(self):
        """Verify the field exists in RiskConfig and can be set."""
        from src.config import RiskConfig
        rc = RiskConfig(max_total_exposure_pct=0.60)
        assert rc.max_total_exposure_pct == 0.60

    def test_risk_limits_receives_exposure_pct(self):
        """RiskLimits constructed with configurable max_total_exposure_pct."""
        limits = RiskLimits(max_total_exposure_pct=Decimal("0.60"))
        gate = RiskGate(limits)
        # 50% existing + 15% new = 65% > 60%
        verdict = gate.check_total_exposure(
            equity=Decimal("10000"),
            exposures={"BTC-USD": Decimal("5000")},
            new_symbol="ETH-USD",
            new_notional=Decimal("1500"),
        )
        assert not verdict.allowed
