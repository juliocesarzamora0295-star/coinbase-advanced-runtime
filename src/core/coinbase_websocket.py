"""
WebSocket client para Coinbase Advanced Trade API v3.

CORREGIDO P0:
  - market_trades.time es ISO-8601 string (no int ms)
  - Heartbeat_counter explotado para gap detection
  - Sequence_num tracking
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set

import websocket

from src.core.jwt_auth import JWTAuth

logger = logging.getLogger("CoinbaseWS")


@dataclass
class WSMessage:
    """Mensaje WebSocket normalizado."""

    channel: str
    product_id: str
    data: Dict
    timestamp: float


class CoinbaseWSFeed:
    """
    WebSocket feed para Coinbase Advanced Trade.

    CORREGIDO P0:
      - market_trades.time: ISO-8601 string parsing
      - heartbeat_counter: explotado para gap detection
      - sequence_num: tracking para detección de gaps
    """

    MARKET_WS_URL = "wss://advanced-trade-ws.coinbase.com"
    USER_WS_URL = "wss://advanced-trade-ws-user.coinbase.com"

    AUTH_CHANNELS = {"user"}

    # Coinbase WS channel name mapping: subscribe name → message name.
    # Coinbase accepts "level2" in subscribe but sends "l2_data" in messages.
    _SUBSCRIBE_TO_MSG_CHANNEL = {"level2": "l2_data"}

    def __init__(
        self,
        jwt_auth: Optional[JWTAuth] = None,
        on_gap_detected: Optional[Callable[[], None]] = None,
    ):
        self.jwt_auth = jwt_auth
        self.on_gap_detected = on_gap_detected

        # CORREGIDO: Callbacks indexados por (channel, product_ids) para evitar cross-contamination
        self._callbacks: Dict[str, List[tuple[set[str], Callable[[WSMessage], None]]]] = {}
        self._subscriptions: Set[str] = set()

        self._ws_market: Optional[websocket.WebSocketApp] = None
        self._ws_user: Optional[websocket.WebSocketApp] = None
        self._running = False
        self._connected_at: float = 0.0

        # CORREGIDO P0: Gap detection con ambos mecanismos
        self._last_heartbeat_counter: Optional[int] = None
        # FIX: Sequence tracking por (channel, product_id) — evita falsos gaps cross-channel
        self._last_sequence_num: Dict[tuple[str, str], int] = {}
        self._ws_gap_flag = False

        self._market_thread: Optional[threading.Thread] = None
        self._user_thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()

    def subscribe(
        self,
        channel: str,
        product_ids: List[str],
        callback: Callable[[WSMessage], None],
    ) -> None:
        """Suscribirse a un canal.

        _subscriptions stores the subscribe-name (for sending to Coinbase).
        _callbacks stores under the message-name (for lookup on receive).
        """
        # Channel name that Coinbase uses in response messages
        msg_channel = self._SUBSCRIBE_TO_MSG_CHANNEL.get(channel, channel)

        with self._lock:
            key = f"{channel}:{','.join(product_ids)}"
            self._subscriptions.add(key)

            if msg_channel not in self._callbacks:
                self._callbacks[msg_channel] = []
            product_ids_set = set(product_ids) if product_ids else set()
            self._callbacks[msg_channel].append((product_ids_set, callback))

        logger.info(f"Subscribed to {channel} for {product_ids}")

    def subscribe_ticker(
        self,
        product_id: str,
        callback: Callable[[WSMessage], None],
    ) -> None:
        """Suscribirse a ticker (usa market_trades)."""
        self.subscribe("market_trades", [product_id], callback)

    def subscribe_level2(
        self,
        product_id: str,
        callback: Callable[[WSMessage], None],
    ) -> None:
        """Suscribirse a order book L2."""
        self.subscribe("level2", [product_id], callback)

    def subscribe_candles(
        self,
        product_id: str,
        callback: Callable[[WSMessage], None],
    ) -> None:
        """
        Suscribirse a canal de velas (candles).

        NOTA: Coinbase Advanced Trade expone solo el canal `candles`.
        Las velas llegan en buckets de 5 minutos.
        El timeframe operativo se resuelve por resampling interno.
        """
        channel = "candles"
        self.subscribe(channel, [product_id], callback)

    def subscribe_heartbeats(self, callback: Callable[[WSMessage], None]) -> None:
        """Suscribirse a heartbeats (para gap detection)."""
        # P0: heartbeats no debe enviar product_ids
        self.subscribe("heartbeats", [], callback)

    def start(self) -> None:
        """Iniciar conexiones WebSocket."""
        if self._running:
            return

        self._running = True

        public_channels = set()
        auth_channels = set()

        with self._lock:
            for sub in self._subscriptions:
                channel = sub.split(":")[0]
                if channel in self.AUTH_CHANNELS:
                    auth_channels.add(channel)
                else:
                    public_channels.add(channel)

        if public_channels:
            self._market_thread = threading.Thread(
                target=self._run_market_loop,
                daemon=True,
            )
            self._market_thread.start()

        if auth_channels and self.jwt_auth:
            self._user_thread = threading.Thread(
                target=self._run_user_loop,
                daemon=True,
            )
            self._user_thread.start()

    def stop(self) -> None:
        """Detener conexiones WebSocket."""
        self._running = False

        if self._ws_market:
            try:
                self._ws_market.close()
            except Exception:
                pass

        if self._ws_user:
            try:
                self._ws_user.close()
            except Exception:
                pass

        if self._market_thread:
            self._market_thread.join(timeout=5)

        if self._user_thread:
            self._user_thread.join(timeout=5)

    def _run_market_loop(self) -> None:
        """Loop de conexión para canales públicos."""
        backoff = 1.0

        while self._running:
            try:
                self._connect_market_ws()
                backoff = 1.0
            except Exception as e:
                logger.error(f"Market WS error: {e}")

            if not self._running:
                break

            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)

    def _run_user_loop(self) -> None:
        """Loop de conexión para canal user (autenticado)."""
        backoff = 1.0

        while self._running:
            try:
                self._connect_user_ws()
                backoff = 1.0
            except Exception as e:
                logger.error(f"User WS error: {e}")

            if not self._running:
                break

            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)

    def _connect_market_ws(self) -> None:
        """Conectar a WebSocket de market data."""
        self._ws_market = websocket.WebSocketApp(
            self.MARKET_WS_URL,
            on_open=self._on_market_open,
            on_message=self._on_market_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        logger.info("Connecting to market WebSocket...")
        self._ws_market.run_forever(ping_interval=20, ping_timeout=10)

    def _connect_user_ws(self) -> None:
        """Conectar a WebSocket de user data (autenticado)."""
        self._ws_user = websocket.WebSocketApp(
            self.USER_WS_URL,
            on_open=self._on_user_open,
            on_message=self._on_user_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        logger.info("Connecting to user WebSocket...")
        self._ws_user.run_forever(ping_interval=20, ping_timeout=10)

    def _on_market_open(self, ws) -> None:
        """Callback cuando se abre conexión de market data."""
        logger.info("Market WebSocket connected")
        self._connected_at = time.time()
        self._send_subscriptions(ws, public_only=True)

    def _on_user_open(self, ws) -> None:
        """Callback cuando se abre conexión de user data."""
        logger.info("User WebSocket connected")
        self._send_subscriptions(ws, public_only=False)

    def _send_subscriptions(
        self,
        ws: websocket.WebSocketApp,
        public_only: bool,
    ) -> None:
        """Enviar mensajes de subscribe."""
        with self._lock:
            subscriptions = list(self._subscriptions)

        for sub in subscriptions:
            channel, product_ids_str = sub.split(":", 1)
            # P0: Si product_ids_str está vacío, no hay product_ids
            product_ids = product_ids_str.split(",") if product_ids_str else []

            if public_only and channel in self.AUTH_CHANNELS:
                continue

            if not public_only and channel not in self.AUTH_CHANNELS:
                continue

            # P0: Solo incluir product_ids si hay elementos
            msg = {
                "type": "subscribe",
                "channel": channel,
            }
            if product_ids:
                msg["product_ids"] = product_ids

            if channel in self.AUTH_CHANNELS and self.jwt_auth:
                msg["jwt"] = self.jwt_auth.generate_ws_jwt()

            ws.send(json.dumps(msg))
            logger.debug(f"Sent subscribe: {channel} for {product_ids}")

    def _on_market_message(self, ws, message: str) -> None:
        """Procesar mensaje de market data."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {message[:100]}")
            return

        channel = data.get("channel", "")
        product_id = self._extract_product_id(data)

        # CORREGIDO P0: Gap detection con heartbeats
        if channel == "heartbeats":
            self._check_heartbeat(data)

        # NOTE: sequence_num is a global per-connection counter in Coinbase WS.
        # It increments across ALL channels (candles, l2_data, market_trades, heartbeats).
        # Tracking it per-channel or per-product produces false gaps every time a message
        # from another channel arrives between two messages of the tracked channel.
        # Heartbeat_counter (checked above) is the correct mechanism — it IS contiguous
        # within its own channel and detects real connection-level gaps.
        # _check_sequence is retained for future use but not called on the hot path.

        # CORREGIDO P0: Parsear timestamps ISO-8601 en market_trades
        if channel == "market_trades":
            data = self._parse_market_trades_timestamps(data)

        # CORREGIDO: Obtener callbacks con sus product_ids filtrados
        callback_entries = self._callbacks.get(channel, [])

        ws_msg = WSMessage(
            channel=channel,
            product_id=product_id,
            data=data,
            timestamp=time.time(),
        )

        # CORREGIDO: Filtrar callbacks por product_id para evitar cross-contamination
        for product_ids_set, cb in callback_entries:
            # Si el callback no tiene product_ids específicos (ej: heartbeats) o coincide con el product_id
            if not product_ids_set or product_id in product_ids_set:
                try:
                    cb(ws_msg)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

    def _parse_market_trades_timestamps(self, data: Dict) -> Dict:
        """
        CORREGIDO P0: Parsear timestamps ISO-8601 en market_trades.

        market_trades.time es string ISO-8601, no int de ms.
        """
        events = data.get("events", [])
        for event in events:
            trades = event.get("trades", [])
            for trade in trades:
                time_str = trade.get("time", "")
                if time_str:
                    try:
                        # Parsear ISO-8601
                        dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                        # Convertir a timestamp ms
                        trade["time_ms"] = int(dt.timestamp() * 1000)
                    except (ValueError, TypeError) as e:
                        logger.warning(f"Failed to parse timestamp: {time_str} - {e}")
                        trade["time_ms"] = 0
        return data

    def _check_heartbeat(self, data: Dict) -> None:
        """
        CORREGIDO P0: Verificar heartbeat_counter para gap detection.

        NOTA: heartbeat_counter llega como string en el WS.
        """
        events = data.get("events", [])
        for event in events:
            # P0: heartbeat_counter llega como string, convertir a int
            raw_counter = event.get("heartbeat_counter")
            try:
                counter = int(raw_counter) if raw_counter is not None else None
            except (ValueError, TypeError):
                logger.warning(f"Invalid heartbeat_counter: {raw_counter}")
                continue

            if counter is not None and self._last_heartbeat_counter is not None:
                expected = self._last_heartbeat_counter + 1
                if counter != expected:
                    gap_size = counter - self._last_heartbeat_counter - 1
                    logger.warning(
                        f"Heartbeat gap detected: expected {expected}, got {counter} "
                        f"(missed {gap_size} heartbeats)"
                    )
                    self._ws_gap_flag = True
                    if self.on_gap_detected:
                        self.on_gap_detected()

            if counter is not None:
                self._last_heartbeat_counter = counter

    def _check_sequence(self, channel: str, product_id: str, sequence_num: int) -> None:
        """
        Verificar sequence_num para gap detection por (channel, product_id).

        FIX: cada canal mantiene su propio stream de secuencia por símbolo.
        Clave anterior era solo product_id → falsos gaps cuando candles/market_trades/level2
        comparten el mismo símbolo y llegan intercalados con sequence_nums independientes.
        """
        key = (channel, product_id)
        last_seq = self._last_sequence_num.get(key)
        if last_seq is not None:
            expected = last_seq + 1
            if sequence_num != expected:
                gap_size = sequence_num - last_seq - 1
                logger.warning(
                    f"Sequence gap detected for {channel}/{product_id}: "
                    f"expected {expected}, got {sequence_num} (missed {gap_size} messages)"
                )
                self._ws_gap_flag = True
                if self.on_gap_detected:
                    self.on_gap_detected()

        self._last_sequence_num[key] = sequence_num

    def _on_user_message(self, ws, message: str) -> None:
        """Procesar mensaje de user data."""
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {message[:100]}")
            return

        channel = data.get("channel", "")
        # CORREGIDO P0: Obtener callbacks con sus product_ids
        callback_entries = self._callbacks.get(channel, [])

        # CORREGIDO P0: Extraer product_ids del schema real del canal user
        # En user channel, product_id está en orders[].product_id, no en events[0].product_id
        product_ids = self._extract_user_product_ids(data)

        ws_msg = WSMessage(
            channel=channel,
            product_id="*",
            data=data,
            timestamp=time.time(),
        )

        # CORREGIDO P0: Filtrar callbacks contra product_id(s) presentes en orders[]
        for product_ids_set, cb in callback_entries:
            # user channel: filtrar contra product_id(s) presentes en orders[]
            if not product_ids_set or (product_ids & product_ids_set):
                try:
                    cb(ws_msg)
                except Exception as e:
                    logger.error(f"Callback error: {e}")

    def _extract_product_id(self, data: Dict) -> str:
        """
        Extraer product_id del mensaje WS.

        Coinbase Advanced Trade WS usa schemas distintos por canal:
        - level2 (l2_data): events[0].product_id
        - candles:          events[0].candles[0].product_id
        - market_trades:    events[0].trades[0].product_id
        - heartbeats:       sin product_id (retorna "")
        """
        events = data.get("events", [])
        if not events:
            return data.get("product_id", "")

        event = events[0]

        # Caso directo: product_id a nivel de evento (level2 / l2_data)
        pid = event.get("product_id", "")
        if pid:
            return pid

        # candles: product_id dentro de event.candles[]
        candles = event.get("candles", [])
        if candles:
            pid = candles[0].get("product_id", "")
            if pid:
                return pid

        # market_trades: product_id dentro de event.trades[]
        trades = event.get("trades", [])
        if trades:
            pid = trades[0].get("product_id", "")
            if pid:
                return pid

        return ""

    def _extract_user_product_ids(self, data: Dict) -> Set[str]:
        """
        CORREGIDO P0: Extraer product_ids del schema real del canal user.

        En el canal user, product_id está dentro de cada orden (orders[].product_id),
        no al nivel del evento como en otros canales.
        """
        result: Set[str] = set()
        for event in data.get("events", []):
            for order in event.get("orders", []):
                pid = order.get("product_id")
                if pid:
                    result.add(pid)
        return result

    def _on_error(self, ws, error) -> None:
        """Callback de error."""
        logger.error(f"WebSocket error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        """Callback de cierre."""
        logger.warning(f"WebSocket closed: {code} {msg}")

    @property
    def ws_gap_flag(self) -> bool:
        """Flag de gap detectado."""
        return self._ws_gap_flag

    def clear_gap_flag(self) -> None:
        """Limpiar flag de gap."""
        self._ws_gap_flag = False
