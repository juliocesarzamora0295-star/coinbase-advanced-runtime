# Multi-Timeframe Data Pipeline Analysis

## Current State

`MarketDataService` (src/marketdata/service.py) **already supports** multiple
timeframes per symbol. The key design:

- `register_symbol(symbol, target_timeframe)` creates a `SymbolBarBuilder`
  keyed by `{symbol}:{target_timeframe}` (e.g., `BTC-USD:5m`, `BTC-USD:1h`).
- Each builder independently accumulates 5m candles and resamples to its
  target timeframe.
- `subscribe(symbol, callback)` delivers `CandleClosed` events for all
  timeframes of that symbol.

### Architecture

```
Coinbase WS (5m candles)
        │
        ▼
  ingest_5m_candle(symbol, target_timeframe, ...)
        │
        ├── SymbolBarBuilder("BTC-USD", "5m")  → CandleClosed every 5m
        ├── SymbolBarBuilder("BTC-USD", "1h")  → CandleClosed every 1h
        └── SymbolBarBuilder("BTC-USD", "4h")  → CandleClosed every 4h
                │
                ▼
        callbacks per symbol (shared across timeframes)
```

### Supported Timeframes

| Timeframe | 5m Candles Required |
|-----------|-------------------|
| 5m        | 1                 |
| 15m       | 3                 |
| 30m       | 6                 |
| 1h        | 12                |
| 2h        | 24                |
| 4h        | 48                |
| 6h        | 72                |
| 1d        | 288               |

## What Works

1. **Multi-registration**: `register_symbol("BTC-USD", "5m")` +
   `register_symbol("BTC-USD", "1h")` creates independent builders.
2. **Correct resampling**: Buckets align to UTC boundaries.
3. **Upsert deduplication**: Frequent 5m updates don't duplicate.
4. **Partial bucket discard**: First bucket on startup is discarded.
5. **Thread safety**: Lock-protected ingestion and callback dispatch.

## Limitations / Changes Needed for Full MTF

1. **Callback routing**: `subscribe(symbol, cb)` delivers ALL timeframe
   events for that symbol. Consumers must filter by `event.timeframe`.
   **Enhancement**: Add `subscribe(symbol, timeframe, cb)` for targeted routing.

2. **main.py wiring**: Currently `main.py` registers only ONE timeframe per
   symbol (from config `symbols[].timeframe`). To use MTF, the config
   schema needs `timeframes: ["5m", "1h"]` and main.py needs to register
   and route each.

3. **Strategy interface**: `StrategyManager.on_candle_closed()` receives a
   single candle. MTF strategies need access to candles from multiple
   timeframes (e.g., 5m for entry, 1h for trend filter). This requires
   either a multi-timeframe candle buffer or the existing `mtf_filter.py`
   approach.

4. **Existing MTF filter**: `src/strategy/mtf_filter.py` already exists as
   a trend confirmation filter using higher timeframes. It can be enabled
   via `trading.mtf_enabled: true` in config.

## Recommendation

The data pipeline is MTF-ready at the service level. To fully activate:
1. Extend config to allow multiple timeframes per symbol
2. Add timeframe-aware callback routing
3. Wire StrategyManager to receive multi-TF data

No blocking changes needed for current single-timeframe operation.
