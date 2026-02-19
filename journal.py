"""
journal.py — Persistent trade journal for the Polymarket HFT Bot.

Stores every trade as a JSON-Lines file (one JSON object per line),
making it easy to analyse with pandas or jq after the session.

Each entry captures:
  • Session metadata (session_id, timestamps)
  • Market context (question, slug, end_date, window start price)
  • Signal context (prob, edge, vol, velocity, momentum, market price)
  • Trade execution (side, entry price, shares, USDC risked)
  • Outcome (winner, PnL, ROI, close price)
"""

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import logging

logger = logging.getLogger("journal")

# Default journal location — same directory as the bot
JOURNAL_DIR = Path(__file__).parent / "journals"
JOURNAL_DIR.mkdir(exist_ok=True)


@dataclass
class JournalEntry:
    """Complete record of a single trade."""

    # ── Session ────────────────────────────────────────────────────
    session_id: str
    trade_id: str
    timestamp_open: str          # ISO-8601
    timestamp_close: str = ""    # filled on settlement

    # ── Market context ─────────────────────────────────────────────
    market_question: str = ""
    market_slug: str = ""
    market_end_date: str = ""
    window_start_price: float = 0.0  # BTC at window open (effective strike)

    # ── Signal / decision data ─────────────────────────────────────
    signal_side: str = ""        # "YES" or "NO" (model side)
    true_prob: float = 0.0       # model's fair probability
    edge: float = 0.0            # true_prob - market_price
    micro_vol: float = 0.0       # 1-sec micro-volatility at signal time
    tick_velocity: float = 0.0   # $ move over lookback window
    momentum_sign: float = 0.0   # +1 rising, -1 falling
    btc_price_at_entry: float = 0.0  # BTC spot when trade was placed

    # ── Execution ──────────────────────────────────────────────────
    position_side: str = ""      # "Up" or "Down" (token bought)
    entry_price: float = 0.0     # share price paid
    shares: int = 0              # number of shares (strict integer)
    usdc_risked: float = 0.0     # total USDC spent
    event_type: str = "OPEN"     # trade lifecycle: OPEN / CLOSE
    expected_price: float = 0.0  # price the strategy wanted
    slippage_cents: float = 0.0  # (actual - expected) × 100

    # ── Outcome (filled at settlement) ─────────────────────────────
    btc_price_at_close: float = 0.0
    winner: str = ""             # "Up" or "Down"
    won: bool = False
    pnl: float = 0.0
    roi: float = 0.0             # pnl / usdc_risked
    exit_reason: str = ""        # "settlement" / "early_exit" / "timeout"


