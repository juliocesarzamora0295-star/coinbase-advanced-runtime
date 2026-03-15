"""
Fortress v4 - Coinbase Advanced Trade Edition

Entry point del bot de trading.
"""
import logging
import sys
import time
import threading
from decimal import Decimal
from typing import Dict, List

import pandas as pd

from src.config import get_config, PathsConfig
from src.core.jwt_auth import JWTAuth, load_credentials_from_env
from src.core.coinbase_exchange import CoinbaseRESTClient
from src.core.coinbase_websocket import CoinbaseWSFeed, WSMessage
from src.core.quantization import create_quantizer_from_api_response
from src.execution.idempotency import IdempotencyStore
from src.execution.orders import OrderExecutor
from src.accounting.ledger import TradeLedger
from src.risk.circuit_breaker import CircuitBreaker, BreakerConfig
from src.risk.gate import RiskGate, RiskLimits, RiskSnapshot
from src.oms.reconcile import OMSReconcileService
from src.marketdata.service import MarketDataService, SignalEngine, create_naive_ma_strategy
from src.simulation.paper_engine import PaperEngine

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
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
        
        # Market data y señales (P0 FIX: SignalEngine por símbolo, no global)
        self.price_data: Dict[str, List[Dict]] = {}
        self.current_prices: Dict[str, Decimal] = {}
        self.market_data = MarketDataService()
        self.signal_engines: Dict[str, SignalEngine] = {}  # Uno por símbolo
        
        # Risk Gate (integrado en pipeline)
        self.risk_gate: RiskGate = None
        
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
        logger.info("- Strategy Layer: NO IMPLEMENTADA")
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
                oms = OMSReconcileService(
                    idempotency=idempotency,
                    ledger=ledger,
                    fill_fetcher=lambda order_id: self.client.list_fills(order_id=order_id),
                )
                self.oms_services[symbol] = oms
                
                # Inicializar buffer de precios
                self.price_data[symbol] = []
                self.current_prices[symbol] = Decimal("0")
                
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
        Inicializar pipeline de trading:
        CandleClosed -> SignalEngine -> RiskGate -> OrderExecutor -> OMSReconcile -> Ledger
        
        P0 FIX: Un SignalEngine por símbolo, no global.
        """
        logger.info("Initializing trading pipeline...")
        
        for symbol_cfg in self.config.symbols:
            if not symbol_cfg.enabled:
                continue
            
            symbol = symbol_cfg.symbol
            timeframe = symbol_cfg.timeframe
            
            # P0 FIX: Registrar símbolo con timeframe en MarketDataService
            self.market_data.register_symbol(symbol, timeframe)
            
            # P0 FIX: Crear SignalEngine por símbolo (aislado)
            engine = SignalEngine(symbol)
            strategy = create_naive_ma_strategy(symbol=symbol, fast_period=10, slow_period=30)
            engine.add_strategy(strategy)
            self.signal_engines[symbol] = engine
            
            # Suscribir callback a CandleClosed events
            self.market_data.subscribe(symbol, lambda candle, sym=symbol: self._on_candle_closed(sym, candle))
        
        logger.info("Trading pipeline initialized")
    
    def _on_candle_closed(self, symbol: str, candle) -> None:
        """
        Callback para eventos CandleClosed.
        
        Pipeline:
        1. Generar señales con SignalEngine (por símbolo)
        2. Evaluar riesgo con RiskGate
        3. Ejecutar órdenes con OrderExecutor (si pasa risk)
        """
        # P0 FIX: Usar SignalEngine del símbolo específico
        engine = self.signal_engines.get(symbol)
        if not engine:
            logger.warning(f"[{symbol}] No SignalEngine found")
            return
        
        # 1. Generar señales
        signals = engine.on_candle_closed(candle)
        
        if not signals:
            return
        
        # 2. Evaluar cada señal con RiskGate
        for signal in signals:
            self._process_signal_with_risk(symbol, signal, candle)
    
    def _process_signal_with_risk(self, symbol: str, signal: dict, candle) -> None:
        """
        Procesar señal a través del RiskGate.
        
        Si RiskDecision.allow == True, ejecutar orden.
        Si RiskDecision.allow == False, loggear rechazo.
        """
        side = signal.get("side", "BUY")
        
        # Verificar circuit breaker primero
        allowed, reason = self.circuit_breaker.check_before_trade()
        if not allowed:
            logger.warning(f"[{symbol}] Trading blocked by circuit breaker: {reason}")
            return
        
        # Obtener snapshot de riesgo
        ledger = self.ledgers.get(symbol)
        if not ledger:
            logger.error(f"[{symbol}] No ledger found")
            return
        
        current_price = self.current_prices.get(symbol, candle.close)
        equity = ledger.get_equity(current_price)
        position_qty = ledger.position_qty
        
        # Calcular métricas de riesgo reales desde ledger (fail-closed: None = bloquear)
        day_pnl_pct = ledger.get_day_pnl_pct(current_price)
        drawdown_pct = ledger.get_drawdown_pct(current_price)
        
        # Calcular órdenes por minuto desde executor
        executor = self.executors.get(symbol)
        orders_last_minute = executor.get_orders_last_minute() if executor else None
        
        # Fail-closed: si CUALQUIER métrica clave no está disponible, bloquear
        if any(x is None for x in [day_pnl_pct, drawdown_pct, orders_last_minute]):
            missing = []
            if day_pnl_pct is None:
                missing.append("day_pnl_pct")
            if drawdown_pct is None:
                missing.append("drawdown_pct")
            if orders_last_minute is None:
                missing.append("orders_last_minute")
            logger.warning(f"[{symbol}] Risk inputs unavailable ({', '.join(missing)}); blocking trading")
            return
        
        snapshot = RiskSnapshot(
            equity=equity,
            position_qty=position_qty,
            day_pnl_pct=day_pnl_pct,
            drawdown_pct=drawdown_pct,
            orders_last_minute=orders_last_minute,
        )
        
        # Calcular cost estimate (fees + slippage estimada)
        cost_estimate = self._estimate_cost(symbol, side, candle.close)
        
        # Evaluar con RiskGate
        entry_ref = candle.close
        stop_ref = candle.low * Decimal("0.99") if side == "BUY" else candle.high * Decimal("1.01")
        
        risk_decision = self.risk_gate.evaluate(
            symbol=symbol,
            side=side,
            snapshot=snapshot,
            entry_ref=entry_ref,
            stop_ref=stop_ref,
            cost_estimate=cost_estimate,
        )
        
        if not risk_decision.allow:
            logger.warning(
                f"[{symbol}] Signal REJECTED by RiskGate: {risk_decision.reason}"
            )
            return
        
        # RiskGate aprobó - ejecutar orden según modo configurado
        self._execute_order(symbol, side, risk_decision.qty, entry_ref, signal.get('reason', 'N/A'))
    
    def _estimate_cost(self, symbol: str, side: str, price: Decimal) -> Decimal:
        """
        Estimar costo total (fees + slippage) para una orden.
        
        Usa fee tier del exchange + estimación de slippage basada en volumen.
        """
        # Fee taker por defecto (0.6% para usuarios normales en Coinbase)
        fee_rate = Decimal("0.006")
        
        # Slippage estimado (10 bps para órdenes market pequeñas)
        slippage_bps = Decimal("0.001")
        
        # Costo total como % del notional
        total_cost_pct = fee_rate + slippage_bps
        
        # Retornar costo estimado en términos absolutos (para compatibilidad con RiskGate)
        # El RiskGate compara contra notional, así que retornamos %
        return total_cost_pct * Decimal("100")  # Convertir a basis points equivalentes
    
    def _execute_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        reason: str,
    ) -> None:
        """
        Ejecutar orden según modo configurado.
        
        Modos mutuamente excluyentes:
        - observe_only=True: solo observa, nunca ejecuta
        - dry_run=True: simula ejecución sin enviar al exchange
        - ambos=False: trading real (envía orden al exchange)
        
        Invariante: observe_only tiene prioridad sobre dry_run.
        """
        observe = self.config.trading.observe_only
        dry_run = self.config.trading.dry_run
        
        # Modo observación: solo loggear, nunca ejecutar
        if observe:
            logger.info(
                f"[{symbol}] OBSERVE ONLY: {side} {qty} @ {price} "
                f"(reason: {reason})"
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
                intent = {
                    "client_id": f"paper_{symbol}_{side}_{int(time.time() * 1000)}",
                    "symbol": symbol,
                    "side": side.lower(),
                    "type": "market",
                    "amount": qty,
                    "position_side": "LONG" if side == "BUY" else "SHORT",
                    "reduce_only": False,
                }
                bid = self.current_prices.get(symbol, price) * Decimal("0.999")
                ask = self.current_prices.get(symbol, price) * Decimal("1.001")
                result = self.paper_engine.submit_order(intent, bid, ask)
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
                            f"[{symbol}] PAPER FILL: {side} {qty} @ {price} "
                            f"fee={fill.fee_cost} (reason: {reason})"
                        )
                else:
                    logger.info(
                        f"[{symbol}] DRY RUN ORDER: {side} {qty} @ {price} "
                        f"(reason: {reason}, status: {result.get('status')})"
                    )
            else:
                logger.info(
                    f"[{symbol}] DRY RUN: {side} {qty} @ {price} "
                    f"(reason: {reason})"
                )
            return
        
        # Trading real: enviar orden al exchange
        # Solo llegamos aquí si observe_only=False y dry_run=False
        try:
            result = executor.create_market_order(
                product_id=symbol,
                side=side,
                qty=qty,
            )
            logger.info(
                f"[{symbol}] ORDER SUBMITTED: {side} {qty} @ {price} "
                f"(reason: {reason}, result: {result})"
            )
        except Exception as e:
            logger.error(f"[{symbol}] ORDER FAILED: {e}")
    
    def _init_websocket(self) -> None:
        """Inicializar WebSocket."""
        self.ws = CoinbaseWSFeed(self.jwt_auth)
        
        # Callback para gap detection
        def on_gap_detected():
            logger.warning("WebSocket gap detected! Initiating reconciliation...")
            self.circuit_breaker.record_ws_gap()
        
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
            """Procesar order book L2."""
            pass  # TODO: Implementar order book management
        
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
                data[-1].update({
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                })
            else:
                data.append({
                    "timestamp": ts_ms,
                    "open": open_p,
                    "high": high_p,
                    "low": low_p,
                    "close": close_p,
                    "volume": volume,
                })
                
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
            ohlcv = df.resample(rule, label="right", closed="left").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            })
            
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
                logger.info(f"🧪 [{symbol}] Smoke cycle {self.cycle_count}: pos={stats['position_qty']}, pnl={stats['realized_pnl']}")
    
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
        
        # MODO OBSERVACIÓN: No se ejecutan estrategias ni órdenes
        # TODO: Implementar strategy layer con:
        # - Feature engineering
        # - Signal generation
        # - Position sizing
        # - Risk checks pre-trade
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
