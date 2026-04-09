"""
Test de integración E2E del pipeline de trading completo.

Flujo cubierto:
    YAML -> Config -> Signal -> PositionSizer -> RiskGate -> OrderPlanner -> PaperEngine

Invariantes testeados:
- Happy path BUY: pipeline completo produce fill válido en PaperEngine.
- observe_only=True: señal no avanza al sizing/risk/planner (bloqueada en guardia).
- dry_run=True: orden va a PaperEngine, no a exchange live (live_fn nunca llamada).
- dry_run=False, observe_only=False: PaperEngine usado como destino de ejecución.
- RiskGate bloquea cuando drawdown excede límite → OrderNotAllowedError.
- validate_config() detecta invariantes cruzados inválidos.
- Config cargada desde YAML de prueba produce valores correctos.

No usa:
- Coinbase API real
- SQLite persistente (tmp_path)
- src.core.coinbase_exchange (sin requests)
"""

import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Optional

import pytest
import yaml

from src.config import Config, MonitoringConfig, PathsConfig, RiskConfig, TradingConfig
from src.execution.order_planner import (
    OrderIntent,
    OrderNotAllowedError,
    OrderPlanner,
    RiskDecisionInput,
)
from src.risk.gate import RiskDecision, RiskGate, RiskLimits, RiskSnapshot
from src.risk.position_sizer import (
    FailClosedError,
    PositionSizer,
    SizingDecision,
    SymbolConstraints,
)
from src.simulation.paper_engine import PaperEngine
from src.strategy.signal import make_signal

# ──────────────────────────────────────────────────────────────
# Constantes de prueba
# ──────────────────────────────────────────────────────────────

SYMBOL = "BTC-USD"
EQUITY = Decimal("10000")
ENTRY_PRICE = Decimal("50000")
BID = Decimal("49900")
ASK = Decimal("50100")

BTC_CONSTRAINTS = SymbolConstraints(
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    max_qty=Decimal("100"),
    min_notional=Decimal("1"),
)


# ──────────────────────────────────────────────────────────────
# Helpers de construcción
# ──────────────────────────────────────────────────────────────


def make_test_config(
    *,
    dry_run: bool = False,
    observe_only: bool = False,
    max_position_pct: float = 0.20,
    max_notional_per_symbol: float = 10000.0,
    max_orders_per_minute: int = 100,
    risk_per_trade_pct: float = 0.01,
    max_daily_loss: float = 0.05,
    max_drawdown: float = 0.15,
) -> tuple[TradingConfig, RiskConfig]:
    """Construye TradingConfig + RiskConfig para tests sin YAML ni paths."""
    trading = TradingConfig(
        dry_run=dry_run,
        observe_only=observe_only,
        max_position_pct=max_position_pct,
        max_notional_per_symbol=max_notional_per_symbol,
        max_orders_per_minute=max_orders_per_minute,
        risk_per_trade_pct=risk_per_trade_pct,
    )
    risk = RiskConfig(
        max_daily_loss=max_daily_loss,
        max_drawdown=max_drawdown,
        max_consecutive_losses=3,
        max_position_pct=max_position_pct,
    )
    return trading, risk


def make_risk_limits(trading: TradingConfig, risk: RiskConfig) -> RiskLimits:
    return RiskLimits(
        max_position_pct=Decimal(str(risk.max_position_pct)),
        max_notional_per_symbol=Decimal(str(trading.max_notional_per_symbol)),
        max_orders_per_minute=trading.max_orders_per_minute,
        max_daily_loss_pct=Decimal(str(risk.max_daily_loss)),
        max_drawdown_pct=Decimal(str(risk.max_drawdown)),
    )


def make_snapshot(
    *,
    equity: str = "10000",
    position_qty: str = "0",
    day_pnl_pct: str = "0",
    drawdown_pct: str = "0",
) -> RiskSnapshot:
    return RiskSnapshot(
        equity=Decimal(equity),
        position_qty=Decimal(position_qty),
        day_pnl_pct=Decimal(day_pnl_pct),
        drawdown_pct=Decimal(drawdown_pct),
    )


