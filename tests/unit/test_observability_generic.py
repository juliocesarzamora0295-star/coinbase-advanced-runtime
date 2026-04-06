"""
Tests para API genérica de métricas y JSONLineSink.

Invariantes testeadas:
- inc() incrementa contadores
- gauge() establece valores
- observe() registra en histogramas
- Labels generan keys distintos
- generic_snapshot() incluye counters, gauges, histograms con stats
- JSONLineSink escribe JSON lines a archivo
- JSONLineSink rota por tamaño
- Global registry singleton
"""

import json
import os
import tempfile

from src.observability import get_collector, reset_collector
from src.observability.json_sink import JSONLineSink
from src.observability.metrics import MetricsCollector


class TestGenericCounters:

    def test_inc_default(self):
        c = MetricsCollector()
        c.inc("orders.submitted")
        assert c.get_counter("orders.submitted") == 1

    def test_inc_multiple(self):
        c = MetricsCollector()
        c.inc("orders.submitted")
        c.inc("orders.submitted")
        c.inc("orders.submitted", delta=3)
        assert c.get_counter("orders.submitted") == 5

    def test_inc_with_labels(self):
        c = MetricsCollector()
        c.inc("orders.submitted", labels={"symbol": "BTC-USD"})
        c.inc("orders.submitted", labels={"symbol": "ETH-USD"})
        c.inc("orders.submitted", labels={"symbol": "BTC-USD"})
        assert c.get_counter("orders.submitted", labels={"symbol": "BTC-USD"}) == 2
        assert c.get_counter("orders.submitted", labels={"symbol": "ETH-USD"}) == 1

    def test_get_counter_default_zero(self):
        c = MetricsCollector()
        assert c.get_counter("nonexistent") == 0


class TestGenericGauges:

    def test_gauge_set(self):
        c = MetricsCollector()
        c.gauge("equity.current", 10000.0)
        assert c.get_gauge("equity.current") == 10000.0

    def test_gauge_overwrite(self):
        c = MetricsCollector()
        c.gauge("equity.current", 10000.0)
        c.gauge("equity.current", 9500.0)
        assert c.get_gauge("equity.current") == 9500.0

    def test_gauge_with_labels(self):
        c = MetricsCollector()
        c.gauge("equity.current", 10000.0, labels={"symbol": "BTC-USD"})
        c.gauge("equity.current", 5000.0, labels={"symbol": "ETH-USD"})
        assert c.get_gauge("equity.current", labels={"symbol": "BTC-USD"}) == 10000.0

    def test_get_gauge_default_zero(self):
        c = MetricsCollector()
        assert c.get_gauge("nonexistent") == 0.0


class TestGenericHistograms:

    def test_observe_single(self):
        c = MetricsCollector()
        c.observe("latency.ms", 150.0)
        assert c.get_histogram("latency.ms") == [150.0]

    def test_observe_multiple(self):
        c = MetricsCollector()
        c.observe("latency.ms", 100.0)
        c.observe("latency.ms", 200.0)
        c.observe("latency.ms", 150.0)
        assert len(c.get_histogram("latency.ms")) == 3

    def test_observe_with_labels(self):
        c = MetricsCollector()
        c.observe("latency.ms", 100.0, labels={"exchange": "coinbase"})
        c.observe("latency.ms", 200.0, labels={"exchange": "other"})
        assert len(c.get_histogram("latency.ms", labels={"exchange": "coinbase"})) == 1

    def test_get_histogram_default_empty(self):
        c = MetricsCollector()
        assert c.get_histogram("nonexistent") == []


class TestGenericSnapshot:

    def test_snapshot_has_all_sections(self):
        c = MetricsCollector()
        c.inc("a")
        c.gauge("b", 1.0)
        c.observe("c", 2.0)
        snap = c.generic_snapshot()
        assert "ts_ms" in snap
        assert "counters" in snap
        assert "gauges" in snap
        assert "histograms" in snap

    def test_histogram_stats(self):
        c = MetricsCollector()
        for v in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            c.observe("latency", float(v))
        snap = c.generic_snapshot()
        h = snap["histograms"]["latency"]
        assert h["count"] == 10
        assert h["min"] == 10.0
        assert h["max"] == 100.0
        assert h["mean"] == 55.0
        assert "p50" in h
        assert "p95" in h
        assert "p99" in h


class TestJSONLineSink:

    def test_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "metrics.jsonl")
            sink = JSONLineSink(path=path)
            sink.write("test.counter", 1, metric_type="counter")
            sink.close()

            assert os.path.exists(path)
            with open(path) as f:
                line = f.readline()
                data = json.loads(line)
                assert data["name"] == "test.counter"
                assert data["value"] == 1
                assert data["type"] == "counter"
                assert "ts" in data

    def test_write_with_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "metrics.jsonl")
            sink = JSONLineSink(path=path)
            sink.write("orders", 5, labels={"symbol": "BTC-USD"})
            sink.close()

            with open(path) as f:
                data = json.loads(f.readline())
                assert data["labels"]["symbol"] == "BTC-USD"

    def test_write_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "metrics.jsonl")
            sink = JSONLineSink(path=path)
            sink.write_snapshot({"equity": 10000, "drawdown": 0.05})
            sink.close()

            with open(path) as f:
                data = json.loads(f.readline())
                assert data["name"] == "_snapshot"
                assert data["data"]["equity"] == 10000

    def test_rotation_by_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "metrics.jsonl")
            sink = JSONLineSink(path=path, max_bytes=200, backup_count=2)

            # Write enough to trigger rotation
            for i in range(50):
                sink.write(f"metric.{i}", i)

            sink.close()

            # Should have rotated — .1 backup should exist
            assert os.path.exists(path)
            assert os.path.exists(f"{path}.1")

    def test_multiple_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "metrics.jsonl")
            sink = JSONLineSink(path=path)
            sink.write("a", 1)
            sink.write("b", 2)
            sink.write("c", 3)
            sink.close()

            with open(path) as f:
                lines = f.readlines()
                assert len(lines) == 3
                for line in lines:
                    json.loads(line)  # all valid JSON


class TestGlobalRegistry:

    def test_get_collector_singleton(self):
        reset_collector()
        c1 = get_collector()
        c2 = get_collector()
        assert c1 is c2

    def test_reset_creates_new_instance(self):
        reset_collector()
        c1 = get_collector()
        reset_collector()
        c2 = get_collector()
        assert c1 is not c2

    def test_global_collector_is_functional(self):
        reset_collector()
        c = get_collector()
        c.inc("test.global")
        assert c.get_counter("test.global") == 1
        reset_collector()


class TestResetClearsGeneric:

    def test_reset_clears_generic_stores(self):
        c = MetricsCollector()
        c.inc("x")
        c.gauge("y", 1.0)
        c.observe("z", 2.0)
        c.reset()
        assert c.get_counter("x") == 0
        assert c.get_gauge("y") == 0.0
        assert c.get_histogram("z") == []
