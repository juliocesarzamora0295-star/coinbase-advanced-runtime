# Centinela v2.2 — Reference Material

Read-only copies from `crypto-bot - dev/Bot Híbrido v2.2 Centinela/`.
These files are **not imported by the runtime**. They exist as design references
for fortress-v4's future strategy layer.

## Files

### `strategy_manager.py`
Adaptive grid trading strategy with AI-powered signal filter.

**Patterns applicable to fortress-v4:**
- **Grid with ATR-based spacing**: Levels auto-recenter when price deviates beyond `ATR * threshold`. Could be adapted as a `GridStrategy` subclass of `src/strategy/base.py`.
- **RandomForest veto filter**: Trains on RSI/MACD/BB/regime features, vetoes BUY signals predicted as unfavorable (prediction=0). Sell signals always execute. This is a clean signal-filtering pattern that could sit between `generate_signals()` and the order planner.
- **Market regime detection via ADX**: ADX > 25 = TENDENCIA, else RANGO. Simple but effective. Could feed into `src/strategy/regime_detector.py`.
- **JSON state persistence**: Grid levels and retrain timestamp survive restarts. fortress-v4 already uses SQLite; the pattern is the same but the medium differs.

### `risk_manager.py`
Persistent risk state with confidence-based position sizing.

**Patterns applicable to fortress-v4:**
- **Peak capital tracking with JSON persistence**: `peak_capital` never decreases. Drawdown = `(peak - current) / peak`. fortress-v4's `TradeLedger.equity_peak` already does this with SQLite.
- **Confidence multiplier on loss streaks**: After N consecutive losses, position size is halved (multiplier = 0.5). Resets on first win. This could enhance `src/risk/position_sizer.py` or `adaptive_sizer.py`.
- **load_state/save_state lifecycle**: Clear pattern for crash-recovery. fortress-v4 uses SQLite for the same purpose.

### `config.py`
Simple dotenv-based configuration with validation.

**Patterns applicable to fortress-v4:**
- **Fail-fast on missing credentials**: Raises ValueError at import time if API keys are missing. fortress-v4's `src/config.py` already does this via `is_configured()`.
- **Grid + TWAP parameters as module-level constants**: Simple approach for single-strategy bots. fortress-v4 uses YAML configs for multi-strategy support.

## How to use these references

1. Read the pattern descriptions above
2. Check if fortress-v4 already has equivalent logic (it usually does)
3. If adapting a pattern, implement it in the canonical module — do NOT import from this directory
