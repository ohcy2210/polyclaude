"""
strategy_engine.py — Ultra-fast HFT microstructure math helpers.

Design rules
  • numpy + collections.deque only  →  O(1) amortised updates
  • NO pandas, NO heavy for-loops, NO lagging retail indicators (RSI etc.)
  • Student-t (df=3) for fat-tail pricing — NOT Gaussian CDF
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Optional

import config

# ────────────────────────────────────────────────────────────────────────────
# 1.  compute_micro_vol
# ────────────────────────────────────────────────────────────────────────────

def compute_micro_vol(tick_deque: deque) -> float:
    """
    Realised micro-volatility over the last ≤60 ticks.

    Each element of *tick_deque* is ``(price, timestamp)``.
    We compute **1-second log-returns** (grouping ticks whose timestamps
    fall in the same integer second) and return their standard deviation.

    Returns 0.0 when there are fewer than 2 distinct 1-s buckets.

    Complexity: O(n) where n = len(tick_deque), but n ≤ 60 by design
    so this is effectively O(1) amortised.
    """
    if len(tick_deque) < 2:
        return 0.0

    # Dict keeps last price per second (Python dicts preserve insertion order)
    sec_bars = {int(ts): price for price, ts in tick_deque}
    sec_prices = [sec_bars[k] for k in sorted(sec_bars.keys())]

    if len(sec_prices) < 2:
        return 0.0

    # Log-returns between consecutive 1-second bars
    log_rets = [math.log(sec_prices[i] / sec_prices[i - 1])
                for i in range(1, len(sec_prices))]

    if len(log_rets) < 2:
        return 0.0

    try:
        return statistics.stdev(log_rets)
    except statistics.StatisticsError:
        return 0.0


# ────────────────────────────────────────────────────────────────────────────
# 2.  compute_tick_velocity
# ────────────────────────────────────────────────────────────────────────────

def compute_tick_velocity(tick_deque: deque, lookback: int = 3) -> float:
    """
    Absolute dollar change over the last *lookback* seconds.

    Detects toxic flow and falling-knife events instantly.

    Each element of *tick_deque* is ``(price, timestamp)``.
    Returns 0.0 if the deque spans less than *lookback* seconds.
    """
    if len(tick_deque) < 2:
        return 0.0

    latest_price, latest_ts = tick_deque[-1]
    cutoff_ts = latest_ts - lookback

    # Walk backwards to find the first tick at or before the cutoff
    ref_price: Optional[float] = None
    for price, ts in reversed(tick_deque):
        if ts <= cutoff_ts:
            ref_price = price
            break

    if ref_price is None:
        # All ticks are within the lookback window — use the oldest
        ref_price = tick_deque[0][0]

    return abs(latest_price - ref_price)


# ────────────────────────────────────────────────────────────────────────────
# 3.  student_t_cdf_approx   (df = 3, fat-tail pricing)
# ────────────────────────────────────────────────────────────────────────────

def student_t_cdf_approx(
    distance_to_strike: float,
    vol: float,
    time_left: float,
) -> float:
    """
    Fast approximation of the Student-t CDF (df = 3).

    Returns the probability that the price finishes **above** the strike,
    i.e.  P(X > strike)  =  1 − CDF_t3(−z)  where

        z = distance_to_strike / (vol × √time_left)

    *distance_to_strike* is positive when spot > strike (favours YES),
    negative when spot < strike (favours NO).

    We use the incomplete-beta / closed-form identity for ν = 3:

        CDF_t3(t) = 0.5 + (1/π) × [ arctan(t/√3) + (t√3 / (3 + t²)) ]

    This is **exact** for df = 3, not an approximation — and avoids
    scipy / erfc entirely.

    Returns a probability clamped to [0.001, 0.999] to avoid infinities.
    """
    # Avoid division by zero
    if vol <= 0.0 or time_left <= 0.0:
        # At expiry: binary outcome
        return 0.999 if distance_to_strike > 0 else 0.001

    # Standardise
    sigma = vol * math.sqrt(time_left)
    t = distance_to_strike / sigma

    # ── Exact CDF for Student-t with df = 3 ─────────────────────────────
    sqrt3 = math.sqrt(3.0)
    cdf = 0.5 + (1.0 / math.pi) * (
        math.atan(t / sqrt3) + (t * sqrt3) / (3.0 + t * t)
    )

    # P(finish above strike) = CDF(t)  because t is positive when
    # spot is already above strike.
    prob = max(0.001, min(0.999, cdf))
    return prob


# ────────────────────────────────────────────────────────────────────────────
# 4.  Signal data object
# ────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass


@dataclass(slots=True)
class Signal:
    """Immutable trade signal emitted by the engine."""
    side: str          # "YES" or "NO"
    true_prob: float   # model's fair probability for YES
    edge: float        # net edge after execution friction
    size: float        # suggested size (USDC)
    # Decision context (for journal / post-analysis)
    micro_vol: float = 0.0       # 1-second micro-volatility
    tick_velocity: float = 0.0   # $ move over lookback window
    momentum_sign: float = 0.0   # +1.0 rising, -1.0 falling
    market_price: float = 0.0    # orderbook ask at signal time
    ev_usdc: float = 0.0         # expected dollar value of trade


@dataclass(slots=True)
class OpenPosition:
    """Snapshot of a live position held by the bot."""
    side: str                # "YES" or "NO"
    average_entry_price: float
    size_usdc: float
    shares: float = 0.0      # actual share count (whole number)


@dataclass(slots=True)
class ExitSignal:
    """Returned by check_exit_conditions when we should close."""
    action: str    # "SELL_TO_CLOSE"
    reason: str    # human-readable tag
    side: str      # which token to sell


# ────────────────────────────────────────────────────────────────────────────
# 5.  StrategyEngine
# ────────────────────────────────────────────────────────────────────────────

# Tunables
TOXIC_FLOW_THRESH = 10.0    # $10 move in 3 s → abort
TOXIC_LOOKBACK    = 3       # seconds
MOMENTUM_WEIGHT   = 0.15    # how much tick-velocity nudges log-odds
MIN_TICK_VELOCITY = 2.0     # $2 min tick velocity to enter (filters low-flow noise)


class StrategyEngine:
    """
    Pure-EV microstructure engine.

    No lagging indicators.  Signal generation is entirely driven by:
      1. Student-t(df=3) base probability
      2. Log-odds adjustment for micro-momentum
      3. Toxic-flow safety gate
      4. Strict edge threshold
    """

    def __init__(self, tick_deque: deque):
        """
        Args:
            tick_deque: shared deque of ``(price, timestamp)`` tuples,
                        maintained by the data-stream layer.
        """
        self.ticks = tick_deque
        self.current_tier_idx = 0  # stepwise bet tier

    # ── public API ──────────────────────────────────────────────────────

    def generate_signals(
        self,
        current_btc_price: float,
        strike: float,
        time_left: float,
        yes_ask: float,
        no_ask: float,
        balance: float = 50.0,
    ) -> Optional[Signal]:
        """
        Evaluate the current micro-state and return a Signal or None.

        Args:
            current_btc_price: latest BTC spot from Binance.
            strike: the market's strike price (e.g. 97 250.00).
            time_left: seconds until the 5-min window closes.
            yes_ask: cheapest YES ask on Polymarket (0–1).
            no_ask: cheapest NO ask on Polymarket (0–1).

        Returns:
            A ``Signal`` if edge > MIN_EDGE and no toxic flow,
            otherwise ``None``.
        """
        # ── 0. Micro-volatility (needed for probability) ───────────────
        vol = compute_micro_vol(self.ticks)
        if vol <= 0.0:
            # Not enough data to price — stay flat
            return None

        # ── 1. Tick velocity (used for momentum AND toxic-flow gate) ───
        velocity = compute_tick_velocity(self.ticks, lookback=TOXIC_LOOKBACK)
        # Signed direction: positive = price rising, negative = falling
        if len(self.ticks) >= 2:
            signed_move = self.ticks[-1][0] - self.ticks[-2][0]
        else:
            signed_move = 0.0

        # ── 2. Base probability via Student-t(df=3) ────────────────────
        distance = current_btc_price - strike
        base_prob = student_t_cdf_approx(distance, vol, time_left)

        # ── 3. Log-odds transformation + momentum adjustment ──────────
        p_clip = max(0.001, min(0.999, base_prob))
        lo = math.log(p_clip / (1.0 - p_clip))

        # Momentum nudge: convert velocity to the same log-return scale
        # as vol before combining.  velocity is in $ — divide by price
        # to get a fractional move, then log(1 + x) ≈ x for small x.
        momentum_sign = 1.0 if signed_move >= 0 else -1.0
        velocity_lr = math.log(1.0 + velocity / current_btc_price) if current_btc_price > 0 else 0.0
        nudge = MOMENTUM_WEIGHT * momentum_sign * (velocity_lr / max(vol, 1e-6))
        # Safety clamp: never let momentum shift log-odds by more than ±2
        nudge = max(-2.0, min(2.0, float(nudge)))
        lo += nudge

        # Back to probability space
        true_prob = 1.0 / (1.0 + math.exp(-max(-20.0, min(20.0, lo))))

        # ── 4. Determine candidate side ────────────────────────────────
        yes_prob = true_prob
        no_prob = 1.0 - true_prob
        yes_edge = yes_prob - yes_ask
        no_edge = no_prob - no_ask

        if yes_edge >= no_edge:
            candidate_side = "YES"
            eval_prob = yes_prob
            eval_ask = yes_ask
            eval_edge = yes_edge
        else:
            candidate_side = "NO"
            eval_prob = no_prob
            eval_ask = no_ask
            eval_edge = no_edge

        # ── 5. Taker Tax ───────────────────────────────────────────────
        net_edge = eval_edge - config.EXECUTION_FRICTION

        # ── 6. Adaptive Time-Decay (Calibration Surface) ──────────────
        dynamic_min_edge = config.BASE_MIN_EDGE + (
            (config.MAX_START_EDGE - config.BASE_MIN_EDGE) * (time_left / 300.0)
        )
        if net_edge < dynamic_min_edge:
            return None

        # ── 7. Anti-Longshot filter ────────────────────────────────────
        if eval_ask < config.ENTRY_MIN_PRICE:
            return None

        # ── 8. Toxic-flow safety gate ──────────────────────────────────
        if velocity >= TOXIC_FLOW_THRESH:
            price_falling = signed_move < 0
            if candidate_side == "YES" and price_falling:
                return None   # toxic flow against YES
            if candidate_side == "NO" and not price_falling:
                return None   # toxic flow against NO

        # ── 9. Minimum tick velocity ──────────────────────────────────
        if velocity < MIN_TICK_VELOCITY:
            return None

        # ── 10. Sizing & EV floor ─────────────────────────────────────
        size = self.calculate_bet_size(
            eval_price=eval_ask,
            balance=balance,
            net_edge=net_edge,
            velocity=velocity,
            momentum_sign=momentum_sign,
            candidate_side=candidate_side,
            vol=vol,
        )
        if size <= 0:
            return None

        ev_usdc = net_edge * size
        if ev_usdc < config.MIN_EV_USDC:
            return None

        return Signal(
            side=candidate_side,
            true_prob=eval_prob,
            edge=net_edge,
            size=size,
            micro_vol=vol,
            tick_velocity=velocity,
            momentum_sign=momentum_sign,
            market_price=eval_ask,
            ev_usdc=ev_usdc,
        )

    # ── Institutional Kelly Sizing ────────────────────────────────────────

    def calculate_bet_size(
        self,
        eval_price: float,
        balance: float,
        net_edge: float,
        velocity: float,
        momentum_sign: float,
        candidate_side: str,
        vol: float,
    ) -> float:
        """
        Institutional bet sizing with Kelly penalty and sniper boost.

        1. Hard safety bounds on entry price.
        2. Full Kelly fraction from net edge.
        3. Uncertainty penalty (high vol → smaller bets).
        4. Sniper boost (momentum-aligned + fast velocity → bigger bets).
        5. Tier-ladder quantisation.
        6. Strict integer shares (CLOB minimum enforced).
        """
        # ── Hard safety bounds on entry price ──────────────────────────
        if eval_price < config.ENTRY_MIN_PRICE:
            return 0.0
        if eval_price > config.ENTRY_MAX_PRICE:
            return 0.0
        if net_edge <= 0:
            return 0.0

        # ── Standard bet from tier ladder ──────────────────────────────
        standard_bet, self.current_tier_idx = config.get_tier_bet(
            balance, self.current_tier_idx,
        )

        # ── Full Kelly fraction ────────────────────────────────────────
        denom = 1.0 - eval_price
        if denom <= 0:
            return 0.0
        f_full = net_edge / denom

        # ── Empirical Kelly Penalty & Sniper Boost ─────────────────────
        uncertainty_penalty = min(0.5, vol * 20.0)

        is_sniping = (
            (candidate_side == "YES" and momentum_sign > 0)
            or (candidate_side == "NO" and momentum_sign < 0)
        )
        sniper_mult = (
            min(2.0, 1.0 + (velocity / 5.0))
            if is_sniping and velocity >= 2.0
            else 1.0
        )

        f_kelly = f_full * config.KELLY_FRACTION * (1.0 - uncertainty_penalty) * sniper_mult
        kelly_size = f_kelly * balance

        # Scale relative to the standard bet
        kelly_mult = kelly_size / standard_bet if standard_bet > 0 else 1.0
        kelly_mult = max(1.0, min(float(config.MAX_BET_MULTIPLIER), kelly_mult))

        size = standard_bet * kelly_mult

        # ── Hard cap at 5× standard ───────────────────────────────────
        max_size = standard_bet * config.MAX_BET_MULTIPLIER
        size = min(size, max_size)

        # ── STRICT Integer Shares ──────────────────────────────────────
        shares = int(size / eval_price) if eval_price > 0 else 0
        if shares < config.MIN_SHARES:
            return 0.0
        return float(shares * eval_price)

    # ── Intra-candle exit logic ─────────────────────────────────────────

    @staticmethod
    def check_exit_conditions(
        position: OpenPosition,
        current_market_bid: float,
    ) -> Optional[ExitSignal]:
        """
        Decide whether to close an open position *before* expiry.

        Single trigger:

        **99-Cent Eject** — if bid ≥ 0.99, sell immediately.
        Sacrifice the last 1¢ of EV to eliminate expiry tail-risk.
        All other positions hold to settlement.
        """
        if current_market_bid >= config.EJECT_PRICE:
            return ExitSignal(
                action="SELL_TO_CLOSE",
                reason=f"99¢ EJECT — bid {current_market_bid:.2f}",
                side=position.side,
            )

        return None
