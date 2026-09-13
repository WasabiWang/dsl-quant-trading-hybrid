#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/repair_kline_amount.py — 修复退市股行情缺失的「成交额」

背景 (阶段1-B 步骤3 暴露):
  麦蕊(当前上市股)返回真实 amount; 但兜底源未产出成交额 → 退市股 amount 恒 0
  → R2/R3 按成交额排序会结构性排除退市股 → 幸存者偏差消不掉。

修复口径 (已用当前上市股对麦蕊真值交叉验证):
  amount = 不复权收盘价 × 成交量 × unit, 其中
    · 腾讯 fqkline(raw, 不复权): volume 单位=手(100股) → unit=100   (验证: 600519 比值 0.997~1.004)
    · 新浪 getKLineData(raw)   : volume 单位=股       → unit=1     (验证: 600519 raw_close×vol=2.295e9 vs 麦蕊 2.3028e9)
  两个源的 close 均为不复权价 → 推导出的成交额是**真实元**量级, 跨标的可比。

策略:
  · 本地已是不复权(raw_sina)的文件 → 直接用现有 close×volume 推导(零网络)
  · hfq 文件 → 拉一次腾讯**不复权**全历史, 按日期对齐后推导 amount

只填 amount==0 的行, 不改动价格列; 逐码写 provenance 到 _amount_repair.jsonl。

用法:
  python3 scripts/repair_kline_amount.py --only-source raw_sina      # 先做零网络的 71 只
  python3 scripts/repair_kline_amount.py                            # 全量(含 296 只需要联网)
  python3 scripts/repair_kline_amount.py --limit 5 --dry-run        # 冒烟
"""
from __future__ import annotations

import argparse
import json
import os
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
PROV = KLINES_DIR / "_amount_repair.jsonl"

# volume 单位换算 (见模块 docstring 的交叉验证)
UNIT_TENCENT = 100       # 手 → 股
UNIT_SINA = 1            # 已是股


def _derive(close: pd.Series, volume: pd.Series, unit: int) -> pd.Series:
    c = pd.to_numeric(close, errors="coerce").fillna(0.0)
    v = pd.to_numeric(volume, errors="coerce").fillna(0.0)
    out = c * v * unit
    out[(c <= 0) | (v <= 0)] = 0.0
    return out


def _repair_local(df: pd.DataFrame) -> pd.Series:
    """raw_sina: close 与 volume 同为不复权口径 → 直接推导。"""
    return _derive(df["close"], df["volume"], UNIT_SINA)


def _repair_from_tencent(code: str, df: pd.DataFrame):
    """hfq 文件: 拉腾讯不复权全历史, 按日期对齐推导 amount。"""
    import scripts.fetch_all_klines as fk

    start = str(df["date"].min())[:10]
    end = str(df["date"].max())[:10]
    raw = fk._fetch_tencent(code, start, end, adjust="raw")
    if raw is None or len(raw) == 0:
        return None
    raw = raw[["date", "close", "volume"]].copy()
    raw["date"] = raw["date"].astype(str).str[:10]
    raw["amt"] = _derive(raw["close"], raw["volume"], UNIT_TENCENT)
    m = dict(zip(raw["date"], raw["amt"]))
    return df["date"].astype(str).str[:10].map(m).fillna(0.0)


def repair_one(code: str, dry_run: bool) -> dict:
    p = KLINES_DIR / f"{code}.parquet"
    if not p.exists():
        return {"code": code, "status": "no_file"}
    df = pd.read_parquet(p)
    if "amount" not in df.columns:
        return {"code": code, "status": "no_amount_col"}
    amt = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    need = amt <= 0
    if not need.any():
        return {"code": code, "status": "already_ok"}
    src = str(df["adjust"].iloc[0]) if "adjust" in df.columns else "unknown"

    if src.startswith("raw"):
        new = _repair_local(df)
        method = "local_raw_close_x_volume"
    else:
        new = _repair_from_tencent(code, df)
        if new is None:
            return {"code": code, "status": "fetch_failed", "source": src}
        method = "tencent_raw_close_x_volume_x100"

    filled = int(((new > 0) & need).sum())
    if filled == 0:
        return {"code": code, "status": "derived_zero", "source": src, "method": method}

    if not dry_run:
        df["amount"] = amt.where(~need, new)
        df.to_parquet(p, index=False)
    return {"code": code, "status": "repaired", "source": src, "method": method,
            "filled": filled, "rows": int(len(df))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file", default=None, help="JSON {codes:[...]}; 默认取 membership 的退市股")
    ap.add_argument("--only-source", choices=["raw_sina", "hfq", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.codes_file:
        codes = json.load(open(args.codes_file, encoding="utf-8"))["codes"]
    else:
        mem = json.load(open(MEMBERSHIP, encoding="utf-8"))
        codes = sorted(str(m["code"]).zfill(6) for m in mem["members"]
                       if str(m["src"]).startswith("delisted"))
    if args.limit:
        codes = codes[:args.limit]

    # 先按源分流 (raw_sina 零网络, 先做)
    todo = []
    for c in codes:
        p = KLINES_DIR / f"{c}.parquet"
        if not p.exists():
            continue
        try:
            src = str(pd.read_parquet(p, columns=["adjust"])["adjust"].iloc[0])
        except Exception:
            continue
        if args.only_source == "raw_sina" and not src.startswith("raw"):
            continue
        if args.only_source == "hfq" and src.startswith("raw"):
            continue
        todo.append(c)

    print(f"[repair] 目标 {len(todo)} 只 (only_source={args.only_source}, dry_run={args.dry_run})", flush=True)
    t0 = time.time()
    stats = {}
    for i, c in enumerate(todo, 1):
        try:
            r = repair_one(c, args.dry_run)
        except Exception as e:  # noqa: BLE001
            r = {"code": c, "status": "error", "error": f"{type(e).__name__}: {e}"[:200]}
        stats[r["status"]] = stats.get(r["status"], 0) + 1
        if not args.dry_run:
            with PROV.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({**r, "ts": datetime.now().isoformat(timespec="seconds")},
                                    ensure_ascii=False) + "\n")
        if i % 20 == 0 or i == len(todo):
            el = time.time() - t0
            print(f"[{i}/{len(todo)}] {stats} elapsed={el:.0f}s avg={el/i:.1f}s/只", flush=True)

    print(f"[done] {stats}  用时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
