#!/usr/bin/env python3
"""v4.7.4: 兑现精度显著性检验 (每日15:47 cron)

对每只标的的非hold兑现精度做 Wilson 95%置信区间检验, 判定是否显著优于/劣于50%:
  above_50: Wilson下限 > 0.5 且 n >= 10  → 显著优于抛硬币(真实正边沿)
  below_50: Wilson上限 < 0.5 且 n >= 10  → 显著劣于抛硬币(信号负边沿)
  noise:    置信区间跨过50%               → 统计上与抛硬币无法区分
  low_n:    n < 10                        → 样本不足不判定
  no_data:  无兑现记录

输出: confidence_data/realized_significance.json
消费: Dashboard模型页(兑现精度列 ↑/↓ 标注) + 全局判定(model-freshness)

纯本地计算, 无API调用, 幂等。
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

CALIB_PATH = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
OUT_PATH = os.path.join(PROJECT_ROOT, "confidence_data", "realized_significance.json")
MIN_N = 10  # 最小样本门槛: n<10 不判定
Z = 1.959964  # 95% 双尾


def wilson_ci(k: int, n: int):
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + Z * Z / n
    center = (p + Z * Z / (2 * n)) / denom
    half = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def classify(k: int, n: int) -> str:
    if n == 0:
        return "no_data"
    if n < MIN_N:
        return "low_n"
    lo, hi = wilson_ci(k, n)
    if lo > 0.5:
        return "above_50"
    if hi < 0.5:
        return "below_50"
    return "noise"


def z_vs_50(k: int, n: int):
    """H0: p=0.5 下的 z 统计量"""
    if n == 0:
        return 0.0
    return (k / n - 0.5) / math.sqrt(0.25 / n)


def _pool_syms():
    import yaml
    master, obs = set(), set()
    try:
        with open(os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml"), "r", encoding="utf-8") as f:
            pool = yaml.safe_load(f) or {}
        for s in pool.get("master_pool", []) or []:
            sym = str(s.get("symbol", s.get("code", ""))).zfill(6)
            if sym:
                master.add(sym)
    except Exception:
        pass
    try:
        with open(os.path.join(PROJECT_ROOT, "config", "observation_pool.yaml"), "r", encoding="utf-8") as f:
            obs_cfg = yaml.safe_load(f) or {}
        for s in obs_cfg.get("observation_pool", []) or []:
            sym = str(s.get("symbol", s.get("code", ""))).zfill(6)
            if sym:
                obs.add(sym)
    except Exception:
        pass
    return master, obs


def main():
    with open(CALIB_PATH, "r", encoding="utf-8") as f:
        cal = json.load(f)
    daily_records = cal.get("daily_records", [])
    today = datetime.now().date()
    cutoff30 = (today - timedelta(days=30)).strftime("%Y-%m-%d")

    # ── 逐票统计(非hold) ──
    agg = {}          # sym -> [correct, total]
    agg30 = {}        # 近30天
    for dr in daily_records:
        date = dr.get("date", "")
        for s in dr.get("stocks", []):
            if s.get("signal", "hold") == "hold" or not s.get("realized_checked", False):
                continue
            sym = str(s.get("symbol", "")).zfill(6)
            if not sym:
                continue
            a = agg.setdefault(sym, [0, 0])
            a[1] += 1
            if s.get("realized_correct") is True or s.get("realized_correct") == 1:
                a[0] += 1
            if date >= cutoff30:
                b = agg30.setdefault(sym, [0, 0])
                b[1] += 1
                if s.get("realized_correct") is True or s.get("realized_correct") == 1:
                    b[0] += 1

    master, obs = _pool_syms()

    stocks = {}
    for sym, (c, t) in sorted(agg.items()):
        lo, hi = wilson_ci(c, t)
        pool = "master" if sym in master else ("observation" if sym in obs else "historical")
        stocks[sym] = {
            "n": t,
            "correct": c,
            "accuracy": round(c / t, 4),
            "wilson_lo": round(lo, 4),
            "wilson_hi": round(hi, 4),
            "verdict": classify(c, t),
            "z": round(z_vs_50(c, t), 2),
            "pool": pool,
        }

    def _block(scope: str, a: dict):
        c = sum(v[0] for v in a.values())
        t = sum(v[1] for v in a.values())
        if t == 0:
            return {"n": 0, "correct": 0, "accuracy": None, "verdict": "no_data", "z": None}
        lo, hi = wilson_ci(c, t)
        return {
            "n": t, "correct": c,
            "accuracy": round(c / t, 4),
            "wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4),
            "verdict": classify(c, t) if t >= MIN_N else "low_n",
            "z": round(z_vs_50(c, t), 2),
        }

    overall = {
        "master": _block("master", {s: agg[s] for s in agg if s in master}),
        "all": _block("all", agg),
        "recent_30d": _block("recent_30d", agg30),
    }

    # ── 与上次运行对比: 新进入below_50的标的 ──
    prev_below = set()
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, "r", encoding="utf-8") as f:
                prev = json.load(f)
            prev_below = {s for s, v in prev.get("stocks", {}).items() if v.get("verdict") == "below_50"}
        except Exception:
            pass
    new_below = sorted(s for s, v in stocks.items() if v["verdict"] == "below_50" and s not in prev_below)

    out = {
        "updated_at": datetime.now().isoformat(),
        "method": "wilson_95",
        "min_n": MIN_N,
        "overall": overall,
        "new_below": new_below,
        "stocks": stocks,
    }
    from common.file_lock import locked_json_write
    locked_json_write(OUT_PATH, out)

    # ── 摘要输出 ──
    n_above = sum(1 for v in stocks.values() if v["verdict"] == "above_50")
    n_below = sum(1 for v in stocks.values() if v["verdict"] == "below_50")
    n_noise = sum(1 for v in stocks.values() if v["verdict"] == "noise")
    n_low = sum(1 for v in stocks.values() if v["verdict"] == "low_n")
    ov = overall["master"]
    print(f"✅ 兑现精度显著性检验完成 ({len(stocks)}只标的)")
    print(f"   显著>50%: {n_above} | 显著<50%: {n_below} | 噪声: {n_noise} | 低样本: {n_low}")
    print(f"   主池全局: {ov['correct']}/{ov['n']} = {(ov['accuracy'] or 0)*100:.1f}% "
          f"(z={ov['z']}, {ov['verdict']})")
    if new_below:
        print(f"   🔔 新进入显著劣于50%: {', '.join(new_below)}")
    else:
        print(f"   无新增显著劣于50%标的")


if __name__ == "__main__":
    main()
