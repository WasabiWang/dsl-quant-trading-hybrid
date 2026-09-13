#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/refetch_truncated_klines.py — 用腾讯全历史覆盖「被截断」的退市股行情

背景 (阶段1-B 步骤4 暴露):
  早期抓取时腾讯被 WAF 限流 → 部分退市股落到新浪兜底, 而新浪只给 ≈1023 根(近约4年)
  → 这些标的在更早的季度末没有数据 → R5/有效行情不足 → 从未进入时点宇宙。
  实测: 腾讯不复权可取**全历史**(600898: 现有1023行 → 腾讯 5729 行, 1996-04-18 起)。

本脚本: 对「现有行数 < 阈值」的标的, 重新拉腾讯**不复权**全历史并**整文件覆盖**;
  成交额按已验证口径推导: amount = 不复权收盘 × 成交量(手) × 100。
  原文件先备份到 data/universe_klines/_backup_truncated/ (可回退)。

用法:
  python3 scripts/refetch_truncated_klines.py --only-source raw_sina        # 先做零冲突的一批
  python3 scripts/refetch_truncated_klines.py                              # 全部被截断的
  python3 scripts/refetch_truncated_klines.py --limit 3 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

KLINES_DIR = ROOT / "data" / "universe_klines"
MEMBERSHIP = ROOT / "data" / "universe_pit_membership.json"
BACKUP_DIR = KLINES_DIR / "_backup_truncated"
PROV = KLINES_DIR / "_refetch_truncated.jsonl"
UNIT_TENCENT = 100   # 手 → 股 (已对麦蕊真值验证: 600519 比值 0.997~1.004)
ADJUST_TAG = "raw_tencent"


def build_df(code: str, start: str, end: str):
    """拉腾讯不复权全历史 → 标准 schema 的 DataFrame。"""
    import scripts.fetch_all_klines as fk
    from common.adjust import _KLINE_FIELDS

    t = fk._fetch_tencent(code, start, end, adjust="raw")
    if t is None or len(t) == 0:
        return None
    d = t.copy()
    d["date"] = d["date"].astype(str).str[:10]
    d = d[d["date"].str.len() == 10].sort_values("date")
    d["close"] = pd.to_numeric(d["close"], errors="coerce").fillna(0.0)
    d["volume"] = pd.to_numeric(d["volume"], errors="coerce").fillna(0.0)
    d["amount"] = d["close"] * d["volume"] * UNIT_TENCENT
    d["adjust"] = ADJUST_TAG
    for c in ("open", "high", "low", "prev_close"):
        if c not in d.columns:
            d[c] = 0.0
    return d[_KLINE_FIELDS].reset_index(drop=True)


def targets(max_rows: int, only_source: str, limit: int) -> list:
    mem = json.load(open(MEMBERSHIP, encoding="utf-8"))
    ot = []
    for m in mem["members"]:
        code = str(m["code"]).zfill(6)
        if not str(m["src"]).startswith("delisted"):
            continue
        p = KLINES_DIR / f"{code}.parquet"
        if not p.exists():
            continue
        try:
            n = len(pd.read_parquet(p, columns=["date"]))
            src = str(pd.read_parquet(p, columns=["adjust"])["adjust"].iloc[0])
        except Exception:
            continue
        if n >= max_rows:
            continue
        if only_source == "raw_sina" and not src.startswith("raw"):
            continue
        if only_source == "hfq" and src.startswith("raw"):
            continue
        ot.append((code, n, src))
    ot.sort(key=lambda x: x[1])
    return ot[:limit] if limit else ot


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rows", type=int, default=1200, help="行数低于此值视为被截断")
    ap.add_argument("--only-source", choices=["raw_sina", "hfq", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    tg = targets(args.max_rows, args.only_source, args.limit)
    print(f"[refetch] 目标 {len(tg)} 只 (max_rows<{args.max_rows}, only_source={args.only_source})", flush=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    stats = {}
    for i, (code, n0, src) in enumerate(tg, 1):
        rec = {"code": code, "rows_before": n0, "source_before": src}
        try:
            df = build_df(code, "1990-01-01", datetime.now().strftime("%Y-%m-%d"))
            if df is None or len(df) == 0:
                rec["status"] = "fetch_failed"
            else:
                rec.update({"rows_after": int(len(df)), "start_after": str(df["date"].min()),
                            "end_after": str(df["date"].max()),
                            "status": "improved" if len(df) > n0 else "no_gain"})
                if not args.dry_run and len(df) > n0:
                    shutil.copy2(KLINES_DIR / f"{code}.parquet", BACKUP_DIR / f"{code}.parquet")
                    df.to_parquet(KLINES_DIR / f"{code}.parquet", index=False)
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["error"] = f"{type(e).__name__}: {e}"[:200]
        stats[rec["status"]] = stats.get(rec["status"], 0) + 1
        if not args.dry_run:
            with PROV.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({**rec, "ts": datetime.now().isoformat(timespec="seconds")},
                                    ensure_ascii=False) + "\n")
        if i % 10 == 0 or i == len(tg):
            el = time.time() - t0
            print(f"[{i}/{len(tg)}] {stats} elapsed={el:.0f}s avg={el/i:.1f}s/只", flush=True)

    print(f"[done] {stats}  用时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
