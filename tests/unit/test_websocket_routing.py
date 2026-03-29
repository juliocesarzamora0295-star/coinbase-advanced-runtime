"""
Tests para WebSocket callback routing y sequence tracking.

Verifica corrección de bugs:
- WS callback routing por (channel, product_id)
- Sequence tracking por (channel, product_id) — evita falsos gaps cross-channel
"""

import json
from unittest.mock import MagicMock

from src.core.coinbase_websocket import CoinbaseWSFeed


class TestWSCallbackRouting:
    """Test que callbacks sean enrutados correctamente por product_id."""

    def test_callback_routing_by_product_id(self):
        """
        Verificar que callbacks para diferentes product_ids no se contaminen.

        Bug corregido: Si te suscribes a `candles` para dos símbolos,
        el mismo mensaje no debe disparar ambos callbacks.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_callback = MagicMock()
        eth_callback = MagicMock()

        # Suscribir a candles para BTC y ETH por separado
        ws.subscribe("candles", ["BTC-USD"], btc_callback)
        ws.subscribe("candles", ["ETH-USD"], eth_callback)

        # Simular mensaje de candles para BTC
        btc_message = {
            "channel": "candles",
            "sequence_num": 100,
            "events": [{"product_id": "BTC-USD", "candles": []}],
        }

        # Llamar directamente al handler de mensajes
        ws._on_market_message(None, json.dumps(btc_message))

        # Solo el callback de BTC debe ser llamado
        assert btc_callback.called, "BTC callback should be called for BTC message"
        assert not eth_callback.called, "ETH callback should NOT be called for BTC message"

    def test_multiple_products_same_callback(self):
        """
        Verificar que un callback suscrito a múltiples productos reciba todos.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        multi_callback = MagicMock()

        # Suscribir a un callback para múltiples productos
        ws.subscribe("market_trades", ["BTC-USD", "ETH-USD"], multi_callback)

        # Simular mensaje para BTC
        btc_message = {
            "channel": "market_trades",
            "sequence_num": 100,
            "events": [{"product_id": "BTC-USD", "trades": []}],
        }

        ws._on_market_message(None, json.dumps(btc_message))
        assert multi_callback.called, "Callback should be called for BTC"

        # Reset mock
        multi_callback.reset_mock()

        # Simular mensaje para ETH
        eth_message = {
            "channel": "market_trades",
            "sequence_num": 101,
            "events": [{"product_id": "ETH-USD", "trades": []}],
        }

        ws._on_market_message(None, json.dumps(eth_message))
        assert multi_callback.called, "Callback should be called for ETH"


