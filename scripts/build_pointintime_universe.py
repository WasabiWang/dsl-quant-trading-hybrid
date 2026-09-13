#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/build_pointintime_universe.py — 阶段1-B 步骤3: 规则化时点宇宙 (R1–R6)

设计依据: audits/phase1-B-规则化时点宇宙-设计.md
  —— 规则**已预注册, 锁定后不得事后调整**; 本实现只做"算子落地", 不改规则。

输入:
  data/universe_pit_membership.json   步骤1 产出 (members: code/name/start/end/src)
  data/universe_klines/*.parquet      步骤2 产出 (date/open/high/low/close/volume/amount/adjust)

输出:
  data/universe_pit.json = {schema_version, generated_at, rules, rebalance_dates:{t:[codes]}, audit:{t:{...}}, limitations:[...]}

规则 (每个再平衡日 t = 季度末; 归一化到 <= t 的最近交易日 t_norm):
  R1 上市时长  截至 t 已上市 >= 250 个交易日
  R2 流动性    过去 60 个交易日日均成交额 >= **当期可交易集合**的中位数
  R3 规模截断  按该 60 日日均成交额排序, 取前 K=300
  R4 未停牌    t_norm 日有成交记录 (volume > 0)
  R5 数据完整  截至 t 该股自身有效行情 >= 250 个交易日
  R6 排除 ST   历史 ST 状态不可得 → **未生效**, 必须在结论中标注

时点一致性: 只用 <= t 的数据; 每个 t 独立计算; 落盘后冻结不得回改。

实现说明(算子约定, 需 owner 确认; 不是规则变更):
  a) t 非交易日 → t_norm = <= t 的最近交易日
  b) R2 的"全市场中位数"取"通过 R1/R5/R4 的当期可交易集合"(否则停牌/次新会压低分母)
  c) R1 用全市场交易日历算 [上市日, t_norm] 交易日数; 上市日缺失时退化为 R5 口径
  d) 成交额口径: 不复权概念, 跨 adjust(hfq/raw_sina) 可比; 不用 close 参与规则
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

MEMBERSHIP = ROOT / "data" / "universe_pit_membership.json"
KLINES_DIR = ROOT / "data" / "universe_klines"
OUT_PATH = ROOT / "data" / "universe_pit.json"

# ── 预注册规则参数 (与设计文档一致, 不得事后调整) ───────────────────────────
RULES = {
    "R1_min_listed_trading_days": 250,
    "R2_liq_window": 60,
    "R3_top_k": 300,
    "R4_require_traded_at_t": True,
    "R5_min_own_trading_days": 250,
    "R6_exclude_st": "unavailable",   # 历史 ST 不可得 → 未生效
}
RULES_VERSION = "phase1-B-R1-R6-v1-20260912"


def load_klines(klines_dir: Path):
    """读全部 parquet → {code: {"dates":[...], "amount":[...], "volume":[...], "roll60":[...]}}"""
    data = {}
    files = sorted(klines_dir.glob("*.parquet"))
    for i, p in enumerate(files, 1):
        code = p.stem
        try:
            df = pd.read_parquet(p)
        except Exception:
            continue
        if df is None or len(df) == 0 or "date" not in df.columns:
            continue
        df = df.copy()
        df["_d"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[df["_d"].notna()].sort_values("_d")
        if df.empty:
            continue
        amount = pd.to_numeric(df.get("amount"), errors="coerce").fillna(0.0)
        volume = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0.0)
        roll60 = amount.rolling(RULES["R2_liq_window"], min_periods=RULES["R2_liq_window"]).mean()
        data[code] = {
            "dates": [d.strftime("%Y-%m-%d") for d in df["_d"]],
            "amount": amount.to_numpy(dtype=float),
            "volume": volume.to_numpy(dtype=float),
            "roll60": roll60.to_numpy(dtype=float),
        }
        if i % 1000 == 0:
            print(f"  [klines] {i}/{len(files)}", flush=True)
    return data


def market_calendar(klines: dict) -> list:
    """全市场交易日历 = 所有标的日期的并集(升序)。"""
    days = set()
    for v in klines.values():
        days.update(v["dates"])
    return sorted(days)


def _index_le(dates: list, t: str) -> int:
    """dates 中 <= t 的最后一个位置; 无则 -1。"""
    i = bisect.bisect_right(dates, t) - 1
    return i


def build(membership: dict, klines: dict, as_of: str, top_k: int, min_days: int,
          min_liq: int, quarters: int | None = None):
    cal = market_calendar(klines)
    if not cal:
        raise RuntimeError("交易日历为空 (universe_klines 无数据)")
    meta = {str(m["code"]).strip().zfill(6): m for m in membership.get("members", [])}

    series = membership.get("quarterly_tradable_series") or []
    q_ends = [q["quarter_end"] for q in series if q.get("quarter_end")]
    q_ends = [t for t in q_ends if t <= as_of]   # 未到的季度末不参与
    if quarters:
        q_ends = q_ends[-quarters:]

    rebalance, audit = {}, {}
    for t in q_ends:
        if t > as_of:
            continue
        i_cal = _index_le(cal, t)
        if i_cal < 0:
            continue
        t_norm = cal[i_cal]

        cand = []          # 通过 R1/R5/R4 的 (code, roll60)
        drop = {"R1": 0, "R4": 0, "R5": 0}
        for code, k in klines.items():
            idx = _index_le(k["dates"], t_norm)
            if idx < 0:
                drop["R5"] += 1
                continue
            own_days = idx + 1
            if own_days < min_days:                       # R5
                drop["R5"] += 1
                continue
            start = (meta.get(code) or {}).get("start")
            if start:
                # O(log): 全市场日历中 [start, t_norm] 的交易日数
                listed = i_cal - bisect.bisect_left(cal, start) + 1
                if listed < min_days:
                    drop["R1"] += 1
                    continue
            if k["dates"][idx] != t_norm or k["volume"][idx] <= 0:       # R4
                drop["R4"] += 1
                continue
            liq = k["roll60"][idx]                                       # R2 指标
            if liq != liq or liq <= 0:      # NaN
                drop["R5"] += 1
                continue
            cand.append((code, float(liq)))

        med = None
        if cand:
            med = float(pd.Series([c[1] for c in cand]).median())
        after_r2 = [(c, v) for c, v in cand if med is not None and v >= med]
        after_r3 = sorted(after_r2, key=lambda x: (-x[1], x[0]))[:top_k]
        codes = sorted(c for c, _ in after_r3)

        rebalance[t] = codes
        audit[t] = {
            "t_norm": t_norm,
            "candidates": len(cand),
            "dropped": drop,
            "after_R2_median": len(after_r2),
            "R2_median_amount": round(med, 2) if med is not None else None,
            "final": len(codes),
        }

    return {
        "schema_version": 1,
        "rules_version": RULES_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of,
        "klines_codes": len(klines),
        "membership_codes": len(meta),
        "rules": {**RULES, "R3_top_k": top_k, "R1_min_listed_trading_days": min_days,
                  "R5_min_own_trading_days": min_days, "R2_liq_window": min_liq},
        "operator_notes": [
            "t 非交易日 → 归一化到 <= t 的最近交易日 t_norm",
            "R2 中位数取『通过 R1/R5/R4 的当期可交易集合』",
            "R1 用全市场交易日历算 [上市日, t_norm]; 上市日缺失时退化为 R5 口径",
            "成交额口径为不复权概念, 跨 adjust(hfq/raw_sina) 可比; 规则不使用 close",
        ],
        "diagnostics": _diagnostics(meta, rebalance),
        "rebalance_dates": rebalance,
        "audit": audit,
        "limitations": [
            "⛔ 退市股行情缺成交额: 麦蕊(当前上市股)有 amount, 但兜底源(腾讯/新浪)未产出 —— 抽样 8/8 退市股 amount 恒 0。",
            "   因此 R2/R3(按成交额排序)会**结构性排除退市股** ⇒ **幸存者偏差尚未消除**, 不得声称已消除。详见 audits/phase1-B-步骤3-结果.md",
            "R6 未生效: 历史 ST 状态不可得(未排除 ST)",
            "新浪兜底源仅 ≈1023 根(近约4年), 长历史退市股的滚动窗口可能因此不足",
            "K=300 是设计选择, 会引入规模效应, 需随结论一并披露",
            "规则化宇宙是可复现的近似, 不等于当时的全部可投资机会",
        ],
    }


def _diagnostics(meta: dict, rebalance: dict) -> dict:
    """可追溯诊断: 退市股是否真能进入宇宙 (实测: 因 amount 缺失而几乎进不去)。"""
    delisted = {c for c, m in meta.items() if str(m.get("src", "")).startswith("delisted")}
    ever = set()
    for codes in rebalance.values():
        ever.update(codes)
    return {
        "delisted_members": len(delisted),
        "delisted_ever_in_universe": len(delisted & ever),
        "delisted_never_in_universe": len(delisted - ever),
        "distinct_codes_ever_in_universe": len(ever),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--membership", default=str(MEMBERSHIP))
    ap.add_argument("--klines-dir", default=str(KLINES_DIR))
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--as-of", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--top-k", type=int, default=RULES["R3_top_k"])
    ap.add_argument("--min-days", type=int, default=RULES["R1_min_listed_trading_days"])
    ap.add_argument("--min-liq-window", type=int, default=RULES["R2_liq_window"])
    ap.add_argument("--quarters", type=int, default=None, help="只算最近 N 个季度末(冒烟用)")
    args = ap.parse_args()

    with open(args.membership, encoding="utf-8") as f:
        membership = json.load(f)
    print(f"[membership] {len(membership.get('members', []))} 只 | 季度末 {len(membership.get('quarterly_tradable_series') or [])} 个")
    print("[klines] 读取 parquet ...", flush=True)
    klines = load_klines(Path(args.klines_dir))
    print(f"[klines] {len(klines)} 只就绪", flush=True)

    out = build(membership, klines, args.as_of, args.top_k, args.min_days,
                args.min_liq_window, args.quarters)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    n = len(out["rebalance_dates"])
    print(f"[out] {args.out} | 再平衡日 {n} 个")
    for t in list(out["rebalance_dates"])[-3:]:
        a = out["audit"][t]
        print(f"  {t} (t_norm={a['t_norm']}) 候选={a['candidates']} 过R2={a['after_R2_median']} 最终={a['final']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
