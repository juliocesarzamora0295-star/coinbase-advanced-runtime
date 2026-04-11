"""
Historical OHLCV data downloader.

Downloads candle data from public APIs (Coinbase, Binance) and saves as CSV.
No API keys required — uses public endpoints only.

Output CSV format: timestamp,open,high,low,close,volume
timestamp is epoch milliseconds.
"""

import csv
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Tuple
from urllib.request import urlopen, Request
import json

logger = logging.getLogger("DataDownloader")

# Coinbase Advanced Trade public candles endpoint
# Granularity: ONE_MINUTE, FIVE_MINUTE, FIFTEEN_MINUTE, THIRTY_MINUTE,
#              ONE_HOUR, TWO_HOUR, SIX_HOUR, ONE_DAY
_COINBASE_CANDLES_URL = (
    "https://api.exchange.coinbase.com/products/{product_id}/candles"
)

# Max 300 candles per request on Coinbase
_COINBASE_MAX_CANDLES = 300

_GRANULARITY_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "6h": 21600,
    "1d": 86400,
}


def _fetch_json(url: str, max_attempts: int = 5) -> any:
    """Fetch JSON from URL with exponential backoff retry (fail-closed on exhaustion)."""
    for attempt in range(max_attempts):
        try:
            req = Request(url, headers={"User-Agent": "fortress-backtest/1.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            if attempt == max_attempts - 1:
                raise
            backoff = min(30, 2 ** attempt)
            logger.warning(
                "Fetch attempt %d/%d failed: %s — retry in %ds",
                attempt + 1, max_attempts, exc, backoff,
            )
            time.sleep(backoff)


def download_coinbase(
    product_id: str,
    granularity: str,
    start: datetime,
    end: datetime,
    output_path: str | Path,
) -> Path:
    """
    Download OHLCV data from Coinbase Exchange public API.

    Args:
        product_id: e.g. "BTC-USD"
        granularity: "1m", "5m", "15m", "30m", "1h", "2h", "6h", "1d"
        start: UTC start datetime
        end: UTC end datetime
        output_path: Path to save CSV

    Returns:
        Path to the saved CSV file.

    Coinbase candle format: [timestamp, low, high, open, close, volume]
    timestamp is Unix epoch seconds.
    """
    if granularity not in _GRANULARITY_SECONDS:
        raise ValueError(f"Unsupported granularity: {granularity}")

    gran_sec = _GRANULARITY_SECONDS[granularity]
    base_url = _COINBASE_CANDLES_URL.format(product_id=product_id)

    all_candles: List[Tuple[int, str, str, str, str, str]] = []
    current_start = int(start.timestamp())
    end_ts = int(end.timestamp())

    total_expected = (end_ts - current_start) // gran_sec
    logger.info(
        "Downloading %s %s candles for %s (%s to %s) — ~%d candles expected",
        product_id,
        granularity,
        product_id,
        start.isoformat(),
        end.isoformat(),
        total_expected,
    )

    while current_start < end_ts:
        chunk_end = min(current_start + _COINBASE_MAX_CANDLES * gran_sec, end_ts)
        url = (
            f"{base_url}?granularity={gran_sec}"
            f"&start={current_start}&end={chunk_end}"
        )

        data = _fetch_json(url)
        if not data:
            logger.warning("Empty response at %d — advancing", current_start)
            current_start = chunk_end
            continue

        for candle in data:
            # Coinbase format: [timestamp, low, high, open, close, volume]
            ts_sec, low, high, opn, close, volume = candle
            ts_ms = int(ts_sec) * 1000
            all_candles.append((ts_ms, str(opn), str(high), str(low), str(close), str(volume)))

        logger.info(
            "  fetched %d candles (total so far: %d)", len(data), len(all_candles)
        )

        current_start = chunk_end
        time.sleep(0.35)  # rate limit courtesy

    # Deduplicate and sort by timestamp
    seen = set()
    unique: List[Tuple[int, str, str, str, str, str]] = []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)
    unique.sort(key=lambda x: x[0])

    # Write CSV
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for ts_ms, opn, high, low, close, volume in unique:
            writer.writerow([ts_ms, opn, high, low, close, volume])

    logger.info("Saved %d candles to %s", len(unique), output)
    return output


# ── Pre-defined market regimes ────────────────────────────────────────────
# Dates track crypto market cycles (bull/bear/sideways) and apply to any
# correlated asset (BTC, ETH, SOL). Each tuple: (label, start, end, desc).
MARKET_REGIMES = [
    (
        "bull_2020_q4",
        "2020-10-01T00:00:00Z",
        "2020-12-31T23:59:59Z",
        "BTC rally from $10k to $29k",
    ),
    (
        "bull_2021_q1",
        "2021-01-01T00:00:00Z",
        "2021-04-14T23:59:59Z",
        "BTC rally to $64k ATH",
    ),
    (
        "crash_may_2021",
        "2021-04-15T00:00:00Z",
        "2021-07-20T23:59:59Z",
        "BTC crash from $64k to $29k",
    ),
    (
        "recovery_2021_q3",
        "2021-07-21T00:00:00Z",
        "2021-11-10T23:59:59Z",
        "BTC recovery to $69k ATH",
    ),
    (
        "bear_2022_h1",
        "2022-01-01T00:00:00Z",
        "2022-06-30T23:59:59Z",
        "BTC bear from $47k to $19k (Luna/3AC collapse)",
    ),
    (
        "sideways_2022_h2",
        "2022-07-01T00:00:00Z",
        "2022-12-31T23:59:59Z",
        "BTC sideways $16k-$24k range",
    ),
    (
        "recovery_2023",
        "2023-01-01T00:00:00Z",
        "2023-06-30T23:59:59Z",
        "BTC recovery from $16k to $31k",
    ),
    (
        "pre_etf_2023_h2",
        "2023-07-01T00:00:00Z",
        "2023-12-31T23:59:59Z",
        "BTC ETF anticipation rally to $42k",
    ),
    (
        "etf_bull_2024_q1",
        "2024-01-01T00:00:00Z",
        "2024-03-31T23:59:59Z",
        "BTC ETF approval rally to $73k",
    ),
    (
        "consolidation_2024_q2q3",
        "2024-04-01T00:00:00Z",
        "2024-09-30T23:59:59Z",
        "BTC consolidation $56k-$72k range",
    ),
]


BTC_MARKET_REGIMES = MARKET_REGIMES  # backward-compat alias


def parse_regime_dates(
    regime: tuple[str, str, str, str],
) -> tuple[str, datetime, datetime, str]:
    """Parse a regime tuple into (label, start_dt, end_dt, description)."""
    label, start_iso, end_iso, desc = regime
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return label, start_dt, end_dt, desc


def resample_1h_to_4h(csv_1h: Path, csv_4h: Path) -> Path:
    """
    Resample a 1h OHLCV CSV into 4h bars. Coinbase has no native 4h candle
    (only 2h and 6h), so we aggregate four 1h bars into one 4h bar:
    - open: first bar's open
    - high: max of highs
    - low: min of lows
    - close: last bar's close
    - volume: sum of volumes
    - timestamp_ms: first bar's timestamp
    Incomplete trailing groups are dropped (fail-closed on partial bars).
    """
    import pandas as pd

    df = pd.read_csv(csv_1h)
    if len(df) < 4:
        raise ValueError(f"Not enough 1h bars to resample: {len(df)}")

    groups = []
    for i in range(0, len(df) - (len(df) % 4), 4):
        chunk = df.iloc[i : i + 4]
        groups.append(
            {
                "timestamp": int(chunk.iloc[0]["timestamp"]),
                "open": float(chunk.iloc[0]["open"]),
                "high": float(chunk["high"].max()),
                "low": float(chunk["low"].min()),
                "close": float(chunk.iloc[-1]["close"]),
                "volume": float(chunk["volume"].sum()),
            }
        )

    out = pd.DataFrame(groups)
    csv_4h.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(csv_4h, index=False)
    return csv_4h
