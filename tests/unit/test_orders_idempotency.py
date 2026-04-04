"""
Tests de idempotencia de OrderExecutor.

Invariantes testeadas:
- LIMIT order creada exitosamente → estado OPEN_RESTING (no FILLED)
- MARKET order tras ACK → estado OPEN_PENDING (no FILLED) — P1 FIX
- exchange_order_id y client_order_id enlazados correctamente
- error en exchange → estado FAILED, intent persistido
- cancel de orden activa → succeeds
- cancel de orden inexistente → False
- cancel de orden terminal (FILLED) → False, estado no cambia
- cancel transiciona a CANCEL_QUEUED (no directamente a CANCELLED)
- orders_last_minute tracking funciona
- submit_intent: client_order_id determinista preservado end-to-end
- submit_intent: duplicate detectado → no reenvío al exchange
- submit_intent: error exchange → FAILED, sin uuid4
- from_planner_intent: conversión correcta desde OrderPlanner.OrderIntent
"""

from decimal import Decimal
from unittest.mock import MagicMock

from src.core.coinbase_exchange import CoinbaseAPIError
from src.core.quantization import ProductInfo, Quantizer
from src.execution.idempotency import (
    IdempotencyStore,
    OrderState,
)
from src.execution.idempotency import (
    OrderIntent as IdempotencyOrderIntent,
)
from src.execution.order_planner import OrderIntent as PlannerOrderIntent
from src.execution.order_planner import _make_client_order_id
from src.execution.orders import OrderExecutor


def make_store(tmp_path, name: str = "orders") -> IdempotencyStore:
    db_path = str(tmp_path / f"{name}.db")
    return IdempotencyStore(db_path=db_path)


def make_quantizer() -> Quantizer:
    return Quantizer(
        ProductInfo(
            product_id="BTC-USD",
            base_increment=Decimal("0.00000001"),
            quote_increment=Decimal("0.01"),
            min_market_funds=Decimal("1"),
            base_currency="BTC",
            quote_currency="USD",
        )
    )


def make_executor(store: IdempotencyStore, mock_client: MagicMock) -> OrderExecutor:
    return OrderExecutor(
        client=mock_client,
        idempotency=store,
        quantizer=make_quantizer(),
    )


