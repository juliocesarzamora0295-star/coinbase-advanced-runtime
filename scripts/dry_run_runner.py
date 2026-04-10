"""
Dry-run live validation runner.

Connects to Coinbase Advanced Trade via WebSocket, streams real market data,
runs the full signal-to-order pipeline with PaperEngine simulation.

Orders are logged but never sent to the exchange.

Usage:
    python scripts/dry_run_runner.py --duration 600
    python scripts/dry_run_runner.py --config config/dry_run.yaml --duration 600
    python scripts/dry_run_runner.py --dry-check
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path for src imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Structured JSON logger (shared pattern with observe_only_runner)
# ---------------------------------------------------------------------------

class StructuredLogger:
    """Writes structured JSON events to a log file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "a")
        self._start_ts = time.time()

    def event(self, category: str, name: str, **data) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsed_s": round(time.time() - self._start_ts, 2),
            "category": category,
            "event": name,
        }
        record.update(data)
        self._file.write(json.dumps(record, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.close()


# ---------------------------------------------------------------------------
# Config safety checks
# ---------------------------------------------------------------------------

def load_and_validate_config(config_path: str) -> dict:
    """Load YAML config and validate it is safe for dry-run."""
    import yaml

    p = Path(config_path)
    if not p.exists():
        print(f"FATAL: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(p) as f:
        data = yaml.safe_load(f) or {}

    from src.config_validator import validate_config
    result = validate_config(data)
    if not result.ok:
        print(f"FATAL: config validation failed:\n{result}", file=sys.stderr)
        sys.exit(1)

    return data


def assert_dry_run(data: dict) -> None:
    """Hard gate: abort if config enables live trading."""
    trading = data.get("trading", {})

    if trading.get("observe_only", False):
        print("FATAL: observe_only=true — use observe_only_runner.py instead.", file=sys.stderr)
        sys.exit(1)

    if not trading.get("dry_run", True):
        print("FATAL: dry_run is not true. Live trading not allowed in this runner.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Observation hooks — wire into TradingBot internals
# ---------------------------------------------------------------------------

def patch_bot_for_dry_run(bot, slog: StructuredLogger) -> None:
    """
    Monkey-patch TradingBot callbacks to capture structured events.

    Captures: candle ingestion, signal generation, risk evaluation,
    paper execution results.
    """
    # 1. Capture candle ingestion
    original_ingest = bot.market_data.ingest_5m_candle

    def wrapped_ingest(symbol, target_timeframe, timestamp_ms, open_p, high_p, low_p, close_p, volume):
        slog.event("candle", "ingest_5m", symbol=symbol,
                   timestamp_ms=timestamp_ms, close=str(close_p), volume=str(volume))
        return original_ingest(
            symbol=symbol, target_timeframe=target_timeframe,
            timestamp_ms=timestamp_ms, open_p=open_p, high_p=high_p,
            low_p=low_p, close_p=close_p, volume=volume,
        )

    bot.market_data.ingest_5m_candle = wrapped_ingest

    # 2. Capture candle closed events
    original_on_candle = bot._on_candle_closed

    def wrapped_on_candle(sym, candle):
        slog.event("candle", "closed", symbol=sym,
                   close=str(candle.close), volume=str(candle.volume),
                   timestamp_ms=candle.timestamp_ms)
        return original_on_candle(sym, candle)

    bot._on_candle_closed = wrapped_on_candle

    # 3. Capture order execution (dry-run paper fills)
    original_execute = bot._execute_order

    def wrapped_execute(intent, expected_price=None):
        from decimal import Decimal
        ep = expected_price if expected_price is not None else Decimal("0")
        slog.event("execution", "dry_run_order",
                   symbol=intent.symbol, side=intent.side,
                   qty=str(intent.final_qty), signal_id=intent.signal_id)
        result = original_execute(intent, ep)
        slog.event("execution", "dry_run_complete",
                   symbol=intent.symbol, side=intent.side,
                   qty=str(intent.final_qty))
        return result

    bot._execute_order = wrapped_execute

    # 4. Capture circuit breaker state
    original_cb_check = bot.circuit_breaker.check_before_trade

    def wrapped_cb_check():
        result = original_cb_check()
        allowed, reason = result
        slog.event("risk", "circuit_breaker_check",
                   allowed=allowed, reason=reason,
                   state=bot.circuit_breaker.state.value)
        return result

    bot.circuit_breaker.check_before_trade = wrapped_cb_check

    # 5. Kill switch state
    slog.event("risk", "kill_switch_state",
               active=bot.kill_switch.is_active,
               mode=bot.kill_switch.state.mode.value)


def capture_periodic_snapshot(bot, slog: StructuredLogger) -> None:
    """Capture a point-in-time snapshot of all subsystems."""
    from decimal import Decimal

    prices = {sym: str(p) for sym, p in bot.current_prices.items()}
    slog.event("snapshot", "prices", prices=prices)

    cb_status = bot.circuit_breaker.get_status()
    slog.event("snapshot", "circuit_breaker", **cb_status)

    slog.event("snapshot", "kill_switch",
               active=bot.kill_switch.is_active,
               mode=bot.kill_switch.state.mode.value,
               reason=bot.kill_switch.state.reason)

    for sym, ledger in bot.ledgers.items():
        price = bot.current_prices.get(sym, Decimal("0"))
        stats = ledger.get_stats()
        if price > Decimal("0"):
            stats["equity"] = str(ledger.get_equity(price))
            stats["unrealized_pnl"] = str(ledger.get_unrealized_pnl(price))
        slog.event("snapshot", "ledger", **stats)

    for sym, oms in bot.oms_services.items():
        oms_stats = oms.get_stats()
        oms_stats["symbol"] = sym
        slog.event("snapshot", "oms", **oms_stats)

    # Paper engine stats if available
    if hasattr(bot, 'paper_engine') and bot.paper_engine:
        pe = bot.paper_engine
        slog.event("snapshot", "paper_engine",
                   fills_count=len(pe.fills) if hasattr(pe, 'fills') else 0)

    metrics_snap = bot.metrics.generic_snapshot()
    slog.event("snapshot", "metrics", data=metrics_snap)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    """Run the dry-run validation session."""
    config_path = args.config
    duration_s = args.duration

    data = load_and_validate_config(config_path)
    assert_dry_run(data)

    if args.dry_check:
        print("DRY CHECK PASSED: config is dry_run=true, no live trading enabled.")
        return 0

    os.environ["FORTRESS_CONFIG"] = str(Path(config_path).resolve())

    from src.config import reset_config
    reset_config()

    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"dry_run_{ts_tag}.json"

    slog = StructuredLogger(log_path)
    slog.event("session", "start",
               config=config_path,
               duration_s=duration_s,
               mode="dry_run",
               log_file=str(log_path))

    print(f"Dry-run session: duration={duration_s}s, log={log_path}")

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

    from src.main import TradingBot
    bot = TradingBot()

    slog.event("session", "initializing")
    if not bot.initialize():
        slog.event("session", "init_failed")
        slog.close()
        print("FATAL: Bot initialization failed.", file=sys.stderr)
        return 1

    slog.event("session", "initialized",
               symbols=[s.symbol for s in bot.config.symbols if s.enabled],
               observe_only=bot.config.trading.observe_only,
               dry_run=bot.config.trading.dry_run)

    # Safety: verify dry_run mode after init
    if bot.config.trading.observe_only:
        slog.event("session", "safety_abort", reason="observe_only is true after init")
        slog.close()
        print("FATAL: observe_only is true after initialization.", file=sys.stderr)
        return 1

    if not bot.config.trading.dry_run:
        slog.event("session", "safety_abort", reason="dry_run became false after init")
        slog.close()
        print("FATAL: dry_run is false after initialization.", file=sys.stderr)
        return 1

    patch_bot_for_dry_run(bot, slog)

    try:
        bot.ws.start()
        slog.event("ws", "connected")
    except Exception as e:
        slog.event("ws", "connect_failed", error=str(e))
        slog.close()
        print(f"FATAL: WebSocket connection failed: {e}", file=sys.stderr)
        return 1

    shutdown_requested = [False]

    def on_signal(signum, frame):
        shutdown_requested[0] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    start_time = time.time()
    snapshot_interval_s = 30
    last_snapshot = 0.0
    bootstrap_checked = False

    print(f"Streaming live data for {duration_s}s... (Ctrl+C to stop early)")
    print(f"Mode: DRY_RUN — signals generated, orders simulated via PaperEngine")

    try:
        while not shutdown_requested[0]:
            elapsed = time.time() - start_time
            if elapsed >= duration_s:
                slog.event("session", "duration_reached", elapsed_s=round(elapsed, 1))
                break

            # OMS bootstrap timeout
            if not bootstrap_checked and elapsed >= bot._oms_bootstrap_timeout_s:
                bootstrap_checked = True
                for sym, oms in bot.oms_services.items():
                    if oms.complete_bootstrap_if_no_snapshot():
                        slog.event("oms", "bootstrap_force_completed", symbol=sym,
                                   elapsed_s=round(elapsed, 1))
                        print(f"  OMS [{sym}] bootstrap force-completed")

            # Periodic snapshots
            now = time.time()
            if now - last_snapshot >= snapshot_interval_s:
                last_snapshot = now
                capture_periodic_snapshot(bot, slog)

                prices_str = ", ".join(
                    f"{sym}=${p}" for sym, p in bot.current_prices.items()
                )
                remaining = int(duration_s - elapsed)
                print(f"  [{int(elapsed)}s / {duration_s}s] {prices_str or 'waiting...'} "
                      f"| CB={bot.circuit_breaker.state.value} "
                      f"| remaining={remaining}s")

            time.sleep(1)

    except Exception as e:
        slog.event("session", "error", error=str(e))
        print(f"ERROR during dry-run: {e}", file=sys.stderr)

    # Shutdown
    slog.event("session", "shutting_down")
    try:
        bot.ws.stop()
        slog.event("ws", "stopped")
    except Exception as e:
        slog.event("ws", "stop_error", error=str(e))

    capture_periodic_snapshot(bot, slog)

    elapsed = round(time.time() - start_time, 1)
    prices_final = {sym: str(p) for sym, p in bot.current_prices.items()}
    received_prices = any(p > 0 for p in bot.current_prices.values())

    summary = {
        "elapsed_s": elapsed,
        "prices_received": received_prices,
        "final_prices": prices_final,
        "circuit_breaker_state": bot.circuit_breaker.state.value,
        "kill_switch_active": bot.kill_switch.is_active,
        "mode": "dry_run",
    }
    slog.event("session", "complete", **summary)
    slog.close()

    print(f"\nDry-run session complete: {elapsed}s elapsed")
    print(f"  Prices received: {received_prices}")
    print(f"  Final prices: {prices_final}")
    print(f"  Circuit breaker: {bot.circuit_breaker.state.value}")
    print(f"  Log file: {log_path}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run live validation runner for Coinbase Advanced Trade"
    )
    parser.add_argument(
        "--duration", type=int, default=600,
        help="Session duration in seconds (default: 600 = 10 minutes)"
    )
    parser.add_argument(
        "--config", type=str, default="config/dry_run.yaml",
        help="Path to dry-run YAML config (default: config/dry_run.yaml)"
    )
    parser.add_argument(
        "--dry-check", action="store_true",
        help="Validate config and exit without connecting"
    )
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
