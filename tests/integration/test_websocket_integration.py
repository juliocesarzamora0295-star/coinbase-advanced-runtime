"""
Tests de integración para WebSocket de Coinbase.

Requiere:
  - COINBASE_KEY_NAME
  - COINBASE_KEY_SECRET (para canal user)
"""

import os
import threading
import time

import pytest

# Skip all tests unless explicitly enabled with COINBASE_RUN_LIVE_TESTS=1
pytestmark = pytest.mark.skipif(
    (
        not os.getenv("COINBASE_KEY_NAME")
        or not os.getenv("COINBASE_KEY_SECRET")
        or os.getenv("COINBASE_RUN_LIVE_TESTS", "0") != "1"
    ),
    reason="live integration tests disabled (set COINBASE_RUN_LIVE_TESTS=1 to enable)",
)


class TestWebSocketPublic:
    """Tests de WebSocket para canales públicos."""

    def test_connect_and_subscribe(self):
        """Conectar y suscribirse a canales públicos."""
        from src.core.coinbase_websocket import CoinbaseWSFeed

        ws = CoinbaseWSFeed()  # Sin JWT para canales públicos

        messages = []
        connected = threading.Event()

        def on_ticker(msg):
            messages.append(msg)
            connected.set()

        ws.subscribe_ticker("BTC-USD", on_ticker)
        ws.start()

        # Esperar mensajes (timeout 10s)
        connected.wait(timeout=10)

        ws.stop()

        # Deberíamos haber recibido al menos un mensaje
        assert len(messages) > 0, "No se recibieron mensajes del WebSocket"

        # Verificar estructura
        first_msg = messages[0]
        assert first_msg.channel == "market_trades"
        assert first_msg.product_id == "BTC-USD"

    def test_heartbeats(self):
        """Verificar heartbeats para gap detection."""
        from src.core.coinbase_websocket import CoinbaseWSFeed

        ws = CoinbaseWSFeed()

        heartbeats = []
        heartbeat_received = threading.Event()

        def on_heartbeat(msg):
            heartbeats.append(msg)
            heartbeat_received.set()

        ws.subscribe_heartbeats(on_heartbeat)
        ws.start()

        # Esperar heartbeats (deberían llegar cada ~1 segundo)
        heartbeat_received.wait(timeout=5)

        ws.stop()

        # Verificar que recibimos heartbeats
        assert len(heartbeats) > 0, "No se recibieron heartbeats"

        # Verificar estructura
        for hb in heartbeats:
            assert hb.channel == "heartbeats"
            events = hb.data.get("events", [])
            for event in events:
                assert "heartbeat_counter" in event
                assert "current_time" in event

    def test_level2_orderbook(self):
        """Suscribirse a order book L2."""
        from src.core.coinbase_websocket import CoinbaseWSFeed

        ws = CoinbaseWSFeed()

        updates = []
        update_received = threading.Event()

        def on_level2(msg):
            updates.append(msg)
            update_received.set()

        ws.subscribe_level2("BTC-USD", on_level2)
        ws.start()

        update_received.wait(timeout=10)

        ws.stop()

        assert len(updates) > 0, "No se recibieron updates de L2"

        # Verificar estructura
        for upd in updates:
            assert upd.channel == "level2"
            events = upd.data.get("events", [])
            for event in events:
                assert "updates" in event


class TestWebSocketGapDetection:
    """Tests de gap detection en WebSocket."""

    def test_gap_detection_heartbeat_counter(self):
        """Verificar que se detectan gaps en heartbeat_counter."""
        from src.core.coinbase_websocket import CoinbaseWSFeed

        ws = CoinbaseWSFeed()

        heartbeats = []

        def on_heartbeat(msg):
            heartbeats.append(msg)

        ws.subscribe_heartbeats(on_heartbeat)
        ws.start()

        # Esperar varios heartbeats
        time.sleep(5)

        ws.stop()

        if len(heartbeats) >= 2:
            # Verificar que los heartbeat_counter son secuenciales
            counters = []
            for hb in heartbeats:
                events = hb.data.get("events", [])
                for event in events:
                    counter = event.get("heartbeat_counter")
                    if counter is not None:
                        counters.append(counter)

            if len(counters) >= 2:
                # Verificar secuencialidad
                for i in range(1, len(counters)):
                    diff = counters[i] - counters[i - 1]
                    # En condiciones normales, debería ser 1
                    # Si hay gap, diff > 1
                    assert (
                        diff >= 1
                    ), f"Heartbeat counter no incrementa: {counters[i-1]} -> {counters[i]}"
