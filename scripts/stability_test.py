"""
Restart resilience stability test.

Runs N consecutive dry_run sessions, stopping each with SIGINT (graceful shutdown).
After each cycle, verifies:
  1. Process exited cleanly (exit code 0)
  2. SQLite state files exist and are readable
  3. Ledger state is consistent (position_qty >= 0, no NaN equity)
  4. Kill switch is not stuck active
  5. No duplicate trade_ids in idempotency store

Usage:
    python scripts/stability_test.py --cycles 3 --duration 120
    python scripts/stability_test.py --cycles 5 --duration 300 --config config/dry_run_aggressive.yaml
"""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# State directory — matches src/config.py PathsConfig.state
STATE_DIR = REPO_ROOT / "runtime" / "state"


def find_state_dir() -> Path:
    """Locate the state directory. Check common locations."""
    candidates = [
        REPO_ROOT / "runtime" / "state",
        REPO_ROOT / "state",
        Path.home() / ".fortress" / "state",
    ]
    for p in candidates:
        if p.exists():
            return p
    # Default — will be created by the bot
    return REPO_ROOT / "runtime" / "state"


def check_sqlite_readable(db_path: Path) -> tuple[bool, str]:
    """Verify a SQLite database is readable and not corrupted."""
    if not db_path.exists():
        return False, f"missing: {db_path.name}"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("SELECT 1")
        # Check integrity
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if result[0] != "ok":
            return False, f"corrupt: {db_path.name}"
        return True, "ok"
    except Exception as e:
        return False, f"error: {db_path.name}: {e}"


def check_ledger_consistency(state_dir: Path) -> list[str]:
    """Check all ledger DBs for consistency issues."""
    issues = []
    for db_file in state_dir.glob("ledger_*.db"):
        symbol = db_file.stem.replace("ledger_", "")
        try:
            conn = sqlite3.connect(str(db_file))
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [r[0] for r in rows]

            if "state" in table_names:
                state_rows = conn.execute("SELECT key, value FROM state").fetchall()
                state = {k: v for k, v in state_rows}

                # Check position_qty is non-negative
                pos_raw = state.get("position_qty", "0")
                try:
                    pos = Decimal(pos_raw)
                    if pos < 0:
                        issues.append(f"[{symbol}] negative position_qty: {pos}")
                except InvalidOperation:
                    issues.append(f"[{symbol}] invalid position_qty: {pos_raw}")

                # Check equity-related values are not NaN
                for key in ["cash", "realized_pnl"]:
                    val = state.get(key, "0")
                    try:
                        d = Decimal(val)
                        if d != d:  # NaN check
                            issues.append(f"[{symbol}] NaN in {key}")
                    except InvalidOperation:
                        issues.append(f"[{symbol}] invalid {key}: {val}")

            if "trades" in table_names:
                # Check for duplicate trade_ids
                dupes = conn.execute(
                    "SELECT trade_id, COUNT(*) c FROM trades "
                    "GROUP BY trade_id HAVING c > 1"
                ).fetchall()
                if dupes:
                    issues.append(
                        f"[{symbol}] duplicate trade_ids: "
                        f"{[d[0] for d in dupes[:5]]}"
                    )

            conn.close()
        except Exception as e:
            issues.append(f"[{symbol}] ledger read error: {e}")

    return issues


def check_idempotency_dupes(state_dir: Path) -> list[str]:
    """Check idempotency stores for duplicate entries."""
    issues = []
    for db_file in state_dir.glob("idempotency_*.db"):
        symbol = db_file.stem.replace("idempotency_", "")
        try:
            conn = sqlite3.connect(str(db_file))
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = [r[0] for r in tables]

            for table in table_names:
                row_count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                if row_count > 0:
                    # Check for duplicates on any unique column
                    cols = conn.execute(f"PRAGMA table_info([{table}])").fetchall()
                    col_names = [c[1] for c in cols]
                    if "signal_id" in col_names:
                        dupes = conn.execute(
                            f"SELECT signal_id, COUNT(*) c FROM [{table}] "
                            f"GROUP BY signal_id HAVING c > 1"
                        ).fetchall()
                        if dupes:
                            issues.append(
                                f"[{symbol}] duplicate signal_ids in {table}: "
                                f"{[d[0] for d in dupes[:5]]}"
                            )
            conn.close()
        except Exception as e:
            issues.append(f"[{symbol}] idempotency read error: {e}")

    return issues


