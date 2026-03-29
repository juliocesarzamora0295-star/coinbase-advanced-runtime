"""
Tests para WebSocket callback routing y gap detection.

Verifica:
- WS callback routing por (channel, product_id)
- Heartbeat-based gap detection (sole mechanism)
- sequence_num NOT used in hot path (global counter produces false gaps)
"""

import json
from unittest.mock import MagicMock

from src.core.coinbase_websocket import CoinbaseWSFeed


class TestWSCallbackRouting:
    """Test que callbacks sean enrutados correctamente por product_id."""

    def test_callback_routing_by_product_id(self):
        """
        Verificar que callbacks para diferentes product_ids no se contaminen.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_callback = MagicMock()
        eth_callback = MagicMock()

        ws.subscribe("candles", ["BTC-USD"], btc_callback)
        ws.subscribe("candles", ["ETH-USD"], eth_callback)

        btc_message = {
            "channel": "candles",
            "sequence_num": 100,
            "events": [{"product_id": "BTC-USD", "candles": []}],
        }

        ws._on_market_message(None, json.dumps(btc_message))

        assert btc_callback.called, "BTC callback should be called for BTC message"
        assert not eth_callback.called, "ETH callback should NOT be called for BTC message"

    def test_multiple_products_same_callback(self):
        """
        Verificar que un callback suscrito a múltiples productos reciba todos.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)

        multi_callback = MagicMock()
        ws.subscribe("market_trades", ["BTC-USD", "ETH-USD"], multi_callback)

        btc_message = {
            "channel": "market_trades",
            "sequence_num": 100,
            "events": [{"product_id": "BTC-USD", "trades": []}],
        }
        ws._on_market_message(None, json.dumps(btc_message))
        assert multi_callback.called, "Callback should be called for BTC"

        multi_callback.reset_mock()

        eth_message = {
            "channel": "market_trades",
            "sequence_num": 101,
            "events": [{"product_id": "ETH-USD", "trades": []}],
        }
        ws._on_market_message(None, json.dumps(eth_message))
        assert multi_callback.called, "Callback should be called for ETH"


class TestSequenceNumNotUsedInHotPath:
    """
    Verify that _on_market_message does NOT trigger gap detection via sequence_num.

    Root cause: Coinbase WS sequence_num is a GLOBAL per-connection counter.
    It increments across ALL channels (candles, l2_data, market_trades, heartbeats).
    Tracking it per-channel or per-product produces false gaps every time a message
    from a different channel arrives between two messages of the tracked channel.

    The sole gap detection mechanism is heartbeat_counter, which IS contiguous.
    """

    def test_interleaved_channels_no_false_gaps(self):
        """
        Messages from candles/l2_data/market_trades interleaved with non-contiguous
        per-channel sequence numbers must NOT trigger gap detection.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        # Simulate the exact interleaving pattern that caused the gap storm:
        # Global sequence 1-9, each channel sees only every 3rd number.
        messages = [
            {"channel": "candles", "sequence_num": 1, "events": [{"product_id": "BTC-USD"}]},
            {"channel": "l2_data", "sequence_num": 2, "events": [{"product_id": "BTC-USD"}]},
            {"channel": "market_trades", "sequence_num": 3, "events": [{"product_id": "BTC-USD", "trades": []}]},
            {"channel": "candles", "sequence_num": 4, "events": [{"product_id": "BTC-USD"}]},
            {"channel": "l2_data", "sequence_num": 5, "events": [{"product_id": "BTC-USD"}]},
            {"channel": "market_trades", "sequence_num": 6, "events": [{"product_id": "BTC-USD", "trades": []}]},
            {"channel": "candles", "sequence_num": 7, "events": [{"product_id": "BTC-USD"}]},
            {"channel": "l2_data", "sequence_num": 8, "events": [{"product_id": "BTC-USD"}]},
            {"channel": "market_trades", "sequence_num": 9, "events": [{"product_id": "BTC-USD", "trades": []}]},
        ]

        for msg in messages:
            ws._on_market_message(None, json.dumps(msg))

        assert not gap.called, (
            f"No gaps expected from interleaved channels, but gap_detected "
            f"called {gap.call_count} time(s)"
        )

    def test_l2_data_with_gaps_in_sequence_no_trigger(self):
        """
        Even if l2_data sequence numbers are non-contiguous (because global counter
        includes other channels), _on_market_message must not trigger gap detection.
        """
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        # l2_data sees sequence 2, 5, 8 (others consumed by candles/market_trades)
        for seq in [2, 5, 8, 11, 14]:
            msg = {
                "channel": "l2_data",
                "sequence_num": seq,
                "events": [{"product_id": "BTC-USD"}],
            }
            ws._on_market_message(None, json.dumps(msg))

        assert not gap.called


class TestCheckSequenceMethodRetained:
    """
    _check_sequence is retained as a method but NOT called from _on_market_message.
    These tests verify the method still works correctly for direct invocation.
    """

    def test_check_sequence_detects_real_gap(self):
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        ws._check_sequence("level2", "BTC-USD", 100)
        ws._check_sequence("level2", "BTC-USD", 101)
        assert not gap.called

        ws._check_sequence("level2", "BTC-USD", 104)  # gap: 102,103 missed
        assert gap.called

    def test_check_sequence_per_channel_key(self):
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        ws._check_sequence("level2", "BTC-USD", 100)
        ws._check_sequence("candles", "BTC-USD", 50)  # different channel, no gap
        ws._check_sequence("level2", "BTC-USD", 101)  # contiguous within level2

        assert not gap.called


class TestHeartbeatGapDetection:
    """Test that heartbeat_counter is the sole gap detection mechanism."""

    def test_heartbeats_delivered_to_all(self):
        """Heartbeats must reach all subscribers without product_id filtering."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        callback1 = MagicMock()
        callback2 = MagicMock()

        ws.subscribe("heartbeats", [], callback1)
        ws.subscribe("heartbeats", [], callback2)

        heartbeat_msg = {"channel": "heartbeats", "events": [{"heartbeat_counter": "42"}]}
        ws._on_market_message(None, json.dumps(heartbeat_msg))

        assert callback1.called
        assert callback2.called

    def test_heartbeat_gap_triggers_on_gap_detected(self):
        """Heartbeat gap must trigger on_gap_detected callback."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        # Send heartbeat 1
        msg1 = {"channel": "heartbeats", "events": [{"heartbeat_counter": "100"}]}
        ws._on_market_message(None, json.dumps(msg1))
        assert not gap.called

        # Send heartbeat 2 (contiguous)
        msg2 = {"channel": "heartbeats", "events": [{"heartbeat_counter": "101"}]}
        ws._on_market_message(None, json.dumps(msg2))
        assert not gap.called

        # Send heartbeat 4 (gap: 102 missed)
        msg3 = {"channel": "heartbeats", "events": [{"heartbeat_counter": "103"}]}
        ws._on_market_message(None, json.dumps(msg3))
        assert gap.called

    def test_heartbeat_contiguous_no_gap(self):
        """Contiguous heartbeats must not trigger gap detection."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        for i in range(100, 200):
            msg = {"channel": "heartbeats", "events": [{"heartbeat_counter": str(i)}]}
            ws._on_market_message(None, json.dumps(msg))

        assert not gap.called


class TestUserChannelRouting:
    """Test P0: User channel callback routing."""

    def test_user_channel_routing_uses_orders_product_ids(self):
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