class TestLimitOrderCreation:

    def test_limit_order_created_with_open_resting(self, tmp_path):
        """Limit order creada exitosamente → estado OPEN_RESTING."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-limit-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order(
            product_id="BTC-USD",
            side="BUY",
            qty=Decimal("0.001"),
            price=Decimal("50000"),
        )

        assert result.success is True
        assert result.state == OrderState.OPEN_RESTING
        assert result.exchange_order_id == "ex-limit-001"

    def test_limit_order_exchange_and_client_ids_linked(self, tmp_path):
        """exchange_order_id y client_order_id enlazados correctamente en store."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-linked-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        record = store.get_by_client_order_id(result.client_order_id)
        assert record is not None
        assert record.exchange_order_id == "ex-linked-001"
        assert record.client_order_id == result.client_order_id

    def test_limit_order_intent_persisted_on_success(self, tmp_path):
        """Intent persiste en store tras creación exitosa."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-persist-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        record = store.get_by_intent_id(result.intent_id)
        assert record is not None
        assert record.state == OrderState.OPEN_RESTING

    def test_limit_order_exchange_error_sets_failed(self, tmp_path):
        """Error en exchange → estado FAILED, intent guardado en store."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.side_effect = CoinbaseAPIError("rejected by exchange")

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        assert result.success is False
        assert result.state == OrderState.FAILED

        record = store.get_by_intent_id(result.intent_id)
        assert record is not None
        assert record.state == OrderState.FAILED

    def test_limit_order_failed_not_in_pending_or_open(self, tmp_path):
        """Intent FAILED no aparece en get_pending_or_open()."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.side_effect = CoinbaseAPIError("error")

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert result.intent_id not in active_ids


class TestMarketOrderCreation:

    def test_market_order_after_ack_is_open_pending(self, tmp_path):
        """
        MARKET order tras ACK → OPEN_PENDING, NO FILLED.

        P1 FIX: ACK confirma recepción, no ejecución.
        Solo user channel o REST reconcile puede mover a FILLED.
        """
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-market-001"}

        executor = make_executor(store, client)
        result = executor.create_market_order("BTC-USD", "BUY", qty=Decimal("0.001"))

        assert result.success is True
        assert result.state == OrderState.OPEN_PENDING
        assert result.state != OrderState.FILLED

    def test_market_order_state_not_filled_in_store(self, tmp_path):
        """MARKET order en store tras ACK: OPEN_PENDING, no FILLED."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-market-002"}

        executor = make_executor(store, client)
        result = executor.create_market_order("BTC-USD", "SELL", qty=Decimal("0.001"))

        record = store.get_by_intent_id(result.intent_id)
        assert record.state == OrderState.OPEN_PENDING
        assert record.state != OrderState.FILLED

    def test_market_order_exchange_id_linked_in_store(self, tmp_path):
        """exchange_order_id enlazado en store para market orders."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-market-link"}

        executor = make_executor(store, client)
        result = executor.create_market_order("BTC-USD", "BUY", qty=Decimal("0.001"))

        record = store.get_by_client_order_id(result.client_order_id)
        assert record is not None
        assert record.exchange_order_id == "ex-market-link"


class TestCancelOrder:

    def test_cancel_active_limit_order_succeeds(self, tmp_path):
        """Cancelar orden OPEN_RESTING activa → retorna True."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-cancel-001"}
        client.cancel_orders.return_value = [{"success": True}]

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        cancelled = executor.cancel_order(result.intent_id)
        assert cancelled is True

    def test_cancel_transitions_to_cancel_queued(self, tmp_path):
        """
        cancel_order → estado CANCEL_QUEUED (no directamente CANCELLED).

        El estado final CANCELLED solo llega via user channel o reconcile.
        CANCEL_QUEUED indica que la solicitud fue aceptada pero no confirmada aún.
        """
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-cq-001"}
        client.cancel_orders.return_value = [{"success": True}]

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        executor.cancel_order(result.intent_id)

        record = store.get_by_intent_id(result.intent_id)
        assert record.state == OrderState.CANCEL_QUEUED, (
            f"cancel_order debe transicionar a CANCEL_QUEUED, got {record.state.name}. "
            "El estado final CANCELLED llega via user channel/reconcile."
        )

    def test_cancel_nonexistent_order_returns_false(self, tmp_path):
        """Cancelar intent inexistente → False."""
        store = make_store(tmp_path)
        client = MagicMock()
        executor = make_executor(store, client)

        result = executor.cancel_order("nonexistent-intent-id-xyz")
        assert result is False

    def test_cancel_already_filled_returns_false(self, tmp_path):
        """Cancelar orden en estado FILLED → False, estado no cambia."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-filled-001"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        # Simular user channel marcando como FILLED
        store.update_state(intent_id=result.intent_id, state=OrderState.FILLED)

        cancelled = executor.cancel_order(result.intent_id)
        assert cancelled is False

        # Estado terminal no modificado
        record = store.get_by_intent_id(result.intent_id)
        assert record.state == OrderState.FILLED

    def test_cancel_already_cancelled_returns_false(self, tmp_path):
        """Cancelar orden ya CANCELLED → False."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-already-cancelled"}

        executor = make_executor(store, client)
        result = executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))
        store.update_state(intent_id=result.intent_id, state=OrderState.CANCELLED)

        cancelled = executor.cancel_order(result.intent_id)
        assert cancelled is False


class TestOrdersLastMinute:

    def test_orders_last_minute_zero_initially(self, tmp_path):
        """orders_last_minute es 0 al inicio."""
        store = make_store(tmp_path)
        client = MagicMock()
        executor = make_executor(store, client)

        assert executor.get_orders_last_minute() == 0

    def test_orders_last_minute_increments_after_limit_order(self, tmp_path):
        """Crear orden límite incrementa orders_last_minute."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-rate-001"}

        executor = make_executor(store, client)
        executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        assert executor.get_orders_last_minute() == 1

    def test_failed_order_does_not_count_in_rate(self, tmp_path):
        """Orden fallida (exchange error) no debe contar en rate limit."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.side_effect = CoinbaseAPIError("error")

        executor = make_executor(store, client)
        executor.create_limit_order("BTC-USD", "BUY", Decimal("0.001"), Decimal("50000"))

        # Orden fallida no incrementa el rate (no llegó al exchange)
        assert executor.get_orders_last_minute() == 0


