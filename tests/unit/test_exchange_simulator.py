"""Tests for ExchangeSimulator."""

import json
import os
import tempfile

from src.exchange_simulator import ExchangeSimulator


class TestTicker:

    def test_ticker_returns_price(self):
        sim = ExchangeSimulator(initial_price=50000.0, seed=1)
        t = sim.get_ticker("BTC-USD")
        assert "price" in t
        assert float(t["price"]) > 0

    def test_ticker_has_bid_ask_spread(self):
        sim = ExchangeSimulator(initial_price=50000.0, spread_pct=0.02, seed=2)
        t = sim.get_ticker()
        bid, ask = float(t["bid"]), float(t["ask"])
        assert bid < ask
        assert bid > 0

    def test_ticker_deterministic_with_seed(self):
        s1 = ExchangeSimulator(seed=42)
        s2 = ExchangeSimulator(seed=42)
        t1 = s1.get_ticker()
        t2 = s2.get_ticker()
        assert t1["price"] == t2["price"]

    def test_price_evolves_random_walk(self):
        sim = ExchangeSimulator(seed=3)
        prices = [float(sim.get_ticker()["price"]) for _ in range(10)]
        assert len(set(prices)) > 1  # prices should vary


class TestOrderPlacement:

    def test_market_buy_fills_immediately(self):
        sim = ExchangeSimulator(seed=10)
        result = sim.place_order("c1", "BTC-USD", "BUY", 0.1)
        assert result["status"] == "FILLED"
        assert float(result["fill_qty"]) == 0.1
        assert float(result["fill_price"]) > 0

    def test_market_sell_fills_immediately(self):
        sim = ExchangeSimulator(
            seed=11, initial_balances={"USD": 10000, "BTC": 1.0},
        )
        result = sim.place_order("c2", "BTC-USD", "SELL", 0.5)
        assert result["status"] == "FILLED"

    def test_buy_slippage_unfavorable(self):
        sim = ExchangeSimulator(
            initial_price=50000.0, slippage_bps=10.0, spread_pct=0.0, seed=12,
        )
        sim.get_ticker()  # advance price
        result = sim.place_order("c3", "BTC-USD", "BUY", 0.1)
        fill = float(result["fill_price"])
        assert fill > 50000 * 0.999  # slippage makes it higher

    def test_sell_slippage_unfavorable(self):
        sim = ExchangeSimulator(
            initial_price=50000.0, slippage_bps=10.0, spread_pct=0.0, seed=13,
            initial_balances={"USD": 0, "BTC": 1.0},
        )
        sim.get_ticker()
        result = sim.place_order("c4", "BTC-USD", "SELL", 0.1)
        fill = float(result["fill_price"])
        assert fill < 50100  # slippage makes it lower


class TestBalances:

    def test_initial_balances(self):
        sim = ExchangeSimulator(initial_balances={"USD": 5000, "BTC": 0.5})
        bals = sim.get_balances()
        usd = next(b for b in bals if b["currency"] == "USD")
        assert float(usd["available_balance"]["value"]) == 5000

    def test_buy_reduces_usd_increases_btc(self):
        sim = ExchangeSimulator(
            initial_balances={"USD": 100000, "BTC": 0}, seed=20,
        )
        sim.get_ticker()
        sim.place_order("b1", "BTC-USD", "BUY", 0.1)
        bals = {b["currency"]: float(b["available_balance"]["value"]) for b in sim.get_balances()}
        assert bals["USD"] < 100000
        assert bals["BTC"] > 0


class TestCancelOrder:

    def test_cancel_nonexistent(self):
        sim = ExchangeSimulator()
        result = sim.cancel_order("nope")
        assert not result["success"]

    def test_cancel_filled_fails(self):
        sim = ExchangeSimulator(seed=30)
        sim.get_ticker()
        order = sim.place_order("c1", "BTC-USD", "BUY", 0.1)
        result = sim.cancel_order(order["order_id"])
        assert not result["success"]  # already filled


class TestLogging:

    def test_log_file_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "ops.jsonl")
            sim = ExchangeSimulator(log_path=path, seed=40)
            sim.get_ticker()
            sim.close()
            assert os.path.exists(path)
            with open(path) as f:
                line = json.loads(f.readline())
                assert line["op"] == "ticker"
