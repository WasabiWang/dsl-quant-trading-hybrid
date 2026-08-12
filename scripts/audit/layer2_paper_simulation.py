#!/usr/bin/env python3
"""
DSL Layer 2: 模拟交易回放 — 读取paper_trading.db计算绩效指标
"""
import os, sys, json, argparse, sqlite3
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np

COMMISSION = 0.0003; STAMP_TAX = 0.001; MIN_COMM = 5.0

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=(datetime.now()-timedelta(days=60)).strftime("%Y-%m-%d"))
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    args = p.parse_args()

    print("="*60)
    print(f"DSL Layer2: {args.start} -> {args.end}")
    print("="*60)

    db = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
    if not os.path.exists(db):
        print("no paper_trading.db")
        return

    conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
    try:
        trades = conn.execute(
            "SELECT * FROM trade_history "
            "WHERE timestamp BETWEEN ? AND ? AND COALESCE(is_valid_for_metrics, 1)=1 "
            "ORDER BY timestamp",
            (args.start, args.end)
        ).fetchall()
    except Exception:
        trades = conn.execute(
            "SELECT * FROM trade_history WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (args.start, args.end)
        ).fetchall()

    if not trades:
        print("no trades in period")
        conn.close()
        return

    # Summary stats
    n_buy = sum(1 for t in trades if t["action"] == "BUY")
    n_sell = sum(1 for t in trades if t["action"] == "SELL")
    total_comm = sum(t["commission"] for t in trades if t["commission"])
    total_stamp = sum(t["stamp_tax"] for t in trades if t["stamp_tax"])

    # Fee ratio
    total_amount = sum(t["amount"] for t in trades if t["amount"])
    fee_ratio = (total_comm + total_stamp) / max(total_amount, 1e-6)

    try:
        isolated = conn.execute(
            "SELECT COUNT(*) AS c FROM trade_history WHERE COALESCE(is_valid_for_metrics, 1)=0"
        ).fetchone()["c"]
    except Exception:
        isolated = 0

    print(f"\ntrades: {len(trades)} (buy={n_buy} sell={n_sell})")
    if isolated:
        print(f"isolated legacy trades excluded from metrics: {isolated}")
    print(f"total amount: {total_amount:,.0f}")
    print(f"commission: {total_comm:,.1f} stamp: {total_stamp:,.1f}")
    print(f"fee ratio: {fee_ratio:.3%}")

    # Get portfolio value from ledger
    try:
        cash = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
        cash_val = float(cash["value"]) if cash else 1000000
    except Exception:
        cash_val = 1000000

    positions = conn.execute("SELECT * FROM positions").fetchall()
    pos_val = sum(p["quantity"] * p["current_price"] for p in positions)
    total_val = cash_val + pos_val

    print(f"current cash: {cash_val:,.0f} positions: {pos_val:,.0f} total: {total_val:,.0f}")

    # Last 5 trades
    print(f"\nrecent trades:")
    for t in trades[-5:]:
        print(f"  {t['timestamp']} {t['action']} {t['stock_code']} x{t['quantity']} @{t['price']:.2f} fee={t['total_fee']:.1f}")

    conn.close()
    print(f"\nverdict: {n_sell} sells, {n_buy} buys, fee={fee_ratio:.2%}")

if __name__ == "__main__":
    main()
