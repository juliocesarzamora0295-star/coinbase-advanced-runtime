#!/bin/bash
# Build smoke test — verifies the project installs cleanly in a fresh venv
# and all critical imports work.
set -euo pipefail

echo "=== Build Smoke Test ==="

VENV_DIR=$(mktemp -d)/venv
echo "[1/4] Creating clean virtualenv at $VENV_DIR..."
python -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

echo "[2/4] Installing project..."
pip install --quiet -e ".[dev]"

echo "[3/4] Verifying critical imports..."
python -c "
from src.main import TradingBot
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot, RiskVerdict
from src.risk.circuit_breaker import CircuitBreaker, BreakerConfig
from src.risk.kill_switch import KillSwitch, KillSwitchMode
from src.risk.position_sizer import PositionSizer, SizingMode
from src.execution.order_planner import OrderPlanner, OrderIntent
from src.execution.execution_report import build_execution_report
from src.execution.pending_store import PendingReportStore
from src.execution.twap import TWAPExecutor
from src.oms.reconcile import OMSReconcileService
from src.accounting.ledger import TradeLedger
from src.observability import get_collector
from src.observability.json_sink import JSONLineSink
from src.backtest.engine import BacktestEngine
from src.quantitative.metrics import compute_metrics
from src.strategy.examples.momentum import momentum_strategy
print('All imports OK')
"

echo "[4/4] Verifying compileall..."
python -m compileall -q src tests

deactivate
rm -rf "$(dirname $VENV_DIR)"

echo "=== Build Smoke PASSED ==="
