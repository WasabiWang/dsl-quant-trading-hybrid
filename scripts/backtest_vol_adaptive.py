#!/usr/bin/env python3
"""
v4.7.0 P2-1: 波动率自适应信号阈值回测

问题: 统一±1%收益门槛无视波动率极差4.7倍(通富92% vs 东财19%)
设计: 有效门槛 = 1% × clamp(vol20/截面中位数, 0.5, 2.0)

回测方法(用校准库的真实历史预测):
  1. 读 calibration.daily_records 中已回验(buy/sell信号, realized_checked)的记录
  2. 对每只标的拉麦蕊日K, 计算每个预测日的 vol20 缩放系数
  3. 静态门槛 vs 自适应门槛 → 比较触发数/方向精度/盈亏比
  4. 分解: 自适应新增信号(低波) vs 自适应过滤信号(高波) 的各自精度

用法: python3 scripts/backtest_vol_adaptive.py [--days 90] [--min-acc 0.55] [--min-ret 0.01]
"""
import os, sys, json, math
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

CALIB_PATH = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"
FLOOR, CAP = 0.5, 2.0


def load_kline(symbol: str, limit: int = 250) -> dict:
    """{date_str: close} 供波动率/收益计算"""
    try:
        from config.mairui_api_config import get_kline_history
        kl = get_kline_history(symbol, period="d", adjust="f", limit=limit)
        out = {}
        for x in kl:
            try:
                out[x["t"]] = float(x["c"])
            except Exception:
                continue
        return out
    except Exception:
        return {}


def vol20_at(closes_sorted: list, date_str: str) -> float:
    """date_str当天前20个交易日的年化波动率"""
    idx = None
    for i, (d, c) in enumerate(closes_sorted):
        if d <= date_str:
            idx = i
        else:
            break
    if idx is None or idx < 21:
        return 0.0
    window = [closes_sorted[j][1] for j in range(idx - 20, idx + 1)]
    rets = [window[i] / window[i - 1] - 1 for i in range(1, len(window))]
    if not rets:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / max(len(rets) - 1, 1)
    return math.sqrt(var) * math.sqrt(252)


def actual_return(closes_sorted: list, date_str: str, horizon: int) -> float:
    """预测日收盘 → horizon日后收盘 的实际收益"""
    idx = None
    for i, (d, c) in enumerate(closes_sorted):
        if d <= date_str:
            idx = i
        else:
            break
    if idx is None:
        return 0.0
    end = min(idx + horizon, len(closes_sorted) - 1)
    base = closes_sorted[idx][1]
    endc = closes_sorted[end][1]
    return endc / base - 1 if base else 0.0


