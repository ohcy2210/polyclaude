"""
config.py — Centralized configuration for the Polymarket HFT Bot.
Loads credentials from .env and defines trading constants / API endpoints.

Strategy: Zero-Delay Latency Arbitrage
Based on institutional research of 400M Polymarket trades and the
removal of the 500ms taker delay.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── Polymarket Credentials ────────────────────────────────────────────────
POLYMARKET_PRIVATE_KEY  = os.getenv("POLYMARKET_PRIVATE_KEY")
POLYMARKET_API_KEY      = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET   = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_API_PASSPHRASE = os.getenv("POLYMARKET_API_PASSPHRASE")
POLYMARKET_PROXY_ADDRESS  = os.getenv("POLYMARKET_PROXY_ADDRESS")

# ─── Binance Credentials ───────────────────────────────────────────────────
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# ─── Polymarket API Endpoints ──────────────────────────────────────────────
CLOB_HOST    = "https://clob.polymarket.com"
GAMMA_HOST   = "https://gamma-api.polymarket.com"
CLOB_WS_URL  = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CHAIN_ID     = 137  # Polygon Mainnet

# ─── Binance API Endpoints ─────────────────────────────────────────────────
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"

# ─── Trading Constants ─────────────────────────────────────────────────────
MIN_SHARES = 3  # Polymarket CLOB minimum: size × price ≥ $1 → 3 shares @ ~$0.50

# Fractional Kelly — conservative
KELLY_FRACTION = 0.10

# ─── Stepwise Bet Sizing ──────────────────────────────────────────────────
# Standard bet = 3% of balance, quantised to the tier ladder below.
BET_TIERS = [
    0.50, 1.00, 1.50, 2.00, 2.50, 3.00, 4.00, 5.00,
    7.50, 10.00, 12.50, 15.00, 17.50, 20.00, 22.50, 25.00,
    30.00, 35.00, 40.00, 45.00, 50.00,
    60.00, 70.00, 80.00, 90.00, 100.00,
]

# Max shares per trade = 5× the standard bet in shares
MAX_BET_MULTIPLIER = 5

# Hysteresis — stay in current tier down to 80% of its balance threshold
# to prevent oscillation when balance fluctuates near a boundary.
TIER_HYSTERESIS = 0.80


def get_tier_bet(balance: float, current_tier_idx: int = 0) -> tuple:
    """
    Return (standard_bet_usdc, tier_index) using stepwise 3%-of-balance tiers.

    The tier only steps DOWN when 3% of balance drops below 80% of the
    current tier's bet size (hysteresis prevents bounce).
    """
    raw = balance * 0.03

    # Find the highest tier where bet ≤ raw
    target_idx = 0
    for i, bet in enumerate(BET_TIERS):
        if bet <= raw:
            target_idx = i
        else:
            break

    # Hysteresis: only step DOWN if raw < current_tier × TIER_HYSTERESIS
    if target_idx < current_tier_idx and current_tier_idx < len(BET_TIERS):
        current_bet = BET_TIERS[current_tier_idx]
        if raw >= current_bet * TIER_HYSTERESIS:
            # Stay at current tier — balance hasn't dropped enough
            return current_bet, current_tier_idx

    return BET_TIERS[target_idx], target_idx


# ─── Entry Safety Bounds ──────────────────────────────────────────────────
# Research: 57% loss rate on retail taker longshots < 10¢
ENTRY_MIN_PRICE = 0.10
ENTRY_MAX_PRICE = 0.95

# Order-book guardrails
MIN_PRICE = 0.01
MAX_PRICE = 0.99

# ─── Exit Rules ───────────────────────────────────────────────────────────
EJECT_PRICE = 0.99  # sell immediately if bid ≥ 99¢

# ─── Execution Constants ─────────────────────────────────────────────────
MARKET_REFRESH_INTERVAL  = 1      # seconds between market polls
EXPIRY_BLACKOUT_SECONDS  = 20     # no new entries in last N seconds
TAKER_SLIPPAGE_CENTS     = 0.02   # aggressive spread crossing to snipe stale maker quotes

# ─── Institutional Edge Constants ─────────────────────────────────────────
# Subtracts 1.5% edge to account for taker fee + structural taker disadvantage
EXECUTION_FRICTION = 0.015
# Absolute dollar EV floor
MIN_EV_USDC        = 0.05
# Edge required near expiry (settlement lag sniping)
BASE_MIN_EDGE      = 0.015
# Edge required at window open
MAX_START_EDGE     = 0.040