class TradeJournal:
    """
    Append-only trade journal backed by a JSONL file.

    Usage:
        journal = TradeJournal()
        entry = journal.open_trade(signal, market, ...)
        # ... later on settlement ...
        journal.close_trade(entry, outcome_data)
    """

    def __init__(self, journal_dir: Optional[Path] = None):
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        self.journal_dir = journal_dir or JOURNAL_DIR
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        self.journal_file = self.journal_dir / "trades.jsonl"
        self._pending: dict[str, JournalEntry] = {}  # trade_id → entry

        # Async writer: offload disk I/O to a daemon thread
        self._write_queue: queue.Queue = queue.Queue()
        self._file = open(self.journal_file, "a", encoding="utf-8")
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()

        logger.info(f"📓 Journal: {self.journal_file}  (session {self.session_id})")

    def open_trade(
        self,
        *,
        market_question: str,
        market_slug: str,
        market_end_date: str,
        window_start_price: float,
        signal_side: str,
        true_prob: float,
        edge: float,
        micro_vol: float,
        tick_velocity: float,
        momentum_sign: float,
        btc_price: float,
        position_side: str,
        entry_price: float,
        shares: int,
        usdc_risked: float,
        expected_price: float,
    ) -> JournalEntry:
        """Record a new trade entry. Returns the entry for later close."""
        shares = int(shares)
        slippage_cents = round((entry_price - expected_price) * 100, 2)
        entry = JournalEntry(
            session_id=self.session_id,
            trade_id=uuid.uuid4().hex[:12],
            timestamp_open=datetime.now(timezone.utc).isoformat(),
            market_question=market_question,
            market_slug=market_slug,
            market_end_date=market_end_date,
            window_start_price=window_start_price,
            signal_side=signal_side,
            true_prob=round(true_prob, 6),
            edge=round(edge, 6),
            micro_vol=round(micro_vol, 8),
            tick_velocity=round(tick_velocity, 4),
            momentum_sign=momentum_sign,
            btc_price_at_entry=round(btc_price, 2),
            position_side=position_side,
            entry_price=round(entry_price, 4),
            shares=shares,
            usdc_risked=round(usdc_risked, 4),
            event_type="OPEN",
            expected_price=round(expected_price, 4),
            slippage_cents=slippage_cents,
        )
        self._pending[entry.trade_id] = entry
        self._flush(entry)  # Event-sourcing: persist OPEN immediately (crash resilience)
        logger.info(f"📓 OPEN  trade={entry.trade_id}  {position_side}  ${usdc_risked:.2f}")
        return entry

    def close_trade(
        self,
        trade_id: str,
        *,
        btc_price_at_close: float,
        winner: str,
        pnl: float,
        exit_reason: str = "settlement",
    ):
        """
        Record trade outcome and flush to disk.

        If the trade_id is not found in pending (e.g. bot restarted),
        we still write a partial record.
        """
        entry = self._pending.pop(trade_id, None)

        if entry is None:
            # Create a minimal record for trades opened before a restart
            entry = JournalEntry(
                session_id=self.session_id,
                trade_id=trade_id,
                timestamp_open="unknown",
            )

        entry.timestamp_close = datetime.now(timezone.utc).isoformat()
        entry.btc_price_at_close = round(btc_price_at_close, 2)
        entry.winner = winner
        entry.won = (entry.position_side == winner)
        entry.pnl = round(pnl, 4)
        entry.roi = round(pnl / entry.usdc_risked, 4) if entry.usdc_risked > 0 else 0.0
        entry.exit_reason = exit_reason

        entry.event_type = "CLOSE"
        self._flush(entry)

        emoji = "✅" if entry.won else "❌"
        logger.info(
            f"📓 CLOSE {emoji}  trade={trade_id}  {entry.position_side}  "
            f"PnL=${pnl:+.2f}  ROI={entry.roi:+.1%}  reason={exit_reason}"
        )

    def close_all_pending(self, btc_price: float, reason: str = "bot_shutdown"):
        """Flush all pending trades on shutdown (as losses if unsettled)."""
        for trade_id in list(self._pending.keys()):
            entry = self._pending[trade_id]
            self.close_trade(
                trade_id,
                btc_price_at_close=btc_price,
                winner="unknown",
                pnl=-entry.usdc_risked,  # assume worst case
                exit_reason=reason,
            )

    def _flush(self, entry: JournalEntry):
        """Enqueue entry for async disk write (non-blocking)."""
        self._write_queue.put(asdict(entry))

    def _writer_loop(self):
        """Daemon thread: drain queue and write to JSONL file."""
        while True:
            entry_dict = self._write_queue.get()
            if entry_dict is None:
                break
            try:
                self._file.write(json.dumps(entry_dict, default=str) + "\n")
                self._file.flush()
            except Exception as e:
                logger.error(f"Journal write failed: {e}")
            self._write_queue.task_done()
        self._file.close()

    def shutdown(self):
        """Flush pending trades, drain queue, and close the writer thread."""
        self.close_all_pending(0.0, reason="bot_shutdown")
        self._write_queue.put(None)  # sentinel to stop writer loop
        self._writer_thread.join(timeout=2)