def make_btc_signal(direction: str = "BUY") -> object:
    return make_signal(
        symbol=SYMBOL,
        direction=direction,
        strength=Decimal("0.8"),
        strategy_id="e2e_test_strategy",
        bar_timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def intent_to_paper_dict(intent: OrderIntent) -> dict:
    """Convierte OrderIntent al dict que acepta PaperEngine.submit_order()."""
    return {
        "client_id": intent.client_order_id,
        "symbol": intent.symbol,
        "side": intent.side.lower(),
        "type": intent.order_type.lower(),
        "amount": str(intent.final_qty),
        "price": str(intent.price) if intent.price is not None else None,
        "reduce_only": intent.reduce_only,
    }


# ──────────────────────────────────────────────────────────────
# Orquestador de pipeline (documenta el contrato E2E)
# ──────────────────────────────────────────────────────────────


@dataclass
class PipelineResult:
    """Resultado del pipeline E2E."""

    blocked_by: Optional[str]  # None si pasó completo
    sizing: Optional[SizingDecision] = None
    risk_decision: Optional[RiskDecision] = None
    intent: Optional[OrderIntent] = None
    paper_result: Optional[dict] = None
    live_called: bool = False


def run_pipeline(
    *,
    trading: TradingConfig,
    risk: RiskConfig,
    signal,
    equity: Decimal,
    position_qty: Decimal,
    entry_price: Decimal,
    constraints: SymbolConstraints,
    paper_engine: Optional[PaperEngine] = None,
    live_exchange_fn: Optional[Callable[[OrderIntent], dict]] = None,
    bid: Decimal = BID,
    ask: Decimal = ASK,
    day_pnl_pct: Decimal = Decimal("0"),
    drawdown_pct: Decimal = Decimal("0"),
) -> PipelineResult:
    """
    Ejecuta el pipeline completo respetando observe_only y dry_run.

    Guardia 1 (observe_only): si True, señal no avanza. No se genera orden.
    Guardia 2 (dry_run):      si True, usa PaperEngine. live_exchange_fn nunca se llama.
    """
    # Guardia observe_only: solo observar, no operar
    if trading.observe_only:
        return PipelineResult(blocked_by="observe_only")

    # Paso 1: PositionSizer
    sizer = PositionSizer()
    sizing = sizer.compute(
        symbol=signal.symbol,
        equity=equity,
        entry_price=entry_price,
        risk_per_trade_pct=Decimal(str(trading.risk_per_trade_pct)),
        constraints=constraints,
        max_notional=Decimal(str(trading.max_notional_per_symbol)),
    )

    if sizing.target_qty <= Decimal("0"):
        return PipelineResult(blocked_by="sizing_zero", sizing=sizing)

    # Paso 2: RiskGate
    limits = make_risk_limits(trading, risk)
    gate = RiskGate(limits)
    snapshot = RiskSnapshot(
        equity=equity,
        position_qty=position_qty,
        day_pnl_pct=day_pnl_pct,
        drawdown_pct=drawdown_pct,
    )
    risk_decision = gate.evaluate(
        symbol=signal.symbol,
        side=signal.direction,
        snapshot=snapshot,
        target_qty=sizing.target_qty,
        entry_ref=entry_price,
    )

    if not risk_decision.allowed:
        return PipelineResult(
            blocked_by="risk_gate",
            sizing=sizing,
            risk_decision=risk_decision,
        )

    # Paso 3: OrderPlanner
    planner = OrderPlanner()
    risk_input = RiskDecisionInput(
        allowed=risk_decision.allowed,
        hard_max_qty=risk_decision.hard_max_qty,
        hard_max_notional=risk_decision.hard_max_notional,
        reduce_only=risk_decision.reduce_only,
        reason=risk_decision.reason,
    )
    intent = planner.plan(
        signal_id=signal.signal_id,
        strategy_id=signal.strategy_id,
        symbol=signal.symbol,
        side=signal.direction,
        sizing=sizing,
        risk=risk_input,
        constraints=constraints,
    )

    if not intent.viable:
        return PipelineResult(
            blocked_by="intent_not_viable",
            sizing=sizing,
            risk_decision=risk_decision,
            intent=intent,
        )

    # Paso 4: Ejecución — dry_run decide el destino
    if trading.dry_run:
        # Ejecución simulada (paper) — live_exchange_fn nunca se llama
        engine = paper_engine or PaperEngine()
        paper_result = engine.submit_order(
            intent=intent_to_paper_dict(intent),
            bid=bid,
            ask=ask,
        )
        return PipelineResult(
            blocked_by=None,
            sizing=sizing,
            risk_decision=risk_decision,
            intent=intent,
            paper_result=paper_result,
            live_called=False,
        )
    else:
        # Ejecución live — en producción sería OrderExecutor.submit()
        # En tests este path se verifica con un callable mock
        if live_exchange_fn is not None:
            live_result = live_exchange_fn(intent)
            return PipelineResult(
                blocked_by=None,
                sizing=sizing,
                risk_decision=risk_decision,
                intent=intent,
                paper_result=live_result,
                live_called=True,
            )
        # Sin live_fn en tests: usar paper como fallback para dry_run=False con engine
        engine = paper_engine or PaperEngine()
        paper_result = engine.submit_order(
            intent=intent_to_paper_dict(intent),
            bid=bid,
            ask=ask,
        )
        return PipelineResult(
            blocked_by=None,
            sizing=sizing,
            risk_decision=risk_decision,
            intent=intent,
            paper_result=paper_result,
            live_called=False,
        )


# ──────────────────────────────────────────────────────────────
# Test: Config desde YAML de prueba
# ──────────────────────────────────────────────────────────────


class TestConfigFromYaml:
    """Verifica que Config se carga correctamente desde YAML de prueba."""

    def _make_config_with_yaml(self, yaml_data: dict) -> Config:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "configs").mkdir()
            (repo / "configs" / "symbols.yaml").write_text(yaml.dump(yaml_data))
            paths = PathsConfig(
                repo=repo,
                runtime=repo / "runtime",
                secrets=repo / "secrets",
            )
            paths.ensure_directories()
            cfg = Config.__new__(Config)
            cfg.paths = paths
            cfg.coinbase = Config.__dataclass_fields__["coinbase"].default_factory()
            cfg.trading = TradingConfig()
            cfg.risk = RiskConfig()
            cfg.monitoring = MonitoringConfig()
            cfg.symbols = []
            cfg._load_yaml_config()
            return cfg

    def test_trading_values_loaded_from_yaml(self):
        data = {
            "trading": {
                "dry_run": False,
                "observe_only": False,
                "max_position_pct": 0.15,
                "max_notional_per_symbol": 5000.0,
                "max_orders_per_minute": 5,
                "risk_per_trade_pct": 0.02,
            },
        }
        cfg = self._make_config_with_yaml(data)
        assert cfg.trading.dry_run is False
        assert cfg.trading.observe_only is False
        assert cfg.trading.max_position_pct == 0.15
        assert cfg.trading.max_notional_per_symbol == 5000.0
        assert cfg.trading.max_orders_per_minute == 5
        assert cfg.trading.risk_per_trade_pct == 0.02

    def test_risk_values_loaded_from_yaml(self):
        data = {
            "risk": {
                "max_daily_loss": 0.03,
                "max_drawdown": 0.10,
                "max_consecutive_losses": 2,
            },
        }
        cfg = self._make_config_with_yaml(data)
        assert cfg.risk.max_daily_loss == 0.03
        assert cfg.risk.max_drawdown == 0.10
        assert cfg.risk.max_consecutive_losses == 2

    def test_symbols_loaded_from_yaml(self):
        data = {
            "symbols": [
                {"symbol": "BTC-USD", "enabled": True, "timeframe": "1h"},
                {"symbol": "ETH-USD", "enabled": False, "timeframe": "4h"},
            ]
        }
        cfg = self._make_config_with_yaml(data)
        assert len(cfg.symbols) == 2
        assert cfg.symbols[0].symbol == "BTC-USD"
        assert cfg.symbols[1].symbol == "ETH-USD"
        assert cfg.symbols[1].enabled is False


