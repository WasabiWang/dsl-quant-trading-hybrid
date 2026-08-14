#!/usr/bin/env python3
"""v4.6.9i(审计F1-5): 校准实现闭环历史回填脚本

背景: daily_records 74条 realized_checked 全部 false(审计P0-5), 校准闭环从未运行。
本脚本一次性回填: 每只标的拉取一次麦蕊全量K线(前复权), 本地计算全部历史预测的
5日实际收益与方向正确性, 更新 prediction_calibration.json。

用法: .venv/bin/python3 scripts/backfill_realized_checks.py [--max-days N] [--dry-run]
默认回填全部历史(max_days=999)。每只标的仅1次API调用(麦蕊), 全量约29次。
"""
import os, sys, json, argparse
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

CALIB_PATH = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")


def load_kline_map(symbol: str):
    """拉取一只标的全量前复权K线, 返回 {date: close}"""
    try:
        from config.mairui_api_config import get_kline_history
        rows = get_kline_history(symbol, period="d", adjust="f")
        if not rows:
            return None
        m = {}
        for r in rows:
            try:
                m[r["t"]] = float(r["c"])
            except (KeyError, TypeError, ValueError):
                continue
        return m
    except Exception as e:
        print(f"  ⚠️ {symbol} K线拉取失败: {str(e)[:80]}")
        return None


def is_trading_day_ish(date_str):
    """简单判断: 该日期在任一标的K线中存在即可, 由调用方传入kline_days集合"""
    return date_str in TRADING_DAYS


TRADING_DAYS = set()


def main():
    global TRADING_DAYS
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-days", type=int, default=999, help="最多回溯天数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写回")
    args = parser.parse_args()

    with open(CALIB_PATH, "r", encoding="utf-8") as f:
        cal = json.load(f)

    daily_records = cal.get("daily_records", [])
    today = datetime.now().date()

    # 收集需要检查的 (date, symbol) 集合
    pending = []
    for dr in daily_records:
        try:
            pred_date = datetime.strptime(dr["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        days_elapsed = (today - pred_date).days
        if days_elapsed > args.max_days or days_elapsed < 1:
            continue
        for s in dr.get("stocks", []):
            if s.get("realized_checked", False):
                continue
            if s.get("signal", "hold") == "hold":
                continue
            if "predicted_return" not in s:
                continue
            pending.append((dr["date"], s.get("symbol", ""), s.get("horizon", "5d")))

    symbols = sorted({s for _, s, _ in pending if s})
    print(f"待回填: {len(pending)} 条预测 × {len(symbols)} 只标的")

    # 每只标的拉一次K线
    kline_maps = {}
    for sym in symbols:
        m = load_kline_map(sym)
        if m:
            kline_maps[sym] = m
            TRADING_DAYS.update(m.keys())
    print(f"K线就绪: {len(kline_maps)}/{len(symbols)} 只")

    # 逐条计算
    checked = correct = 0
    for dr in daily_records:
        try:
            pred_date = datetime.strptime(dr["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        days_elapsed = (today - pred_date).days
        if days_elapsed > args.max_days or days_elapsed < 1:
            continue
        day_modified = False
        for s in dr.get("stocks", []):
            if s.get("realized_checked", False):
                continue
            if s.get("signal", "hold") == "hold":
                s["realized_checked"] = True
                s["realized_correct"] = True  # hold=未操作=正确(与check_realized_accuracy一致)
                day_modified = True
                continue
            if "predicted_return" not in s:
                continue
            sym = s.get("symbol", "")
            km = kline_maps.get(sym)
            if not km or dr["date"] not in km:
                continue
            horizon = s.get("horizon", "5d")
            try:
                hd = int(''.join(c for c in horizon if c.isdigit())) or 5
            except Exception:
                hd = 5
            # 交易日序列: 从该日期起第hd个交易日
            sorted_days = sorted(d for d in TRADING_DAYS if dr["date"] <= d <= (pred_date + timedelta(days=hd * 5)).strftime("%Y-%m-%d"))
            if len(sorted_days) <= hd:
                continue
            target_day = sorted_days[hd]
            pred_close = km[dr["date"]]
            future_close = km[target_day]
            actual = (future_close - pred_close) / pred_close
            pred_ret = s.get("predicted_return", 0)
            pred_dir = 1 if pred_ret > 0 else (-1 if pred_ret < 0 else 0)
            act_dir = 1 if actual > 0 else (-1 if actual < 0 else 0)
            s["realized_return"] = round(actual, 4)
            s["realized_correct"] = (pred_dir == act_dir)
            s["realized_checked"] = True
            day_modified = True
            checked += 1
            if pred_dir == act_dir:
                correct += 1
        if day_modified:
            dr["correct_predictions"] = sum(
                1 for x in dr.get("stocks", []) if x.get("realized_correct", False))

    # 汇总 overall_stats
    total_correct = sum(dr.get("correct_predictions", 0) for dr in daily_records)
    ov = cal.get("overall_stats", {}) or {}
    ov["correct_predictions"] = total_correct
    ov["realized_checked_total"] = sum(
        1 for dr in daily_records for s in dr.get("stocks", []) if s.get("realized_checked", False))
    cal["overall_stats"] = ov
    cal["daily_records"] = daily_records

    print(f"回填完成: 新增{checked}条 | 正确{correct} | 精度{correct / max(checked, 1):.1%}")
    if not args.dry_run:
        cal["last_updated"] = datetime.now().isoformat()
        with open(CALIB_PATH, "w", encoding="utf-8") as f:
            json.dump(cal, f, indent=2, ensure_ascii=False)
        print(f"✅ 已写回 {CALIB_PATH}")
    else:
        print("(dry-run, 未写回)")


if __name__ == "__main__":
    main()
