"""
Analyze structured JSON logs from dry_run or observe_only sessions.

Reads the JSON-lines log file and produces a summary report covering:
  - Session metadata (duration, mode, config)
  - Price feed quality (gaps, latency)
  - Candle statistics (count, frequency)
  - Signal pipeline (generated, blocked, executed)
  - Risk subsystem (circuit breaker trips, kill switch events)
  - Ledger state evolution
  - Issues and warnings

Usage:
    python scripts/analyze_dry_run.py logs/dry_run_20260410T191215Z.json
    python scripts/analyze_dry_run.py logs/dry_run_*.json --format markdown
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


def load_events(path: Path) -> list[dict]:
    """Load JSON-lines log file into list of event dicts."""
    events = []
    with open(path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  WARN: line {line_num}: {e}", file=sys.stderr)
    return events


def analyze(events: list[dict]) -> dict:
    """Analyze events and return summary dict."""
    if not events:
        return {"error": "no events"}

    summary = {
        "total_events": len(events),
        "first_ts": events[0].get("ts", "?"),
        "last_ts": events[-1].get("ts", "?"),
        "elapsed_s": events[-1].get("elapsed_s", 0),
    }

    # Categorize events
    categories = Counter()
    event_types = Counter()
    for e in events:
        cat = e.get("category", "unknown")
        evt = e.get("event", "unknown")
        categories[cat] += 1
        event_types[f"{cat}.{evt}"] += 1

    summary["categories"] = dict(categories)
    summary["event_types"] = dict(event_types)

    # Session info
    session_starts = [e for e in events if e.get("category") == "session" and e.get("event") == "start"]
    if session_starts:
        s = session_starts[0]
        summary["session"] = {
            "config": s.get("config", "?"),
            "duration_s": s.get("duration_s", 0),
            "mode": s.get("mode", "?"),
            "fresh": s.get("fresh", False),
        }

    session_completes = [e for e in events if e.get("category") == "session" and e.get("event") == "complete"]
    if session_completes:
        sc = session_completes[-1]
        summary["final_state"] = {
            "prices_received": sc.get("prices_received", False),
            "final_prices": sc.get("final_prices", {}),
            "circuit_breaker_state": sc.get("circuit_breaker_state", "?"),
            "kill_switch_active": sc.get("kill_switch_active", False),
        }

    # Candle analysis
    candle_closes = [e for e in events if e.get("category") == "candle" and e.get("event") == "closed"]
    historical_candles = [c for c in candle_closes if c.get("elapsed_s", 0) < 10]
    live_candles = [c for c in candle_closes if c.get("elapsed_s", 0) >= 10]

    summary["candles"] = {
        "total_closes": len(candle_closes),
        "historical_bootstrap": len(historical_candles),
        "live_closes": len(live_candles),
    }

    if live_candles:
        closes = [float(c.get("close", 0)) for c in live_candles]
        summary["candles"]["live_price_range"] = {
            "min": min(closes),
            "max": max(closes),
            "first": closes[0],
            "last": closes[-1],
            "change_pct": round((closes[-1] - closes[0]) / closes[0] * 100, 4) if closes[0] > 0 else 0,
        }

        # Candle frequency
        if len(live_candles) >= 2:
            timestamps = [c.get("timestamp_ms", 0) for c in live_candles]
            intervals = [(timestamps[i+1] - timestamps[i]) / 1000 for i in range(len(timestamps)-1)]
            summary["candles"]["avg_interval_s"] = round(sum(intervals) / len(intervals), 1)

    # Signal metrics
    metrics_events = [e for e in events if e.get("category") == "snapshot" and e.get("event") == "metrics"]
    if metrics_events:
        last_metrics = metrics_events[-1].get("data", {})
        counters = last_metrics.get("counters", {})
        summary["signals"] = {
            "counters": counters,
            "total_blocked": sum(v for k, v in counters.items() if "blocked" in k),
        }

    # Execution events
    executions = [e for e in events if e.get("category") == "execution"]
    if executions:
        exec_types = Counter(e.get("event", "?") for e in executions)
        summary["executions"] = {
            "total": len(executions),
            "by_type": dict(exec_types),
        }

    # Circuit breaker
    cb_events = [e for e in events if e.get("category") == "risk" and "circuit_breaker" in e.get("event", "")]
    if cb_events:
        cb_states = Counter()
        for cbe in cb_events:
            st = cbe.get("state", "?")
            cb_states[st] += 1
        summary["circuit_breaker"] = {
            "checks": len(cb_events),
            "states": dict(cb_states),
        }

    # Kill switch
    ks_events = [e for e in events if e.get("category") == "risk" and "kill_switch" in e.get("event", "")]
    if ks_events:
        last_ks = ks_events[-1]
        summary["kill_switch"] = {
            "events": len(ks_events),
            "last_active": last_ks.get("active", False),
            "last_mode": last_ks.get("mode", "?"),
        }

    # Ledger snapshots
    ledger_snaps = [e for e in events if e.get("category") == "snapshot" and e.get("event") == "ledger"]
    if ledger_snaps:
        last_ledger = ledger_snaps[-1]
        summary["ledger_final"] = {
            "symbol": last_ledger.get("symbol", "?"),
            "position_qty": last_ledger.get("position_qty", "0"),
            "realized_pnl": last_ledger.get("realized_pnl", "0"),
            "total_fills": last_ledger.get("total_fills", 0),
            "equity": last_ledger.get("equity", "?"),
        }

    # WS events
    ws_events = [e for e in events if e.get("category") == "ws"]
    if ws_events:
        ws_types = Counter(e.get("event", "?") for e in ws_events)
        summary["websocket"] = dict(ws_types)

    # Errors
    errors = [e for e in events if e.get("category") == "session" and e.get("event") == "error"]
    if errors:
        summary["errors"] = [e.get("error", "?") for e in errors]

    return summary


def format_text(summary: dict, path: str) -> str:
    """Format summary as human-readable text."""
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  DRY-RUN LOG ANALYSIS: {Path(path).name}")
    lines.append(f"{'='*60}")
    lines.append(f"  Events: {summary.get('total_events', 0)}")
    lines.append(f"  Time range: {summary.get('first_ts', '?')} → {summary.get('last_ts', '?')}")
    lines.append(f"  Elapsed: {summary.get('elapsed_s', 0)}s")

    session = summary.get("session", {})
    if session:
        lines.append(f"\n  Session:")
        lines.append(f"    Config: {session.get('config', '?')}")
        lines.append(f"    Duration: {session.get('duration_s', 0)}s")
        lines.append(f"    Mode: {session.get('mode', '?')}")
        lines.append(f"    Fresh: {session.get('fresh', False)}")

    candles = summary.get("candles", {})
    if candles:
        lines.append(f"\n  Candles:")
        lines.append(f"    Historical bootstrap: {candles.get('historical_bootstrap', 0)}")
        lines.append(f"    Live closes: {candles.get('live_closes', 0)}")
        price_range = candles.get("live_price_range", {})
        if price_range:
            lines.append(f"    Price range: ${price_range.get('min', 0):.2f} — ${price_range.get('max', 0):.2f}")
            lines.append(f"    Session change: {price_range.get('change_pct', 0):.4f}%")
        if "avg_interval_s" in candles:
            lines.append(f"    Avg candle interval: {candles['avg_interval_s']}s")

    signals = summary.get("signals", {})
    if signals:
        lines.append(f"\n  Signals:")
        lines.append(f"    Total blocked: {signals.get('total_blocked', 0)}")
        for k, v in signals.get("counters", {}).items():
            lines.append(f"      {k}: {v}")

    executions = summary.get("executions", {})
    if executions:
        lines.append(f"\n  Executions:")
        lines.append(f"    Total: {executions.get('total', 0)}")
        for k, v in executions.get("by_type", {}).items():
            lines.append(f"      {k}: {v}")

    cb = summary.get("circuit_breaker", {})
    if cb:
        lines.append(f"\n  Circuit Breaker:")
        lines.append(f"    Checks: {cb.get('checks', 0)}")
        lines.append(f"    States: {cb.get('states', {})}")

    ks = summary.get("kill_switch", {})
    if ks:
        lines.append(f"\n  Kill Switch:")
        lines.append(f"    Active: {ks.get('last_active', False)}")
        lines.append(f"    Mode: {ks.get('last_mode', '?')}")

    ledger = summary.get("ledger_final", {})
    if ledger:
        lines.append(f"\n  Ledger (final):")
        lines.append(f"    Symbol: {ledger.get('symbol', '?')}")
        lines.append(f"    Position: {ledger.get('position_qty', '0')}")
        lines.append(f"    Realized PnL: {ledger.get('realized_pnl', '0')}")
        lines.append(f"    Total fills: {ledger.get('total_fills', 0)}")
        lines.append(f"    Equity: {ledger.get('equity', '?')}")

    final = summary.get("final_state", {})
    if final:
        lines.append(f"\n  Final State:")
        lines.append(f"    Prices received: {final.get('prices_received', False)}")
        lines.append(f"    Final prices: {final.get('final_prices', {})}")
        lines.append(f"    CB state: {final.get('circuit_breaker_state', '?')}")
        lines.append(f"    Kill switch: {final.get('kill_switch_active', False)}")

    ws = summary.get("websocket", {})
    if ws:
        lines.append(f"\n  WebSocket: {ws}")

    errors = summary.get("errors", [])
    if errors:
        lines.append(f"\n  ERRORS ({len(errors)}):")
        for err in errors:
            lines.append(f"    - {err}")

    lines.append(f"\n{'='*60}")
    return "\n".join(lines)


def format_markdown(summary: dict, path: str) -> str:
    """Format summary as markdown."""
    lines = []
    lines.append(f"# Dry-Run Analysis: {Path(path).name}\n")

    lines.append(f"| Metric | Value |")
    lines.append(f"|--------|-------|")
    lines.append(f"| Events | {summary.get('total_events', 0)} |")
    lines.append(f"| Elapsed | {summary.get('elapsed_s', 0)}s |")
    lines.append(f"| Time range | {summary.get('first_ts', '?')} → {summary.get('last_ts', '?')} |")

    session = summary.get("session", {})
    if session:
        lines.append(f"\n## Session")
        lines.append(f"- Config: `{session.get('config', '?')}`")
        lines.append(f"- Mode: {session.get('mode', '?')}")
        lines.append(f"- Fresh: {session.get('fresh', False)}")

    candles = summary.get("candles", {})
    if candles:
        lines.append(f"\n## Candles")
        lines.append(f"- Historical: {candles.get('historical_bootstrap', 0)}")
        lines.append(f"- Live: {candles.get('live_closes', 0)}")
        pr = candles.get("live_price_range", {})
        if pr:
            lines.append(f"- Price: ${pr.get('min', 0):.2f} — ${pr.get('max', 0):.2f} ({pr.get('change_pct', 0):.4f}%)")

    signals = summary.get("signals", {})
    if signals:
        lines.append(f"\n## Signals")
        lines.append(f"- Blocked: {signals.get('total_blocked', 0)}")
        for k, v in signals.get("counters", {}).items():
            lines.append(f"  - `{k}`: {v}")

    executions = summary.get("executions", {})
    if executions:
        lines.append(f"\n## Executions")
        lines.append(f"- Total: {executions.get('total', 0)}")
        for k, v in executions.get("by_type", {}).items():
            lines.append(f"  - `{k}`: {v}")

    ledger = summary.get("ledger_final", {})
    if ledger:
        lines.append(f"\n## Ledger")
        lines.append(f"- Position: {ledger.get('position_qty', '0')}")
        lines.append(f"- PnL: {ledger.get('realized_pnl', '0')}")
        lines.append(f"- Fills: {ledger.get('total_fills', 0)}")
        lines.append(f"- Equity: {ledger.get('equity', '?')}")

    errors = summary.get("errors", [])
    if errors:
        lines.append(f"\n## Errors")
        for err in errors:
            lines.append(f"- {err}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze dry_run/observe_only JSON logs")
    parser.add_argument("logs", nargs="+", help="Path(s) to JSON-lines log files")
    parser.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    parser.add_argument("--output", type=str, default="", help="Write output to file")
    args = parser.parse_args()

    all_output = []
    for log_path in args.logs:
        p = Path(log_path)
        if not p.exists():
            print(f"WARN: {log_path} not found, skipping", file=sys.stderr)
            continue

        events = load_events(p)
        summary = analyze(events)

        if args.format == "json":
            all_output.append(json.dumps(summary, indent=2, default=str))
        elif args.format == "markdown":
            all_output.append(format_markdown(summary, log_path))
        else:
            all_output.append(format_text(summary, log_path))

    output = "\n\n".join(all_output)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Report written to: {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
