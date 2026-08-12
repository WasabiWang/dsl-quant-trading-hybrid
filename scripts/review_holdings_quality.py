#!/usr/bin/env python3
"""
v4.6.9h P2: 持仓质量复核 — 检查持仓中 degraded 标的 + 输出建议

用途: 定期(每周五15:00)检查持仓 vs 最新精度
输出: 持仓中 degraded 标的清单 + 建议动作 (仅报告, 不自动交易)

规则 (与 morning_decision P0 退出一致):
  - degraded(精度<50%) AND 亏损>5% → 建议减50%
  - degraded AND 亏损<5% → 观察
  - degraded AND 盈利 → 保留(给观察期)

用法:
  python3 scripts/review_holdings_quality.py [--json]
"""
import os, sys, json, sqlite3, yaml
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

POOL_PATH = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
CALIB_PATH = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
DB_PATH = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")

LOSS_THRESHOLD = -0.05  # 亏损>5%触发建议
REDUCE_RATIO = 0.5      # 建议减50%


def load_degraded() -> dict:
    """返回 {code: accuracy} 的 degraded 标的"""
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = yaml.safe_load(f)
    deg = {}
    for s in pool.get("master_pool", []):
        if s.get("degraded"):
            deg[s["symbol"]] = s.get("accuracy", 0)
    return deg


def load_positions() -> list:
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM positions").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def main(json_out: bool = False):
    deg = load_degraded()
    positions = load_positions()
    if not positions:
        print("📭 无持仓")
        return 0

    result = {"checked_at": datetime.now().isoformat(), "degraded_holdings": []}
    for p in positions:
        code = p.get("stock_code", "")
        if code not in deg:
            continue
        avg = p.get("avg_cost", 0)
        cur = p.get("current_price", 0)
        qty = p.get("quantity", 0)
        if avg <= 0 or cur <= 0:
            continue
        pnl_pct = (cur - avg) / avg
        acc = deg.get(code, 0)
        if pnl_pct < LOSS_THRESHOLD:
            action = f"建议减{REDUCE_RATIO:.0%}({max(100, int(qty*REDUCE_RATIO/100)*100)}股)"
            level = "🔴"
        elif pnl_pct < 0:
            action = "观察(亏损未达5%)"
            level = "🟡"
        else:
            action = "保留(盈利观察)"
            level = "🟢"
        item = {
            "symbol": code, "accuracy": round(acc, 4),
            "pnl_pct": round(pnl_pct, 4), "action": action,
            "quantity": qty,
        }
        result["degraded_holdings"].append(item)
        if not json_out:
            print(f"  {level} {code}: acc={acc:.1%} 盈亏{pnl_pct:+.1%} → {action}")

    if json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\n📊 持仓中 degraded 标的: {len(result['degraded_holdings'])}/{len(positions)}")
        if not result["degraded_holdings"]:
            print("  ✅ 无 degraded 持仓")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="持仓质量复核")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()
    sys.exit(main(json_out=args.json))
