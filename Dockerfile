FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml setup.cfg* setup.py* ./
COPY src/ src/
COPY configs/ configs/

RUN pip install --no-cache-dir -e ".[dev]"

# Health check: verify core imports
RUN python -c "from src.risk.gate import RiskGate; from src.oms.reconcile import OMSReconcileService; print('OK')"

# Default: run smoke test
CMD ["python", "-m", "pytest", "tests/", "-q", "--tb=short", "-x"]
