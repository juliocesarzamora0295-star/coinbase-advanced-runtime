"""
Tests unitarios: PaperEngine.

Invariantes testeadas:
- market BUY ejecuta al ask con taker_fee
- market SELL ejecuta al bid con taker_fee
- market order con amount=0 → rejected
- market order con ask/bid=0 → rejected
- limit BUY con price >= ask → fill inmediato (taker)
- limit SELL con price <= bid → fill inmediato (taker)
- limit BUY con price < ask → orden abierta
- limit SELL con price > bid → orden abierta
- limit con price=0 → rejected
- tipo desconocido → rejected
- on_tick: buy limit se ejecuta cuando ask baja hasta precio
- on_tick: sell limit se ejecuta cuando bid sube hasta precio
- on_tick: aislamiento por símbolo
- cancel_order: remueve orden abierta
- cancel_order: retorna False si no existe
- get_open_orders: filtrado por símbolo
- force_fill: ejecuta orden abierta al precio dado
- fee correctamente aplicado en fill (taker vs maker)
"""

from decimal import Decimal

from src.simulation.paper_engine import PaperEngine, PaperFill

# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

BID = Decimal("49900")
ASK = Decimal("50100")


def make_engine(maker: str = "0.0002", taker: str = "0.0004") -> PaperEngine:
    return PaperEngine(maker_fee=Decimal(maker), taker_fee=Decimal(taker))


def market_intent(
    side: str,
    amount: str = "0.1",
    client_id: str = "test-001",
    symbol: str = "BTC-USD",
) -> dict:
    return {
        "client_id": client_id,
        "symbol": symbol,
        "side": side,
        "type": "market",
        "amount": amount,
    }


def limit_intent(
    side: str,
    price: str,
    amount: str = "0.1",
    client_id: str = "test-001",
    symbol: str = "BTC-USD",
) -> dict:
    return {
        "client_id": client_id,
        "symbol": symbol,
        "side": side,
        "type": "limit",
        "amount": amount,
        "price": price,
    }


# ──────────────────────────────────────────────
# Market orders
# ──────────────────────────────────────────────


class TestMarketOrder:

    def test_buy_executes_at_ask(self):
        """Market BUY ejecuta al ask."""
        engine = make_engine(taker="0")
        result = engine.submit_order(market_intent("buy"), bid=BID, ask=ASK)
        assert result["status"] == "filled"
        assert result["fill"].price == ASK

    def test_sell_executes_at_bid(self):
        """Market SELL ejecuta al bid."""
        engine = make_engine(taker="0")
        result = engine.submit_order(market_intent("sell"), bid=BID, ask=ASK)
        assert result["status"] == "filled"
        assert result["fill"].price == BID

    def test_fill_is_paperfill_instance(self):
        """Fill retornado es PaperFill."""
        engine = make_engine()
        result = engine.submit_order(market_intent("buy"), bid=BID, ask=ASK)
        assert isinstance(result["fill"], PaperFill)

    def test_fill_amount_matches_intent(self):
        """Fill tiene el amount solicitado."""
        engine = make_engine(taker="0")
        result = engine.submit_order(market_intent("buy", amount="0.25"), bid=BID, ask=ASK)
        assert result["fill"].amount == Decimal("0.25")

    def test_taker_fee_applied_to_market_buy(self):
        """Market BUY aplica taker_fee correctamente."""
        engine = make_engine(taker="0.001")
        result = engine.submit_order(market_intent("buy", amount="1.0"), bid=BID, ask=ASK)
        fill = result["fill"]
        expected_fee = Decimal("1.0") * ASK * Decimal("0.001")
        assert abs(fill.fee_cost - expected_fee) < Decimal("0.01")

    def test_market_buy_zero_amount_rejected(self):
        """Market BUY con amount=0 → rejected."""
        engine = make_engine()
        result = engine.submit_order(market_intent("buy", amount="0"), bid=BID, ask=ASK)
        assert result["status"] == "rejected"

    def test_market_buy_zero_ask_rejected(self):
        """Market BUY con ask=0 → rejected."""
        engine = make_engine()
        result = engine.submit_order(market_intent("buy"), bid=BID, ask=Decimal("0"))
        assert result["status"] == "rejected"

    def test_market_sell_zero_bid_rejected(self):
        """Market SELL con bid=0 → rejected."""
        engine = make_engine()
        result = engine.submit_order(market_intent("sell"), bid=Decimal("0"), ask=ASK)
        assert result["status"] == "rejected"

    def test_fill_side_matches_intent(self):
        """Fill.side coincide con intent.side."""
        engine = make_engine()
        result = engine.submit_order(market_intent("buy"), bid=BID, ask=ASK)
        assert result["fill"].side == "buy"

    def test_fill_symbol_matches_intent(self):
        """Fill.symbol coincide con intent.symbol."""
        engine = make_engine()
        result = engine.submit_order(market_intent("sell", symbol="ETH-USD"), bid=BID, ask=ASK)
        assert result["fill"].symbol == "ETH-USD"