# ──────────────────────────────────────────────────────────────
# Test: Happy path E2E completo
# ──────────────────────────────────────────────────────────────


class TestPipelineE2EHappyPath:

    def test_full_pipeline_buy_produces_fill(self):
        """BUY con dry_run=False, observe_only=False → fill en PaperEngine."""
        trading, risk = make_test_config(dry_run=False, observe_only=False)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        assert result.blocked_by is None
        assert result.sizing is not None
        assert result.sizing.target_qty > Decimal("0")
        assert result.risk_decision is not None
        assert result.risk_decision.allowed is True
        assert result.intent is not None
        assert result.intent.viable is True
        assert result.intent.side == "BUY"
        assert result.intent.final_qty > Decimal("0")
        assert result.paper_result is not None
        assert result.paper_result["status"] == "filled"

    def test_full_pipeline_sizing_respects_risk_per_trade_pct(self):
        """target_qty se calcula correctamente desde risk_per_trade_pct y equity."""
        trading, risk = make_test_config(dry_run=False, observe_only=False, risk_per_trade_pct=0.01)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        assert result.blocked_by is None
        # risk_amount = 10000 * 0.01 = 100 USD
        # raw_qty = 100 / 50000 = 0.002 BTC
        # quantized by step_size=0.001 → 0.002
        assert result.sizing.target_qty == Decimal("0.002")

    def test_intent_client_order_id_is_deterministic(self):
        """Mismo signal_id + symbol → mismo client_order_id (idempotencia)."""
        trading, risk = make_test_config(dry_run=False, observe_only=False)
        signal = make_btc_signal("BUY")

        result1 = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )
        result2 = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,  # mismo signal → mismo signal_id
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        assert result1.intent.client_order_id == result2.intent.client_order_id

    def test_paper_engine_fill_amount_matches_intent_final_qty(self):
        """El fill del PaperEngine refleja el final_qty del OrderIntent."""
        trading, risk = make_test_config(dry_run=False, observe_only=False)
        signal = make_btc_signal("BUY")
        engine = PaperEngine(taker_fee=Decimal("0"))  # fee=0 para simplificar asserts

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
            paper_engine=engine,
        )

        assert result.paper_result["status"] == "filled"
        fill = result.paper_result["fill"]
        assert fill.amount == result.intent.final_qty


