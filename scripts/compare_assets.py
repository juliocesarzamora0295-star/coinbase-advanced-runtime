"""
Parse segmented_runner results_*.txt files and print a comparative table
across symbols/timeframes.

Usage:
    python scripts/compare_assets.py results_*.txt
or:
    python scripts/compare_assets.py  # auto-globs results_*.txt in cwd
"""

import glob
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple


_AGG_RE = re.compile(
    r"AGGREGATE\s+(\d+)\s+([\d.]+)%\s+([-\d.]+)",
    re.MULTILINE,
)
_REGIME_RE = re.compile(
    r"^(\w+)\s+([-\d.]+)%\s+(\d+)\s+([\d.]+)%\s+([-\d.]+)\s+([\d.]+)%\s+([-\d.]+)\s+([\d.]+)$",
    re.MULTILINE,
)


def parse_file(path: Path) -> Dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    regimes: List[Tuple[str, float, int, float, float, float, float]] = []
    for m in _REGIME_RE.finditer(text):
        label, ret, trades, wr, sharpe, mdd, pnl = (
            m.group(1), float(m.group(2)), int(m.group(3)),
            float(m.group(4)), float(m.group(5)), float(m.group(6)), float(m.group(7)),
        )
        regimes.append((label, ret, trades, wr, sharpe, mdd, pnl))

    agg_m = _AGG_RE.search(text)
    total_trades = int(agg_m.group(1)) if agg_m else 0
    total_wr = float(agg_m.group(2)) if agg_m else 0.0
    total_pnl = float(agg_m.group(3)) if agg_m else 0.0

    sharpes = [r[4] for r in regimes if r[4] != 0]
    avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else 0.0
    max_dd = max((r[5] for r in regimes), default=0.0)

    return {
        "file": path.name,
        "regimes": regimes,
        "total_trades": total_trades,
        "total_wr": total_wr,
        "total_pnl": total_pnl,
        "avg_sharpe": avg_sharpe,
        "max_dd": max_dd,
    }


def main() -> None:
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("results_*.txt"))
    if not files:
        print("No results_*.txt files found.")
        return

    rows = [parse_file(Path(f)) for f in files]

    print()
    print("=" * 92)
    print("MULTI-ASSET COMPARATIVE REPORT")
    print("=" * 92)
    print(f"{'File':<42} {'Trades':>7} {'WR':>7} {'Sharpe':>8} {'MaxDD':>8} {'PnL':>12}")
    print("-" * 92)
    for r in rows:
        print(
            f"{r['file']:<42} {r['total_trades']:>7} "
            f"{r['total_wr']:>6.1f}% {r['avg_sharpe']:>8.2f} "
            f"{r['max_dd']:>7.2f}% {r['total_pnl']:>12.2f}"
        )
    print("=" * 92)

    qualified = [
        r for r in rows
        if r["avg_sharpe"] > 0.3 and r["total_pnl"] > 100
    ]
    print(f"\nQualified for prod_symbols.yaml (Sharpe>0.3 AND PnL>$100): {len(qualified)}")
    for r in qualified:
        print(f"  ✓ {r['file']} — Sharpe={r['avg_sharpe']:.2f} PnL=${r['total_pnl']:.2f}")


if __name__ == "__main__":
    main()
