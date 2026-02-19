"""
dashboard.py — Rich terminal dashboard for the Polymarket HFT Bot.

Renders a live TUI with panels for:
  • Header bar (system info, balances)
  • Market Data (current 5-min window, BTC price)
  • Active Trade (position, P&L, status)
  • Strategy Engine (bankroll, latest signal)
  • Trade History (last N trades)
  • Session Stats (runtime, W/L, ROI)
"""

from __future__ import annotations
import logging

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

import config


# ─── Trade record for history ──────────────────────────────────────────────

@dataclass
class TradeRecord:
    """Single completed trade for the history panel."""
    side: str            # "Up" or "Down"
    size: float          # USDC risked
    pnl: float           # profit/loss
    won: bool            # True if winning side
    timestamp: float = 0.0


# ─── Dashboard state (updated by bot.py) ───────────────────────────────────

@dataclass
class DashboardState:
    """Mutable state object shared between the bot and the dashboard renderer."""

    # Header
    system_name: str = "Baboon | PolyQuant Systems"
    usdc_balance: float = 0.0
    starting_balance: float = 0.0

    # Market Data
    market_question: str = "—"
    market_end_date: str = ""
    time_left_secs: float = 0.0
    btc_price: float = 0.0
    window_start_price: Optional[float] = None
    up_price: Optional[float] = None
    down_price: Optional[float] = None
    up_bid: Optional[float] = None
    down_bid: Optional[float] = None
    market_end_ts: float = 0.0       # epoch timestamp when market closes

    # Active Trade
    position_side: Optional[str] = None   # "Up" or "Down" or None
    position_entry: float = 0.0
    position_size: float = 0.0
    position_status: str = "—"            # "WINNING" / "LOSING" / "—"
    projected_pnl: float = 0.0

    # Strategy Engine
    last_signal_text: str = ""
    last_edge: float = 0.0
    last_ev: float = 0.0

    # Order lifecycle: "" → "pending" → "filled"
    order_status: str = ""        # "", "pending", "filled"
    order_side: str = ""          # "Up" / "Down" while pending

    # Trade History
    trade_history: deque = field(default_factory=lambda: deque(maxlen=50))
    total_volume_risked: float = 0.0

    # Session Stats
    start_time: float = field(default_factory=time.time)
    markets_seen: int = 0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0

    @property
    def runtime_secs(self) -> int:
        return int(time.time() - self.start_time)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def avg_pnl(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades


# ─── Dashboard renderer ───────────────────────────────────────────────────

class Dashboard:
    """
    Renders the live terminal dashboard using rich.

    Usage:
        state = DashboardState()
        dash = Dashboard(state)
        dash.start()           # starts Live rendering in background
        state.btc_price = ...  # update from bot
        dash.stop()            # clean exit
    """

    def __init__(self, state: DashboardState):
        self.state = state
        self.console = Console()
        self._live: Optional[Live] = None
        self.layout = self._create_base_layout()

    def start(self) -> Live:
        """Start the live display. Returns the Live object."""
        self._live = Live(
            self.layout,
            console=self.console,
            refresh_per_second=4,
            screen=True,
        )
        self._live.start()
        return self._live

    def stop(self):
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

    def update(self):
        """Refresh the display by patching existing layout slots (no rebuild)."""
        if self._live:
            self.layout["header"].update(self._safe_render(self._render_header))
            self.layout["market"].update(self._safe_render(self._render_market_data))
            self.layout["strategy"].update(self._safe_render(self._render_strategy))
            self.layout["center"].update(self._safe_render(self._render_active_trade))
            self.layout["right"].update(self._safe_render(self._render_trade_history))
            self.layout["footer"].update(self._safe_render(self._render_session_stats))

    # ── Layout skeleton (built once) ─────────────────────────────────────

    def _create_base_layout(self) -> Layout:
        """Build the structural layout skeleton — panels are patched in update()."""
        layout = Layout()

        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=7),
        )

        # Body — three columns
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="center", ratio=1),
            Layout(name="right", ratio=1),
        )
        layout["left"].split_column(
            Layout(name="market", ratio=1),
            Layout(name="strategy", ratio=1),
        )

        return layout

    @staticmethod
    def _safe_render(render_fn):
        """Call a panel renderer; return an error panel on any exception."""
        try:
            return render_fn()
        except Exception as e:
            logging.getLogger(__name__).debug(f"Dashboard render error in {render_fn.__name__}: {e}")
            return Panel(
                Text(f"[render error: {e}]", style="dim red"),
                border_style="red", box=box.ROUNDED,
            )

    # ── Panel renderers ─────────────────────────────────────────────────

    def _render_header(self) -> Panel:
        s = self.state
        header = Text()
        header.append(" Baboon", style="bold #1e3a5f on cyan")
        header.append(" | PolyQuant Systems ", style="bold black on cyan")
        header.append("  │  ", style="dim")
        header.append(f"Balance: ", style="dim")
        header.append(f"${s.usdc_balance:.2f}", style="bold green")
        header.append("  │  ", style="dim")
        header.append(f"BTC: ", style="dim")
        header.append(f"${s.btc_price:,.2f}", style="bold yellow")
        header.append("  │  ", style="dim")
        header.append(f"Std Bet: ", style="dim")
        std_bet, _ = config.get_tier_bet(s.usdc_balance)
        header.append(f"${std_bet:.2f}", style="white")

        rt = s.runtime_secs
        header.append("  │  ", style="dim")
        header.append(f"Runtime: {rt // 3600}h {(rt % 3600) // 60}m {rt % 60}s", style="dim cyan")

        return Panel(header, style="cyan", box=box.DOUBLE)

    def _render_market_data(self) -> Panel:
        s = self.state
        t = Text()

        t.append("Market: ", style="dim")
        t.append(f"{s.market_question}\n", style="bold white")

        # Time remaining — compute from wall clock for smooth display
        if s.market_end_ts > 0:
            live_left = max(0.0, s.market_end_ts - time.time())
        else:
            live_left = s.time_left_secs
        mins = int(live_left) // 60
        secs = int(live_left) % 60
        time_style = "bold red" if live_left < 30 else "bold green"
        t.append("Resolves in: ", style="dim")
        t.append(f"{mins:02d}:{secs:02d}\n", style=time_style)

        # Window start price (effective strike)
        t.append("\nStrike: ", style="dim")
        if s.window_start_price:
            t.append(f"${s.window_start_price:,.2f}\n", style="bold magenta")
        else:
            t.append("—\n", style="dim")

        # CLOB prices & MM spread
        if s.up_price is not None and s.down_price is not None:
            t.append("\nUP Ask: ", style="dim")
            t.append(f"${s.up_price:.3f}", style="white")
            t.append("  │  DOWN Ask: ", style="dim")
            t.append(f"${s.down_price:.3f}\n", style="white")

            spread = (s.up_price + s.down_price) - 1.0
            if spread <= 0.02:
                spread_style = "bold green"
            elif spread <= 0.05:
                spread_style = "yellow"
            else:
                spread_style = "bold red"
            t.append("Spread: ", style="dim")
            t.append(f"{spread * 100:.1f}¢\n", style=spread_style)

        t.append("\n")
        t.append("Binance BTC: ", style="dim")
        t.append(f"${s.btc_price:,.2f}\n", style="bold yellow")

        # Strike vs BTC delta
        if s.window_start_price and s.btc_price > 0:
            delta = s.btc_price - s.window_start_price
            delta_pct = (delta / s.window_start_price) * 100
            above = delta >= 0
            arrow = "▲" if above else "▼"
            delta_style = "bold green" if above else "bold red"
            side_label = "UP" if above else "DOWN"
            t.append(f"\n{arrow} BTC is ", style="dim")
            t.append(f"${abs(delta):,.2f} ({delta_pct:+.2f}%) {side_label}", style=delta_style)
            t.append(" from strike", style="dim")

        return Panel(t, title="[bold cyan]Market Data[/]", border_style="cyan",
                     box=box.ROUNDED)

    def _render_active_trade(self) -> Panel:
        s = self.state
        t = Text()

        if s.position_side is None:
            t.append("\n\n")
            t.append("     No active position\n", style="dim italic")
            t.append("     Waiting for signal …\n", style="dim")
            t.append("\n\n")
        else:
            # Market window
            t.append("Market: ", style="dim")
            t.append(f"{s.market_question}\n", style="white")

            # Resolution timer
            if s.market_end_ts > 0:
                live_left = max(0.0, s.market_end_ts - time.time())
            else:
                live_left = s.time_left_secs
            mins = int(live_left) // 60
            secs = int(live_left) % 60
            time_style = "bold red" if live_left < 30 else "bold green"
            t.append("Resolves: ", style="dim")
            t.append(f"{mins:02d}:{secs:02d}\n", style=time_style)

            # Position
            side_style = "bold green" if s.position_side == "Up" else "bold red"
            t.append("Bet: ", style="dim")
            t.append(f"{s.position_side.upper()} ", style=side_style)

            shares = int(s.position_size / s.position_entry) if s.position_entry > 0 else 0
            t.append(f"${s.position_size:.2f} ({shares} shares)\n", style="white")

            t.append("Entry: ", style="dim")
            t.append(f"${s.position_entry:.4f}", style="white")
            t.append("  │  Strike: ", style="dim")
            start_str = f"${s.window_start_price:,.2f}" if s.window_start_price else "—"
            t.append(f"{start_str}\n", style="bold magenta")

            # Live bid — what shares could sell for right now
            live_bid = s.up_bid if s.position_side == "Up" else s.down_bid
            if live_bid is not None:
                bid_delta = live_bid - s.position_entry
                bid_pct = (bid_delta / s.position_entry * 100) if s.position_entry > 0 else 0
                bid_style = "green" if bid_delta >= 0 else "red"
                t.append("Live Bid: ", style="dim")
                t.append(f"${live_bid:.2f}", style=f"bold {bid_style}")
                t.append(f"  ({bid_delta:+.2f} / {bid_pct:+.1f}%)", style=bid_style)
                t.append("\n")

            # Current BTC vs start
            t.append("\nBTC Now: ", style="dim")
            t.append(f"${s.btc_price:,.2f}", style="bold yellow")
            if s.window_start_price and s.window_start_price > 0:
                delta = s.btc_price - s.window_start_price
                delta_style = "green" if delta >= 0 else "red"
                arrow = "↑" if delta >= 0 else "↓"
                t.append(f" ({arrow} ${abs(delta):,.2f})\n",
                         style=delta_style)
            else:
                t.append("\n")

            # Status
            t.append("Status: ", style="dim")
            if s.position_status == "WINNING":
                t.append("WINNING ✓\n", style="bold green")
            elif s.position_status == "LOSING":
                t.append("LOSING ✗\n", style="bold red")
            else:
                t.append(f"{s.position_status}\n", style="yellow")

            # Projected PnL
            t.append("If settled: ", style="dim")
            pnl_style = "bold green" if s.projected_pnl >= 0 else "bold red"
            t.append(f"${s.projected_pnl:+.2f}", style=pnl_style)

        # Dynamic border color
        if s.position_side and s.position_status == "WINNING":
            border = "green"
        elif s.position_side and s.position_status == "LOSING":
            border = "red"
        else:
            border = "yellow"

        return Panel(t, title="[bold yellow]ACTIVE TRADE[/]",
                     border_style=border, box=box.ROUNDED)

    def _render_strategy(self) -> Panel:
        s = self.state
        t = Text()

        # Bankroll
        t.append("Bankroll: ", style="bold white")
        t.append(f"${s.usdc_balance:.2f}\n", style="green")

        t.append("\n")

        # Latest signal / order status
        if s.order_status == "pending":
            t.append("Order: ", style="bold white")
            t.append(f"⏳ LIMIT ORDER PENDING  ({s.order_side})\n", style="bold yellow")
            t.append(f"  Edge: {s.last_edge:.1%}", style="green")
            t.append(f"  │  EV: ${s.last_ev:.3f}\n", style="green")
        elif s.order_status == "filled":
            t.append("Order: ", style="bold white")
            t.append(f"✓ FILLED — position active\n", style="bold green")
        elif s.last_signal_text:
            t.append("Signal: ", style="bold white")
            t.append(f"{s.last_signal_text}\n", style="bold cyan")
            t.append(f"  Edge: {s.last_edge:.1%}", style="green")
            t.append(f"  │  EV: ${s.last_ev:.3f}\n", style="green")
        else:
            t.append("Signal: ", style="bold white")
            t.append("SCANNING …\n", style="dim italic")

        return Panel(t, title="[bold green]Strategy Engine[/]",
                     border_style="green", box=box.ROUNDED)

    def _render_trade_history(self) -> Panel:
        import datetime

        trades = list(self.state.trade_history)[-20:]  # Show last 20

        table = Table(box=box.SIMPLE_HEAVY, show_header=False, pad_edge=False,
                      expand=True)
        table.add_column("Time", style="dim", width=8)
        table.add_column("W/L", width=4)
        table.add_column("Side", width=5)
        table.add_column("Size", justify="right", width=7)
        table.add_column("PnL", justify="right", width=8)

        if not trades:
            table.add_row("", "", Text("No trades yet", style="dim italic"), "", "")
        else:
            for tr in trades:
                time_str = datetime.datetime.fromtimestamp(tr.timestamp).strftime('%H:%M:%S')
                tag_style = "green" if tr.won else "red"
                tag = "WIN" if tr.won else "LOSS"
                pnl_style = "green" if tr.pnl >= 0 else "red"
                table.add_row(
                    time_str,
                    Text(tag, style=tag_style),
                    Text(tr.side.upper(), style="white"),
                    Text(f"${tr.size:.2f}", style="dim"),
                    Text(f"${tr.pnl:+.2f}", style=pnl_style),
                )

        return Panel(table, title="[bold magenta]Trade History[/]",
                     border_style="magenta", box=box.ROUNDED)

    def _render_session_stats(self) -> Panel:
        s = self.state

        table = Table(box=box.SIMPLE_HEAVY, style="yellow", expand=True,
                      show_edge=False, pad_edge=False)
        table.add_column("Markets", justify="center")
        table.add_column("Trades", justify="center")
        table.add_column("W / L", justify="center")
        table.add_column("Win Rate", justify="center")
        table.add_column("Avg PnL", justify="center")
        table.add_column("Total PnL", justify="center")
        table.add_column("Session ROI", justify="center")
        table.add_column("ROI/Trade", justify="center")

        wl_text = Text()
        wl_text.append(f"{s.wins}W", style="green")
        wl_text.append(" / ", style="dim")
        wl_text.append(f"{s.losses}L", style="red")

        wr_style = "green" if s.win_rate >= 50 else "red"
        wr_text = Text(f"{s.win_rate:.1f}%", style=wr_style)

        avg_pnl = s.avg_pnl
        avg_style = "green" if avg_pnl >= 0 else "red"

        total_style = "bold green" if s.total_pnl >= 0 else "bold red"

        # Session ROI = total_pnl / starting_balance
        start_bal = s.starting_balance if s.starting_balance > 0 else 1.0
        roi = (s.total_pnl / start_bal * 100)
        roi_style = "green" if roi >= 0 else "red"

        # ROI per trade = average PnL / average bet size
        total_risked = s.total_volume_risked
        avg_bet = total_risked / s.total_trades if s.total_trades > 0 else 1
        roi_per_trade = (avg_pnl / avg_bet * 100) if avg_bet > 0 else 0
        rpt_style = "green" if roi_per_trade >= 0 else "red"

        table.add_row(
            str(s.markets_seen),
            str(s.total_trades),
            wl_text,
            wr_text,
            Text(f"${avg_pnl:.2f}", style=avg_style),
            Text(f"${s.total_pnl:+.2f}", style=total_style),
            Text(f"{roi:+.1f}%", style=roi_style),
            Text(f"{roi_per_trade:+.1f}%", style=rpt_style),
        )

        return Panel(table, title="[bold yellow]Session Stats[/]",
                     border_style="yellow", box=box.ROUNDED)