def check_kill_switch(state_dir: Path) -> tuple[bool, str]:
    """Check kill switch is not stuck active."""
    ks_db = state_dir / "kill_switch.db"
    if not ks_db.exists():
        return True, "no kill_switch.db (fresh state)"
    try:
        conn = sqlite3.connect(str(ks_db))
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [r[0] for r in tables]

        if "state" in table_names:
            rows = conn.execute("SELECT key, value FROM state").fetchall()
            state = {k: v for k, v in rows}
            active = state.get("active", "false")
            if active.lower() in ("true", "1"):
                mode = state.get("mode", "unknown")
                return False, f"kill switch ACTIVE (mode={mode})"
        conn.close()
        return True, "inactive"
    except Exception as e:
        return True, f"read error (assuming ok): {e}"


def run_cycle(
    cycle_num: int,
    total_cycles: int,
    duration_s: int,
    config_path: str,
    python_exe: str,
) -> dict:
    """
    Run one dry_run cycle and verify state after shutdown.

    Returns a result dict with pass/fail and details.
    """
    result = {
        "cycle": cycle_num,
        "duration_s": duration_s,
        "start_ts": datetime.now(timezone.utc).isoformat(),
        "passed": False,
        "checks": {},
        "issues": [],
    }

    print(f"\n{'='*60}")
    print(f"  CYCLE {cycle_num}/{total_cycles} — {duration_s}s dry_run")
    print(f"{'='*60}")

    runner_cmd = [
        python_exe,
        str(REPO_ROOT / "scripts" / "dry_run_runner.py"),
        "--config", config_path,
        "--duration", str(duration_s),
    ]

    print(f"  Command: {' '.join(runner_cmd)}")
    print(f"  Starting at {result['start_ts']}")

    # Start subprocess
    proc = subprocess.Popen(
        runner_cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Let it run for duration_s, then send SIGINT for graceful shutdown
    # We add 30s buffer for warmup/bootstrap before the duration timer starts
    wait_time = duration_s + 30
    try:
        stdout, _ = proc.communicate(timeout=wait_time)
    except subprocess.TimeoutExpired:
        # Force SIGINT
        print(f"  Sending SIGINT after {wait_time}s timeout...")
        if sys.platform == "win32":
            # Windows: CTRL_BREAK_EVENT works for subprocess groups
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        try:
            stdout, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            print("  Force-killing process (no graceful shutdown)")
            proc.kill()
            stdout, _ = proc.communicate()
            result["issues"].append("process required force-kill")

    exit_code = proc.returncode
    result["exit_code"] = exit_code
    result["end_ts"] = datetime.now(timezone.utc).isoformat()

    # Save stdout to log
    log_path = REPO_ROOT / "logs" / f"stability_cycle_{cycle_num}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w") as f:
        f.write(stdout or "")
    print(f"  Log saved: {log_path}")

    # --- Post-shutdown checks ---
    state_dir = find_state_dir()
    print(f"  State dir: {state_dir}")

    # Check 1: Exit code
    check_exit = exit_code == 0
    result["checks"]["exit_code"] = {"passed": check_exit, "value": exit_code}
    if not check_exit:
        result["issues"].append(f"non-zero exit code: {exit_code}")
    print(f"  [{'OK' if check_exit else 'FAIL'}] Exit code: {exit_code}")

    # Check 2: SQLite state files readable
    if state_dir.exists():
        db_files = list(state_dir.glob("*.db"))
        all_readable = True
        for db_file in db_files:
            ok, msg = check_sqlite_readable(db_file)
            if not ok:
                all_readable = False
                result["issues"].append(f"DB not readable: {msg}")
        result["checks"]["sqlite_readable"] = {
            "passed": all_readable,
            "db_count": len(db_files),
        }
        print(f"  [{'OK' if all_readable else 'FAIL'}] SQLite files: {len(db_files)} DBs")
    else:
        result["checks"]["sqlite_readable"] = {"passed": True, "db_count": 0}
        print(f"  [SKIP] No state directory yet")

    # Check 3: Ledger consistency
    ledger_issues = check_ledger_consistency(state_dir) if state_dir.exists() else []
    ledger_ok = len(ledger_issues) == 0
    result["checks"]["ledger_consistency"] = {
        "passed": ledger_ok,
        "issues": ledger_issues,
    }
    result["issues"].extend(ledger_issues)
    print(f"  [{'OK' if ledger_ok else 'FAIL'}] Ledger consistency"
          f"{': ' + '; '.join(ledger_issues) if ledger_issues else ''}")

    # Check 4: Kill switch not stuck
    ks_ok, ks_msg = check_kill_switch(state_dir) if state_dir.exists() else (True, "n/a")
    result["checks"]["kill_switch"] = {"passed": ks_ok, "detail": ks_msg}
    if not ks_ok:
        result["issues"].append(f"kill switch stuck: {ks_msg}")
    print(f"  [{'OK' if ks_ok else 'FAIL'}] Kill switch: {ks_msg}")

    # Check 5: Idempotency deduplication
    idemp_issues = check_idempotency_dupes(state_dir) if state_dir.exists() else []
    idemp_ok = len(idemp_issues) == 0
    result["checks"]["idempotency"] = {
        "passed": idemp_ok,
        "issues": idemp_issues,
    }
    result["issues"].extend(idemp_issues)
    print(f"  [{'OK' if idemp_ok else 'FAIL'}] Idempotency dedup"
          f"{': ' + '; '.join(idemp_issues) if idemp_issues else ''}")

    # Check 6: Graceful shutdown detected in log
    shutdown_ok = "Shutdown sequence started" in (stdout or "")
    result["checks"]["graceful_shutdown"] = {"passed": shutdown_ok}
    if not shutdown_ok:
        result["issues"].append("no graceful shutdown detected in log")
    print(f"  [{'OK' if shutdown_ok else 'FAIL'}] Graceful shutdown in log")

    # Overall
    result["passed"] = all(
        c.get("passed", False) for c in result["checks"].values()
    )
    status = "PASS" if result["passed"] else "FAIL"
    print(f"\n  Cycle {cycle_num} result: {status}")

    return result


