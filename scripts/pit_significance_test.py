#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/pit_significance_test.py — PIT 回测精度的显著性检验

为什么需要：152,477 次预测看着样本极大，但
  · 20 日 horizon + 每日预测 ⇒ 同一标的相邻预测**高度重叠**
  · 同一交易日的横截面**高度相关**（共同市场因子）
⇒ 名义 n 严重高估有效样本，直接算二项检验会给出虚假显著。

两种模式：
  A. --from-backtest <eval_fix1_*.json>（现有产物，无需重跑）
     横截面聚类 bootstrap：以**标的为簇**重采样 → 均值/池化精度 的 95%CI。
     能处理"同标的内相关"，但**不处理同日横截面相关** → CI 偏窄，需在报告中标注。
  B. --from-dump <predictions_*.json>（回测以 --dump-predictions 产出）
     ① 时间块 bootstrap：按交易日分块(block=20日)重采样 → 95%CI（处理时间重叠+同日相关）
     ② 非重叠样本：每标的按 20 日间隔抽样 → 近似独立样本 → 二项检验
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

N_BOOT = 2000
SEED = 42
HORIZON = 20          # 非重叠抽样的最小间隔(交易日)
BLOCK = 20            # 时间块 bootstrap 的块长(交易日)


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """Wilson 95%CI（单样本比例）"""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def cluster_bootstrap_stocks(stocks: list, n_boot: int = N_BOOT) -> dict:
    """以标的为簇重采样 → (预测数加权)池化精度 与 每股精度均值 的分布。"""
    rng = random.Random(SEED)
    m = len(stocks)
    pooled, means = [], []
    for _ in range(n_boot):
        idx = [rng.randrange(m) for _ in range(m)]
        c = sum(stocks[i]["correct"] for i in idx)
        n = sum(stocks[i]["total_predictions"] for i in idx)
        accs = [stocks[i]["direction_accuracy"] for i in idx]
        if n:
            pooled.append(c / n)
        means.append(sum(accs) / len(accs))
    def ci(x):
        x = sorted(x)
        return (x[int(0.025 * len(x))], x[int(0.975 * len(x))])
    return {"pooled_mean": st.mean(pooled), "pooled_ci95": ci(pooled),
            "per_stock_mean": st.mean(means), "per_stock_ci95": ci(means)}


def block_bootstrap_by_date(recs: list, n_boot: int = N_BOOT, block: int = BLOCK) -> dict:
    """recs: [(date, correct)] → 按交易日分块重采样。"""
    by_date = defaultdict(list)
    for d, c in recs:
        by_date[d].append(c)
    dates = sorted(by_date)
    if not dates:
        return {}
    rng = random.Random(SEED)
    n_blocks = max(1, math.ceil(len(dates) / block))
    out = []
    for _ in range(n_boot):
        k = 0
        n = 0
        for _ in range(n_blocks):
            start = rng.randrange(len(dates))
            seg = dates[start:start + block]
            for d in seg:
                cs = by_date[d]
                k += sum(cs)
                n += len(cs)
        if n:
            out.append(k / n)
    out.sort()
    return {"acc": sum(sum(v) for v in by_date.values()) / max(1, sum(len(v) for v in by_date.values())),
            "blocks": n_blocks, "block_len": block,
            "ci95": (out[int(0.025 * len(out))], out[int(0.975 * len(out))])}


def _acc_from_counts(idx_counts: list) -> float:
    k = sum(idx_counts[i][0] for i in range(len(idx_counts)))
    n = sum(idx_counts[i][1] for i in range(len(idx_counts)))
    return k / n if n else float("nan")


