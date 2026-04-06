#!/bin/bash
# CI smoke test — verifies the project builds and core imports work.
# Exit on first error.
set -euo pipefail

echo "=== CI Smoke Test ==="

echo "[1/3] Installing project..."
pip install -e ".[dev]" --quiet

echo "[2/3] Compile check..."
python -m compileall -q src tests

echo "[3/3] Core import check..."
python -c "
from src.accounting.ledger import TradeLedger
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskVerdict
from src.risk.circuit_breaker import CircuitBreaker, BreakerConfig
from src.risk.kill_switch import KillSwitch, KillSwitchMode
from src.risk.position_sizer import PositionSizer, SizingMode
from src.execution.order_planner import OrderPlanner, OrderIntent
from src.execution.execution_report import build_execution_report
from src.oms.reconcile import OMSReconcileService
from src.observability import get_collector
from src.backtest.engine import BacktestEngine
print('All core imports OK')
"

echo "=== Smoke test PASSED ==="