# ──────────────────────────────────────────────────────────────
# submit_intent: client_order_id determinista end-to-end
# ──────────────────────────────────────────────────────────────


def make_planner_intent(
    signal_id: str = "sig-abc-001",
    symbol: str = "BTC-USD",
    side: str = "BUY",
    final_qty: Decimal = Decimal("0.002"),
    order_type: str = "MARKET",
    price=None,
    reduce_only: bool = False,
) -> PlannerOrderIntent:
    """Construir PlannerOrderIntent mínimo para tests de submit_intent."""
    return PlannerOrderIntent(
        client_order_id=_make_client_order_id(signal_id, symbol),
        signal_id=signal_id,
        strategy_id="test_strategy",
        symbol=symbol,
        side=side,
        final_qty=final_qty,
        order_type=order_type,
        price=price,
        reduce_only=reduce_only,
        viable=True,
        planner_version="1.0",
    )


class TestSubmitIntentDeterministicId:

    def test_submit_intent_uses_planner_client_order_id(self, tmp_path):
        """submit_intent usa client_order_id del planner (sha256), no uuid4."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-det-001"}

        executor = make_executor(store, client)
        planner_intent = make_planner_intent(signal_id="signal-xyz-001")

        result = executor.submit_intent(planner_intent)

        assert result.success is True
        assert result.client_order_id == planner_intent.client_order_id
        assert result.intent_id == planner_intent.client_order_id
        # Verificar que es el sha256 determinista, no un uuid
        expected_coid = _make_client_order_id("signal-xyz-001", "BTC-USD")
        assert result.client_order_id == expected_coid

    def test_submit_intent_same_signal_same_client_order_id(self, tmp_path):
        """Mismo signal_id+symbol siempre produce el mismo client_order_id."""
        signal_id = "signal-determinism-001"
        intent1 = make_planner_intent(signal_id=signal_id)
        intent2 = make_planner_intent(signal_id=signal_id)

        assert intent1.client_order_id == intent2.client_order_id
        assert intent1.client_order_id == _make_client_order_id(signal_id, "BTC-USD")

    def test_submit_intent_different_signals_different_ids(self, tmp_path):
        """Señales distintas producen client_order_id distintos."""
        intent1 = make_planner_intent(signal_id="sig-aaa-001")
        intent2 = make_planner_intent(signal_id="sig-bbb-002")

        assert intent1.client_order_id != intent2.client_order_id

    def test_submit_intent_client_order_id_propagated_to_exchange(self, tmp_path):
        """client_order_id determinista se pasa al exchange sin modificación."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-prop-001"}

        executor = make_executor(store, client)
        planner_intent = make_planner_intent(signal_id="signal-prop-001")

        executor.submit_intent(planner_intent)

        # Verificar que el exchange recibió exactamente el client_order_id del planner
        call_kwargs = client.create_market_order.call_args
        assert call_kwargs.kwargs["client_order_id"] == planner_intent.client_order_id

    def test_submit_intent_market_order_state_open_pending(self, tmp_path):
        """MARKET order via submit_intent → OPEN_PENDING (no FILLED tras ACK)."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-mkt-intent-001"}

        executor = make_executor(store, client)
        result = executor.submit_intent(make_planner_intent(order_type="MARKET"))

        assert result.success is True
        assert result.state == OrderState.OPEN_PENDING

    def test_submit_intent_limit_order_state_open_resting(self, tmp_path):
        """LIMIT order via submit_intent → OPEN_RESTING."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_limit_order_gtc.return_value = {"order_id": "ex-lmt-intent-001"}

        executor = make_executor(store, client)
        result = executor.submit_intent(
            make_planner_intent(
                order_type="LIMIT",
                price=Decimal("50000"),
            )
        )

        assert result.success is True
        assert result.state == OrderState.OPEN_RESTING

    def test_submit_intent_duplicate_skips_exchange(self, tmp_path):
        """Mismo intent_id ya en store → exchange no recibe segunda llamada."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.return_value = {"order_id": "ex-dedup-001"}

        executor = make_executor(store, client)
        planner_intent = make_planner_intent(signal_id="signal-dedup-001")

        result1 = executor.submit_intent(planner_intent)
        result2 = executor.submit_intent(planner_intent)  # duplicado

        assert result1.client_order_id == result2.client_order_id
        # El exchange solo fue llamado una vez
        assert client.create_market_order.call_count == 1
        # El segundo resultado refleja el estado almacenado
        assert result2.state == OrderState.OPEN_PENDING

    def test_submit_intent_exchange_error_sets_failed(self, tmp_path):
        """Error en exchange via submit_intent → FAILED, client_order_id intacto."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.side_effect = CoinbaseAPIError("rejected")

        executor = make_executor(store, client)
        planner_intent = make_planner_intent(signal_id="signal-err-001")

        result = executor.submit_intent(planner_intent)

        assert result.success is False
        assert result.state == OrderState.FAILED
        assert result.client_order_id == planner_intent.client_order_id

    def test_submit_intent_failed_order_not_in_pending_or_open(self, tmp_path):
        """Intent FAILED via submit_intent no aparece en get_pending_or_open()."""
        store = make_store(tmp_path)
        client = MagicMock()
        client.create_market_order.side_effect = CoinbaseAPIError("error")

        executor = make_executor(store, client)
        result = executor.submit_intent(make_planner_intent(signal_id="signal-fail-002"))

        active_ids = [r.intent_id for r in store.get_pending_or_open()]
        assert result.intent_id not in active_ids


