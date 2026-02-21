"""
bot.py — Main orchestrator for the Polymarket HFT Bot.

Wires together:
  • BinanceStream   (real-time BTC price via WebSocket)
  • PolymarketClient (market discovery + order execution via CLOB)
  • StrategyEngine   (signal generation + Kelly sizing + exit logic)

Run:  python bot.py
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections import deque
from typing import Optional

import config
from data_stream import BinanceStream
from market_maker import PolymarketClient
from strategy_engine import (
    StrategyEngine,
    Signal,
    OpenPosition,
    ExitSignal,
)
from dashboard import Dashboard, DashboardState, TradeRecord
from journal import TradeJournal

# ─── Logging (file only — console is owned by the rich dashboard) ──────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-14s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger("bot")


class TradingBot:
    """
    Async orchestrator.

    Lifecycle:
      1. Discover the active 5-min BTC market (Gamma API).
      2. Stream BTC ticks from Binance into a shared deque.
      3. On every tick, run the StrategyEngine.
      4. If a Signal fires, place the order via PolymarketClient.
      5. While holding a position, check exit conditions on every tick.
      6. Repeat.  Periodically re-discover markets for the next window.
    """

    __slots__ = (
        "ticks", "poly", "engine", "stream",
        "active_market", "position", "_running",
        "window_start_price", "_placing_order", "_exiting_order",
        "_skip_first_window", "_startup_market_cid",
        "_trades_this_window", "_last_exit_time",
        "total_pnl", "wins", "losses", "trades",
        "dash_state", "dashboard", "_tick_counter",
        "journal", "_active_trade_id",
        "active_market_end_ts",
        # Basis tracker (Step 2)
        "basis_offset", "basis_history",
        # Circuit breaker (Step 4b)
        "_circuit_breaker_until",
    )

    def __init__(self):
        # Shared tick buffer — (price, unix_ts)
        self.ticks: deque = deque(maxlen=120)

        # Components
        self.poly   = PolymarketClient()
        self.engine = StrategyEngine(
            tick_deque=self.ticks,
        )
        self.stream = BinanceStream(on_price_update=self._on_price_tick)

        # State
        self.active_market: Optional[dict] = None
        self.position: Optional[OpenPosition] = None
        self._running = True
        self.window_start_price: Optional[float] = None  # BTC price at window open
        self._placing_order = False   # async lock — prevents concurrent order placement
        self._exiting_order = False    # async lock — prevents concurrent exit sells
        self._skip_first_window = True  # safety — wait for fresh window on startup
        self._startup_market_cid: Optional[str] = None  # condition_id at boot time
        self._trades_this_window = 0           # hard cap on trades per window
        self._last_exit_time: float = 0.0      # cooldown after exit

        # Session PnL tracking
        self.total_pnl: float = 0.0
        self.wins: int = 0
        self.losses: int = 0
        self.trades: int = 0

        # Dashboard
        self.dash_state = DashboardState()
        self.dashboard = Dashboard(self.dash_state)
        self._tick_counter = 0  # throttle dashboard updates

        # Trade journal (persistent to disk)
        self.journal = TradeJournal()
        self._active_trade_id: Optional[str] = None

        # Basis tracker — aligns Binance feed to Polymarket oracle settlement
        self.basis_offset: float = 0.0
        self.basis_history: deque = deque(maxlen=60)  # 60-second moving average

        # Circuit breaker — blocks entries during WebSocket lag
        self._circuit_breaker_until: float = 0.0

    # ─── Startup position recovery ─────────────────────────────────────

    async def _sync_positions_on_startup(self):
        """
        Cold boot recovery: check for open positions on the active 5-min
        market. If found, populate self.position and self._active_trade_id
        so the bot can manage the exit without "losing" the position.
        """
        try:
            mkt = await self.poly.get_5min_btc_market()
            if not mkt:
                logger.info("No active market at startup — skipping position sync")
                return

            token_map = mkt.get("token_map", {})
            if not token_map:
                return

            positions = await self.poly.get_open_positions()
            if not positions:
                logger.info("No open positions at startup")
                return

            # Check if any position matches the current 5-min market's tokens
            token_to_side = {tid: side for side, tid in token_map.items()}
            for pos in positions:
                asset = pos.get("asset", "")
                if asset in token_to_side:
                    side = token_to_side[asset]
                    size = float(pos.get("size", 0))
                    avg_price = float(pos.get("avgPrice", 0))
                    if size > 0 and avg_price > 0:
                        usdc_risked = size * avg_price
                        self.position = OpenPosition(
                            side=side,
                            average_entry_price=avg_price,
                            size_usdc=usdc_risked,
                            shares=size,
                        )
                        self.active_market = mkt
                        # Parse end_date for time_left
                        end_str = mkt.get("end_date", "")
                        if end_str:
                            from datetime import datetime, timezone
                            end_dt = datetime.fromisoformat(
                                end_str.replace("Z", "+00:00")
                            )
                            self.active_market_end_ts = end_dt.timestamp()
                        # Generate a synthetic trade_id for journal tracking
                        import uuid
                        self._active_trade_id = f"recovered-{uuid.uuid4().hex[:8]}"

                        # Update dashboard
                        self.dash_state.position_side = side
                        self.dash_state.position_entry = avg_price
                        self.dash_state.position_size = usdc_risked
                        self.dash_state.market_question = mkt.get("question", "?")
                        self.dash_state.market_end_date = end_str

                        logger.info(
                            f"🔄 RECOVERED position: {side} "
                            f"{size:.0f} shares @ ${avg_price:.4f} "
                            f"(${usdc_risked:.2f} risked)"
                        )
                        return

            logger.info("No matching position for current 5-min market")
        except Exception as e:
            logger.error(f"Position sync failed: {e}", exc_info=True)

    # ─── Main entry point ──────────────────────────────────────────────

    async def run(self):
        """Start all concurrent loops and block until shutdown."""
        logger.info("🚀 Bot starting …")

        # Print starting balance
        balance = await self.poly.get_usdc_balance()
        self.dash_state.usdc_balance = balance
        self.dash_state.starting_balance = balance
        logger.info(f"USDC balance: ${balance:.2f}")

        # Cancel any stale orders from previous runs (e.g. unfilled GTC orders)
        await self.poly.cancel_all()

        # Cold boot recovery: check for existing positions
        await self._sync_positions_on_startup()

        # Start dashboard
        self.dashboard.start()

        # Launch concurrent tasks
        tasks = [
            asyncio.create_task(self.stream.run(),         name="binance-ws"),
            asyncio.create_task(self._market_loop(),       name="market-discovery"),
            asyncio.create_task(self._dashboard_loop(),    name="dashboard-refresh"),
        ]

        try:
            # Keep running until _running is set to False
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Shutting down …")
            self.dashboard.stop()
            self.journal.shutdown()
            self.stream.stop()
            await self.poly.cancel_all()
            await self.poly.close()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info("✅ Bot stopped cleanly")

    # ─── Binance tick callback ─────────────────────────────────────────

    async def _on_price_tick(self, price: float, quantity: float, timestamp_ms: int):
        """Called on every Binance aggTrade — the hot path."""
        ts = timestamp_ms / 1000.0          # ms → seconds
        self.ticks.append((price, ts))

        # ── Circuit breaker: detect WebSocket lag ────────────────────
        tick_age = time.time() - ts
        if tick_age > 0.5:
            logger.critical(
                f"⚠️ CIRCUIT BREAKER: tick {tick_age*1000:.0f}ms stale — "
                f"blocking entries for 5s"
            )
            self._circuit_breaker_until = time.time() + 5.0
            return  # drop this stale tick entirely

        # O(1) incremental updates for vol + velocity
        # Apply basis offset to align Binance price with Polymarket oracle
        adjusted_price = price + self.basis_offset
        self.engine.update_tick(adjusted_price, ts)

        # Update dashboard BTC price on every tick
        self.dash_state.btc_price = price

        if self.active_market is None:
            return  # no market yet, just accumulate ticks

        # Lazy capture: set window start price on first tick after market found
        if self.window_start_price is None:
            self.window_start_price = price
            self.dash_state.window_start_price = price
            logger.info(f"📌 Window start price: ${price:,.2f}")

        # Update time left for dashboard
        self.dash_state.time_left_secs = self._time_left(self.active_market)

        # Update position status on dashboard
        if self.position and self.window_start_price:
            winning = (self.position.side == "Up" and price >= self.window_start_price) or \
                      (self.position.side == "Down" and price < self.window_start_price)
            self.dash_state.position_status = "WINNING" if winning else "LOSING"
            # Projected PnL
            if winning:
                shares = self.position.size_usdc / self.position.average_entry_price
                self.dash_state.projected_pnl = (1.0 - self.position.average_entry_price) * shares
            else:
                self.dash_state.projected_pnl = -self.position.size_usdc

        # ── Expiry check — did the 5-min window just end? ──────────────
        time_left = self._time_left(self.active_market)
        if time_left <= 0:
            await self._settle_market(adjusted_price)
            return

        # ── Exit check (if we hold a position) ─────────────────────────
        if self.position is not None:
            if not self._exiting_order:
                await self._maybe_exit(adjusted_price)
            return  # don't open a new position while holding one

        # ── Safety: block if an order is already in flight ─────────────
        if self._placing_order:
            return

        # ── Startup cooldown: skip the first (stale) window ───────────
        if self._skip_first_window:
            return

        # ── Double-check: never trade on the market that was active at boot ──
        if self._startup_market_cid and self.active_market:
            if self.active_market.get("condition_id") == self._startup_market_cid:
                return

        # ── Max trades per window (hard cap = 2) ───────────────────────
        if self._trades_this_window >= 2:
            return

        # ── Cooldown after exit (5 s before re-entering) ──────────────
        if self._last_exit_time > 0 and (time.time() - self._last_exit_time) < 5.0:
            return

        # ── Circuit breaker cooldown (stale ticks detected) ──────────
        if time.time() < self._circuit_breaker_until:
            return

        # ── Signal generation ──────────────────────────────────────────
        await self._maybe_enter(adjusted_price)

    # ─── Entry logic ───────────────────────────────────────────────────

    async def _maybe_enter(self, btc_price: float):
        """
        HOT PATH — Entry logic for Up/Down markets.

        1. Use window_start_price as the effective "strike".
        2. Run StrategyEngine.generate_signals()  →  Signal | None.
        3. If Signal fires:
           • YES signal (BTC going up)   → Buy the "Up" token.
           • NO  signal (BTC going down) → Buy the "Down" token.
        """
        mkt = self.active_market
        if not mkt:
            return

        # ── 1. Market parameters ───────────────────────────────────────
        strike    = self.window_start_price  # BTC price at window open
        time_left = self._time_left(mkt)
        up_price  = self._get_up_price(mkt)
        down_price = self._get_down_price(mkt)

        if strike is None or time_left <= 0 or up_price is None:
            return

        # Fallback spread approximation if no Down ask available
        if down_price is None:
            down_price = min(0.99, round((1.0 - up_price) + 0.02, 4))

        # Blackout zone: MMs pull liquidity late — never enter near expiry
        if time_left < config.EXPIRY_BLACKOUT_SECONDS:
            return

        # ── 2. Signal (includes Kelly sizing) ──────────────────────────
        sig: Optional[Signal] = self.engine.generate_signals(
            current_btc_price=btc_price,
            strike=strike,
            time_left=time_left,
            yes_ask=up_price,
            no_ask=down_price,
            balance=self.dash_state.usdc_balance,
        )

        if sig is None:
            return

        # Guard against NaN propagation from vol edge cases
        import math as _math
        if _math.isnan(sig.true_prob) or _math.isnan(sig.edge) or _math.isnan(sig.size):
            logger.debug("Signal contains NaN — skipping")
            return

        # ── 3. Token mapping (Up/Down) ─────────────────────────────────
        #   Signal side="YES" (above strike)  →  Buy "Up" token
        #   Signal side="NO"  (below strike)  →  Buy "Down" token
        order_price = sig.market_price  # engine exports the correct side's ask
        if sig.side == "YES":
            token_id      = self._token_id_for_side(mkt, "Up")
            position_side = "Up"
            log_action    = "BUY UP"
        else:
            token_id      = self._token_id_for_side(mkt, "Down")
            position_side = "Down"
            log_action    = "BUY DOWN"

        if not token_id:
            logger.error("Could not resolve token ID — skipping")
            return

        # Update dashboard with signal info
        self.dash_state.last_signal_text = f"{log_action} @ {order_price:.4f}"
        self.dash_state.last_edge = sig.edge
        self.dash_state.last_ev = sig.edge * sig.size

        # ── 4. Convert USDC size → strict integer shares ───────────────
        size_usdc = sig.size  # strategy returns dollars
        shares = int(size_usdc / order_price) if order_price > 0 else 0

        # Safety cap: max shares = 5× standard_bet converted to shares
        standard_bet, _ = config.get_tier_bet(
            self.dash_state.usdc_balance, self.engine.current_tier_idx,
        )
        max_usdc = standard_bet * config.MAX_BET_MULTIPLIER
        max_shares = int(max_usdc / order_price) if order_price > 0 else 1

        # Enforce minimum and cap
        shares = max(config.MIN_SHARES, shares)
        shares = min(shares, max_shares)

        if shares < config.MIN_SHARES:
            return

        # Recalculate actual USDC risked
        actual_usdc = round(shares * order_price, 4)

        logger.info(
            f"📡 SIGNAL  {log_action}  "
            f"prob={sig.true_prob:.3f}  edge={sig.edge:.3f}  "
            f"size=${actual_usdc:.2f} ({shares} shares @ {order_price:.4f})"
        )

        # ── 5. Fire execution in background — does NOT block WebSocket ──
        self._placing_order = True  # lock BEFORE create_task to prevent races
        asyncio.create_task(self._execute_entry_task(
            token_id=token_id,
            order_price=order_price,
            shares=shares,
            actual_usdc=actual_usdc,
            position_side=position_side,
            sig=sig,
            btc_price=btc_price,
            log_action=log_action,
        ))

    async def _execute_entry_task(
        self, *, token_id, order_price, shares, actual_usdc,
        position_side, sig, btc_price, log_action,
    ):
        """Background task: place entry order, set position, journal.

        Runs decoupled from the WebSocket stream so ticks keep flowing.
        """
        self._trades_this_window += 1
        self.dash_state.order_status = "pending"
        self.dash_state.order_side = position_side
        self.dash_state.last_signal_text = ""

        try:
            # Taker slippage: add slippage to buy price to ensure FOK fill
            aggressive_price = min(0.99, round(order_price + config.TAKER_SLIPPAGE_CENTS, 4))
            resp = await self.poly.place_order(
                token_id=token_id,
                side="BUY",
                price=aggressive_price,
                size=int(shares),
            )

            if resp:
                # Parse fill amounts — check what actually filled
                try:
                    fill_shares = float(resp.get("takingAmount", 0) or 0)
                except (TypeError, ValueError):
                    fill_shares = 0.0
                try:
                    fill_usdc = float(resp.get("makingAmount", 0) or 0)
                except (TypeError, ValueError):
                    fill_usdc = 0.0

                # GHOST POSITION GUARD: only set position if we actually got shares
                if fill_shares <= 0:
                    logger.warning(f"⚠️ Order returned resp but 0 shares filled — no position set")
                    self.dash_state.order_status = ""
                    self.dash_state.order_side = ""
                    self._trades_this_window -= 1
                    return

                fill_price = fill_usdc / fill_shares if fill_shares > 0 else order_price

                # SET POSITION — validated fill
                self.position = OpenPosition(
                    side=position_side,
                    average_entry_price=fill_price,
                    size_usdc=fill_usdc,
                    shares=fill_shares,
                )
                logger.info(
                    f"📥 FILL  {fill_shares:.2f} shares @ ${fill_price:.4f}  "
                    f"(paid ${fill_usdc:.4f})"
                )

                # ── Place GTC sell limit at 99¢ for take-profit ────────
                try:
                    await self.poly.place_gtc_sell(
                        token_id=token_id,
                        price=config.EJECT_PRICE,
                        size=int(fill_shares),
                    )
                    logger.info(
                        f"📝 GTC SELL placed: {int(fill_shares)} shares @ "
                        f"${config.EJECT_PRICE:.2f}"
                    )
                except Exception as e:
                    logger.error(f"GTC sell placement failed: {e}")

                # Secondary updates (non-critical)
                try:
                    self.dash_state.order_status = "filled"
                    self.dash_state.position_side = position_side
                    self.dash_state.position_entry = fill_price
                    self.dash_state.position_size = fill_usdc
                    mkt = self.active_market or {}
                    j_entry = self.journal.open_trade(
                        market_question=mkt.get("question", "?"),
                        market_slug=mkt.get("slug", "?"),
                        market_end_date=mkt.get("end_date", ""),
                        window_start_price=self.window_start_price or 0.0,
                        signal_side=sig.side,
                        true_prob=sig.true_prob,
                        edge=sig.edge,
                        micro_vol=sig.micro_vol,
                        tick_velocity=sig.tick_velocity,
                        momentum_sign=sig.momentum_sign,
                        btc_price=btc_price,
                        position_side=position_side,
                        entry_price=fill_price,
                        shares=fill_shares,
                        usdc_risked=fill_usdc,
                        expected_price=order_price,
                    )
                    self._active_trade_id = j_entry.trade_id
                except Exception as e:
                    logger.error(f"Post-fill bookkeeping error (position IS set): {e}")

                try:
                    self.dash_state.usdc_balance = await self.poly.get_usdc_balance()
                except Exception:
                    pass
                logger.info(f"📥 OPENED  {self.position}")
            else:
                self.dash_state.order_status = ""
                self.dash_state.order_side = ""
                self._trades_this_window -= 1
        except Exception as e:
            logger.error(f"Order placement error: {e}", exc_info=True)
            self._trades_this_window -= 1
        finally:
            self._placing_order = False  # always release the lock

    # ─── Exit logic ────────────────────────────────────────────────────

    async def _maybe_exit(self, btc_price: float):
        """Check exit conditions; fire background task if triggered."""
        if self.position is None or self.active_market is None:
            return

        # Use actual BID price (what we'd receive for selling)
        if self.position.side == "Up":
            effective_bid = self.dash_state.up_bid
        else:
            effective_bid = self.dash_state.down_bid

        if effective_bid is None or effective_bid <= 0:
            return

        exit_sig: Optional[ExitSignal] = StrategyEngine.check_exit_conditions(
            self.position, effective_bid
        )

        if exit_sig is None:
            return

        logger.info(f"🚪 EXIT  {exit_sig.reason}")

        # Capture position data BEFORE firing background task
        token_id = self._token_id_for_side(
            self.active_market, self.position.side
        )
        sell_shares = self.position.shares if self.position.shares > 0 else round(self.position.size_usdc / effective_bid)
        pos_entry = self.position.average_entry_price
        pos_usdc = self.position.size_usdc
        pos_side = self.position.side

        # Lock BEFORE create_task to prevent double-sells
        self._exiting_order = True
        asyncio.create_task(self._execute_exit_task(
            token_id=token_id,
            effective_bid=effective_bid,
            sell_shares=sell_shares,
            pos_entry=pos_entry,
            pos_usdc=pos_usdc,
            pos_side=pos_side,
            btc_price=btc_price,
            exit_reason=exit_sig.reason,
        ))

    async def _execute_exit_task(
        self, *, token_id, effective_bid, sell_shares,
        pos_entry, pos_usdc, pos_side, btc_price, exit_reason,
    ):
        """Background task: place exit order, update stats, journal.

        Runs decoupled from the WebSocket stream so ticks keep flowing.
        """
        try:
            sell_resp = None
            if token_id:
                # Taker slippage: subtract slippage from sell price to ensure FOK fill
                aggressive_sell = max(0.01, round(effective_bid - config.TAKER_SLIPPAGE_CENTS, 4))
                sell_resp = await self.poly.place_order(
                    token_id=token_id,
                    side="SELL",
                    price=aggressive_sell,
                    size=int(sell_shares),
                )

            # GHOST POSITION GUARD: only clear position if sell actually filled
            if sell_resp is None and token_id:
                logger.warning(
                    f"⚠️ Exit order FAILED/KILLED — position kept intact for retry. "
                    f"side={pos_side} shares={sell_shares}"
                )
                return  # leave position, _exiting_order released in finally

            # Sell confirmed — clear position
            self.position = None
            self.dash_state.position_side = None
            self.dash_state.position_status = "—"
            self.dash_state.projected_pnl = 0.0

            # ── Record in session stats & dashboard ────────────────────
            early_pnl = (effective_bid - pos_entry) * (pos_usdc / pos_entry) if pos_entry > 0 else 0
            self.trades += 1
            self.total_pnl += early_pnl
            won = early_pnl >= 0
            if won:
                self.wins += 1
            else:
                self.losses += 1

            trade_record = TradeRecord(
                side=pos_side,
                size=pos_usdc,
                pnl=early_pnl,
                won=won,
                timestamp=time.time(),
            )
            self.dash_state.trade_history.append(trade_record)
            self.dash_state.total_volume_risked += pos_usdc
            self.dash_state.total_trades = self.trades
            self.dash_state.wins = self.wins
            self.dash_state.losses = self.losses
            self.dash_state.total_pnl = self.total_pnl

            logger.info(
                f"{'✅' if won else '❌'} EARLY EXIT  side={pos_side}  "
                f"entry={pos_entry:.4f}  bid={effective_bid:.4f}  "
                f"pnl=${early_pnl:+.2f}  reason={exit_reason}"
            )

            # Journal early exit
            if self._active_trade_id:
                self.journal.close_trade(
                    self._active_trade_id,
                    btc_price_at_close=btc_price,
                    winner="early_exit",
                    pnl=early_pnl,
                    exit_reason=f"early_exit:{exit_reason}",
                )
                self._active_trade_id = None
            self._last_exit_time = time.time()
            logger.info("📤 POSITION CLOSED")

            # Refresh balance after selling
            try:
                self.dash_state.usdc_balance = await self.poly.get_usdc_balance()
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Exit execution error: {e}", exc_info=True)
        finally:
            self._exiting_order = False

    # ─── Expiry settlement & PnL ───────────────────────────────────────

    async def _settle_market(self, final_btc_price: float):
        """
        Called when time_left ≤ 0.  Calculates PnL using local guess,
        resets state IMMEDIATELY, then fires a background task for the
        slow API winner check + on-chain redemption.

        CRITICAL: The finally block guarantees state cleanup — the bot
        must never get stuck with a stale position or market.
        """
        mkt = self.active_market
        if mkt is None:
            return

        start_price = self.window_start_price
        condition_id = mkt.get("condition_id", "")
        slug = mkt.get("slug", "")

        try:
            # ── Instant local winner guess (no network wait) ───────────
            up_wins = (start_price is not None) and (final_btc_price >= start_price)
            winner = "Up" if up_wins else "Down"
            start_str = f"${start_price:,.2f}" if start_price else "?"
            logger.info(
                f"⏰ MARKET EXPIRED  BTC=${final_btc_price:,.2f}  "
                f"Start={start_str}  →  {winner} wins  (local, API check in background)"
            )

            # ── PnL calculation (if we held a position) ────────────────
            traded_pos = None
            if self.position is not None:
                self.trades += 1
                pos = self.position
                traded_pos = pos  # keep for background correction

                if pos.side == winner:
                    shares = pos.size_usdc / pos.average_entry_price
                    pnl = (1.0 - pos.average_entry_price) * shares
                    self.wins += 1
                    emoji = "✅"
                else:
                    pnl = -pos.size_usdc
                    self.losses += 1
                    emoji = "❌"

                self.total_pnl += pnl

                # Record trade in dashboard history
                trade_record = TradeRecord(
                    side=pos.side,
                    size=pos.size_usdc,
                    pnl=pnl,
                    won=(pos.side == winner),
                    timestamp=time.time(),
                )
                self.dash_state.trade_history.append(trade_record)
                self.dash_state.total_volume_risked += pos.size_usdc
                self.dash_state.total_trades = self.trades
                self.dash_state.wins = self.wins
                self.dash_state.losses = self.losses
                self.dash_state.total_pnl = self.total_pnl

                logger.info(
                    f"{emoji} TRADE RESULT  side={pos.side}  "
                    f"entry={pos.average_entry_price:.4f}  "
                    f"pnl=${pnl:+.2f}"
                )
                # Journal the trade outcome
                try:
                    if self._active_trade_id:
                        self.journal.close_trade(
                            self._active_trade_id,
                            btc_price_at_close=final_btc_price,
                            winner=winner,
                            pnl=pnl,
                            exit_reason="settlement",
                        )
                except Exception as e:
                    logger.error(f"Journal close_trade failed: {e}")
                self._active_trade_id = None

                logger.info(
                    f"📊 SESSION  pnl=${self.total_pnl:+.2f}  "
                    f"W/L={self.wins}/{self.losses}  "
                    f"trades={self.trades}"
                )

            # ── Fire background task for slow operations ───────────────
            asyncio.create_task(
                self._background_settle(
                    slug, condition_id, winner, traded_pos,
                )
            )

        except Exception as e:
            logger.error(f"Settlement error: {e}", exc_info=True)

        finally:
            # ── ALWAYS reset state — never leave the bot stuck ─────────
            self.position = None
            self.active_market = None
            self.window_start_price = None
            self._trades_this_window = 0  # reset trade counter for new window
            self._last_exit_time = 0.0    # reset cooldown
            self.basis_offset = 0.0       # reset basis for new window
            self.basis_history.clear()
            # Clear dashboard
            self.dash_state.position_side = None
            self.dash_state.position_status = "—"
            self.dash_state.projected_pnl = 0.0
            self.dash_state.window_start_price = None
            self.dash_state.market_question = "Waiting for next window …"
            self.dash_state.last_signal_text = ""
            self.dash_state.order_status = ""
            self.dash_state.order_side = ""
            self._placing_order = False
            self._exiting_order = False
            self._skip_first_window = False  # fresh window incoming
            # Refresh balance
            try:
                self.dash_state.usdc_balance = await self.poly.get_usdc_balance()
            except Exception:
                pass
            logger.info("🔄 Ready for next 5-min window")

    async def _background_settle(
        self, slug: str, condition_id: str,
        local_winner: str, traded_pos,
    ):
        """Background: API winner check + redemption. Corrects stats if needed."""
        try:
            api_winner = await self.poly.get_market_winner(slug)
            if api_winner and api_winner != local_winner and traded_pos:
                # API disagrees with local guess — correct stats
                logger.warning(
                    f"⚠️ API correction: {api_winner} won, not {local_winner}!"
                )
                pos = traded_pos
                if pos.side == api_winner:
                    # We actually WON (local said loss)
                    shares = pos.size_usdc / pos.average_entry_price
                    correct_pnl = (1.0 - pos.average_entry_price) * shares
                    wrong_pnl = -pos.size_usdc
                    delta = correct_pnl - wrong_pnl
                    self.total_pnl += delta
                    self.wins += 1
                    self.losses -= 1
                    logger.info(f"📊 CORRECTED: was ❌→✅  pnl delta=${delta:+.2f}")
                else:
                    # We actually LOST (local said win)
                    shares = pos.size_usdc / pos.average_entry_price
                    wrong_pnl = (1.0 - pos.average_entry_price) * shares
                    correct_pnl = -pos.size_usdc
                    delta = correct_pnl - wrong_pnl
                    self.total_pnl += delta
                    self.wins -= 1
                    self.losses += 1
                    logger.info(f"📊 CORRECTED: was ✅→❌  pnl delta=${delta:+.2f}")

                # Update dashboard stats
                self.dash_state.wins = self.wins
                self.dash_state.losses = self.losses
                self.dash_state.total_pnl = self.total_pnl
                # Correct the last trade record
                if self.dash_state.trade_history:
                    last = self.dash_state.trade_history[-1]
                    last.won = (pos.side == api_winner)
                    last.pnl = correct_pnl
            elif api_winner:
                logger.info(f"🏆 API confirmed: {api_winner} wins (matches local)")

            # On-chain redemption
            if condition_id:
                await self.poly.redeem_market(condition_id)
        except Exception as e:
            logger.error(f"Background settle failed: {e}")

    # ─── Market discovery loop ─────────────────────────────────────────

    async def _market_loop(self):
        """Periodically find / refresh the active 5-min market."""
        while self._running:
            try:
                mkt = await self.poly.get_5min_btc_market()
                if mkt:
                    cid = mkt.get("condition_id", "?")
                    if (
                        self.active_market is None
                        or self.active_market.get("condition_id") != cid
                    ):
                        logger.info(
                            f"🎯 New market: {mkt.get('question', cid)}"
                        )
                        # Track the very first market we see at boot
                        if self._startup_market_cid is None:
                            self._startup_market_cid = cid
                            logger.info(f"🛡️ Startup market {cid[:12]}… — will skip until next window")
                        self.dash_state.markets_seen += 1
                        # Reset strike for the new window
                        self.window_start_price = None
                        self.dash_state.window_start_price = None
                        # Pre-parse end_date once (avoids ISO parse on every tick)
                        end_str = mkt.get("end_date", "")
                        if end_str:
                            from datetime import datetime, timezone
                            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                            self.active_market_end_ts = end_dt.timestamp()
                        else:
                            self.active_market_end_ts = 0.0
                        # Update dashboard market info
                        self.dash_state.market_question = mkt.get("question", "?")
                        self.dash_state.market_end_date = end_str
                        # Capture BTC price at window start (only once per window)
                        if self.window_start_price is None and self.ticks:
                            self.window_start_price = self.ticks[-1][0]
                            self.dash_state.window_start_price = self.window_start_price
                            logger.info(
                                f"📌 Window start price: ${self.window_start_price:,.2f}"
                            )

                    # ── Always refresh live CLOB prices (every poll) ─────
                    self.active_market = mkt
                    token_map = mkt.get("token_map", {})
                    if token_map:
                        try:
                            ask_map, bid_map = await self.poly.get_live_prices(token_map)
                            if ask_map:
                                mkt["price_map"].update(ask_map)
                                self.active_market = mkt
                            else:
                                logger.warning("CLOB book returned no asks — using Gamma fallback")
                            # Update live bids for active trade display
                            self.dash_state.up_bid = bid_map.get("Up")
                            self.dash_state.down_bid = bid_map.get("Down")

                            # ── Basis tracker: align Binance to Polymarket oracle ──
                            yes_bid = bid_map.get("Up")
                            yes_ask = ask_map.get("Up")
                            if (
                                yes_bid is not None
                                and yes_ask is not None
                                and self.window_start_price is not None
                                and self.dash_state.btc_price > 0
                            ):
                                implied_btc = (
                                    (yes_bid + yes_ask) / 2.0 * 100.0
                                    + self.window_start_price
                                )
                                current_basis = implied_btc - self.dash_state.btc_price
                                self.basis_history.append(current_basis)
                                # 60-second moving average
                                self.basis_offset = (
                                    sum(self.basis_history) / len(self.basis_history)
                                )
                        except Exception as e:
                            logger.error(f"Live price fetch failed: {e}")
                    self.dash_state.up_price = self._get_up_price(mkt)
                    self.dash_state.down_price = self._get_down_price(mkt)
                else:
                    logger.warning("No active 5-min BTC market found")
            except Exception as e:
                logger.error(f"Market discovery error: {e}", exc_info=True)

            await asyncio.sleep(config.MARKET_REFRESH_INTERVAL)

    # ─── Dashboard refresh loop ────────────────────────────────────────

    async def _dashboard_loop(self):
        """Refresh the terminal dashboard ~4× per second."""
        while self._running:
            try:
                self.dashboard.update()
            except Exception:
                pass  # never let dashboard errors crash the bot
            await asyncio.sleep(0.25)

    # ─── Helpers ───────────────────────────────────────────────────────

    def _time_left(self, mkt=None) -> float:
        """Seconds until the market's end date (hot-path safe, no string parsing)."""
        return max(0.0, getattr(self, 'active_market_end_ts', 0) - time.time())

    @staticmethod
    def _token_id_for_side(mkt: dict, side: str) -> Optional[str]:
        """Resolve the CLOB token ID for Up or Down."""
        token_map = mkt.get("token_map", {})
        return token_map.get(side)

    @staticmethod
    def _get_up_price(mkt: dict) -> Optional[float]:
        """Current price of the 'Up' token."""
        price_map = mkt.get("price_map", {})
        up = price_map.get("Up")
        return float(up) if up is not None else None

    @staticmethod
    def _get_down_price(mkt: dict) -> Optional[float]:
        """Current price of the 'Down' token."""
        price_map = mkt.get("price_map", {})
        down = price_map.get("Down")
        return float(down) if down is not None else None


# ─── Entry point ───────────────────────────────────────────────────────────

async def main():
    bot = TradingBot()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: setattr(bot, "_running", False))

    await bot.run()


if __name__ == "__main__":
    # Inject uvloop for ~2-4× faster async I/O (macOS/Linux only)
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass  # fallback to default asyncio loop

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
