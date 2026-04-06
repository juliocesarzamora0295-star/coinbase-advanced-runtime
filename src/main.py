"""
Fortress v4 - Coinbase Advanced Trade Edition

Entry point del bot de trading.
"""

import logging
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

import pandas as pd

from src.accounting.ledger import TradeLedger
from src.config import PathsConfig, get_config
from src.core.coinbase_exchange import CoinbaseRESTClient
from src.core.coinbase_websocket import CoinbaseWSFeed, WSMessage
from src.core.jwt_auth import JWTAuth, load_credentials_from_env
from src.core.quantization import create_quantizer_from_api_response
from src.execution.idempotency import IdempotencyStore
from src.execution.order_planner import OrderIntent, OrderNotAllowedError, OrderPlanner, RiskDecisionInput
from src.execution.orders import OrderExecutor
from src.marketdata.orderbook import OrderBook
from src.marketdata.service import MarketDataService
from src.oms.reconcile import OMSReconcileService
from src.risk.circuit_breaker import BreakerConfig, CircuitBreaker
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot
from src.risk.position_sizer import FailClosedError, PositionSizer, SymbolConstraints
from src.simulation.paper_engine import PaperEngine
from src.strategy.manager import StrategyManager

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("Main")


def setup_file_logging(logs_path: PathsConfig) -> None:
    """Configurar logging a archivo."""
    from logging.handlers import RotatingFileHandler

    file_handler = RotatingFileHandler(
        logs_path.logs / "fortress.log",
        maxBytes=10_000_000,  # 10MB
        backupCount=5,
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    )

    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)


