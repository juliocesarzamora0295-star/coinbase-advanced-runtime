"""
Config validator — validates YAML config schema, types, ranges.

Detects dangerous configurations before runtime.
Runnable as: python -m src.config_validator configs/symbols.yaml
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml


@dataclass
class ValidationResult:
    """Result of config validation."""

    ok: bool
    errors: List[str]
    warnings: List[str]

    def __str__(self) -> str:
        lines = []
        if self.errors:
            for e in self.errors:
                lines.append(f"  ERROR: {e}")
        if self.warnings:
            for w in self.warnings:
                lines.append(f"  WARNING: {w}")
        if self.ok:
            lines.append("  Config validation PASSED")
        else:
            lines.append("  Config validation FAILED")
        return "\n".join(lines)


def validate_config(data: dict) -> ValidationResult:
    """
    Validate config dict (from YAML).

    Checks:
    - Required sections: trading, risk
    - Required fields with types
    - Dangerous ranges (risk limits disabled, no caps)
    """
    errors: List[str] = []
    warnings: List[str] = []

    # Required sections
    if "trading" not in data:
        errors.append("Missing required section: trading")
    if "risk" not in data:
        errors.append("Missing required section: risk")
    if "symbols" not in data:
        errors.append("Missing required section: symbols")

    trading = data.get("trading", {})
    risk = data.get("risk", {})

    # Trading fields
    if not isinstance(trading.get("dry_run"), bool):
        warnings.append("trading.dry_run should be bool (default: true)")

    if not isinstance(trading.get("observe_only"), bool):
        warnings.append("trading.observe_only should be bool (default: true)")

    max_pos = trading.get("max_position_pct", 0)
    if isinstance(max_pos, (int, float)) and max_pos > 1.0:
        errors.append(f"trading.max_position_pct={max_pos} > 1.0 (should be fraction)")
    if isinstance(max_pos, (int, float)) and max_pos <= 0:
        errors.append(f"trading.max_position_pct={max_pos} <= 0 (no position allowed)")

    max_notional = trading.get("max_notional_per_symbol", 0)
    if isinstance(max_notional, (int, float)) and max_notional <= 0:
        errors.append(f"trading.max_notional_per_symbol={max_notional} <= 0")

    max_orders = trading.get("max_orders_per_minute", 0)
    if isinstance(max_orders, int) and max_orders <= 0:
        warnings.append(f"trading.max_orders_per_minute={max_orders} <= 0 (rate limit disabled)")

    # Risk fields
    max_loss = risk.get("max_daily_loss", 0)
    if isinstance(max_loss, (int, float)) and max_loss <= 0:
        errors.append(f"risk.max_daily_loss={max_loss} <= 0 (daily loss limit disabled)")
    if isinstance(max_loss, (int, float)) and max_loss > 0.5:
        warnings.append(f"risk.max_daily_loss={max_loss} > 50% (very aggressive)")

    max_dd = risk.get("max_drawdown", 0)
    if isinstance(max_dd, (int, float)) and max_dd <= 0:
        errors.append(f"risk.max_drawdown={max_dd} <= 0 (drawdown limit disabled)")
    if isinstance(max_dd, (int, float)) and max_dd > 0.5:
        warnings.append(f"risk.max_drawdown={max_dd} > 50% (very aggressive)")

    # Dangerous: live trading without risk limits
    if not trading.get("dry_run", True) and not trading.get("observe_only", True):
        if max_loss <= 0 or max_dd <= 0:
            errors.append("LIVE TRADING with disabled risk limits — DANGEROUS")

    # Symbols
    symbols = data.get("symbols", [])
    if not symbols:
        warnings.append("No symbols configured")
    for sym in symbols:
        if not sym.get("symbol"):
            errors.append("Symbol entry missing 'symbol' field")

    return ValidationResult(
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


def validate_file(path: str) -> ValidationResult:
    """Validate a YAML config file."""
    p = Path(path)
    if not p.exists():
        return ValidationResult(ok=False, errors=[f"File not found: {path}"], warnings=[])

    with open(p) as f:
        data = yaml.safe_load(f) or {}

    return validate_config(data)


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: python -m src.config_validator <config.yaml>")
        sys.exit(1)

    result = validate_file(sys.argv[1])
    print(result)
    sys.exit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
