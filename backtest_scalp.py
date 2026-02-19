#!/usr/bin/env python3
"""
backtest_scalp.py — Analyse whether the scalping exit is net-positive.

For every completed trade in journals/trades.jsonl:
  • Classifies as SCALP, EJECT, or SETTLEMENT
  • For SCALP trades, estimates what the PnL would have been if held to settlement
  • Computes aggregate metrics for each exit type
  • Renders a clear summary with the verdict

Usage:
    python3 backtest_scalp.py
"""

import json
from collections import defaultdict
from pathlib import Path

JOURNAL = Path(__file__).parent / "journals" / "trades.jsonl"


def load_trades():
    """Load all CLOSE events from the trade journal."""
    trades = []
    seen_ids = set()
    with open(JOURNAL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # Only count closed trades (skip OPEN events)
            if rec.get("event_type") == "OPEN":
                continue
            if not rec.get("timestamp_close"):
                continue
            # Deduplicate by trade_id (keep latest/close)
            tid = rec.get("trade_id", "")
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            trades.append(rec)
    return trades


def classify_exit(rec):
    """Return 'SCALP', 'EJECT', or 'SETTLEMENT'."""
    reason = rec.get("exit_reason", "")
    if "SCALP" in reason.upper():
        return "SCALP"
    if "EJECT" in reason.upper():
        return "EJECT"
    return "SETTLEMENT"


def estimate_settlement_pnl(rec):
    """
    For a scalped trade, estimate what PnL would have been if held to settlement.

    Logic:
      - If position_side matches the winner → full payout: shares × (1.0 − entry)
      - If position_side doesn't match → full loss: −usdc_risked
      - If winner is unknown, return None

    We use the 'winner' field which records the actual market outcome.
    For scalped trades, the winner may be recorded as 'early_exit', so we
    need to infer from btc_price_at_close vs window_start_price.
    """
    side = rec.get("position_side", "")  # "Up" or "Down"
    entry = rec.get("entry_price", 0)
    usdc = rec.get("usdc_risked", 0)
    shares = rec.get("shares", 0)
    if isinstance(shares, str):
        shares = float(shares)

    winner = rec.get("winner", "")
    btc_close = rec.get("btc_price_at_close", 0)
    strike = rec.get("window_start_price", 0)

    # For early exits, infer the actual settlement winner
    if winner in ("early_exit", ""):
        if btc_close > 0 and strike > 0:
            winner = "Up" if btc_close > strike else "Down"
        else:
            return None  # can't determine

    if entry <= 0 or shares <= 0:
        return None

    if side == winner:
        # Won: each share pays $1.00
        settlement_pnl = shares * (1.0 - entry)
    else:
        # Lost: shares worth $0
        settlement_pnl = -usdc

    return settlement_pnl


def main():
    trades = load_trades()
    if not trades:
        print("No completed trades found in journal.")
        return

    # Classify
    by_type = defaultdict(list)
    for rec in trades:
        exit_type = classify_exit(rec)
        by_type[exit_type].append(rec)

    # ── Summary per exit type ──────────────────────────────────────────
    print("=" * 70)
    print("  SCALP BACKTEST — Real Trade Journal Analysis")
    print("=" * 70)
    print(f"\n  Total completed trades: {len(trades)}")

    for exit_type in ["SETTLEMENT", "SCALP", "EJECT"]:
        group = by_type.get(exit_type, [])
        if not group:
            print(f"\n  {exit_type}: 0 trades")
            continue

        total_pnl = sum(r.get("pnl", 0) for r in group)
        total_risked = sum(r.get("usdc_risked", 0) for r in group)
        wins = sum(1 for r in group if r.get("pnl", 0) >= 0)
        avg_pnl = total_pnl / len(group)
        roi = (total_pnl / total_risked * 100) if total_risked > 0 else 0

        print(f"\n  {exit_type}:")
        print(f"    Trades:     {len(group)}")
        print(f"    Wins:       {wins}/{len(group)} ({wins/len(group)*100:.0f}%)")
        print(f"    Total PnL:  ${total_pnl:+.2f}")
        print(f"    Avg PnL:    ${avg_pnl:+.4f}")
        print(f"    ROI:        {roi:+.1f}%")

    # ── SCALP vs HOLD-TO-SETTLEMENT comparison ────────────────────────
    scalps = by_type.get("SCALP", [])
    if not scalps:
        print("\n  No scalp trades to analyse.")
        return

    print("\n" + "=" * 70)
    print("  SCALP vs HOLD-TO-SETTLEMENT (What-If Analysis)")
    print("=" * 70)

    total_actual_pnl = 0.0
    total_hypothetical_pnl = 0.0
    comparisons = []
    unknowns = 0

    for rec in scalps:
        actual_pnl = rec.get("pnl", 0)
        hypo_pnl = estimate_settlement_pnl(rec)

        if hypo_pnl is None:
            unknowns += 1
            continue

        total_actual_pnl += actual_pnl
        total_hypothetical_pnl += hypo_pnl
        comparisons.append({
            "trade_id": rec.get("trade_id", "?")[:8],
            "side": rec.get("position_side", "?"),
            "entry": rec.get("entry_price", 0),
            "actual_pnl": actual_pnl,
            "hypo_pnl": hypo_pnl,
            "diff": hypo_pnl - actual_pnl,
            "would_win": hypo_pnl >= 0,
            "btc_close": rec.get("btc_price_at_close", 0),
            "strike": rec.get("window_start_price", 0),
        })

    if not comparisons:
        print("\n  Could not estimate settlement outcome for any scalps.")
        return

    # Per-trade detail
    print(f"\n  {'ID':<10} {'Side':<5} {'Entry':>6} {'Scalp PnL':>10} {'If Held':>10} {'Diff':>10} {'Verdict'}")
    print(f"  {'─'*10} {'─'*5} {'─'*6} {'─'*10} {'─'*10} {'─'*10} {'─'*10}")

    for c in comparisons:
        verdict = "BETTER" if c["diff"] > 0 else "WORSE" if c["diff"] < 0 else "SAME"
        v_mark = "↑ HOLD" if c["diff"] > 0.01 else "↑ SCALP" if c["diff"] < -0.01 else "≈ SAME"
        print(
            f"  {c['trade_id']:<10} {c['side']:<5} "
            f"${c['entry']:.2f}  "
            f"${c['actual_pnl']:>+8.4f}  "
            f"${c['hypo_pnl']:>+8.4f}  "
            f"${c['diff']:>+8.4f}  "
            f"{v_mark}"
        )

    # Aggregate verdict
    diff = total_hypothetical_pnl - total_actual_pnl
    scalps_better = sum(1 for c in comparisons if c["diff"] < -0.01)
    holds_better = sum(1 for c in comparisons if c["diff"] > 0.01)
    ties = len(comparisons) - scalps_better - holds_better

    print(f"\n  {'─' * 60}")
    print(f"  Total scalp PnL (actual):        ${total_actual_pnl:>+10.2f}")
    print(f"  Total PnL if held to settlement: ${total_hypothetical_pnl:>+10.2f}")
    print(f"  Difference (hold − scalp):        ${diff:>+10.2f}")
    if unknowns:
        print(f"  (Could not estimate {unknowns} trades — no settlement data)")

    print(f"\n  Scalp was better:  {scalps_better}/{len(comparisons)} trades")
    print(f"  Hold was better:   {holds_better}/{len(comparisons)} trades")
    print(f"  Approximately tie: {ties}/{len(comparisons)} trades")

    # Also show settlement-only stats for context
    settlements = by_type.get("SETTLEMENT", [])
    if settlements:
        settle_wins = sum(1 for r in settlements if r.get("won", False))
        settle_wr = settle_wins / len(settlements) * 100
        print(f"\n  Settlement win rate (held trades): {settle_wr:.0f}% ({settle_wins}/{len(settlements)})")

    # Final verdict
    print(f"\n  {'=' * 60}")
    if diff > 1.0:
        print(f"  ⚠️  VERDICT: HOLDING is significantly better (+${diff:.2f}).")
        print(f"  → The scalp exits are leaving ${diff:.2f} on the table.")
        print(f"  → Consider REMOVING or LOOSENING the scalp target.")
    elif diff > 0.10:
        print(f"  📊 VERDICT: HOLDING is slightly better (+${diff:.2f}).")
        print(f"  → Scalping is costing a small edge. Consider raising the target.")
    elif diff > -0.10:
        print(f"  ≈  VERDICT: ROUGHLY EVEN (diff = ${diff:.2f}).")
        print(f"  → Scalping adds risk reduction without meaningful PnL cost.")
    elif diff > -1.0:
        print(f"  ✅ VERDICT: SCALPING is slightly better (saves ${-diff:.2f}).")
        print(f"  → The early exits are protecting against reversals.")
    else:
        print(f"  ✅ VERDICT: SCALPING is significantly better (saves ${-diff:.2f}).")
        print(f"  → Holding to settlement exposes to large reversal losses.")
    print()


if __name__ == "__main__":
    main()