def block_bootstrap_multi(cnts: list, L: int, n_boot: int = N_BOOT,
                          method: str = "randstart", seed: int = SEED) -> tuple:
    """三种块抽样下的池化精度 95%CI。

    cnts: [(k_t, n_t)] 按交易日排序
    · randstart: 原实现（B=ceil(T/L) 个随机起点, 块尾部截短, 块可重叠/重复）
    · circular : 标准循环块 bootstrap（起点随机, 每块固定取 L 日, 末尾回绕）
    · moving   : 标准移动块 bootstrap（起点仅取能容纳整块的位置）
    """
    T = len(cnts)
    if T == 0:
        return (float("nan"), float("nan"), 0)
    rng = random.Random(seed)
    B = max(1, math.ceil(T / L))
    out = []
    for _ in range(n_boot):
        k = n = 0
        for _b in range(B):
            if method == "randstart":
                s = rng.randrange(T)
                seg = range(s, min(s + L, T))
            elif method == "circular":
                s = rng.randrange(T)
                seg = [(s + j) % T for j in range(L)]
            else:  # moving
                s = rng.randrange(T - L + 1) if T >= L else 0
                seg = range(s, min(s + L, T))
            for i in seg:
                k += cnts[i][0]
                n += cnts[i][1]
        if n:
            out.append(k / n)
    out.sort()
    return (out[int(0.025 * len(out))], out[int(0.975 * len(out))], B)


def sensitivity_table(dump: dict, blocks: list, n_boot: int, min_preds: int) -> list:
    """对两个样本口径 × 多块长 × 三种块法, 产出全表。"""
    out = []
    for label, keep in (("all", None), (f">={min_preds}", min_preds)):
        codes = [c for c, v in dump.items() if keep is None or len(v) >= keep]
        by_date = defaultdict(lambda: [0, 0])
        for c in codes:
            for d, ok in dump[c]:
                by_date[d][0] += int(ok)
                by_date[d][1] += 1
        cnts = [tuple(by_date[d]) for d in sorted(by_date)]
        tot_k = sum(x[0] for x in cnts)
        tot_n = sum(x[1] for x in cnts)
        for L in blocks:
            row = {"sample": label, "stocks": len(codes), "days": len(cnts),
                   "block": L, "acc": tot_k / tot_n}
            for meth in ("randstart", "circular", "moving"):
                lo, hi, B = block_bootstrap_multi(cnts, L, n_boot, meth)
                row[f"{meth}_ci"] = (lo, hi)
                row[f"{meth}_contains_50"] = lo <= 0.5 <= hi
            row["blocks_per_draw"] = max(1, math.ceil(len(cnts) / L))
            out.append(row)
    return out


