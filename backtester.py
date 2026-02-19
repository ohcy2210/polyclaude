#!/usr/bin/env python3
"""
backtester.py — Backtest strategy_engine against real BTC 1-second klines.

Usage:
    python3 backtester.py                      # default: btc_1s_klines.csv
    python3 backtester.py path/to/klines.csv   # custom data file

Derives synthetic Polymarket orderbook prices from the Student-t model,
then feeds them through the live StrategyEngine. Compares settlement-only
vs the live exit logic (95¢ eject + 30¢ scalp).
"""
import csv
import math
import sys
from collections import deque

import config
from strategy_engine import (
    StrategyEngine, OpenPosition,
    compute_micro_vol, student_t_cdf_approx,
)

# ── Data loading ───────────────────────────────────────────────────────────

def load_klines(path: str = "btc_1s_klines.csv") -> list[dict]:
    """Load 1-second BTC klines from CSV."""
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append({
                "close": float(r["close"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
            })
    return rows


def split_windows(klines: list[dict], n: int = 300) -> list[list[dict]]:
    """Split klines into 5-minute (300-tick) windows."""
    return [klines[i:i + n] for i in range(0, len(klines) - n + 1, n)]


def get_synthetic_orderbook(btc_price: float, strike: float,
                            vol: float, time_left: float) -> dict:
    """Derive a full synthetic Polymarket orderbook from the Student-t model.

    Spread widens near expiry (less liquidity as window closes).
    Returns YES_ASK, YES_BID, NO_ASK, NO_BID — all clamped to [0.01, 0.99].
    """
    yes_true_prob = student_t_cdf_approx(btc_price - strike, vol, time_left)
    no_true_prob = 1.0 - yes_true_prob

    # Spread widens as expiry approaches
    time_frac = (300.0 - time_left) / 300.0
    half_spread = 0.01 + (0.02 * time_frac)   # 1–3¢ half-spread

    clamp = lambda v: max(0.01, min(0.99, v))

    return {
        "YES_ASK": clamp(yes_true_prob + half_spread),
        "YES_BID": clamp(yes_true_prob - half_spread),
        "NO_ASK":  clamp(no_true_prob + half_spread),
        "NO_BID":  clamp(no_true_prob - half_spread),
    }


# ── Simulation ─────────────────────────────────────────────────────────────

def simulate(windows: list, exit_mode: str = "original") -> dict:
    """
    Run the strategy engine over all windows.

    exit_mode:
      'original'   — use live exit logic (95¢ eject + 30¢ scalp)
      'settlement'  — hold every trade to settlement, no early exits
    """
    balance = 50.0
    trades, wins, losses, total_pnl = 0, 0, 0, 0.0
    trade_log: list[dict] = []
    scalp_would_won = scalp_would_lost = 0

    # Persistent tick history — survives across windows so vol is never cold
    global_tick_deque: deque = deque(maxlen=300)
    global_t_idx = 0

    for w_idx, window in enumerate(windows):
        strike = window[0]["close"]
        engine = StrategyEngine(global_tick_deque)
        position = None
        fill_price = fill_usdc = 0.0
        pos_side = ""

        for t_idx, kline in enumerate(window):
            btc_price = kline["close"]
            time_left = 300.0 - t_idx
            global_tick_deque.append((btc_price, float(global_t_idx)))
            global_t_idx += 1

            if len(global_tick_deque) < 5:
                continue

            vol = compute_micro_vol(global_tick_deque)
            if vol <= 0:
                continue

            ob = get_synthetic_orderbook(btc_price, strike, vol, time_left)

            # ── Try to enter ───────────────────────────────────────────
            if position is None and time_left > 15:
                sig = engine.generate_signals(
                    current_btc_price=btc_price,
                    strike=strike,
                    time_left=time_left,
                    orderbook_ask=ob["YES_ASK"],
                    balance=balance,
                )
                if sig is not None:
                    # Fill at the correct side's ask
                    fill_price = ob["YES_ASK"] if sig.side == "YES" else ob["NO_ASK"]
                    alloc = min(getattr(sig, "size", balance), balance)
                    pos_shares = math.floor(alloc / fill_price)
                    if pos_shares >= 1:
                        fill_usdc = pos_shares * fill_price
                        pos_side = sig.side
                        position = OpenPosition(
                            side=sig.side,
                            average_entry_price=fill_price,
                            size_usdc=fill_usdc,
                        )

            # ── Check exits (original mode only) ──────────────────────
            elif position is not None and exit_mode == "original":
                # Liquidity vanishes in the last 10 seconds — skip exits
                if time_left < 10:
                    continue

                # Use the correct bid from the synthetic orderbook
                current_bid = ob["YES_BID"] if pos_side == "YES" else ob["NO_BID"]

                exit_sig = StrategyEngine.check_exit_conditions(
                    position, current_bid,
                )
                if exit_sig is not None:
                    # No fake taker fee — crossing the spread IS the cost
                    exit_price = current_bid
                    pnl = pos_shares * (exit_price - fill_price)
                    total_pnl += pnl
                    balance += pnl
                    trades += 1
                    if pnl >= 0:
                        wins += 1
                    else:
                        losses += 1

                    # Would settlement have been better?
                    final_btc = window[-1]["close"]
                    would_win = (final_btc > strike) if pos_side == "YES" else (final_btc <= strike)
                    reason_str = exit_sig.reason if hasattr(exit_sig, "reason") else str(exit_sig)
                    if "SCALP" in reason_str:
                        if would_win:
                            scalp_would_won += 1
                        else:
                            scalp_would_lost += 1

                    trade_log.append({
                        "window": w_idx, "side": pos_side,
                        "entry": fill_price, "exit": exit_price,
                        "pnl": pnl, "reason": reason_str[:45],
                        "would_settle": "WIN" if would_win else "LOSS",
                    })
                    position = None
                    continue

            # ── Settlement at end of window ────────────────────────────
            if t_idx == len(window) - 1 and position is not None:
                final_price = btc_price
                won = (final_price > strike) if pos_side == "YES" else (final_price <= strike)
                if won:
                    payout = pos_shares * 1.0
                    pnl = payout - fill_usdc
                else:
                    pnl = -fill_usdc
                total_pnl += pnl
                balance += pnl
                trades += 1
                if pnl >= 0:
                    wins += 1
                else:
                    losses += 1
                trade_log.append({
                    "window": w_idx, "side": pos_side,
                    "entry": fill_price,
                    "exit": 1.0 if won else 0.0,
                    "pnl": pnl,
                    "reason": "SETTLEMENT " + ("WIN" if won else "LOSS"),
                    "would_settle": "WIN" if won else "LOSS",
                })
                position = None

    days = len(windows) / 288.0
    wr = (wins / trades * 100) if trades > 0 else 0
    avg = total_pnl / trades if trades > 0 else 0
    tpd = trades / days if days > 0 else 0

    return {
        "trades": trades, "wins": wins, "losses": losses,
        "trades_day": round(tpd, 1), "win_rate": round(wr, 1),
        "avg_pnl": round(avg, 4), "total_pnl": round(total_pnl, 2),
        "balance": round(balance, 2), "trade_log": trade_log,
        "scalp_would_won": scalp_would_won,
        "scalp_would_lost": scalp_would_lost,
    }


# ── CLI Output ─────────────────────────────────────────────────────────────

def print_results(label: str, r: dict) -> None:
    """Pretty-print simulation results."""
    print(f"  Trades: {r['trades']}  |  Trades/Day: {r['trades_day']}")
    print(f"  Wins: {r['wins']}  |  Losses: {r['losses']}  |  Win Rate: {r['win_rate']}%")
    print(f"  Avg PnL/Trade: ${r['avg_pnl']}")
    print(f"  Total PnL: ${r['total_pnl']}  |  Final Balance: ${r['balance']}")


if __name__ == "__main__":
    data_path = sys.argv[1] if len(sys.argv) > 1 else "btc_1s_klines.csv"
    klines = load_klines(data_path)
    windows = split_windows(klines)
    days = len(windows) / 288.0
    print(f"  {len(windows)} windows ({days:.1f} days of real BTC data)\n")

    # === A) Live strategy (95¢ eject + 30¢ scalp) ===
    print("=" * 65)
    print("  A) LIVE STRATEGY (95¢ eject + 30¢ scalp)")
    print("=" * 65)
    rA = simulate(windows, "original")
    print_results("A", rA)

    if rA["scalp_would_won"] + rA["scalp_would_lost"] > 0:
        tot_scalps = rA["scalp_would_won"] + rA["scalp_would_lost"]
        print(f"\n  SCALP vs SETTLEMENT:")
        print(f"    Scalped trades that WOULD have won at settlement: {rA['scalp_would_won']}")
        print(f"    Scalped trades that WOULD have lost: {rA['scalp_would_lost']}")
        print(f"    Settlement win rate for scalped: {rA['scalp_would_won']/tot_scalps*100:.1f}%")

    # Exit breakdown
    reasons: dict[str, dict] = {}
    for t in rA["trade_log"]:
        key = t["reason"].split(" ")[0][:15]
        reasons.setdefault(key, {"count": 0, "pnl": 0.0, "wins": 0})
        reasons[key]["count"] += 1
        reasons[key]["pnl"] += t["pnl"]
        if t["pnl"] >= 0:
            reasons[key]["wins"] += 1

    print(f"\n  EXIT BREAKDOWN:")
    print(f"  {'Reason':<18} {'Count':>6} {'Win%':>6} {'Total PnL':>12}")
    print(f"  " + "-" * 45)
    for reason, s in sorted(reasons.items(), key=lambda x: -x[1]["count"]):
        wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
        print(f"  {reason:<18} {s['count']:>6} {wr:>5.1f}% ${s['pnl']:>+10.2f}")

    # Last 15 trades
    print(f"\n  TRADE LOG (last 15):")
    print(f"  {'#':>4} {'W/L':>4} {'Side':>4} {'Entry':>7} {'Exit':>7} {'PnL':>9} {'Settle?':>7} {'Reason'}")
    for i, t in enumerate(rA["trade_log"][-15:]):
        tag = "W" if t["pnl"] >= 0 else "L"
        idx = len(rA["trade_log"]) - 14 + i
        print(f"  {idx:>4} {tag:>4} {t['side']:>4} "
              f"{t['entry']:>7.4f} {t['exit']:>7.4f} ${t['pnl']:>+8.4f} "
              f"{t['would_settle']:>7} {t['reason'][:35]}")

    # === B) Pure settlement (no early exits) ===
    print(f"\n{'=' * 65}")
    print("  B) PURE SETTLEMENT (no early exits)")
    print("=" * 65)
    rB = simulate(windows, "settlement")
    print_results("B", rB)

    # === Comparison ===
    print(f"\n{'=' * 65}")
    print("  COMPARISON")
    print("=" * 65)
    print(f"  {'Strategy':<30} {'Trades':>7} {'Win%':>7} {'Total PnL':>12} {'ROI':>8}")
    print(f"  " + "-" * 66)
    roiA = (rA["balance"] - 50) / 50 * 100
    roiB = (rB["balance"] - 50) / 50 * 100
    print(f"  {'Live (scalp+eject)':<30} {rA['trades']:>7} {rA['win_rate']:>6}% ${rA['total_pnl']:>+10.2f} {roiA:>+7.1f}%")
    print(f"  {'Pure settlement':<30} {rB['trades']:>7} {rB['win_rate']:>6}% ${rB['total_pnl']:>+10.2f} {roiB:>+7.1f}%")
    better = "Live" if rA["total_pnl"] > rB["total_pnl"] else "Pure settlement"
    print(f"\n  → {better} is better by ${abs(rA['total_pnl'] - rB['total_pnl']):.2f}")