class TradingBot:
    """Bot de trading principal."""

    def __init__(self):
        self.config = get_config()
        self.client: CoinbaseRESTClient = None
        self.jwt_auth: JWTAuth = None
        self.ws: CoinbaseWSFeed = None
        self.circuit_breaker: CircuitBreaker = None

        # Por símbolo
        self.executors: Dict[str, OrderExecutor] = {}
        self.ledgers: Dict[str, TradeLedger] = {}
        self.quantizers: Dict[str, any] = {}
        self.oms_services: Dict[str, OMSReconcileService] = {}

        # Market data y señales
        self.price_data: Dict[str, List[Dict]] = {}
        self.current_prices: Dict[str, Decimal] = {}
        self.market_data = MarketDataService()
        self.strategy_managers: Dict[str, StrategyManager] = {}  # Uno por símbolo
        self.order_books: Dict[str, OrderBook] = {}  # Uno por símbolo

        # Risk Gate (integrado en pipeline)
        self.risk_gate: RiskGate = None
        self.position_sizer = PositionSizer()
        self.order_planner = OrderPlanner()

        # Paper Engine (simulación)
        self.paper_engine: Optional[PaperEngine] = None

        # Modo smoke test - desde config YAML
        self.smoke_test_mode: bool = self.config.trading.smoke_test_mode
        self.cycle_count: int = 0

        # Control
        self._running = False
        self._lock = threading.Lock()

    def initialize(self) -> bool:
        """Inicializar el bot."""
        logger.info("=" * 60)
        logger.info("Fortress v4.0 - Coinbase Advanced Trade Integration")
        logger.info("=" * 60)
        logger.info("")
        logger.info("ESTADO: Esqueleto de integración (NO es un bot completo)")
        logger.info("- API Client: OK")
        logger.info("- WebSocket: OK")
        logger.info("- Ledger: OK")
        logger.info("- Idempotency: OK")
        logger.info("- Strategy Layer: StrategyManager v3 pipeline")
        logger.info("- OMS Reconciliation: PARCIAL")
        logger.info("")
        logger.info("El bot opera en modo OBSERVACIÓN (no ejecuta órdenes)")
        logger.info("")

        # Verificar credenciales
        if not self.config.coinbase.is_configured:
            logger.error("COINBASE_KEY_NAME y COINBASE_KEY_SECRET son requeridos")
            return False

        logger.info(f"Key Name: {self.config.coinbase.key_name[:50]}...")
        logger.info(f"JWT Issuer: {self.config.coinbase.issuer}")

        # Crear JWT Auth
        try:
            credentials = load_credentials_from_env()
            self.jwt_auth = JWTAuth(
                credentials,
                issuer=self.config.coinbase.issuer,
            )
            logger.info("JWT Auth initialized")
        except Exception as e:
            logger.error(f"Failed to initialize JWT Auth: {e}")
            return False

        # Crear cliente REST
        try:
            self.client = CoinbaseRESTClient(
                self.jwt_auth,
                timeout=self.config.coinbase.timeout,
                max_retries=self.config.coinbase.max_retries,
            )
            logger.info("REST Client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize REST Client: {e}")
            return False

        # Verificar conexión
        try:
            accounts = self.client.list_accounts()
            logger.info(f"Connected to Coinbase. Accounts: {len(accounts)}")

            for account in accounts[:3]:
                currency = account.get("currency", "")
                balance = account.get("available_balance", {}).get("value", "0")
                logger.info(f"   {currency}: {balance}")
        except Exception as e:
            logger.error(f"Failed to connect to Coinbase: {e}")
            return False

        # Obtener fees
        try:
            fees = self.client.get_transaction_summary()
            fee_tier = fees.get("fee_tier", {})
            maker = fee_tier.get("maker_fee_rate", "N/A")
            taker = fee_tier.get("taker_fee_rate", "N/A")
            logger.info(f"Fee Tier: Maker={maker}, Taker={taker}")
        except Exception as e:
            logger.warning(f"Could not fetch fees: {e}")

        # Inicializar circuit breaker
        breaker_cfg = BreakerConfig(
            max_daily_loss=self.config.risk.max_daily_loss,
            max_drawdown=self.config.risk.max_drawdown,
            max_consecutive_losses=self.config.risk.max_consecutive_losses,
            latency_p95_threshold_ms=self.config.monitoring.latency_p95_threshold_ms,
            reject_rate_threshold=self.config.monitoring.reject_rate_threshold,
            slippage_drift_threshold_bps=self.config.monitoring.slippage_drift_threshold_bps,
        )
        self.circuit_breaker = CircuitBreaker(breaker_cfg)
        logger.info("Circuit Breaker initialized")

        # Inicializar Risk Gate (integrado en pipeline) - desde config YAML
        risk_limits = RiskLimits(
            max_position_pct=Decimal(str(self.config.trading.max_position_pct)),
            max_notional_per_symbol=Decimal(str(self.config.trading.max_notional_per_symbol)),
            max_orders_per_minute=self.config.trading.max_orders_per_minute,
            max_daily_loss_pct=Decimal(str(self.config.risk.max_daily_loss)),
            max_drawdown_pct=Decimal(str(self.config.risk.max_drawdown)),
        )
        self.risk_gate = RiskGate(risk_limits)
        logger.info(f"Risk Gate initialized: max_position={risk_limits.max_position_pct}%")

        # Inicializar Paper Engine para simulación
        if self.config.trading.dry_run or self.config.trading.observe_only:
            self.paper_engine = PaperEngine(
                maker_fee=Decimal("0.0002"),
                taker_fee=Decimal("0.0004"),
            )
            logger.info("Paper Engine initialized for simulation")

        # Inicializar componentes por símbolo
        for symbol_cfg in self.config.symbols:
            if not symbol_cfg.enabled:
                continue

            symbol = symbol_cfg.symbol
            logger.info(f"Initializing {symbol}...")

            try:
                # Obtener info del producto
                product = self.client.get_product(symbol)
                quantizer = create_quantizer_from_api_response(product)
                self.quantizers[symbol] = quantizer

                # Crear ledger con callback al circuit breaker
                db_path = str(self.config.paths.state / f"ledger_{symbol}.db")
                ledger = TradeLedger(
                    symbol,
                    db_path=db_path,
                    on_fill_callback=self.circuit_breaker.get_fill_callback(),
                )
                self.ledgers[symbol] = ledger

                # Inicializar circuit breaker con equity
                if ledger.position_qty > 0:
                    position_value = ledger.position_qty * ledger.avg_entry
                    self.circuit_breaker.reset_day(position_value)

                logger.info(
                    f"   Position: {ledger.position_qty} "
                    f"| Avg Entry: {ledger.avg_entry} "
                    f"| Realized PnL: {ledger.realized_pnl_quote}"
                )

                # Crear idempotency store y executor
                idempotency_db = str(self.config.paths.state / f"idempotency_{symbol}.db")
                idempotency = IdempotencyStore(db_path=idempotency_db)
                executor = OrderExecutor(
                    self.client,
                    idempotency,
                    quantizer,
                    max_retries=self.config.coinbase.max_retries,
                )
                self.executors[symbol] = executor

                # Crear OMS reconcile service con REST fill fetcher
                # on_degraded: abrir circuit breaker cuando OMS detecta divergencia
                def _make_degraded_handler(sym: str):
                    def handler(incident):
                        logger.error(
                            "[%s] OMS DEGRADED: %s — opening circuit breaker",
                            sym, incident.detail,
                        )
                        if self.circuit_breaker:
                            self.circuit_breaker.force_open(
                                f"OMS degraded: {incident.incident_type}"
                            )
                    return handler

                oms = OMSReconcileService(
                    idempotency=idempotency,
                    ledger=ledger,
                    fill_fetcher=lambda order_id: self.client.list_fills(order_id=order_id),
                    on_bootstrap_complete=lambda: logger.info(
                        f"[{symbol}] OMS bootstrap complete — trading enabled"
                    ),
                    on_degraded=_make_degraded_handler(symbol),
                )
                self.oms_services[symbol] = oms

                # Inicializar buffer de precios y order book
                self.price_data[symbol] = []
                self.current_prices[symbol] = Decimal("0")
                self.order_books[symbol] = OrderBook(symbol)

            except Exception as e:
                logger.error(f"Failed to initialize {symbol}: {e}")
                continue

        if not self.executors:
            logger.error("No symbols initialized. Exiting.")
            return False

        # Inicializar WebSocket
        self._init_websocket()

        # Inicializar pipeline: CandleClosed -> Signal -> RiskGate -> Executor
        self._init_pipeline()

        return True

    def _init_pipeline(self) -> None:
        """
        Inicializar pipeline v3:
        CandleClosed -> StrategyManager -> PositionSizer -> RiskGate -> OrderPlanner -> Executor
        """
        logger.info("Initializing trading pipeline v3...")

        for symbol_cfg in self.config.symbols:
            if not symbol_cfg.enabled:
                continue

            symbol = symbol_cfg.symbol
            timeframe = symbol_cfg.timeframe

            # Registrar símbolo con timeframe en MarketDataService
            self.market_data.register_symbol(symbol, timeframe)

            # Crear StrategyManager por símbolo desde config
            try:
                sm = StrategyManager.load_from_config(
                    symbol=symbol,
                    symbol_config={"strategies": symbol_cfg.strategies},
                )
                self.strategy_managers[symbol] = sm
                logger.info(f"[{symbol}] StrategyManager loaded: {sm.strategy_count} strategy(s)")
            except ValueError as exc:
                logger.warning(f"[{symbol}] StrategyManager not loaded: {exc}")

            # Suscribir callback a CandleClosed events
            self.market_data.subscribe(
                symbol,
                lambda candle, sym=symbol: self._on_candle_closed(sym, candle),
            )

        logger.info("Trading pipeline v3 initialized")

    def _on_candle_closed(self, symbol: str, candle) -> None:
        """
        Callback para eventos CandleClosed.

        Pipeline v3:
        1. StrategyManager.on_candle_closed() → Signal | None
        2. _process_signal_with_risk() → PositionSizer → RiskGate → OrderPlanner → Executor
        """
        sm = self.strategy_managers.get(symbol)
        if not sm:
            return

        import pandas as pd

        candle_series = pd.Series(
            {
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
        )
        bar_ts = datetime.fromtimestamp(candle.timestamp_ms / 1000, tz=timezone.utc)

        signal = sm.on_candle_closed(candle_series, mid=candle.close, bar_timestamp=bar_ts)
        if signal is None:
            return

        self._process_signal_with_risk(symbol, signal, candle)

    def _process_signal_with_risk(self, symbol: str, signal, candle) -> None:
        """
        Procesar señal a través del pipeline v3.

        Signal → OMS readiness check → PositionSizer → RiskGate → OrderPlanner → Executor.
        Fail-closed: cualquier input faltante bloquea trading y loggea motivo.
        """
        # OMS readiness gate — no trading con OMS incompleta o degradada
        oms = self.oms_services.get(symbol)
        if oms and not oms.is_ready():
            stats = oms.get_stats()
            logger.warning(
                f"[{symbol}] Signal BLOCKED: OMS not ready "
                f"(bootstrap={stats['bootstrap_complete']}, "
                f"degraded={stats['degraded']})"
            )
            return

        side = signal.direction  # src.strategy.signal.Signal: "BUY" | "SELL"

        # Estado del circuit breaker como input para RiskGate
        breaker_state = self.circuit_breaker.get_status()["state"].upper()

        # Obtener ledger (fail-closed si no existe)
        ledger = self.ledgers.get(symbol)
        if not ledger:
            logger.error(f"[{symbol}] No ledger found — blocking trading")
            return

        current_price = self.current_prices.get(symbol, candle.close)
        equity = ledger.get_equity(current_price)
        position_qty = ledger.position_qty

        # Métricas de riesgo desde ledger — fail-closed si no disponibles
        day_pnl_pct = ledger.get_day_pnl_pct(current_price)
        drawdown_pct = ledger.get_drawdown_pct(current_price)
        executor = self.executors.get(symbol)
        orders_last_minute = executor.get_orders_last_minute() if executor else None

        missing = [
            name
            for name, val in [
                ("day_pnl_pct", day_pnl_pct),
                ("drawdown_pct", drawdown_pct),
                ("orders_last_minute", orders_last_minute),
            ]
            if val is None
        ]
        if missing:
            logger.warning(
                f"[{symbol}] Risk inputs unavailable ({', '.join(missing)}); blocking trading"
            )
            return

        entry_ref = candle.close

        # PositionSizer — computa target_qty con constraints del símbolo
        quantizer = self.quantizers.get(symbol)
        if quantizer is None:
            logger.error(f"[{symbol}] No quantizer found — blocking trading")
            return

        constraints = SymbolConstraints(
            step_size=quantizer.product.base_increment,
            min_qty=quantizer.product.base_increment,
            max_qty=Decimal("Infinity"),
            min_notional=quantizer.product.min_market_funds,
        )
        max_notional = Decimal(str(self.config.trading.max_notional_per_symbol))

        try:
            sizing = self.position_sizer.compute(
                symbol=symbol,
                equity=equity,
                entry_price=entry_ref,
                risk_per_trade_pct=Decimal(str(self.config.trading.risk_per_trade_pct)),
                constraints=constraints,
                max_notional=max_notional,
            )
        except FailClosedError as exc:
            logger.error(f"[{symbol}] PositionSizer fail-closed: {exc}")
            return

        # RiskGate — snapshot-based deterministic evaluation
        snapshot = RiskSnapshot(
            equity=equity,
            position_qty=position_qty,
            day_pnl_pct=day_pnl_pct,
            drawdown_pct=drawdown_pct,
            orders_last_minute=orders_last_minute,
            symbol=symbol,
            side=side,
            target_qty=sizing.target_qty,
            entry_ref=entry_ref,
            breaker_state=breaker_state,
        )
        risk_decision = self.risk_gate.evaluate(snapshot)

        if not risk_decision.allowed:
            logger.warning(
                f"[{symbol}] Signal REJECTED by RiskGate: {risk_decision.reason} "
                f"rules={risk_decision.blocking_rule_ids}"
            )
            return

        # OrderPlanner — final_qty = min(target_qty, hard_max_qty)
        risk_input = RiskDecisionInput(
            allowed=risk_decision.allowed,
            hard_max_qty=risk_decision.hard_max_qty,
            hard_max_notional=risk_decision.hard_max_notional,
            reduce_only=risk_decision.reduce_only,
            reason=risk_decision.reason,
        )
        signal_id = signal.signal_id
        strategy_id = signal.strategy_id

        try:
            order_intent = self.order_planner.plan(
                signal_id=signal_id,
                strategy_id=strategy_id,
                symbol=symbol,
                side=side,
                sizing=sizing,
                risk=risk_input,
                constraints=constraints,
            )
        except OrderNotAllowedError as exc:
            logger.warning(f"[{symbol}] OrderPlanner blocked: {exc}")
            return

        if not order_intent.viable:
            logger.warning(
                f"[{symbol}] Order not viable: final_qty={order_intent.final_qty} "
                f"< min_qty={constraints.min_qty}"
            )
            return

        # Ejecutar orden según modo configurado
        self._execute_order(order_intent)

    def _execute_order(self, intent: OrderIntent) -> None:
        """
        Ejecutar OrderIntent según modo configurado.

        Modos mutuamente excluyentes:
        - observe_only=True: solo observa, nunca ejecuta
        - dry_run=True: simula ejecución sin enviar al exchange
        - ambos=False: trading real (envía orden al exchange)

        Invariante: observe_only tiene prioridad sobre dry_run.
        El OrderIntent llega intacto desde el planner — sin desempaquetado.
        """
        symbol = intent.symbol
        side = intent.side
        qty = intent.final_qty
        observe = self.config.trading.observe_only
        dry_run = self.config.trading.dry_run
        current_price = self.current_prices.get(symbol, intent.price or Decimal("0"))

        # Modo observación: solo loggear, nunca ejecutar
        if observe:
            logger.info(
                f"[{symbol}] OBSERVE ONLY: {side} {qty} @ {current_price} "
                f"(signal={intent.signal_id})"
            )
            return

        # Verificar que tenemos executor para el símbolo
        executor = self.executors.get(symbol)
        if executor is None:
            logger.error(f"[{symbol}] No executor found")
            return

        # Modo dry run: simular con PaperEngine, no enviar al exchange
        if dry_run:
            if self.paper_engine:
                paper_intent = {
                    "client_id": intent.client_order_id,
                    "symbol": symbol,
                    "side": side.lower(),
                    "type": intent.order_type.lower(),
                    "amount": qty,
                    "position_side": "LONG" if side == "BUY" else "SHORT",
                    "reduce_only": intent.reduce_only,
                }
                bid = current_price * Decimal("0.999")
                ask = current_price * Decimal("1.001")
                result = self.paper_engine.submit_order(paper_intent, bid, ask)
                if result.get("status") == "filled":
                    fill = result.get("fill")
                    ledger = self.ledgers.get(symbol)
                    if ledger and fill:
                        from src.accounting.ledger import Fill

                        ledger_fill = Fill(
                            side=fill.side,
                            amount=fill.amount,
                            price=fill.price,
                            cost=fill.amount * fill.price,
                            fee_cost=fill.fee_cost,
                            fee_currency=fill.fee_currency,
                            ts_ms=int(time.time() * 1000),
                            trade_id=fill.trade_id,
                            order_id=fill.order_id,
                        )
                        ledger.add_fill(ledger_fill)
                        logger.info(
                            f"[{symbol}] PAPER FILL: {side} {qty} @ {fill.price} "
                            f"fee={fill.fee_cost} (signal={intent.signal_id})"
                        )
                else:
                    logger.info(
                        f"[{symbol}] DRY RUN ORDER: {side} {qty} "
                        f"(signal={intent.signal_id}, status: {result.get('status')})"
                    )
            else:
                logger.info(
                    f"[{symbol}] DRY RUN: {side} {qty} (signal={intent.signal_id})"
                )
            return

        # Trading real: enviar OrderIntent al exchange via executor
        try:
            result = executor.submit_order(intent)
            logger.info(
                f"[{symbol}] ORDER SUBMITTED: {side} {qty} "
                f"(signal={intent.signal_id}, client_order_id={intent.client_order_id}, "
                f"result={result})"
            )
        except Exception as e:
            logger.error(f"[{symbol}] ORDER FAILED: {e}")

    def _init_websocket(self) -> None:
        """Inicializar WebSocket."""
        self.ws = CoinbaseWSFeed(self.jwt_auth)

        # Callback para gap detection — invalida order books (L2 stale tras gap)
        def on_gap_detected():
            logger.warning("WebSocket gap detected! Initiating reconciliation...")
            self.circuit_breaker.record_ws_gap()
            for book in self.order_books.values():
                book.invalidate_on_gap()

        self.ws.on_gap_detected = on_gap_detected

        # Subscribirse a canales por símbolo
        for symbol in self.ledgers.keys():
            self._subscribe_symbol(symbol)

        # Subscribirse a heartbeats
        def on_heartbeat(msg: WSMessage):
            pass  # Gap detection se maneja en CoinbaseWSFeed

        self.ws.subscribe_heartbeats(on_heartbeat)

        # P1: Canal user - una sola suscripción con todos los product_ids
        # La doc indica que user channel espera una conexión por usuario
        # con múltiples product_ids en un solo array
        symbols = list(self.ledgers.keys())

        def on_user(msg: WSMessage):
            """Procesar mensajes del canal user (orders, fills)."""
            try:
                events = msg.data.get("events", [])
                for event in events:
                    event_type = event.get("type")
                    orders = event.get("orders", [])

                    # Agrupar órdenes por símbolo (product_id)
                    by_symbol: Dict[str, List[Dict]] = {}
                    for order in orders:
                        product_id = order.get("product_id")
                        if product_id:
                            by_symbol.setdefault(product_id, []).append(order)

                    # Enviar a cada OMS service correspondiente
                    for symbol, bucket in by_symbol.items():
                        oms = self.oms_services.get(symbol)
                        if oms:
                            oms.handle_user_event(event_type, bucket)

            except Exception as e:
                logger.error(f"Error processing user channel: {e}")

        if symbols:
            self.ws.subscribe("user", symbols, on_user)

    def _subscribe_symbol(self, symbol: str) -> None:
        """Subscribirse a canales para un símbolo."""
        # Obtener timeframe de la config
        timeframe = "1h"
        for s in self.config.symbols:
            if s.symbol == symbol:
                timeframe = s.timeframe
                break

        def on_candles(msg: WSMessage, sym=symbol, tf=timeframe):
            """Procesar velas del canal candles."""
            try:
                events = msg.data.get("events", [])
                for event in events:
                    candles = event.get("candles", [])
                    for candle in candles:
                        ts_ms = int(candle.get("start", 0)) * 1000
                        open_p = Decimal(str(candle.get("open", 0)))
                        high_p = Decimal(str(candle.get("high", 0)))
                        low_p = Decimal(str(candle.get("low", 0)))
                        close_p = Decimal(str(candle.get("close", 0)))
                        volume = Decimal(str(candle.get("volume", 0)))

                        if close_p > 0:
                            with self._lock:
                                self.current_prices[sym] = close_p

                            self._store_candle(sym, ts_ms, open_p, high_p, low_p, close_p, volume)

                            # P0 FIX: Ingerir vela 5m - MarketDataService dispara callbacks internamente
                            # No llamar _on_candle_closed aquí, los callbacks ya están suscritos
                            self.market_data.ingest_5m_candle(
                                symbol=sym,
                                target_timeframe=tf,
                                timestamp_ms=ts_ms,
                                open_p=open_p,
                                high_p=high_p,
                                low_p=low_p,
                                close_p=close_p,
                                volume=volume,
                            )
            except Exception as e:
                logger.error(f"Error processing candles for {sym}: {e}")

        def on_ticker(msg: WSMessage, sym=symbol):
            """Procesar ticker (solo para precio actual)."""
            try:
                events = msg.data.get("events", [])
                for event in events:
                    trades = event.get("trades", [])
                    if trades:
                        last_trade = trades[-1]
                        price = Decimal(str(last_trade.get("price", 0)))
                        if price > 0:
                            with self._lock:
                                self.current_prices[sym] = price
            except Exception as e:
                logger.error(f"Error processing ticker for {sym}: {e}")

        def on_level2(msg: WSMessage, sym=symbol):
            """Procesar order book L2 — normaliza eventos Coinbase y actualiza OrderBook."""
            book = self.order_books.get(sym)
            if book is None:
                return
            try:
                events_raw = msg.data.get("events", [])
                normalized: List[dict] = []
                for ev in events_raw:
                    event_type = ev.get("type", "update")
                    for upd in ev.get("updates", []):
                        raw_side = upd.get("side", "")
                        # Coinbase usa "offer" para ask
                        side_norm = "ask" if raw_side == "offer" else raw_side
                        normalized.append(
                            {
                                "type": event_type,
                                "side": side_norm,
                                "price": upd.get("price_level", "0"),
                                "size": upd.get("new_quantity", "0"),
                            }
                        )
                if normalized:
                    book.update(normalized)
            except Exception as exc:
                logger.error(f"[{sym}] Error processing L2: {exc}")

        # Subscribirse a canales públicos
        self.ws.subscribe_candles(symbol, on_candles)
        self.ws.subscribe_ticker(symbol, on_ticker)
        self.ws.subscribe_level2(symbol, on_level2)

    def _store_candle(
        self,
        symbol: str,
        ts_ms: int,
        open_p: Decimal,
        high_p: Decimal,
        low_p: Decimal,
        close_p: Decimal,
        volume: Decimal,
    ) -> None:
        """Almacenar vela recibida del canal candles."""
        with self._lock:
            data = self.price_data[symbol]

            # Verificar si ya tenemos esta vela
            if data and data[-1].get("timestamp") == ts_ms:
                data[-1].update(
                    {
                        "open": open_p,
                        "high": high_p,
                        "low": low_p,
                        "close": close_p,
                        "volume": volume,
                    }
                )
            else:
                data.append(
                    {
                        "timestamp": ts_ms,
                        "open": open_p,
                        "high": high_p,
                        "low": low_p,
                        "close": close_p,
                        "volume": volume,
                    }
                )

                # Mantener solo últimas N velas
                max_candles = 500
                if len(data) > max_candles:
                    data.pop(0)

    def _get_ohlcv_df(self, symbol: str) -> pd.DataFrame:
        """
        Obtener DataFrame OHLCV para un símbolo con resampling a timeframe operativo.

        Coinbase candles entrega buckets de 5 minutos.
        Se hace resampling explícito al timeframe configurado (1h/4h/etc).
        """
        with self._lock:
            data = self.price_data.get(symbol, [])

        if not data:
            return pd.DataFrame()

        df = pd.DataFrame(data).sort_values("timestamp")
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp")

        # Obtener timeframe configurado
        timeframe = "1h"
        for s in self.config.symbols:
            if s.symbol == symbol:
                timeframe = s.timeframe
                break

        # Mapeo de timeframe a regla de pandas
        rule_map = {
            "5m": "5min",
            "15m": "15min",
            "30m": "30min",
            "1h": "1h",
            "2h": "2h",
            "4h": "4h",
            "6h": "6h",
            "1d": "1d",
        }
        rule = rule_map.get(timeframe, "5min")

        # Resampling si el timeframe no es 5m nativo
        if rule != "5min":
            # closed="left": intervalo [inicio, fin) - vela en el borde va al bucket siguiente
            ohlcv = df.resample(rule, label="right", closed="left").agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )

            # Filtrar barras incompletas: contar velas 5m por bucket
            counts = df["close"].resample(rule, label="right", closed="left").count()
            required = {
                "15min": 3,
                "30min": 6,
                "1h": 12,
                "2h": 24,
                "4h": 48,
                "6h": 72,
                "1d": 288,
            }.get(rule, 1)

            # Solo mantener barras completas
            df = ohlcv[counts >= required].dropna()

        return df

    def run(self) -> int:
        """Ejecutar el bot."""
        if not self.initialize():
            return 1

        # Iniciar WebSocket
        try:
            self.ws.start()
            logger.info("WebSocket started")
        except Exception as e:
            logger.error(f"Failed to start WebSocket: {e}")
            return 1

        self._running = True

        logger.info("Bot running. Press Ctrl+C to stop.")
        logger.info("")

        # Main loop
        # CORREGIDO: Modo observación - solo housekeeping/logging.
        # El disparador de trading debe venir de CandleClosed / OMS events.
        try:
            max_cycles = self.config.trading.max_cycles

            while self._running:
                self.cycle_count += 1

                # Modo smoke test: limitar ciclos
                if max_cycles > 0 and self.cycle_count >= max_cycles:
                    logger.info(f"🏁 Smoke test completado: {max_cycles} ciclos ejecutados")
                    self._running = False
                    break

                # Log status periódico
                self._log_status()

                # Validaciones de ledger en modo smoke test
                if self.smoke_test_mode:
                    self._run_smoke_validations()

                # Sleep corto para housekeeping (no es el reloj de trading)
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Shutting down...")

        finally:
            self._running = False
            self.ws.stop()
            logger.info("Shutdown complete")

        return 0

    def _run_smoke_validations(self) -> None:
        """
        Validaciones de smoke test.

        Adaptado de GuardianBot.
        Verifica invariantes del ledger en cada ciclo.
        """
        for symbol, ledger in self.ledgers.items():
            current_price = self.current_prices.get(symbol)
            if not current_price or current_price <= 0:
                continue

            # Validar equity invariant
            ok, msg = ledger.validate_equity_invariant(current_price)
            if not ok:
                logger.critical(f"❌ [{symbol}] {msg}")
                if self.smoke_test_mode:
                    raise RuntimeError(f"Equity invariant violation: {msg}")

            # Log de estado para smoke test
            if self.cycle_count % 10 == 0:
                stats = ledger.get_stats()
                logger.info(
                    f"🧪 [{symbol}] Smoke cycle {self.cycle_count}: pos={stats['position_qty']}, pnl={stats['realized_pnl']}"
                )

    def _process_symbol(self, symbol: str) -> None:
        """
        Procesar un símbolo.

        ADVERTENCIA: Strategy layer no implementada.
        El bot opera en modo observación (no ejecuta órdenes).
        """
        # Verificar circuit breaker
        allowed, reason = self.circuit_breaker.check_before_trade()
        if not allowed:
            if reason:
                logger.warning(f"Trading blocked: {reason}")
            return

        # Obtener datos OHLCV
        df = self._get_ohlcv_df(symbol)
        if len(df) < 50:
            logger.debug(f"Insufficient data for {symbol}: {len(df)} candles")
            return

        # MODO OBSERVACIÓN: Trading triggers vienen de CandleClosed callbacks (_on_candle_closed).
        # Este método solo loggea estado para smoke test / debug.
        current_price = self.current_prices.get(symbol, Decimal("0"))
        ledger = self.ledgers.get(symbol)

        if ledger and current_price > 0:
            unrealized = ledger.get_unrealized_pnl(current_price)
            logger.debug(
                f"{symbol}: price={current_price}, "
                f"position={ledger.position_qty}, "
                f"unrealized={unrealized:.2f}"
            )

    def _log_status(self) -> None:
        """Loggear estado del bot."""
        status = self.circuit_breaker.get_status()

        logger.info(
            f"Status: {status['state']} | "
            f"Equity: ${status['equity']['now']:,.2f} | "
            f"Drawdown: {status['equity']['drawdown']:.2%} | "
            f"Latency p95: {status['health']['latency_p95_ms']:.0f}ms | "
            f"Rejects: {status['health']['reject_rate']:.2%}"
        )


def main():
    """Entry point."""
    # Configurar file logging
    config = get_config()
    setup_file_logging(config.paths)

    bot = TradingBot()
    return bot.run()


if __name__ == "__main__":
    sys.exit(main())
