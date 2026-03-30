"""
Tests para WebSocket callback routing y gap detection.

Verifica:
- WS callback routing por (channel, product_id)
- _extract_product_id handles all Coinbase WS schemas correctly
- Heartbeat-based gap detection (sole mechanism)
- sequence_num NOT used in hot path (global counter produces false gaps)
"""

import json
from unittest.mock import MagicMock

from src.core.coinbase_websocket import CoinbaseWSFeed


# ---------------------------------------------------------------------------
# Helpers: realistic Coinbase WS message builders
# ---------------------------------------------------------------------------

def _candles_msg(product_id: str, seq: int = 100) -> dict:
    """Build a realistic Coinbase candles WS message.

    Schema: product_id is inside events[0].candles[].product_id, NOT at event level.
    """
    return {
        "channel": "candles",
        "sequence_num": seq,
        "events": [
            {
                "type": "candle",
                "candles": [
                    {
                        "start": "1743000000",
                        "open": "85000",
                        "high": "86000",
                        "low": "84000",
                        "close": "85500",
                        "volume": "10.5",
                        "product_id": product_id,
                    }
                ],
            }
        ],
    }


def _market_trades_msg(product_id: str, seq: int = 100) -> dict:
    """Build a realistic Coinbase market_trades WS message.

    Schema: product_id is inside events[0].trades[].product_id, NOT at event level.
    """
    return {
        "channel": "market_trades",
        "sequence_num": seq,
        "events": [
            {
                "type": "update",
                "trades": [
                    {
                        "trade_id": "t-1",
                        "product_id": product_id,
                        "price": "85000",
                        "size": "0.001",
                        "side": "BUY",
                        "time": "2026-03-29T00:00:00Z",
                    }
                ],
            }
        ],
    }


def _l2_data_msg(product_id: str, seq: int = 100) -> dict:
    """Build a realistic Coinbase level2 (l2_data) WS message.

    Schema: product_id is at events[0].product_id (event level).
    """
    return {
        "channel": "l2_data",
        "sequence_num": seq,
        "events": [
            {
                "type": "snapshot",
                "product_id": product_id,
                "updates": [],
            }
        ],
    }


def _heartbeat_msg(counter: int) -> dict:
    return {"channel": "heartbeats", "events": [{"heartbeat_counter": str(counter)}]}


# ---------------------------------------------------------------------------
# _extract_product_id tests
# ---------------------------------------------------------------------------

class TestExtractProductId:
    """Verify _extract_product_id handles all Coinbase WS channel schemas."""

    def test_candles_product_id_inside_candles_array(self):
        """candles channel: product_id is inside events[0].candles[0]."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        msg = _candles_msg("BTC-USD")
        assert ws._extract_product_id(msg) == "BTC-USD"

    def test_market_trades_product_id_inside_trades_array(self):
        """market_trades channel: product_id is inside events[0].trades[0]."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        msg = _market_trades_msg("ETH-USD")
        assert ws._extract_product_id(msg) == "ETH-USD"

    def test_l2_data_product_id_at_event_level(self):
        """l2_data channel: product_id is at events[0] level."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        msg = _l2_data_msg("BTC-USD")
        assert ws._extract_product_id(msg) == "BTC-USD"

    def test_heartbeat_returns_empty(self):
        """heartbeats have no product_id."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        msg = _heartbeat_msg(42)
        assert ws._extract_product_id(msg) == ""

    def test_empty_events_returns_empty(self):
        ws = CoinbaseWSFeed(jwt_auth=None)
        assert ws._extract_product_id({"events": []}) == ""
        assert ws._extract_product_id({}) == ""

    def test_event_level_product_id_takes_precedence(self):
        """If product_id exists at event level, use it even if nested also exists."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        msg = {
            "channel": "candles",
            "events": [
                {
                    "product_id": "ETH-USD",
                    "candles": [{"product_id": "BTC-USD"}],
                }
            ],
        }
        assert ws._extract_product_id(msg) == "ETH-USD"


# ---------------------------------------------------------------------------
# Callback routing tests
# ---------------------------------------------------------------------------

class TestWSCallbackRouting:
    """Test que callbacks sean enrutados correctamente por product_id."""

    def test_candles_callback_fires_for_correct_product(self):
        """candles BTC-USD callback must fire for BTC-USD candle message."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_callback = MagicMock()
        eth_callback = MagicMock()

        ws.subscribe("candles", ["BTC-USD"], btc_callback)
        ws.subscribe("candles", ["ETH-USD"], eth_callback)

        ws._on_market_message(None, json.dumps(_candles_msg("BTC-USD")))

        assert btc_callback.called, "BTC callback should be called for BTC candle"
        assert not eth_callback.called, "ETH callback should NOT be called for BTC candle"

    def test_market_trades_callback_fires_for_correct_product(self):
        """market_trades BTC-USD callback must fire for BTC-USD trade message."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_callback = MagicMock()
        eth_callback = MagicMock()

        ws.subscribe("market_trades", ["BTC-USD"], btc_callback)
        ws.subscribe("market_trades", ["ETH-USD"], eth_callback)

        ws._on_market_message(None, json.dumps(_market_trades_msg("BTC-USD")))

        assert btc_callback.called, "BTC callback should fire for BTC trade"
        assert not eth_callback.called, "ETH callback should NOT fire for BTC trade"

    def test_l2_data_callback_fires_for_correct_product(self):
        """l2_data callback routing still works with event-level product_id."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        btc_callback = MagicMock()
        ws.subscribe("level2", ["BTC-USD"], btc_callback)

        ws._on_market_message(None, json.dumps(_l2_data_msg("BTC-USD")))

        assert btc_callback.called

    def test_multiple_products_same_callback(self):
        """Callback subscribed to multiple products receives all."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        multi_callback = MagicMock()
        ws.subscribe("market_trades", ["BTC-USD", "ETH-USD"], multi_callback)

        ws._on_market_message(None, json.dumps(_market_trades_msg("BTC-USD", seq=100)))
        assert multi_callback.called, "Callback should be called for BTC"

        multi_callback.reset_mock()

        ws._on_market_message(None, json.dumps(_market_trades_msg("ETH-USD", seq=101)))
        assert multi_callback.called, "Callback should be called for ETH"

    def test_candles_wrong_product_does_not_fire(self):
        """candles callback for ETH-USD must NOT fire when BTC-USD candle arrives."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        eth_callback = MagicMock()
        ws.subscribe("candles", ["ETH-USD"], eth_callback)

        ws._on_market_message(None, json.dumps(_candles_msg("BTC-USD")))

        assert not eth_callback.called