# ──────────────────────────────────────────────────────────────
# Test: observe_only bloquea ejecución real
# ──────────────────────────────────────────────────────────────


class TestObserveOnlyBlocks:

    def test_observe_only_true_blocks_pipeline(self):
        """observe_only=True → pipeline no avanza más allá de la guardia."""
        trading, risk = make_test_config(observe_only=True)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        assert result.blocked_by == "observe_only"
        assert result.sizing is None
        assert result.risk_decision is None
        assert result.intent is None
        assert result.paper_result is None

    def test_observe_only_true_does_not_call_paper_engine(self):
        """observe_only=True → PaperEngine nunca recibe orden."""
        trading, risk = make_test_config(observe_only=True)
        signal = make_btc_signal("BUY")
        engine = PaperEngine()

        run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
            paper_engine=engine,
        )

        # Ninguna orden abierta ni fill procesado
        assert len(engine.open_orders) == 0

    def test_observe_only_false_allows_pipeline(self):
        """observe_only=False → pipeline continúa y produce fill."""
        trading, risk = make_test_config(observe_only=False, dry_run=False)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        assert result.blocked_by is None
        assert result.paper_result is not None


# ──────────────────────────────────────────────────────────────
# Test: dry_run bloquea ejecución live
# ──────────────────────────────────────────────────────────────