def non_overlapping(recs_by_stock: dict, gap: int = HORIZON) -> dict:
    """每标的按 gap 个交易日间隔抽样 → 近似独立的样本 → 二项/Wilson。"""
    k = n = 0
    per = []
    for code, recs in recs_by_stock.items():
        recs = sorted(recs)                       # 按日期
        picked = recs[::gap]
        c = sum(1 for _, ok in picked if ok)
        if picked:
            per.append(c / len(picked))
            k += c
            n += len(picked)
    if n == 0:
        return {}
    lo, hi = wilson(k, n)
    return {"n": n, "correct": k, "acc": k / n, "wilson95": (lo, hi),
            "per_stock_mean": st.mean(per) if per else None, "stocks": len(per)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-backtest", default=None, help="eval_fix1_*.json 路径(默认取最新 both)")
    ap.add_argument("--from-dump", default=None, help="predictions_*.json 路径")
    ap.add_argument("--universe", default="point_in_time")
    ap.add_argument("--min-preds", type=int, default=50)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--sensitivity", action="store_true",
                    help="块长×块法×样本口径 全表(需 --from-dump)")
    ap.add_argument("--blocks", nargs="*", type=int, default=[5, 10, 20, 40, 60])
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    args = ap.parse_args()

    res = {"generated_at": None, "universe": args.universe}

    if args.from_backtest or not args.from_dump:
        p = args.from_backtest or sorted(
            glob.glob(str(ROOT / "reports" / "h20d_evaluation" / "eval_fix1_both*.json")))[-1]
        d = json.load(open(p, encoding="utf-8"))
        ps = d["per_universe"][args.universe]["per_stock"]
        stocks = [v for v in ps.values() if v["total_predictions"] >= args.min_preds]
        tot_c = sum(v["correct"] for v in stocks)
        tot_n = sum(v["total_predictions"] for v in stocks)
        cb = cluster_bootstrap_stocks(stocks)
        lo, hi = wilson(tot_c, tot_n)
        res["modeA_from_backtest"] = {
            "source": p, "stocks": len(stocks), "predictions": tot_n,
            "pooled_acc": tot_c / tot_n,
            "naive_wilson95": (lo, hi),          # 明确标注: 因重叠而过于乐观
            "cluster_bootstrap": cb,
            "excludes_50": cb["pooled_ci95"][0] > 0.5,
        }
        print(f"[A] 来源 {Path(p).name} | {len(stocks)} 只 / {tot_n} 次预测")
        print(f"    池化精度 = {tot_c/tot_n:.4f}")
        print(f"    朴素 Wilson95 = [{lo:.4f}, {hi:.4f}]  ← 因重叠而偏窄, 仅供参考")
        print(f"    横截面聚类 bootstrap 95%CI = [{cb['pooled_ci95'][0]:.4f}, {cb['pooled_ci95'][1]:.4f}]")
        print(f"    每股精度均值 = {cb['per_stock_mean']:.4f}  CI=[{cb['per_stock_ci95'][0]:.4f}, {cb['per_stock_ci95'][1]:.4f}]")
        print(f"    CI 下界 > 50%? {'是' if cb['pooled_ci95'][0] > 0.5 else '否'}")

    if args.from_dump:
        dd = json.load(open(args.from_dump, encoding="utf-8"))
        per_stock = {c: [(r[0], bool(r[1])) for r in v] for c, v in dd["predictions"].items()}
        per_stock_raw = dd["predictions"]
        allrecs = [r for v in per_stock.values() for r in v]
        bb = block_bootstrap_by_date(allrecs)
        no = non_overlapping(per_stock)
        res["modeB_from_dump"] = {"source": args.from_dump,
                                  "stocks": len(per_stock), "predictions": len(allrecs),
                                  "block_bootstrap": bb, "non_overlapping": no}
        print()
        print(f"[B] 来源 {Path(args.from_dump).name} | {len(per_stock)} 只 / {len(allrecs)} 次预测")
        print(f"    时间块 bootstrap(block={bb.get('block_len')}, {bb.get('blocks')} 块) 95%CI = "
              f"[{bb['ci95'][0]:.4f}, {bb['ci95'][1]:.4f}]  (acc={bb['acc']:.4f})")
        print(f"    非重叠样本(gap={HORIZON}) n={no.get('n')} acc={no.get('acc'):.4f} "
              f"Wilson95=[{no['wilson95'][0]:.4f}, {no['wilson95'][1]:.4f}]")
        print(f"    非重叠下界 > 50%? {'是' if no['wilson95'][0] > 0.5 else '否'}")

        if args.sensitivity:
            rows = sensitivity_table(per_stock_raw, args.blocks, args.n_boot, args.min_preds)
            res["modeB_sensitivity"] = rows
            print()
            print("=== 块长敏感性全表 ===")
            print(f"{'样本':>5} {'块长':>4} {'抽块':>4} {'池化':>8} | {'randstart(原实现)':>23} | {'circular':>23} | {'moving':>23}")
            for r in rows:
                cells = []
                for meth in ("randstart", "circular", "moving"):
                    lo, hi = r[f"{meth}_ci"]
                    cells.append(f"[{lo:.5f},{hi:.5f}]{'*' if r[f'{meth}_contains_50'] else ' '}")
                print(f"{r['sample']:>5} {r['block']:>4} {r['blocks_per_draw']:>4} {r['acc']:>8.5f} | "
                      f"{cells[0]:>23} | {cells[1]:>23} | {cells[2]:>23}")
            print("(* = 该 CI 包含 50%)")

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
        print(f"\n→ {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