# ---------------------------------------------------------------------------
# Sequence num not used in hot path
# ---------------------------------------------------------------------------

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

        messages = [
            _candles_msg("BTC-USD", seq=1),
            _l2_data_msg("BTC-USD", seq=2),
            _market_trades_msg("BTC-USD", seq=3),
            _candles_msg("BTC-USD", seq=4),
            _l2_data_msg("BTC-USD", seq=5),
            _market_trades_msg("BTC-USD", seq=6),
            _candles_msg("BTC-USD", seq=7),
            _l2_data_msg("BTC-USD", seq=8),
            _market_trades_msg("BTC-USD", seq=9),
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

        for seq in [2, 5, 8, 11, 14]:
            ws._on_market_message(None, json.dumps(_l2_data_msg("BTC-USD", seq=seq)))

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


# ---------------------------------------------------------------------------
# Heartbeat gap detection
# ---------------------------------------------------------------------------

class TestHeartbeatGapDetection:
    """Test that heartbeat_counter is the sole gap detection mechanism."""

    def test_heartbeats_delivered_to_all(self):
        """Heartbeats must reach all subscribers without product_id filtering."""
        ws = CoinbaseWSFeed(jwt_auth=None)

        callback1 = MagicMock()
        callback2 = MagicMock()

        ws.subscribe("heartbeats", [], callback1)
        ws.subscribe("heartbeats", [], callback2)

        ws._on_market_message(None, json.dumps(_heartbeat_msg(42)))

        assert callback1.called
        assert callback2.called

    def test_heartbeat_gap_triggers_on_gap_detected(self):
        """Heartbeat gap must trigger on_gap_detected callback."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        ws._on_market_message(None, json.dumps(_heartbeat_msg(100)))
        assert not gap.called

        ws._on_market_message(None, json.dumps(_heartbeat_msg(101)))
        assert not gap.called

        ws._on_market_message(None, json.dumps(_heartbeat_msg(103)))  # gap: 102 missed
        assert gap.called

    def test_heartbeat_contiguous_no_gap(self):
        """Contiguous heartbeats must not trigger gap detection."""
        ws = CoinbaseWSFeed(jwt_auth=None)
        gap = MagicMock()
        ws.on_gap_detected = gap

        for i in range(100, 200):
            ws._on_market_message(None, json.dumps(_heartbeat_msg(i)))

        assert not gap.called


# ---------------------------------------------------------------------------
# User channel routing
# ---------------------------------------------------------------------------

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