class TestDryRunBlocks:

    def test_dry_run_true_never_calls_live_exchange(self):
        """dry_run=True → live_exchange_fn nunca se invoca."""
        trading, risk = make_test_config(dry_run=True, observe_only=False)
        signal = make_btc_signal("BUY")
        live_called = []

        def live_exchange_fn(intent: OrderIntent) -> dict:
            live_called.append(intent)
            return {"status": "sent_live"}

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
            live_exchange_fn=live_exchange_fn,
        )

        assert len(live_called) == 0, "live_exchange_fn was called but dry_run=True"
        assert result.live_called is False
        assert result.paper_result is not None
        assert result.paper_result["status"] == "filled"

    def test_dry_run_false_calls_live_exchange(self):
        """dry_run=False con live_fn → live_exchange_fn se invoca exactamente una vez."""
        trading, risk = make_test_config(dry_run=False, observe_only=False)
        signal = make_btc_signal("BUY")
        live_calls = []

        def live_exchange_fn(intent: OrderIntent) -> dict:
            live_calls.append(intent)
            return {"status": "ack_live"}

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
            live_exchange_fn=live_exchange_fn,
        )

        assert len(live_calls) == 1
        assert result.live_called is True
        assert result.paper_result == {"status": "ack_live"}

    def test_dry_run_true_and_observe_only_true_both_block(self):
        """dry_run=True + observe_only=True → observe_only toma precedencia."""
        trading, risk = make_test_config(dry_run=True, observe_only=True)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        # observe_only se evalúa primero en el pipeline
        assert result.blocked_by == "observe_only"
        assert result.paper_result is None


# ──────────────────────────────────────────────────────────────
# Test: RiskGate bloquea y OrderPlanner lanza error
# ──────────────────────────────────────────────────────────────


class TestRiskGateBlocking:

    def test_drawdown_exceeded_blocks_pipeline(self):
        """drawdown >= max_drawdown → pipeline bloqueado en risk_gate."""
        trading, risk = make_test_config(dry_run=False, observe_only=False, max_drawdown=0.10)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
            drawdown_pct=Decimal("0.12"),  # excede 0.10
        )

        assert result.blocked_by == "risk_gate"
        assert result.risk_decision is not None
        assert result.risk_decision.allowed is False
        assert "MAX_DRAWDOWN" in result.risk_decision.blocking_rule_ids
        assert result.intent is None
        assert result.paper_result is None

    def test_daily_loss_exceeded_blocks_pipeline(self):
        """day_pnl_pct <= -max_daily_loss → bloqueado en risk_gate."""
        trading, risk = make_test_config(dry_run=False, observe_only=False, max_daily_loss=0.05)
        signal = make_btc_signal("BUY")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
            day_pnl_pct=Decimal("-0.05"),  # alcanza el límite
        )

        assert result.blocked_by == "risk_gate"
        assert "DAILY_LOSS_LIMIT" in result.risk_decision.blocking_rule_ids

    def test_sell_without_position_blocked_by_risk_gate(self):
        """SELL sin posición → bloqueado con SELL_NO_POSITION."""
        trading, risk = make_test_config(dry_run=False, observe_only=False)
        signal = make_btc_signal("SELL")

        result = run_pipeline(
            trading=trading,
            risk=risk,
            signal=signal,
            equity=EQUITY,
            position_qty=Decimal("0"),  # sin posición
            entry_price=ENTRY_PRICE,
            constraints=BTC_CONSTRAINTS,
        )

        assert result.blocked_by == "risk_gate"
        assert "SELL_NO_POSITION" in result.risk_decision.blocking_rule_ids

    def test_order_planner_raises_when_risk_not_allowed(self):
        """OrderPlanner.plan() lanza OrderNotAllowedError si risk.allowed=False."""
        planner = OrderPlanner()
        sizer = PositionSizer()

        sizing = sizer.compute(
            symbol=SYMBOL,
            equity=EQUITY,
            entry_price=ENTRY_PRICE,
            risk_per_trade_pct=Decimal("0.01"),
            constraints=BTC_CONSTRAINTS,
            max_notional=Decimal("10000"),
        )

        risk_blocked = RiskDecisionInput(
            allowed=False,
            hard_max_qty=Decimal("0"),
            hard_max_notional=Decimal("0"),
            reduce_only=False,
            reason="DAILY_LOSS_LIMIT",
        )

        with pytest.raises(OrderNotAllowedError):
            planner.plan(
                signal_id="test-signal-001",
                strategy_id="test_strategy",
                symbol=SYMBOL,
                side="BUY",
                sizing=sizing,
                risk=risk_blocked,
                constraints=BTC_CONSTRAINTS,
            )


