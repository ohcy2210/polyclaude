"""
market_maker.py — Polymarket CLOB client wrapper.
Handles authentication, balance queries, market discovery, and order
execution via Proxy Wallet.
"""

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiohttp
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

import config

# Ultra-fast JSON parsing — orjson is ~5× faster than stdlib json
try:
    import orjson
    fast_loads = orjson.loads
except ImportError:
    import json
    fast_loads = json.loads

logger = logging.getLogger(__name__)


class PolymarketClient:
    """
    Thin async-friendly wrapper around the synchronous py-clob-client.

    All blocking CLOB calls are dispatched to a thread via
    ``asyncio.to_thread`` so they never stall the event loop.
    """

    def __init__(self):
        self.client: ClobClient = self._build_client()
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazy singleton: create one persistent HTTP session with TCP_NODELAY."""
        if self._session is None or self._session.closed:
            import socket
            connector = aiohttp.TCPConnector(
                # Disable Nagle's algorithm — send packets immediately
                socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close(self):
        """Close the persistent HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Initialization ──────────────────────────────────────────────────

    def _build_client(self) -> ClobClient:
        """Create and authenticate the ClobClient."""
        print("  → Building CLOB client …", flush=True)
        client = ClobClient(
            host=config.CLOB_HOST,
            key=config.POLYMARKET_PRIVATE_KEY,
            chain_id=config.CHAIN_ID,
            signature_type=1,                    # Proxy / POLY_PROXY
            funder=config.POLYMARKET_PROXY_ADDRESS,
        )
        print("  → CLOB client created, setting API credentials …", flush=True)

        # If the user provided explicit L2 API creds, use them directly
        # (avoids a blocking HTTP call to create_or_derive_api_creds).
        if (
            config.POLYMARKET_API_KEY
            and config.POLYMARKET_API_SECRET
            and config.POLYMARKET_API_PASSPHRASE
        ):
            from py_clob_client.clob_types import ApiCreds
            client.set_api_creds(ApiCreds(
                api_key=config.POLYMARKET_API_KEY,
                api_secret=config.POLYMARKET_API_SECRET,
                api_passphrase=config.POLYMARKET_API_PASSPHRASE,
            ))
            print("  → Using explicit API credentials ✓", flush=True)
            logger.info("CLOB client: using explicit API credentials")
        else:
            print("  → Deriving API credentials from private key (network call) …", flush=True)
            client.set_api_creds(
                client.create_or_derive_api_creds()
            )
            print("  → Derived API credentials ✓", flush=True)
            logger.info("CLOB client: derived API credentials from private key")

        logger.info("PolymarketClient initialized and authenticated")

        # Pre-approve conditional tokens for selling
        try:
            print("  → Setting conditional token allowance for sells …", flush=True)
            client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL)
            )
            print("  → Conditional token allowance set ✓", flush=True)
            logger.info("CTF conditional token allowance approved")
        except Exception as e:
            logger.warning(f"Could not set conditional allowance: {e}")

        return client

    # ── Balance ─────────────────────────────────────────────────────────

    async def get_usdc_balance(self) -> float:
        """
        Returns the USDC (collateral) balance available for trading.

        Uses ``get_balance_allowance`` with ``AssetType.COLLATERAL``
        and extracts the ``balance`` field.
        """
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams
            result = await asyncio.to_thread(
                self.client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            # API returns balance in micro-USDC (6 decimals)
            raw = float(result.get("balance", 0))
            balance = raw / 1_000_000
            logger.info(f"USDC balance: ${balance:.2f}")
            return balance

        except Exception as e:
            logger.error(f"Failed to fetch USDC balance: {e}", exc_info=True)
            return 0.0

    # ── Order Execution ─────────────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
    ) -> Optional[Dict[str, Any]]:
        """
        Place a limit order signed via EIP-712 Proxy Wallet.

        Args:
            token_id: CLOB token ID (YES or NO token).
            side: ``'BUY'`` or ``'SELL'``.
            price: Limit price (must be within MIN_PRICE – MAX_PRICE).
            size: Number of shares.

        Returns:
            The API response dict on success, or ``None``.
        """
        # ── Guardrails ──────────────────────────────────────────────────
        if price < config.MIN_PRICE or price > config.MAX_PRICE:
            logger.warning(
                f"Order rejected — price {price:.4f} outside "
                f"[{config.MIN_PRICE}, {config.MAX_PRICE}]"
            )
            return None

        if size <= 0:
            logger.warning("Order rejected — size must be > 0")
            return None

        size = int(size)  # strict integer shares
        clob_side = BUY if side.upper() == "BUY" else SELL

        logger.debug(
            f"Placing order: {side} {size} shares @ {price:.4f} "
            f"(token {token_id[:12]}…)"
        )

        try:
            # Build, sign (EIP-712 via Proxy Wallet), and post — all blocking
            resp = await asyncio.to_thread(
                self._place_order_sync, token_id, clob_side, price, size
            )
            logger.debug(f"Order response: {resp}")
            return resp

        except Exception as e:
            logger.error(f"Order placement failed: {e}", exc_info=True)
            return None

    def _place_order_sync(self, token_id, clob_side, price, size):
        """Synchronous helper — runs inside ``to_thread``."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )
        # create_order internally signs with EIP-712 using the key +
        # signature_type we gave to ClobClient (Proxy Wallet / type 1).
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, OrderType.FOK)

    async def place_gtc_sell(
        self, token_id: str, price: float, size: int
    ) -> Optional[Dict[str, Any]]:
        """
        Place a resting GTC SELL limit order (stays on the book until filled).

        Used to guarantee a take-profit exit at 99¢ — the CLOB will fill
        it automatically when the bid reaches our price.
        """
        if size <= 0:
            return None
        logger.debug(
            f"GTC SELL {size} shares @ {price:.2f} (token {token_id[:12]}…)"
        )
        try:
            resp = await asyncio.to_thread(
                self._place_gtc_sell_sync, token_id, price, int(size)
            )
            logger.debug(f"GTC sell response: {resp}")
            return resp
        except Exception as e:
            logger.error(f"GTC sell placement failed: {e}", exc_info=True)
            return None

    def _place_gtc_sell_sync(self, token_id, price, size):
        """Synchronous GTC sell helper."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=SELL,
        )
        signed_order = self.client.create_order(order_args)
        return self.client.post_order(signed_order, OrderType.GTC)

    async def cancel_all(self) -> bool:
        """Cancel every open order for this account."""
        try:
            await asyncio.to_thread(self.client.cancel_all)
            logger.info("All open orders cancelled")
            return True
        except Exception as e:
            logger.error(f"cancel_all failed: {e}", exc_info=True)
            return False

    async def redeem_market(self, condition_id: str) -> bool:
        """
        Redeem (claim) winnings for a resolved market on-chain.

        After a 5-minute market settles, winning tokens can be burned
        for their $1.00 face value.
        """
        try:
            logger.info(f"Redeeming condition {condition_id} …")
            # The CLOB client exposes a redeem / claim endpoint
            # that burns winning conditional tokens for USDC.
            resp = await asyncio.to_thread(
                self.client.redeem, condition_id
            )
            logger.info(f"Redemption response: {resp}")
            return True
        except Exception as e:
            logger.error(f"Redemption failed: {e}", exc_info=True)
            logger.warning("Manual claim required via Polymarket UI due to MagicLink Proxy restrictions.")
            return False

    async def get_live_prices(self, token_map: dict) -> tuple[dict, dict]:
        """
        Fetch best ASK and best BID for each token from the CLOB orderbook.

        - Ask = cheapest price to BUY shares (what a buyer pays)
        - Bid = highest price to SELL shares (what a holder gets)

        Returns:
            (ask_map, bid_map): e.g. ({"Up": 0.52, "Down": 0.50},
                                      {"Up": 0.48, "Down": 0.46})
        """
        session = await self._get_session()
        ask_map = {}
        bid_map = {}

        for side, token_id in token_map.items():
            try:
                url = f"{config.CLOB_HOST}/book?token_id={token_id}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    book = fast_loads(await resp.read())

                    asks = book.get("asks", [])
                    bids = book.get("bids", [])

                    if asks:
                        best_ask = min(float(a["price"]) for a in asks)
                        if best_ask > 0:
                            ask_map[side] = best_ask
                    if bids:
                        best_bid = max(float(b["price"]) for b in bids)
                        if best_bid > 0:
                            bid_map[side] = best_bid
            except Exception as e:
                logger.debug(f"Live price fetch failed for {side}: {e}")

        if ask_map:
            parts = [f"{k}={v:.4f}" for k, v in ask_map.items()]
            logger.info(f"📊 Live asks: {' '.join(parts)}")
        return ask_map, bid_map

    # ── Position Discovery ─────────────────────────────────────────────

    async def get_open_positions(self) -> list[dict]:
        """
        Fetch all open positions for this account from the Polymarket API.

        Returns a list of position dicts, each containing:
          - asset (token_id)
          - size (number of shares held)
          - avgPrice (average entry price)
          - side (BUY direction)
        """
        session = await self._get_session()
        try:
            url = f"{config.CLOB_HOST}/positions"
            headers = self.client.create_l2_headers()
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"Positions API returned {resp.status}")
                    return []
                data = fast_loads(await resp.read())
                # Filter for positions with non-zero size
                return [p for p in data if float(p.get("size", 0)) > 0]
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

    # ── Market Discovery ────────────────────────────────────────────────

    @staticmethod
    def _current_5min_window_start() -> int:
        """
        Return the UNIX timestamp (seconds) for the START of the current
        5-minute window (which is the Polymarket slug key).

        Polymarket slug format: ``btc-updown-5m-{start_timestamp}``
        The market's ``endDate`` field is start + 5 minutes.

        Windows are aligned to clock boundaries:
          10:00 – 10:05, 10:05 – 10:10, …

        If the current time is 10:02:37  →  window start = 10:00:00
        If the current time is 10:05:00  →  window start = 10:05:00
        """
        now = time.time()
        bucket = 5 * 60  # 300 seconds
        # Floor-snap to the current 5-min boundary (= window START = slug key)
        start_ts = math.floor(now / bucket) * bucket
        return int(start_ts)

    async def get_5min_btc_market(self) -> Optional[Dict[str, Any]]:
        """
        Find the currently active Polymarket 5-minute BTC market.

        1. Compute the current window's start timestamp.
        2. Build the expected slug: ``btc-updown-5m-{unix_start_timestamp}``
        3. Hit the Gamma API  ``/events/slug/{slug}``  to validate.
        4. If the exact slug is missing, fall back to a text search.

        Returns the full market dict on success, or ``None``.
        """
        start_ts = self._current_5min_window_start()
        slug = f"btc-updown-5m-{start_ts}"
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        logger.info(
            f"Looking for market slug '{slug}'  (window starts {start_dt})"
        )

        session = await self._get_session()

        # ── Attempt 1: exact slug (floor = current window START) ────────
        market = await self._fetch_market_by_slug(session, slug)
        if market:
            return market

        # ── Attempt 2: broad search fallback ──────────────────────────
        logger.warning(
            "Exact slug not found — falling back to text search"
        )
        return await self._search_btc_5min_market(session)

    async def _fetch_market_by_slug(
        self, session: aiohttp.ClientSession, slug: str
    ) -> Optional[Dict[str, Any]]:
        """GET /events/slug/{slug} from the Gamma API and extract market."""
        url = f"{config.GAMMA_HOST}/events/slug/{slug}"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                event = fast_loads(await resp.read())
                markets = event.get("markets", [])
                if not markets:
                    return None

                m = markets[0]  # single market per 5-min event
                outcomes = m.get("outcomes", [])
                token_ids = m.get("clobTokenIds", [])
                prices = m.get("outcomePrices", [])

                # Gamma API returns these as JSON strings, not lists
                if isinstance(outcomes, str):
                    outcomes = fast_loads(outcomes)
                if isinstance(token_ids, str):
                    token_ids = fast_loads(token_ids)
                if isinstance(prices, str):
                    prices = fast_loads(prices)

                # Build token map: {"Up": token_id, "Down": token_id}
                token_map = {}
                price_map = {}
                for i, outcome in enumerate(outcomes):
                    if i < len(token_ids):
                        token_map[outcome] = token_ids[i]
                    if i < len(prices):
                        price_map[outcome] = float(prices[i])

                result = {
                    "condition_id": m.get("conditionId", ""),
                    "question": m.get("question", event.get("title", "")),
                    "slug": slug,
                    "end_date": m.get("endDate", ""),
                    "outcomes": outcomes,          # ["Up", "Down"]
                    "token_map": token_map,        # {"Up": "123...", "Down": "456..."}
                    "price_map": price_map,        # {"Up": 0.515, "Down": 0.485}
                    "market_type": "up_down",
                }
                logger.info(
                    f"Market found: {result['question']}  "
                    f"Up={price_map.get('Up', '?')}  Down={price_map.get('Down', '?')}"
                )
                return result
        except Exception as e:
            logger.debug(f"Event lookup failed for '{slug}': {e}")
        return None

    async def get_market_winner(
        self, slug: str, retries: int = 3, delay: float = 2.0,
    ) -> Optional[str]:
        """
        Poll the Gamma API for the resolved winner of a closed market.

        Returns ``'Up'`` or ``'Down'`` based on ``outcomePrices``,
        or ``None`` if the market hasn't resolved after *retries* attempts.
        """
        for attempt in range(retries):
            try:
                session = await self._get_session()
                url = f"{config.GAMMA_HOST}/markets?slug={slug}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(delay)
                        continue
                    data = fast_loads(await resp.read())
                    if not data or not isinstance(data, list):
                        await asyncio.sleep(delay)
                        continue

                    m = data[0]
                    outcomes = m.get("outcomes", [])
                    prices = m.get("outcomePrices", [])

                    # Parse JSON-encoded strings if needed
                    if isinstance(outcomes, str):
                        outcomes = fast_loads(outcomes)
                    if isinstance(prices, str):
                        prices = fast_loads(prices)

                    if len(outcomes) >= 2 and len(prices) >= 2:
                        up_price = float(prices[0])
                        down_price = float(prices[1])
                        # Resolved: winning side → 1.0, loser → 0.0
                        # Near-resolved: winning side → ~0.95+
                        if up_price >= 0.90:
                            logger.info(
                                f"🏆 API resolution: Up wins "
                                f"(prices={prices}, attempt {attempt+1})"
                            )
                            return "Up"
                        elif down_price >= 0.90:
                            logger.info(
                                f"🏆 API resolution: Down wins "
                                f"(prices={prices}, attempt {attempt+1})"
                            )
                            return "Down"
                        else:
                            # Not resolved yet — both prices still near 50/50
                            logger.debug(
                                f"Market not resolved yet (prices={prices}), "
                                f"attempt {attempt+1}/{retries}"
                            )
            except Exception as e:
                logger.debug(f"Resolution poll failed: {e}")
            await asyncio.sleep(delay)

        logger.warning(f"Could not determine winner from API after {retries} attempts")
        return None

    async def _search_btc_5min_market(
        self, session: aiohttp.ClientSession
    ) -> Optional[Dict[str, Any]]:
        """
        Broad fallback: search for btc-updown-5m events on Gamma.
        Try adjacent windows in case of timing drift.
        """
        now = time.time()
        bucket = 300

        # Try windows: current, previous, next
        start_ts = math.floor(now / bucket) * bucket
        candidates = [start_ts, start_ts - bucket, start_ts + bucket]

        for ts in candidates:
            slug = f"btc-updown-5m-{ts}"
            market = await self._fetch_market_by_slug(session, slug)
            if market:
                return market

        return None