# ──────────────────────────────────────────────
# Limit orders — fill inmediato
# ──────────────────────────────────────────────


class TestLimitOrderImmediateFill:

    def test_buy_limit_above_ask_fills_immediately(self):
        """Limit BUY con price >= ask → fill inmediato."""
        engine = make_engine(taker="0")
        result = engine.submit_order(
            limit_intent("buy", price=str(ASK + 100)),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "filled"
        assert result["fill"].price == ASK

    def test_buy_limit_at_ask_fills_immediately(self):
        """Limit BUY con price == ask → fill inmediato."""
        engine = make_engine(taker="0")
        result = engine.submit_order(
            limit_intent("buy", price=str(ASK)),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "filled"

    def test_sell_limit_below_bid_fills_immediately(self):
        """Limit SELL con price <= bid → fill inmediato."""
        engine = make_engine(taker="0")
        result = engine.submit_order(
            limit_intent("sell", price=str(BID - 100)),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "filled"
        assert result["fill"].price == BID

    def test_sell_limit_at_bid_fills_immediately(self):
        """Limit SELL con price == bid → fill inmediato."""
        engine = make_engine(taker="0")
        result = engine.submit_order(
            limit_intent("sell", price=str(BID)),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "filled"


# ──────────────────────────────────────────────
# Limit orders — queued
# ──────────────────────────────────────────────


class TestLimitOrderQueued:

    def test_buy_limit_below_ask_opens(self):
        """Limit BUY con price < ask → orden abierta."""
        engine = make_engine()
        result = engine.submit_order(
            limit_intent("buy", price=str(ASK - 500), client_id="lim-001"),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "open"
        assert "lim-001" in engine.open_orders

    def test_sell_limit_above_bid_opens(self):
        """Limit SELL con price > bid → orden abierta."""
        engine = make_engine()
        result = engine.submit_order(
            limit_intent("sell", price=str(BID + 500), client_id="lim-002"),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "open"
        assert "lim-002" in engine.open_orders

    def test_limit_zero_price_rejected(self):
        """Limit con price=0 → rejected."""
        engine = make_engine()
        result = engine.submit_order(
            limit_intent("buy", price="0"),
            bid=BID,
            ask=ASK,
        )
        assert result["status"] == "rejected"

    def test_unknown_order_type_rejected(self):
        """Tipo de orden desconocido → rejected."""
        engine = make_engine()
        intent = {
            "client_id": "x",
            "symbol": "BTC-USD",
            "side": "buy",
            "type": "stop",
            "amount": "0.1",
        }
        result = engine.submit_order(intent, bid=BID, ask=ASK)
        assert result["status"] == "rejected"


# ──────────────────────────────────────────────
# on_tick — limit order matching
# ──────────────────────────────────────────────


class TestOnTick:

    def test_buy_limit_fills_when_ask_drops(self):
        """Buy limit ejecuta cuando ask baja hasta el precio."""
        engine = make_engine(maker="0")
        engine.submit_order(
            limit_intent("buy", price="49500", client_id="tick-buy"),
            bid=Decimal("49400"),
            ask=Decimal("49600"),  # ask > price → queda abierta
        )
        assert "tick-buy" in engine.open_orders

        # ask baja a 49400 ≤ 49500
        fills = engine.on_tick("BTC-USD", bid=Decimal("49300"), ask=Decimal("49400"))
        assert len(fills) == 1
        assert fills[0].side == "buy"
        assert fills[0].price == Decimal("49400")
        assert "tick-buy" not in engine.open_orders

    def test_sell_limit_fills_when_bid_rises(self):
        """Sell limit ejecuta cuando bid sube hasta el precio."""
        engine = make_engine(maker="0")
        engine.submit_order(
            limit_intent("sell", price="50500", client_id="tick-sell"),
            bid=Decimal("50400"),
            ask=Decimal("50600"),  # bid < price → queda abierta
        )
        assert "tick-sell" in engine.open_orders

        # bid sube a 50600 >= 50500
        fills = engine.on_tick("BTC-USD", bid=Decimal("50600"), ask=Decimal("50800"))
        assert len(fills) == 1
        assert fills[0].side == "sell"
        assert "tick-sell" not in engine.open_orders

    def test_on_tick_symbol_isolation(self):
        """on_tick no ejecuta órdenes de otro símbolo."""
        engine = make_engine()
        engine.submit_order(
            limit_intent("buy", price="49500", symbol="BTC-USD", client_id="btc-ord"),
            bid=Decimal("49400"),
            ask=Decimal("49600"),
        )

        # Tick de ETH no debe tocar la orden BTC
        fills = engine.on_tick("ETH-USD", bid=Decimal("49300"), ask=Decimal("49400"))
        assert fills == []
        assert "btc-ord" in engine.open_orders

    def test_on_tick_no_open_orders_returns_empty(self):
        """on_tick sin órdenes retorna lista vacía."""
        engine = make_engine()
        fills = engine.on_tick("BTC-USD", bid=BID, ask=ASK)
        assert fills == []

    def test_maker_fee_applied_on_tick_fill(self):
        """Fill por on_tick usa maker_fee (no taker_fee)."""
        engine = make_engine(maker="0.0001", taker="0.001")
        engine.submit_order(
            limit_intent("buy", price="49500", amount="1.0", client_id="fee-test"),
            bid=Decimal("49400"),
            ask=Decimal("49600"),
        )
        fills = engine.on_tick("BTC-USD", bid=Decimal("49300"), ask=Decimal("49400"))
        assert len(fills) == 1
        expected_fee = Decimal("1.0") * Decimal("49400") * Decimal("0.0001")
        assert abs(fills[0].fee_cost - expected_fee) < Decimal("0.01")


# ──────────────────────────────────────────────
# cancel_order / get_open_orders
# ──────────────────────────────────────────────


class TestCancelAndGetOrders:

    def test_cancel_removes_open_order(self):
        """cancel_order remueve la orden del dict."""
        engine = make_engine()
        engine.submit_order(
            limit_intent("buy", price="49000", client_id="cancel-me"),
            bid=BID,
            ask=ASK,
        )
        assert "cancel-me" in engine.open_orders
        result = engine.cancel_order("cancel-me")
        assert result is True
        assert "cancel-me" not in engine.open_orders

    def test_cancel_nonexistent_returns_false(self):
        """cancel_order de orden inexistente retorna False."""
        engine = make_engine()
        assert engine.cancel_order("ghost-order") is False

    def test_get_open_orders_filtered_by_symbol(self):
        """get_open_orders con symbol filtra correctamente."""
        engine = make_engine()
        engine.submit_order(
            limit_intent("buy", price="49000", symbol="BTC-USD", client_id="btc-1"),
            bid=BID,
            ask=ASK,
        )
        engine.submit_order(
            limit_intent("buy", price="3000", symbol="ETH-USD", client_id="eth-1"),
            bid=Decimal("2900"),
            ask=Decimal("3100"),
        )

        btc_orders = engine.get_open_orders("BTC-USD")
        assert "btc-1" in btc_orders
        assert "eth-1" not in btc_orders

    def test_get_open_orders_no_filter_returns_all(self):
        """get_open_orders sin symbol retorna todas las órdenes."""
        engine = make_engine()
        engine.submit_order(
            limit_intent("buy", price="49000", symbol="BTC-USD", client_id="btc-1"),
            bid=BID,
            ask=ASK,
        )
        engine.submit_order(
            limit_intent("buy", price="3000", symbol="ETH-USD", client_id="eth-1"),
            bid=Decimal("2900"),
            ask=Decimal("3100"),
        )

        all_orders = engine.get_open_orders()
        assert len(all_orders) == 2


# ──────────────────────────────────────────────
# force_fill
# ──────────────────────────────────────────────


class TestForceFill:

    def test_force_fill_executes_buy_limit(self):
        """force_fill ejecuta una buy limit order abierta."""
        engine = make_engine(maker="0")
        engine.submit_order(
            limit_intent("buy", price="49500", amount="0.5", client_id="ff-001"),
            bid=Decimal("49400"),
            ask=Decimal("49600"),
        )
        fill = engine.force_fill("ff-001", "BTC-USD", Decimal("49500"))
        assert fill is not None
        assert fill.amount == Decimal("0.5")
        assert "ff-001" not in engine.open_orders

    def test_force_fill_nonexistent_returns_none(self):
        """force_fill con order_id inexistente retorna None."""
        engine = make_engine()
        result = engine.force_fill("ghost", "BTC-USD", Decimal("50000"))
        assert result is None

    def test_force_fill_wrong_symbol_returns_none(self):
        """force_fill con símbolo incorrecto retorna None."""
        engine = make_engine()
        engine.submit_order(
            limit_intent("buy", price="49500", symbol="BTC-USD", client_id="ff-002"),
            bid=Decimal("49400"),
            ask=Decimal("49600"),
        )
        result = engine.force_fill("ff-002", "ETH-USD", Decimal("49500"))
        assert result is None
        assert "ff-002" in engine.open_orders  # no ejecutada
