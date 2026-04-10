"""
Observe-only live validation runner.

Connects to Coinbase Advanced Trade via WebSocket, streams real market data,
runs the full signal pipeline in observe_only mode, and logs structured JSON
for post-run analysis.

No orders are placed. No state is mutated beyond logging.

Usage:
    python scripts/observe_only_runner.py --duration 300
    python scripts/observe_only_runner.py --duration 10 --dry-check
    python scripts/observe_only_runner.py --config config/observe_only.yaml --duration 600
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
# Structured JSON logger
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
    """Load YAML config and validate it is safe for observe-only."""
    import yaml

    p = Path(config_path)
    if not p.exists():
        print(f"FATAL: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(p) as f:
        data = yaml.safe_load(f) or {}

    # Structural validation via config_validator
    from src.config_validator import validate_config
    result = validate_config(data)
    if not result.ok:
        print(f"FATAL: config validation failed:\n{result}", file=sys.stderr)
        sys.exit(1)

    return data


def assert_observe_only(data: dict) -> None:
    """Hard gate: abort if config allows any form of trading."""
    trading = data.get("trading", {})

    if not trading.get("observe_only", False):
        print("FATAL: trading.observe_only is not true. Aborting.", file=sys.stderr)
        sys.exit(1)

    if not trading.get("dry_run", True) and not trading.get("observe_only", False):
        print("FATAL: live trading enabled. Aborting.", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Observation hooks — wire into TradingBot internals
# ---------------------------------------------------------------------------

def patch_bot_for_observation(bot, slog: StructuredLogger) -> None:
    """
    Monkey-patch TradingBot callbacks to capture structured events.

    This does NOT modify src/ modules. It wraps existing callbacks
    at the TradingBot instance level to emit structured log events.
    """
    # 1. Capture WS connection state
    original_ws_start = bot.ws.start

    def wrapped_ws_start():
        slog.event("ws", "start_requested")
        original_ws_start()
        slog.event("ws", "started")

    bot.ws.start = wrapped_ws_start

    # 2. Capture gap detection
    original_gap = bot.ws.on_gap_detected
    if original_gap:
        def wrapped_gap():
            slog.event("ws", "gap_detected")
            original_gap()
        bot.ws.on_gap_detected = wrapped_gap

    # 3. Capture candle ingestion via MarketDataService
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

    # 4. Capture candle closed events
    original_on_candle = bot._on_candle_closed

    def wrapped_on_candle(sym, candle):
        slog.event("candle", "closed", symbol=sym,
                    open=str(candle.open), high=str(candle.high),
                    low=str(candle.low), close=str(candle.close),
                    volume=str(candle.volume), timestamp_ms=candle.timestamp_ms)
        return original_on_candle(sym, candle)

    bot._on_candle_closed = wrapped_on_candle

    # 5. Capture price updates
    original_lock = bot._lock

    class PriceTracker:
        """Tracks price update count per symbol."""
        def __init__(self):
            self.counts: dict = {}
            self.last_logged: dict = {}

    tracker = PriceTracker()

    # We log price updates periodically (every 30s per symbol) to avoid flooding
    _original_subscribe_symbol = bot._subscribe_symbol

    # 6. Capture observe_only signal blocks
    original_execute = bot._execute_order

    def wrapped_execute(intent, expected_price=None):
        from decimal import Decimal
        ep = expected_price if expected_price is not None else Decimal("0")
        slog.event("execution", "observe_only_block",
                    symbol=intent.symbol, side=intent.side,
                    qty=str(intent.final_qty), signal_id=intent.signal_id)
        return original_execute(intent, ep)

    bot._execute_order = wrapped_execute

    # 7. Capture circuit breaker state on each check
    original_cb_check = bot.circuit_breaker.check_before_trade

    def wrapped_cb_check():
        result = original_cb_check()
        allowed, reason = result
        slog.event("risk", "circuit_breaker_check",
                    allowed=allowed, reason=reason,
                    state=bot.circuit_breaker.state.value)
        return result

    bot.circuit_breaker.check_before_trade = wrapped_cb_check

    # 8. Capture kill switch state
    slog.event("risk", "kill_switch_state",
               active=bot.kill_switch.is_active,
               mode=bot.kill_switch.state.mode.value)


def capture_periodic_snapshot(bot, slog: StructuredLogger) -> None:
    """Capture a point-in-time snapshot of all subsystems."""
    from decimal import Decimal

    # Prices
    prices = {sym: str(p) for sym, p in bot.current_prices.items()}
    slog.event("snapshot", "prices", prices=prices)

    # Circuit breaker
    cb_status = bot.circuit_breaker.get_status()
    slog.event("snapshot", "circuit_breaker", **cb_status)

    # Kill switch
    slog.event("snapshot", "kill_switch",
               active=bot.kill_switch.is_active,
               mode=bot.kill_switch.state.mode.value,
               reason=bot.kill_switch.state.reason)

    # Ledger state per symbol
    for sym, ledger in bot.ledgers.items():
        price = bot.current_prices.get(sym, Decimal("0"))
        stats = ledger.get_stats()
        if price > Decimal("0"):
            stats["equity"] = str(ledger.get_equity(price))
            stats["unrealized_pnl"] = str(ledger.get_unrealized_pnl(price))
        slog.event("snapshot", "ledger", symbol=sym, **stats)

    # OMS state per symbol
    for sym, oms in bot.oms_services.items():
        oms_stats = oms.get_stats()
        slog.event("snapshot", "oms", symbol=sym, **oms_stats)

    # Metrics
    metrics_snap = bot.metrics.generic_snapshot()
    slog.event("snapshot", "metrics", data=metrics_snap)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    """Run the observe-only validation session."""
    config_path = args.config
    duration_s = args.duration

    # Load and validate config
    data = load_and_validate_config(config_path)
    assert_observe_only(data)

    if args.dry_check:
        print("DRY CHECK PASSED: config is observe_only=true, no trading enabled.")
        return 0

    # Set config path for TradingBot to pick up
    os.environ["FORTRESS_CONFIG"] = str(Path(config_path).resolve())

    # Reset global config singleton so it reloads from our YAML
    from src.config import reset_config
    reset_config()

    # Set up structured log
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"observe_only_{ts_tag}.json"

    slog = StructuredLogger(log_path)
    slog.event("session", "start",
               config=config_path,
               duration_s=duration_s,
               log_file=str(log_path))

    print(f"Observe-only session: duration={duration_s}s, log={log_path}")

    # Also configure Python logging to console + file
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )

    # Import and create bot
    from src.main import TradingBot
    bot = TradingBot()

    # Initialize (connects to exchange, bootstraps equity, sets up WS subscriptions)
    slog.event("session", "initializing")
    if not bot.initialize():
        slog.event("session", "init_failed")
        slog.close()
        print("FATAL: Bot initialization failed. Check credentials and network.", file=sys.stderr)
        return 1

    slog.event("session", "initialized",
               symbols=[s.symbol for s in bot.config.symbols if s.enabled],
               observe_only=bot.config.trading.observe_only,
               dry_run=bot.config.trading.dry_run)

    # Double-check observe_only is still true after config load
    if not bot.config.trading.observe_only:
        slog.event("session", "safety_abort", reason="observe_only became false after init")
        slog.close()
        print("FATAL: observe_only is false after initialization. Aborting.", file=sys.stderr)
        return 1

    # Patch for structured observation
    patch_bot_for_observation(bot, slog)

    # Start WebSocket
    try:
        bot.ws.start()
        slog.event("ws", "connected")
    except Exception as e:
        slog.event("ws", "connect_failed", error=str(e))
        slog.close()
        print(f"FATAL: WebSocket connection failed: {e}", file=sys.stderr)
        return 1

    # Graceful shutdown on SIGINT/SIGTERM
    shutdown_requested = [False]

    def on_signal(signum, frame):
        shutdown_requested[0] = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    # Main observation loop
    start_time = time.time()
    snapshot_interval_s = 30
    last_snapshot = 0.0
    candle_count_at_start = 0

    print(f"Streaming live data for {duration_s}s... (Ctrl+C to stop early)")

    try:
        while not shutdown_requested[0]:
            elapsed = time.time() - start_time
            if elapsed >= duration_s:
                slog.event("session", "duration_reached", elapsed_s=round(elapsed, 1))
                break

            # Periodic snapshots
            now = time.time()
            if now - last_snapshot >= snapshot_interval_s:
                last_snapshot = now
                capture_periodic_snapshot(bot, slog)

                # Print progress
                prices_str = ", ".join(
                    f"{sym}=${p}" for sym, p in bot.current_prices.items()
                )
                remaining = int(duration_s - elapsed)
                print(f"  [{int(elapsed)}s / {duration_s}s] {prices_str or 'waiting for data...'} "
                      f"| CB={bot.circuit_breaker.state.value} "
                      f"| remaining={remaining}s")

            time.sleep(1)

    except Exception as e:
        slog.event("session", "error", error=str(e))
        print(f"ERROR during observation: {e}", file=sys.stderr)

    # Shutdown
    slog.event("session", "shutting_down")
    try:
        bot.ws.stop()
        slog.event("ws", "stopped")
    except Exception as e:
        slog.event("ws", "stop_error", error=str(e))

    # Final snapshot
    capture_periodic_snapshot(bot, slog)

    # Summary
    elapsed = round(time.time() - start_time, 1)
    prices_final = {sym: str(p) for sym, p in bot.current_prices.items()}
    received_prices = any(p > 0 for p in bot.current_prices.values())

    summary = {
        "elapsed_s": elapsed,
        "prices_received": received_prices,
        "final_prices": prices_final,
        "circuit_breaker_state": bot.circuit_breaker.state.value,
        "kill_switch_active": bot.kill_switch.is_active,
        "symbols_tracked": list(bot.current_prices.keys()),
    }
    slog.event("session", "complete", **summary)
    slog.close()

    print(f"\nSession complete: {elapsed}s elapsed")
    print(f"  Prices received: {received_prices}")
    print(f"  Final prices: {prices_final}")
    print(f"  Circuit breaker: {bot.circuit_breaker.state.value}")
    print(f"  Log file: {log_path}")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Observe-only live validation runner for Coinbase Advanced Trade"
    )
    parser.add_argument(
        "--duration", type=int, default=300,
        help="Session duration in seconds (default: 300 = 5 minutes)"
    )
    parser.add_argument(
        "--config", type=str, default="config/observe_only.yaml",
        help="Path to observe-only YAML config (default: config/observe_only.yaml)"
    )
    parser.add_argument(
        "--dry-check", action="store_true",
        help="Validate config and exit without connecting (no credentials needed)"
    )
    args = parser.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
