"""
Tests para WebSocket callback routing y sequence tracking.

Verifica corrección de bugs:
- WS callback routing por (channel, product_id)
- Sequence tracking por producto (no global)
"""

import json
import sys
from unittest.mock import MagicMock

sys.path.insert(0, "/mnt/okcomputer/output/fortress_v4")

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
    """Test que sequence_num se tracke por producto, no globalmente."""

    def test_sequence_tracking_per_product(self):
        """
        Verificar que sequence_num se tracke independientemente por producto.

        Bug corregido: sequence_num global generaba false positives de gap
        en multi-producto y multi-canal.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        # Simular mensajes con sequence_num diferentes para cada producto
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
            "sequence_num": 50,  # Secuencia diferente para ETH
            "events": [{"product_id": "ETH-USD"}],
        }

        # Procesar mensajes
        ws._on_market_message(None, json.dumps(btc_msg1))
        assert ws._last_sequence_num.get("BTC-USD") == 100

        ws._on_market_message(None, json.dumps(eth_msg1))
        assert ws._last_sequence_num.get("ETH-USD") == 50
        # BTC no debe verse afectado
        assert ws._last_sequence_num.get("BTC-USD") == 100

        ws._on_market_message(None, json.dumps(btc_msg2))
        assert ws._last_sequence_num.get("BTC-USD") == 101
        # ETH no debe verse afectado
        assert ws._last_sequence_num.get("ETH-USD") == 50

    def test_gap_detection_per_product(self):
        """
        Verificar que gap detection funcione por producto.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap_detected = MagicMock()
        ws.on_gap_detected = gap_detected

        # Mensajes consecutivos para BTC
        ws._check_sequence("BTC-USD", 100)
        ws._check_sequence("BTC-USD", 101)

        # No debe haber gap
        assert not gap_detected.called

        # Gap en ETH no debe afectar BTC
        ws._check_sequence("ETH-USD", 50)
        ws._check_sequence("ETH-USD", 52)  # Gap: falta 51

        # Gap debe ser detectado
        assert gap_detected.called


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

        Bug P0: _extract_product_id() busca events[0].product_id pero en el schema
        oficial de user, product_id está dentro de cada orden (orders[].product_id).
        Resultado: product_id="" y el filtro descarta el callback.
        Impacto: OMSReconcileService queda muerto aunque el canal user esté conectado.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        cb = MagicMock()
        ws.subscribe("user", ["BTC-USD", "ETH-USD"], cb)

        # Mensaje user con schema oficial de Coinbase
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

        # Mensaje con órdenes de ambos productos
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

        # Ambos callbacks deben ser llamados porque el mensaje contiene ambos productos
        assert btc_cb.called, "BTC callback should be called"
        assert eth_cb.called, "ETH callback should be called"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