class TestSequencePerProduct:
    """Test que sequence_num se tracke por (channel, product_id)."""

    def test_sequence_tracking_per_product(self):
        """
        Verificar que sequence_num se tracke independientemente por producto.

        Clave ahora es (channel, product_id).
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_msg1 = {
            "channel": "candles",
            "sequence_num": 100,
            "events": [{"product_id": "BTC-USD"}],
        }
        btc_msg2 = {
            "channel": "candles",
            "sequence_num": 101,
            "events": [{"product_id": "BTC-USD"}],
        }
        eth_msg1 = {
            "channel": "candles",
            "sequence_num": 50,
            "events": [{"product_id": "ETH-USD"}],
        }

        ws._on_market_message(None, json.dumps(btc_msg1))
        assert ws._last_sequence_num.get(("candles", "BTC-USD")) == 100

        ws._on_market_message(None, json.dumps(eth_msg1))
        assert ws._last_sequence_num.get(("candles", "ETH-USD")) == 50
        # BTC no debe verse afectado
        assert ws._last_sequence_num.get(("candles", "BTC-USD")) == 100

        ws._on_market_message(None, json.dumps(btc_msg2))
        assert ws._last_sequence_num.get(("candles", "BTC-USD")) == 101
        # ETH no debe verse afectado
        assert ws._last_sequence_num.get(("candles", "ETH-USD")) == 50

    def test_gap_detection_per_product(self):
        """
        Verificar que gap detection funcione por (channel, product_id).
        """
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap_detected = MagicMock()
        ws.on_gap_detected = gap_detected

        # Mensajes consecutivos para BTC — sin gap
        ws._check_sequence("level2", "BTC-USD", 100)
        ws._check_sequence("level2", "BTC-USD", 101)
        assert not gap_detected.called

        # Gap en ETH no debe afectar BTC
        ws._check_sequence("level2", "ETH-USD", 50)
        ws._check_sequence("level2", "ETH-USD", 52)  # Gap: falta 51
        assert gap_detected.called


class TestSequenceCrossChannel:
    """
    FIX: Verificar que canales distintos para el mismo símbolo no mezclen secuencias.

    Antes: key = product_id → market_trades seq 10 → level2 seq 1 → falso gap
    Ahora: key = (channel, product_id) → trackers independientes
    """

    def test_level2_consecutive_no_gap(self):
        """level2 BTC-USD seq 1,2,3 → sin gap."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        ws._check_sequence("level2", "BTC-USD", 1)
        ws._check_sequence("level2", "BTC-USD", 2)
        ws._check_sequence("level2", "BTC-USD", 3)

        assert not gap.called

    def test_market_trades_does_not_contaminate_level2_tracker(self):
        """
        market_trades BTC-USD seq 10,11 no debe afectar el tracker de level2.

        Escenario de fallo anterior:
          level2 seq 3 → market_trades seq 10 → level2 seq 4
          Con key=product_id: last["BTC-USD"]=10, next level2=4 → falso gap (4 != 11)
        """
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        # Establecer estado en level2
        ws._check_sequence("level2", "BTC-USD", 3)

        # Llegan mensajes de market_trades con su propia secuencia
        ws._check_sequence("market_trades", "BTC-USD", 10)
        ws._check_sequence("market_trades", "BTC-USD", 11)

        # Continúa level2 de forma consecutiva — no debe ser gap
        ws._check_sequence("level2", "BTC-USD", 4)

        assert not gap.called

    def test_level2_real_gap_detected(self):
        """level2 BTC-USD seq 3 → 5 → gap real detectado."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        ws._check_sequence("level2", "BTC-USD", 3)
        ws._check_sequence("level2", "BTC-USD", 5)  # falta 4

        assert gap.called

    def test_level2_btc_and_eth_independent(self):
        """level2 BTC-USD y level2 ETH-USD tienen trackers separados."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        ws._check_sequence("level2", "BTC-USD", 100)
        ws._check_sequence("level2", "ETH-USD", 200)

        # Continuar BTC de forma consecutiva — sin gap
        ws._check_sequence("level2", "BTC-USD", 101)
        # ETH salta (gap real)
        ws._check_sequence("level2", "ETH-USD", 205)

        # Solo un gap en ETH
        assert gap.call_count == 1

    def test_candles_market_trades_level2_same_symbol_no_false_gaps(self):
        """
        candles + market_trades + level2 para BTC-USD intercalados → 0 falsos gaps.

        Replica la condición exacta que explotó en shadow-live:
        todos los canales generan sequence_nums desde 1 de forma independiente.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        # Simular mensajes intercalados como llegan en producción
        # Cada canal empieza su propia secuencia desde 1
        interleaved = [
            ("candles", "BTC-USD", 1),
            ("level2", "BTC-USD", 1),
            ("market_trades", "BTC-USD", 1),
            ("candles", "BTC-USD", 2),
            ("level2", "BTC-USD", 2),
            ("market_trades", "BTC-USD", 2),
            ("level2", "BTC-USD", 3),
            ("candles", "BTC-USD", 3),
            ("market_trades", "BTC-USD", 3),
        ]

        for channel, product_id, seq in interleaved:
            ws._check_sequence(channel, product_id, seq)

        assert not gap.called, (
            f"No gaps expected, but gap_detected was called {gap.call_count} time(s)"
        )


class TestHeartbeatsNoCrossContamination:
    """Test que heartbeats no causen cross-contamination."""

    def test_heartbeats_delivered_to_all(self):
        """
        Heartbeats deben llegar a todos los suscriptores sin filtrar por product_id.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        callback1 = MagicMock()
        callback2 = MagicMock()

        # Suscribir a heartbeats (sin product_ids)
        ws.subscribe("heartbeats", [], callback1)
        ws.subscribe("heartbeats", [], callback2)

        # Simular heartbeat
        heartbeat_msg = {"channel": "heartbeats", "events": [{"heartbeat_counter": "42"}]}

        ws._on_market_message(None, json.dumps(heartbeat_msg))

        # Ambos callbacks deben ser llamados
        assert callback1.called
        assert callback2.called


class TestUserChannelRouting:
    """Test P0: User channel callback routing."""

    def test_user_channel_routing_uses_orders_product_ids(self):
        """
        Verificar P0: user channel usa orders[].product_id, no events[0].product_id.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        cb = MagicMock()
        ws.subscribe("user", ["BTC-USD", "ETH-USD"], cb)

        user_msg = {
            "channel": "user",
            "events": [
                {
                    "type": "snapshot",
                    "orders": [
                        {
                            "order_id": "o-1",
                            "client_order_id": "c-1",
                            "product_id": "BTC-USD",
                            "status": "OPEN",
                        }
                    ],
                }
            ],
        }

        ws._on_user_message(None, json.dumps(user_msg))
        assert cb.called, "user callback should be called using orders[].product_id"

    def test_user_channel_multiple_products(self):
        """
        Verificar que user channel enrute correctamente con múltiples productos.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_cb = MagicMock()
        eth_cb = MagicMock()

        ws.subscribe("user", ["BTC-USD"], btc_cb)
        ws.subscribe("user", ["ETH-USD"], eth_cb)

        user_msg = {
            "channel": "user",
            "events": [
                {
                    "type": "snapshot",
                    "orders": [
                        {"order_id": "o-1", "product_id": "BTC-USD", "status": "OPEN"},
                        {"order_id": "o-2", "product_id": "ETH-USD", "status": "OPEN"},
                    ],
                }
            ],
        }

        ws._on_user_message(None, json.dumps(user_msg))

        assert btc_cb.called, "BTC callback should be called"
        assert eth_cb.called, "ETH callback should be called"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
