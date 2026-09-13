#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/pit_calibration_test.py — 选择性交易的前置检验：模型数值输出的校准与信息量

为什么需要：若要"只做高精度信号"，必须先有一个**在决策时刻可得、且样本外单调**的排序分数。
本脚本用逐笔 dump（[date, correct, pred, actual]）回答三个问题：

  A. 校准（单调性）：按预测值分档，实际命中率是否逐档单调上升？
  B. 信息量（IC）：横截面上 pred vs actual 的 rank-IC 是否显著 > 0？（按日聚合 + 时间块 bootstrap）
  C. 样本外稳定性：分档边界在时间段 A 上标定，套到时间段 B 上是否仍然单调/有效？

用法:
  python3 scripts/pit_calibration_test.py --dump reports/pit_predictions_val.json
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BUCKETS = 10
N_BOOT = 2000
SEED = 42
BLOCK = 20


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def _spearman(x: list, y: list) -> float:
    if len(x) < 3:
        return float("nan")
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for t in range(i, j + 1):
                r[order[t]] = avg
            i = j + 1
        return r
    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


def bucket_table(recs: list, edges: list = None) -> tuple:
    """按 pred 分档 → 每档 n / 命中率 / Wilson CI / 平均 actual。返回 (表, 分位边界)"""
    recs = [r for r in recs if r[2] == r[2]]
    vals = sorted(r[2] for r in recs)
    if not vals:
        return [], []
    if edges is None:
        edges = [vals[min(len(vals) - 1, int(len(vals) * i / BUCKETS))] for i in range(1, BUCKETS)]
    table = []
    for b in range(BUCKETS):
        lo = edges[b - 1] if b > 0 else float("-inf")
        hi = edges[b] if b < len(edges) else float("inf")
        grp = [r for r in recs if lo <= r[2] < hi]
        if not grp:
            table.append({"bucket": b + 1, "n": 0, "hit": None, "ci": None, "mean_actual": None})
            continue
        k = sum(r[1] for r in grp)
        n = len(grp)
        table.append({"bucket": b + 1, "n": n, "hit": k / n, "ci": wilson(k, n),
                      "mean_actual": st.mean(r[3] for r in grp)})
    return table, edges


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", default=None)
    ap.add_argument("--json-out", default=str(ROOT / "reports" / "pit_calibration.json"))
    args = ap.parse_args()

    p = args.dump or sorted(glob.glob(str(ROOT / "reports" / "pit_predictions_val.json")))
    if isinstance(p, list):
        p = p[-1] if p else None
    if not p or not Path(p).exists():
        print(f"❌ 找不到数值版 dump（需先跑 --dump-predictions 且等待完成）: {p}")
        return 1
    dd = json.load(open(p, encoding="utf-8"))
    recs = [tuple(x[:4]) for v in dd["predictions"].values() for x in v if len(x) >= 4 and x[2] is not None]
    print(f"[dump] {p} | {len(recs)} 条 (含数值) | 标的 {len(dd['predictions'])}")

    # A. 全样本校准
    table, edges = bucket_table(recs)
    hits = [(t["bucket"], t["hit"]) for t in table if t["hit"] is not None]
    rho = _spearman([b for b, _ in hits], [h for _, h in hits])
    print("\n=== A. 全样本校准（按预测值十分位）===")
    print(f"{'档':>3} {'n':>7} {'命中率':>8}  {'Wilson95':>20} {'平均实现收益':>10}")
    for t in table:
        ci = f"[{t['ci'][0]:.4f},{t['ci'][1]:.4f}]" if t["ci"] else "-"
        ha = f"{t['hit']:.4f}" if t["hit"] is not None else "-"
        ma = f"{t['mean_actual']*100:+.3f}%" if t["mean_actual"] is not None else "-"
        print(f"{t['bucket']:>3} {t['n']:>7} {ha:>8}  {ci:>20} {ma:>10}")
    print(f"  单调性 Spearman(档位, 命中率) = {rho:.3f}")

    # B. 逐日 rank-IC + 时间块 bootstrap
    by_date = defaultdict(list)
    for d, ok, pred, actual in recs:
        by_date[d].append((pred, actual))
    ics = [ic for ic in (_spearman([x[0] for x in v], [x[1] for x in v])
                         for v in by_date.values() if len(v) >= 10) if ic == ic]
    if ics:
        mean_ic = sum(ics) / len(ics)
        dates = sorted(by_date)
        rng = random.Random(SEED)
        B = max(1, math.ceil(len(ics) / BLOCK))
        boot = []
        for _ in range(N_BOOT):
            s = 0
            for _b in range(B):
                i = rng.randrange(len(ics))
                s += ics[i]
            boot.append(s / B)
        boot.sort()
        lo, hi = boot[int(0.025 * len(boot))], boot[int(0.975 * len(boot))]
        print(f"\n=== B. 逐日 rank-IC ===\n  日数={len(ics)} mean_IC={mean_ic:.4f} 时间块CI=[{lo:.4f},{hi:.4f}] 含0? {lo<=0<=hi}")
    else:
        mean_ic = lo = hi = None
        print("\n=== B. 逐日 rank-IC === 样本不足")

    # C. 样本外（A 段标定边界 → B 段评估）
    ds = sorted(by_date)
    cut = ds[len(ds) // 2]
    A = [r for r in recs if r[0] < cut]
    Bx = [r for r in recs if r[0] >= cut]
    _, edgesA = bucket_table(A)
    tableB, _ = bucket_table(Bx, edgesA)
    hitsB = [(t["bucket"], t["hit"]) for t in tableB if t["hit"] is not None]
    rhoB = _spearman([b for b, _ in hitsB], [h for _, h in hitsB]) if hitsB else float("nan")
    print(f"\n=== C. 样本外（切分点 {cut}）===")
    print(f"{'档':>3} {'n':>7} {'命中率':>8}  {'Wilson95':>20}")
    for t in tableB:
        ci = f"[{t['ci'][0]:.4f},{t['ci'][1]:.4f}]" if t["ci"] else "-"
        ha = f"{t['hit']:.4f}" if t["hit"] is not None else "-"
        print(f"{t['bucket']:>3} {t['n']:>7} {ha:>8}  {ci:>20}")
    print(f"  OOS 单调性 = {rhoB:.3f}")

    # 做多侧：pred>0 的最高档
    pos = [r for r in Bx if r[2] > 0]
    if pos:
        pos.sort(key=lambda r: -r[2])
        top = pos[:max(1, len(pos) // 10)]
        k = sum(r[1] for r in top)
        lo_t, hi_t = wilson(k, len(top))
        print(f"\n  样本外·做多侧最高档(|pred| 前10%): n={len(top)} 命中率={k/len(top):.4f} Wilson=[{lo_t:.4f},{hi_t:.4f}]")
        print(f"    下界 > 50%? {lo_t > 0.5}")

    out = {"dump": p, "n": len(recs),
           "calibration_all": table, "monotonicity_all": rho,
           "daily_ic": {"days": len(ics), "mean": mean_ic, "ci95": (lo, hi)} if ics else None,
           "calibration_oos": tableB, "monotonicity_oos": rhoB}
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n→ {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
