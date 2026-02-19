"""
data_stream.py — Async WebSocket streams for real-time market data.
BinanceStream: Connects to Binance aggTrade for tick-by-tick BTC prices.
"""

import asyncio
import logging
import time
from typing import Callable, Awaitable

import websockets

import config

# Ultra-fast JSON parsing — orjson is ~5× faster than stdlib json
try:
    import orjson
    fast_loads = orjson.loads
except ImportError:
    import json
    fast_loads = json.loads

logger = logging.getLogger(__name__)

RECONNECT_DELAY = 5  # seconds


class BinanceStream:
    """
    Async WebSocket client for Binance aggTrade.

    Usage:
        async def my_callback(price, quantity, timestamp):
            print(f"BTC {price}")

        stream = BinanceStream(on_price_update=my_callback)
        await stream.run()          # blocks forever, auto-reconnects
    """

    def __init__(self, on_price_update: Callable[[float, float, int], Awaitable[None]]):
        self.on_price_update = on_price_update
        self._running = True

    async def run(self):
        """Infinite loop: connect → consume → reconnect on failure."""
        while self._running:
            try:
                async with websockets.connect(
                    config.BINANCE_WS_URL,
                    ping_interval=10,
                    ping_timeout=5,
                    close_timeout=3,
                ) as ws:
                    logger.info("Connected to Binance aggTrade stream")
                    await self._consume(ws)
            except (
                websockets.ConnectionClosedError,
                websockets.ConnectionClosedOK,
                ConnectionError,
                OSError,
            ) as e:
                logger.warning(f"Binance WS disconnected: {e}")
            except Exception as e:
                logger.error(f"Binance WS unexpected error: {e}", exc_info=True)

            if self._running:
                logger.info("Reconnecting in 1s …")
                await asyncio.sleep(1)

    async def _consume(self, ws):
        """Read messages until the socket closes."""
        async for raw in ws:
            try:
                msg = fast_loads(raw)
                # aggTrade payload:
                #   p  = price (string)
                #   q  = quantity (string)
                #   T  = trade time (epoch ms)
                price     = float(msg["p"])
                quantity  = float(msg["q"])
                timestamp = int(msg["T"])

                # Drop stale ticks — trading on old data is fatal in HFT
                latency_ms = (time.time() * 1000) - timestamp
                if latency_ms > 400:
                    logger.debug(f"Dropped stale tick: {latency_ms:.0f}ms old")
                    continue

                asyncio.create_task(self.on_price_update(price, quantity, timestamp))

            except (KeyError, ValueError) as e:
                logger.debug(f"Skipping malformed aggTrade message: {e}")

    def stop(self):
        """Signal the run-loop to exit after the current connection drops."""
        self._running = False
