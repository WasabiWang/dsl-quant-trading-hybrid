#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/pit_consistency_audit.py — 阶段1-B 步骤4 验收：时点一致性审计

设计文档 (audits/phase1-B-规则化时点宇宙-设计.md §四·验收) 的硬性要求：
  「对若干 t，用『完整数据』与『截断至 t 的数据』分别算宇宙，**断言 t 日成分完全一致**」

本脚本做三件事：
  A. 时点一致性：对每个采样 t，用截断到 t 的 klines 重算宇宙，
     断言 **所有 <= t 的期** 成分与「完整数据」跑出的结果**逐期完全一致**（比只查 t 更强）。
  B. 换手率：逐期成分换手（相对上一期），报告 max/median。
  C. 与人工池对照：把最后一期成分与当前 master_stock_pool.yaml 做交集/差集对照。

用法:
  python3 scripts/pit_consistency_audit.py                    # 默认采样 8 个 t
  python3 scripts/pit_consistency_audit.py --samples 1998-06-30 2005-06-30 2012-06-30
  python3 scripts/pit_consistency_audit.py --json-out reports/pit_consistency.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

import scripts.build_pointintime_universe as bp  # noqa: E402

DEFAULT_SAMPLES = ["1995-06-30", "1998-06-30", "2003-06-30", "2008-06-30",
                   "2012-06-30", "2016-06-30", "2020-06-30", "2025-06-30"]


def truncate_klines(klines: dict, cutoff: str) -> dict:
    """只保留 date <= cutoff 的行; roll60 为后向窗口, 截断不该改变 <= cutoff 的值。"""
    out = {}
    for code, k in klines.items():
        end = bp._index_le(k["dates"], cutoff)
        if end < 0:
            continue
        out[code] = {
            "dates": k["dates"][:end + 1],
            "amount": k["amount"][:end + 1],
            "volume": k["volume"][:end + 1],
            "roll60": k["roll60"][:end + 1],
        }
    return out


def turnover_series(rebalance: dict) -> list:
    ts = sorted(rebalance)
    out = []
    for a, b in zip(ts, ts[1:]):
        prev, cur = set(rebalance[a]), set(rebalance[b])
        if not prev:
            continue
        out.append({"from": a, "to": b,
                    "added": len(cur - prev), "removed": len(prev - cur),
                    "turnover": round(len(cur ^ prev) / max(len(prev), 1), 4)})
    return out


def compare_with_manual_pool(last_codes: list) -> dict:
    p = ROOT / "config" / "master_stock_pool.yaml"
    if not p.exists():
        return {"available": False}
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    pool = {str(s["symbol"]).zfill(6) for s in (data.get("master_pool") or [])}
    cur = set(last_codes)
    return {
        "available": True,
        "pool_size": len(pool),
        "overlap": len(cur & pool),
        "only_in_pit": sorted(cur - pool),
        "only_in_pool": sorted(pool - cur),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", nargs="*", default=DEFAULT_SAMPLES,
                    help="采样的截断点 t (须为季度末)")
    ap.add_argument("--json-out", default=str(ROOT / "reports" / "pit_consistency.json"))
    args = ap.parse_args()

    with open(bp.MEMBERSHIP, encoding="utf-8") as f:
        membership = json.load(f)
    print("[klines] 读取 parquet ...", flush=True)
    klines = bp.load_klines(bp.KLINES_DIR)
    print(f"[klines] {len(klines)} 只", flush=True)

    # 基准: 完整数据 (as_of=今天: 与 build_pointintime_universe.py 产物口径一致, 不含未来季度)
    today = datetime.now().strftime("%Y-%m-%d")
    base = bp.build(membership, klines, as_of=today, top_k=bp.RULES["R3_top_k"],
                    min_days=bp.RULES["R1_min_listed_trading_days"],
                    min_liq=bp.RULES["R2_liq_window"])
    base_rd = base["rebalance_dates"]
    print(f"[base] 完整数据(as_of={today}) → {len(base_rd)} 期", flush=True)

    # 可复现性: 重算结果 vs 磁盘产物 data/universe_pit.json
    repro = {"checked": False}
    if bp.OUT_PATH.exists():
        on_disk = json.load(open(bp.OUT_PATH, encoding="utf-8"))["rebalance_dates"]
        common = sorted(set(on_disk) & set(base_rd))
        diff = [p for p in common if set(on_disk[p]) != set(base_rd[p])]
        repro = {"checked": True, "periods_compared": len(common),
                 "mismatches": diff, "identical": not diff,
                 "artifact_generated_at": json.load(open(bp.OUT_PATH, encoding="utf-8"))["generated_at"]}
        print(f"[repro] 重算 vs 磁盘产物: {len(common)} 期, 不一致 {len(diff)} 期", flush=True)

    checks = []
    for t in args.samples:
        trunc = truncate_klines(klines, t)
        got = bp.build(membership, trunc, as_of=t, top_k=bp.RULES["R3_top_k"],
                       min_days=bp.RULES["R1_min_listed_trading_days"],
                       min_liq=bp.RULES["R2_liq_window"])
        got_rd = got["rebalance_dates"]
        periods = [p for p in base_rd if p <= t]
        diffs = [p for p in periods
                 if set(base_rd.get(p, [])) != set(got_rd.get(p, []))]
        checks.append({
            "cutoff": t,
            "periods_compared": len(periods),
            "periods": periods,
            "mismatches": diffs,
            "consistent": not diffs,
            "klines_codes_truncated": len(trunc),
        })
        flag = "✅" if not diffs else "❌"
        print(f"  {flag} 截断到 {t}: 比对 {len(periods)} 期, 不一致 {len(diffs)} 期 {diffs[:5]}", flush=True)

    tv = turnover_series(base_rd)
    turns = [x["turnover"] for x in tv]
    last_codes = base_rd[max(base_rd)]

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "universe_file": str(bp.OUT_PATH),
        "rules_version": base["rules_version"],
        "periods": len(base_rd),
        "pit_consistency": {
            "all_consistent": all(c["consistent"] for c in checks),
            "checks": checks,
        },
        "turnover": {
            "pairs": len(tv),
            "max": round(max(turns), 4) if turns else None,
            "median": round(float(pd.Series(turns).median()), 4) if turns else None,
            "first5": tv[:5],
            "last5": tv[-5:],
        },
        "reproducibility_vs_artifact": repro,
        "manual_pool_compare_last_period": {**compare_with_manual_pool(last_codes),
                                            "period": max(base_rd),
                                            "pit_size": len(last_codes)},
    }
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print()
    print(f"=== 验收结论 ===")
    print(f"  时点一致性: {'✅ 全部一致' if summary['pit_consistency']['all_consistent'] else '❌ 存在不一致'}")
    if repro.get("checked"):
        print(f"  可复现性(重算 vs 磁盘产物): {'✅ 一致' if repro['identical'] else '❌ 不一致'}")
    print(f"  换手率: max={summary['turnover']['max']} median={summary['turnover']['median']}")
    mp = summary["manual_pool_compare_last_period"]
    if mp.get("available"):
        print(f"  与人工池对照({mp['period']}): PIT {mp['pit_size']} 只 vs 池 {mp['pool_size']} 只, 交集 {mp['overlap']}")
    print(f"  → {args.json_out}")
    return 0 if summary["pit_consistency"]["all_consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
