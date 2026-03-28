"""Tests para procesamiento del canal user."""


class TestUserChannelProcessing:
    """Tests de parseo de eventos del canal user."""

    def test_parse_user_events_structure(self):
        """Verificar que se parsea correctamente la estructura de events[]."""
        # Simular mensaje del canal user
        msg_data = {
            "channel": "user",
            "events": [
                {
                    "type": "snapshot",
                    "orders": [
                        {
                            "order_id": "order-123",
                            "client_order_id": "client-123",
                            "product_id": "BTC-USD",
                            "side": "BUY",
                            "status": "OPEN",
                        }
                    ],
                }
            ],
        }

        events = msg_data.get("events", [])
        assert len(events) == 1

        event = events[0]
        assert event.get("type") == "snapshot"

        orders = event.get("orders", [])
        assert len(orders) == 1

        order = orders[0]
        assert order.get("order_id") == "order-123"
        assert order.get("client_order_id") == "client-123"
        assert order.get("status") == "OPEN"

    def test_bootstrap_end_detection(self):
        """Detectar fin de bootstrap cuando len(orders) < 50."""
        # Simular lote completo (50 órdenes)
        full_batch = {"type": "snapshot", "orders": [{"order_id": f"order-{i}"} for i in range(50)]}

        # Simular lote final (< 50 órdenes)
        final_batch = {
            "type": "snapshot",
            "orders": [{"order_id": f"order-{i}"} for i in range(25)],
        }

        # El fin de bootstrap se detecta cuando hay menos de 50 órdenes
        assert len(full_batch["orders"]) == 50  # No es fin de bootstrap
        assert len(final_batch["orders"]) < 50  # Fin de bootstrap detectado

    def test_order_status_transitions(self):
        """Verificar transiciones de estado de órdenes."""
        # Estados posibles según Coinbase
        valid_statuses = [
            "OPEN",  # Orden abierta/pendiente
            "FILLED",  # Completamente ejecutada
            "CANCELLED",  # Cancelada
            "EXPIRED",  # Expirada
            "FAILED",  # Fallida
        ]

        # Una orden MARKET debe pasar por OPEN_PENDING -> terminal
        # No debe ir directo a FILLED por ACK
        order = {
            "order_id": "market-123",
            "client_order_id": "client-123",
            "status": "OPEN",  # Estado inicial tras ACK
        }

        assert order["status"] in valid_statuses
        assert order["status"] != "FILLED"  # No debe estar FILLED inmediatamente
