FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml setup.cfg* setup.py* ./
COPY src/ src/
COPY configs/ configs/
COPY tests/ tests/
COPY scripts/ scripts/

RUN pip install --no-cache-dir -e ".[dev]"

# Health check: verify core imports + config validation
RUN python -c "\
from src.risk.gate import RiskGate; \
from src.oms.reconcile import OMSReconcileService; \
from src.execution.order_planner import OrderPlanner; \
from src.monitoring.alert_manager import AlertManager; \
from src.credentials import load_credentials; \
print('All imports OK')"

RUN python -m src.config_validator configs/symbols.yaml

# Default: run full test suite
CMD ["python", "-m", "pytest", "tests/", "-q", "--tb=short", "-x"]
