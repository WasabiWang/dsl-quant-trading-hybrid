#!/usr/bin/env python3
"""
Mark legacy paper trades that should not be used for performance metrics.

The script is idempotent and preserves every trade row. It only updates:
  - is_valid_for_metrics: 0/1
  - quality_flag: comma-separated quality labels
"""
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, time


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")


def ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_history)")}
    if "is_valid_for_metrics" not in cols:
        conn.execute("ALTER TABLE trade_history ADD COLUMN is_valid_for_metrics INTEGER DEFAULT 1")
    if "quality_flag" not in cols:
        conn.execute("ALTER TABLE trade_history ADD COLUMN quality_flag TEXT DEFAULT 'valid'")


def parse_ts(raw: str):
    try:
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def lot_violation(stock_code: str, action: str, quantity: int) -> bool:
    if quantity <= 0:
        return True
    if str(stock_code).startswith("688"):
        return action == "BUY" and quantity < 200
    return quantity % 100 != 0


def main():
    if not os.path.exists(DB_PATH):
        print(f"missing db: {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ensure_columns(conn)
        rows = conn.execute("SELECT * FROM trade_history ORDER BY id").fetchall()

        by_symbol_day = defaultdict(set)
        for row in rows:
            ts = parse_ts(row["timestamp"])
            if ts:
                by_symbol_day[(row["stock_code"], ts.date().isoformat())].add(row["action"])

        flagged = 0
        reasons_count = defaultdict(int)
        for row in rows:
            reasons = []
            ts = parse_ts(row["timestamp"])
            if lot_violation(row["stock_code"], row["action"], int(row["quantity"])):
                reasons.append("invalid_lot")
            if ts and ts.time() > time(15, 0):
                reasons.append("after_market_close")
            if ts and by_symbol_day[(row["stock_code"], ts.date().isoformat())] == {"BUY", "SELL"}:
                reasons.append("same_day_round_trip")

            flag = ",".join(reasons) if reasons else "valid"
            valid = 0 if reasons else 1
            if not valid:
                flagged += 1
                for reason in reasons:
                    reasons_count[reason] += 1
            conn.execute(
                "UPDATE trade_history SET is_valid_for_metrics=?, quality_flag=? WHERE id=?",
                (valid, flag, row["id"]),
            )
        conn.commit()

        total = len(rows)
        valid_count = total - flagged
        print(f"paper_trade_quality total={total} valid={valid_count} isolated={flagged}")
        for reason, count in sorted(reasons_count.items()):
            print(f"  {reason}: {count}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
