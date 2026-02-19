#!/usr/bin/env python3
"""Fetch real BTC 1-second klines from Binance and stream to CSV (memory-safe)."""
import csv
import time

import requests

SYMBOL = "BTCUSDT"
INTERVAL = "1s"
LIMIT = 1000  # max per request
OUTPUT = "btc_1s_klines.csv"
FIELDNAMES = ["timestamp_ms", "open", "high", "low", "close", "volume", "trade_count"]

# Fetch last 3 days (259,200 seconds) in batches of 1000
THREE_DAYS_MS = 3 * 24 * 60 * 60 * 1000
end_ms = int(time.time() * 1000) - 5000  # exclude currently open 1s candle
start_ms = end_ms - THREE_DAYS_MS

# Persistent session for TCP Keep-Alive
session = requests.Session()

total_fetched = 0
batch = 0
retry_count = 0

# Stream to disk — never hold all rows in memory
with open(OUTPUT, "w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
    writer.writeheader()

    while start_ms < end_ms:
        url = (
            f"https://api.binance.com/api/v3/klines"
            f"?symbol={SYMBOL}&interval={INTERVAL}&startTime={start_ms}&limit={LIMIT}"
        )
        try:
            resp = session.get(url, timeout=10)
            data = resp.json()
        except Exception as e:
            print(f"  Error fetching batch {batch}: {e}")
            retry_count += 1
            if retry_count > 5:
                print(f"  ⚠ 5 retries exhausted — skipping {LIMIT}s window")
                start_ms += LIMIT * 1000
                retry_count = 0
            time.sleep(2)
            continue

        retry_count = 0  # reset on success

        if not data:
            # Binance omits klines with zero trades — skip the gap
            print(f"  ⚠ Empty response at {start_ms} — advancing {LIMIT}s")
            start_ms += LIMIT * 1000
            continue

        # Parse batch and write immediately to disk
        batch_rows = []
        for k in data:
            # [open_time, open, high, low, close, volume, close_time, quote_vol, trade_count, ...]
            batch_rows.append({
                "timestamp_ms": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "trade_count": int(k[8]),
            })

        writer.writerows(batch_rows)
        f.flush()  # force OS to save to disk

        total_fetched += len(batch_rows)

        # Advance start to after the last kline
        start_ms = data[-1][0] + 1000  # +1 second
        batch += 1

        # Smart rate limiting — read Binance weight header
        used_weight = int(resp.headers.get("x-mbx-used-weight-1m", 0))

        if batch % 50 == 0:
            print(f"  Fetched {total_fetched:,} klines ({batch} batches) | weight: {used_weight}/6000")

        if used_weight > 4000:
            time.sleep(5)

print(f"\nTotal klines fetched: {total_fetched:,}")
print(f"Saved to {OUTPUT}")