class TestFromPlannerIntent:

    def test_from_planner_intent_preserves_client_order_id(self):
        """from_planner_intent preserva client_order_id sin generar uuid4."""
        planner_intent = make_planner_intent(signal_id="signal-conv-001")

        idem_intent = IdempotencyOrderIntent.from_planner_intent(planner_intent)

        assert idem_intent.client_order_id == planner_intent.client_order_id
        assert idem_intent.intent_id == planner_intent.client_order_id

    def test_from_planner_intent_maps_fields_correctly(self):
        """from_planner_intent mapea todos los campos correctamente."""
        planner_intent = make_planner_intent(
            signal_id="signal-map-001",
            symbol="ETH-USD",
            side="SELL",
            final_qty=Decimal("0.5"),
            order_type="LIMIT",
            price=Decimal("3000"),
        )

        idem_intent = IdempotencyOrderIntent.from_planner_intent(planner_intent)

        assert idem_intent.product_id == "ETH-USD"
        assert idem_intent.side == "SELL"
        assert idem_intent.order_type == "LIMIT"
        assert idem_intent.qty == Decimal("0.5")
        assert idem_intent.price == Decimal("3000")

    def test_from_planner_intent_overrides_qty_and_price(self):
        """from_planner_intent acepta qty y price cuantizados como override."""
        planner_intent = make_planner_intent(
            final_qty=Decimal("0.002345"),
            order_type="LIMIT",
            price=Decimal("50001.50"),
        )

        q_qty = Decimal("0.002")
        q_price = Decimal("50001")
        idem_intent = IdempotencyOrderIntent.from_planner_intent(
            planner_intent,
            qty=q_qty,
            price=q_price,
        )

        assert idem_intent.qty == q_qty
        assert idem_intent.price == q_price
        # client_order_id sigue siendo el del planner
        assert idem_intent.client_order_id == planner_intent.client_order_id

    def test_from_planner_intent_intent_id_equals_client_order_id(self):
        """intent_id == client_order_id para intents del planner (1:1)."""
        planner_intent = make_planner_intent(signal_id="signal-1to1-001")
        idem_intent = IdempotencyOrderIntent.from_planner_intent(planner_intent)

        assert idem_intent.intent_id == idem_intent.client_order_id