# ──────────────────────────────────────────────────────────────
# Test: PositionSizer fail-closed
# ──────────────────────────────────────────────────────────────


class TestPositionSizerFailClosed:

    def test_equity_none_raises_fail_closed_error(self):
        """equity=None → FailClosedError (no defaults inventados)."""
        sizer = PositionSizer()
        with pytest.raises(FailClosedError):
            sizer.compute(
                symbol=SYMBOL,
                equity=None,
                entry_price=ENTRY_PRICE,
                risk_per_trade_pct=Decimal("0.01"),
                constraints=BTC_CONSTRAINTS,
                max_notional=Decimal("10000"),
            )

    def test_equity_zero_returns_zero_qty(self):
        """equity=0 → SizingDecision con target_qty=0, sin error."""
        sizer = PositionSizer()
        result = sizer.compute(
            symbol=SYMBOL,
            equity=Decimal("0"),
            entry_price=ENTRY_PRICE,
            risk_per_trade_pct=Decimal("0.01"),
            constraints=BTC_CONSTRAINTS,
            max_notional=Decimal("10000"),
        )
        assert result.target_qty == Decimal("0")


# ──────────────────────────────────────────────────────────────
# Test: validate_config detecta invariantes cruzados
# ──────────────────────────────────────────────────────────────


class TestValidateConfigCrossInvariants:

    def _make_bare_config(self, trading: TradingConfig, risk: RiskConfig) -> Config:
        """Config sin paths reales — solo para validate_config."""
        cfg = Config.__new__(Config)
        cfg.paths = PathsConfig.__new__(PathsConfig)
        cfg.trading = trading
        cfg.risk = risk
        cfg.monitoring = MonitoringConfig()
        cfg.symbols = []
        return cfg

    def test_valid_config_does_not_raise(self):
        trading, risk = make_test_config()
        cfg = self._make_bare_config(trading, risk)
        cfg.validate_config()  # no debe lanzar

    def test_daily_loss_greater_than_drawdown_raises(self):
        trading, risk = make_test_config(max_daily_loss=0.20, max_drawdown=0.10)
        cfg = self._make_bare_config(trading, risk)
        with pytest.raises(ValueError, match="max_daily_loss"):
            cfg.validate_config()

    def test_risk_position_pct_zero_raises(self):
        trading = TradingConfig(max_position_pct=0.20)
        risk = RiskConfig(max_position_pct=0.0)
        cfg = self._make_bare_config(trading, risk)
        with pytest.raises(ValueError, match="risk.max_position_pct"):
            cfg.validate_config()

    def test_trading_position_pct_above_one_raises(self):
        trading = TradingConfig(max_position_pct=1.5)
        risk = RiskConfig(max_position_pct=0.20)
        cfg = self._make_bare_config(trading, risk)
        with pytest.raises(ValueError, match="trading.max_position_pct"):
            cfg.validate_config()