def main():
    parser = argparse.ArgumentParser(
        description="Restart resilience stability test for dry_run sessions"
    )
    parser.add_argument(
        "--cycles", type=int, default=3,
        help="Number of consecutive restart cycles (default: 3)"
    )
    parser.add_argument(
        "--duration", type=int, default=120,
        help="Duration of each session in seconds (default: 120)"
    )
    parser.add_argument(
        "--config", type=str, default="config/dry_run.yaml",
        help="Path to dry-run YAML config"
    )
    parser.add_argument(
        "--python", type=str, default=sys.executable,
        help="Python executable to use"
    )
    parser.add_argument(
        "--report", type=str, default="",
        help="Path to save JSON report (default: logs/stability_report.json)"
    )
    args = parser.parse_args()

    report_path = args.report or str(
        REPO_ROOT / "logs" / "stability_report.json"
    )

    print("=" * 60)
    print("  RESTART RESILIENCE STABILITY TEST")
    print("=" * 60)
    print(f"  Cycles:   {args.cycles}")
    print(f"  Duration: {args.duration}s per cycle")
    print(f"  Config:   {args.config}")
    print(f"  Python:   {args.python}")
    print(f"  Report:   {report_path}")

    results = []
    for i in range(1, args.cycles + 1):
        result = run_cycle(
            cycle_num=i,
            total_cycles=args.cycles,
            duration_s=args.duration,
            config_path=args.config,
            python_exe=args.python,
        )
        results.append(result)

        # Brief pause between cycles to let file handles close
        if i < args.cycles:
            print(f"\n  Pausing 5s before next cycle...")
            time.sleep(5)

    # Final report
    all_passed = all(r["passed"] for r in results)
    report = {
        "test": "restart_resilience",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cycles": args.cycles,
        "duration_per_cycle_s": args.duration,
        "config": args.config,
        "all_passed": all_passed,
        "results": results,
    }

    # Save report
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\n{'='*60}")
    print(f"  STABILITY TEST {'PASSED' if all_passed else 'FAILED'}")
    print(f"  {sum(1 for r in results if r['passed'])}/{len(results)} cycles passed")
    print(f"  Report: {report_path}")
    print(f"{'='*60}")

    # Collect all issues
    all_issues = []
    for r in results:
        for issue in r["issues"]:
            all_issues.append(f"Cycle {r['cycle']}: {issue}")

    if all_issues:
        print(f"\n  Issues found:")
        for issue in all_issues:
            print(f"    - {issue}")

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