def main(days: int = 90, min_acc: float = 0.55, min_ret: float = 0.01):
    with open(CALIB_PATH, encoding="utf-8") as f:
        cal = json.load(f)
    daily_records = cal.get("daily_records", [])
    print(f"校准库: {len(daily_records)}个交易日记录")

    # 收集(buy/sell且已回验)的预测
    records = []
    symbols = set()
    cutoff = datetime.now().date() - timedelta(days=days)
    for dr in daily_records:
        try:
            d = datetime.strptime(dr["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        if d < cutoff:
            continue
        for s in dr.get("stocks", []):
            if s.get("signal") not in ("buy", "sell"):
                continue
            if not s.get("realized_checked"):
                continue
            pr = s.get("predicted_return", 0) or 0
            acc = s.get("direction_accuracy", s.get("accuracy", 0)) or 0
            h = s.get("horizon", "5d") or "5d"
            try:
                hd = int(''.join(ch for ch in h if ch.isdigit())) or 1
            except Exception:
                hd = 1
            sym = str(s.get("symbol", ""))
            if not sym:
                continue
            records.append({
                "symbol": sym, "date": dr["date"], "pred_ret": float(pr),
                "acc": float(acc), "horizon": hd,
                "realized_correct": bool(s.get("realized_correct")),
            })
            symbols.add(sym)
    print(f"回测样本: {len(records)}条预测 ({len(symbols)}只标的, 近{days}天, buy/sell已回验)")

    # 拉K线
    closes = {}
    for sym in sorted(symbols):
        k = load_kline(sym)
        if k:
            closes[sym] = sorted(k.items())
    print(f"K线获取: {len(closes)}/{len(symbols)}只")

    # 逐日截面vol中位数
    daily_vols = defaultdict(dict)  # date -> {symbol: vol20}
    for sym, cs in closes.items():
        for d, _ in cs:
            if len(daily_vols[d]) > 0 and sym in daily_vols[d]:
                continue
            v = vol20_at(cs, d)
            if v > 0:
                daily_vols[d][sym] = v

    # 评估
    def evaluate(use_adaptive: bool) -> dict:
        fired, correct = 0, 0
        rets_pos, rets_neg = [], []
        added, added_corr = 0, 0
        removed, removed_corr = 0, 0
        for r in records:
            sym = r["symbol"]
            cs = closes.get(sym)
            if not cs:
                continue
            v = daily_vols.get(r["date"], {}).get(sym, 0)
            scale = 1.0
            if v > 0:
                vols_today = [vv for vv in daily_vols.get(r["date"], {}).values() if vv > 0]
                med = sorted(vols_today)[len(vols_today) // 2] if vols_today else v
                scale = max(FLOOR, min(CAP, v / med)) if med > 0 else 1.0
            static_fire = r["acc"] >= min_acc and abs(r["pred_ret"]) >= min_ret
            adaptive_fire = r["acc"] >= min_acc and abs(r["pred_ret"]) >= min_ret * scale
            fire = adaptive_fire if use_adaptive else static_fire
            if not fire:
                continue
            actual = actual_return(cs, r["date"], r["horizon"])
            ok = r["realized_correct"]
            fired += 1
            if ok:
                correct += 1
            (rets_pos if actual >= 0 else rets_neg).append(actual)
            if use_adaptive and not static_fire:
                added += 1
                if ok:
                    added_corr += 1
            if not use_adaptive and static_fire and not adaptive_fire:
                removed += 1
                if ok:
                    removed_corr += 1
        acc_rate = correct / fired if fired else 0.0
        avg_win = sum(rets_pos) / len(rets_pos) if rets_pos else 0.0
        avg_loss = sum(rets_neg) / len(rets_neg) if rets_neg else 0.0
        pl_ratio = (avg_win / abs(avg_loss)) if avg_loss else float("inf")
        return {
            "fired": fired, "correct": correct, "acc": acc_rate,
            "avg_win": avg_win, "avg_loss": avg_loss, "pl_ratio": pl_ratio,
            "added": added, "added_acc": added_corr / added if added else 0.0,
            "removed": removed, "removed_acc": removed_corr / removed if removed else 0.0,
        }

    static = evaluate(use_adaptive=False)
    adaptive = evaluate(use_adaptive=True)

    print("\n" + "=" * 62)
    print(f"回测结果: 静态门槛(±{min_ret:.1%}) vs 自适应门槛(±{min_ret:.1%}×scale)")
    print("=" * 62)
    print(f"{'指标':<18}{'静态':>12}{'自适应':>12}")
    print(f"{'触发信号数':<16}{static['fired']:>12}{adaptive['fired']:>12}")
    print(f"{'方向正确数':<16}{static['correct']:>12}{adaptive['correct']:>12}")
    print(f"{'方向精度':<17}{static['acc']:>11.1%}{adaptive['acc']:>11.1%}")
    print(f"{'平均盈利':<17}{static['avg_win']:>11.2%}{adaptive['avg_win']:>11.2%}")
    print(f"{'平均亏损':<17}{static['avg_loss']:>11.2%}{adaptive['avg_loss']:>11.2%}")
    print(f"{'盈亏比':<18}{static['pl_ratio']:>11.2f}{adaptive['pl_ratio']:>11.2f}")
    print("-" * 62)
    print(f"自适应新增信号(低波标的): {adaptive['added']}条, 方向精度 {adaptive['added_acc']:.1%}")
    print(f"自适应过滤信号(高波标的): {static['removed']}条, 被过滤者原本精度 {static['removed_acc']:.1%}")
    verdict = []
    if adaptive["acc"] > static["acc"] + 0.01:
        verdict.append("✅ 自适应方向精度更高")
    if adaptive["pl_ratio"] > static["pl_ratio"]:
        verdict.append("✅ 自适应盈亏比更好")
    if adaptive["added_acc"] >= 0.5:
        verdict.append(f"✅ 新增信号质量合格({adaptive['added_acc']:.1%})")
    else:
        verdict.append(f"⚠️ 新增信号质量不足({adaptive['added_acc']:.1%})")
    if static["removed"] and static["removed_acc"] < 0.5:
        verdict.append(f"✅ 过滤信号确实低质({static['removed_acc']:.1%})")
    elif static["removed"]:
        verdict.append(f"⚠️ 过滤掉的信号其实还行({static['removed_acc']:.1%})")
    print("\n结论:")
    for v in verdict:
        print("  " + v)
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="波动率自适应阈值回测")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--min-acc", type=float, default=0.55)
    parser.add_argument("--min-ret", type=float, default=0.01)
    args = parser.parse_args()
    sys.exit(main(args.days, args.min_acc, args.min_ret))
