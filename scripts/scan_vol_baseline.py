#!/usr/bin/env python3
"""
P4-C2 波动信号 vs 历史波动率基线：模型的波动信号是否显著打败简单历史波动率基线。

预注册判据（锁定，见 audits/NEXT-edge验证-P4C2预注册.md）：
  基线（严格点内时点，只用 t 及以前数据）：
    hist_vol_20(t) = 截至 t 的过去 20 个交易日日收益率标准差（close-to-close pct_change）
    hist_vol_60(t) = 截至 t 的过去 60 个交易日日收益率标准差
    主基线取 hist_vol_20；hist_vol_60 作稳健性对照。
  主检验（配对差值，按日期配对）：
    IC_model(t) = Spearman(|predicted_return|, |actual_return|)
    IC_base(t)  = Spearman(hist_vol_20, |actual_return|)
    ΔIC(t) = IC_model(t) − IC_base(t)
  对 ΔIC 序列做 20 交易日 circular block bootstrap（B>=2000），得 95%CI 与单侧 p（H1: ΔIC>0）。
  判定（主池 24 只 与 新宇宙 80 只）：
    ✅ 模型有增量 = 两宇宙 ΔIC 的 CI 下限均 >0 且 mean ΔIC >= 0.02
    ❌ 无增量 = 两宇宙 mean ΔIC < 0.02（含负值）
    ⚪ 无结论 = 其他
  次要（报告，不用于判定）：IC_base 自身强度；corr(|pred|, hist_vol_20) 截面均值；
    用 hist_vol_60 重做同一组 ΔIC。

数据来源：
  逐笔记录直接读取 P4-B 的 canonical 输出 reports/h20d_evaluation/scan_ic_20260912_145957.json
  的 per_trade_records.{master_pool_24,new_universe}（{date, code, predicted_return, actual_return}），
  该记录由同一 walk-forward 管线（HORIZON=20/BACKTEST_DAYS=730/WINDOW_DAYS=90/
  MIN_TRAIN_SAMPLES=200/MIN_OOS_PREDICTIONS=50）与相同抽样（沪深300成分、排除主池、
  seed=42、80只）生成。hist_vol 字段由每只股票的 K 线历史（只用 t 及以前数据）点内计算，
  不重新训练模型、不修改任何现有文件。

只读生产数据；唯一新建输出 reports/h20d_evaluation/scan_vol_baseline_*.json。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scipy.stats import spearmanr, pearsonr  # noqa: E402
from dsl_data_sdk_original import get_kline, normalize_symbol  # noqa: E402
import dsl_data_sdk_original as _data_sdk  # noqa: E402

# 生产数据只读：SDK 默认把读取结果写回 cache；本任务显式关闭该副作用。
_data_sdk._write_cache = lambda key, data: None

HORIZON = 20
BLOCK_LENGTH = 20
DEFAULT_BOOTSTRAP_REPS = 4000      # >= 2000（与 P4-B/P4-C 一致）
MIN_CROSS_SECTIONAL = 5            # 当日横截面样本数 <5 的日期剔除
DELTA_EFFECT_SIZE = 0.02           # mean ΔIC 阈值
HIST_VOL_WINDOWS = (20, 60)
KLINE_START = "2022-01-01"         # 覆盖全部记录日期前至少 60 个交易日

CANONICAL_RECORDS = "reports/h20d_evaluation/scan_ic_20260912_145957.json"


# --------------------------------------------------------------------------- #
# 数据读取 / 历史波动率点内计算
# --------------------------------------------------------------------------- #
def load_records(path: str) -> dict[str, list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pr = data.get("per_trade_records")
    if not pr:
        raise ValueError(f"{path} 缺少 per_trade_records")
    out: dict[str, list[dict[str, Any]]] = {}
    for key in ("master_pool_24", "new_universe"):
        out[key] = [dict(r) for r in pr.get(key, [])]
    return out


def _fetch_hist_vol(code: str) -> dict[str, Any]:
    """取单只股票 K 线，返回 date -> (hist_vol_20, hist_vol_60) 点内映射。"""
    end_date = datetime.now().strftime("%Y-%m-%d")
    try:
        kline = get_kline(normalize_symbol(code), KLINE_START, end_date)
    except Exception as exc:
        return {"code": code, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}
    if not kline:
        return {"code": code, "status": "empty"}

    df = pd.DataFrame(kline)
    if "date" not in df.columns or "close" not in df.columns:
        return {"code": code, "status": "missing_columns"}
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"].notna()].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    df["close"] = df["close"].astype(float)

    # 日收益率：close-to-close，与模型 build_features 的 pct_change 口径一致
    ret = df["close"].pct_change()
    # 截至 t 的过去 N 个交易日日收益率标准差（含 t 当日收益，只用 t 及以前数据，无未来）
    hv20 = ret.rolling(HIST_VOL_WINDOWS[0], min_periods=HIST_VOL_WINDOWS[0]).std()
    hv60 = ret.rolling(HIST_VOL_WINDOWS[1], min_periods=HIST_VOL_WINDOWS[1]).std()

    dates = df["date"].dt.strftime("%Y-%m-%d").tolist()
    mapping = {
        dates[i]: (float(hv20.iloc[i]) if pd.notna(hv20.iloc[i]) else None,
                   float(hv60.iloc[i]) if pd.notna(hv60.iloc[i]) else None)
        for i in range(len(dates))
    }
    return {"code": code, "status": "ok", "mapping": mapping, "n_rows": int(len(df))}


def augment_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """给逐笔记录附加 hist_vol_20 / hist_vol_60 点内字段。"""
    codes = sorted({r["code"] for r in records})
    hist_by_code: dict[str, dict[str, tuple]] = {}
    failures: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch_hist_vol, c): c for c in codes}
        for fut in as_completed(futures):
            res = fut.result()
            if res["status"] == "ok":
                hist_by_code[res["code"]] = res["mapping"]
            else:
                failures.append(res)

    n_missing = 0
    augmented: list[dict[str, Any]] = []
    for r in records:
        code = r["code"]
        d = r["date"]
        m = hist_by_code.get(code, {})
        hv = m.get(d)
        hv20 = hv[0] if hv is not None else None
        hv60 = hv[1] if hv is not None else None
        if hv20 is None or hv60 is None:
            n_missing += 1
        new = dict(r)
        new["hist_vol_20"] = round(hv20, 8) if hv20 is not None else None
        new["hist_vol_60"] = round(hv60, 8) if hv60 is not None else None
        augmented.append(new)

    meta = {
        "n_codes": len(codes),
        "n_codes_hist_ok": len(hist_by_code),
        "n_codes_hist_failed": len(failures),
        "failures": failures,
        "n_records_missing_hist_vol": n_missing,
    }
    return augmented, meta


# --------------------------------------------------------------------------- #
# 逐日配对 IC / ΔIC
# --------------------------------------------------------------------------- #
def daily_paired_ic(records: list[dict[str, Any]]) -> dict[str, Any]:
    """逐日计算 IC_model、IC_base20、IC_base60、ΔIC20、ΔIC60 及次要 corr。"""
    df = pd.DataFrame(records)
    out: dict[str, Any] = {
        "dates": [], "ic_model": [], "ic_base20": [], "ic_base60": [],
        "delta_ic20": [], "delta_ic60": [],
        "corr_pred_vs_hv20_spearman": [], "corr_pred_vs_hv20_pearson": [],
        "corr_pred_vs_hv60_spearman": [], "corr_pred_vs_hv60_pearson": [],
        "sizes": [],
    }
    drop = {"n_dates_total": 0, "n_dates_lt5": 0, "n_dates_zero_var_model": 0,
            "n_dates_zero_var_base20": 0, "n_dates_zero_var_base60": 0,
            "n_dates_base20_nan": 0, "n_dates_base60_nan": 0, "n_dates_kept": 0}

    for date, grp in df.groupby("date", sort=True):
        drop["n_dates_total"] += 1
        if len(grp) < MIN_CROSS_SECTIONAL:
            drop["n_dates_lt5"] += 1
            continue

        pred_abs = np.abs(grp["predicted_return"].values.astype(float))
        actual_abs = np.abs(grp["actual_return"].values.astype(float))
        hv20 = grp["hist_vol_20"].values.astype(float)
        hv60 = grp["hist_vol_60"].values.astype(float)

        # 剔除 hist_vol 缺失行
        valid = np.isfinite(hv20) & np.isfinite(hv60)
        if int(valid.sum()) < MIN_CROSS_SECTIONAL:
            drop["n_dates_lt5"] += 1
            continue
        pred_abs = pred_abs[valid]
        actual_abs = actual_abs[valid]
        hv20 = hv20[valid]
        hv60 = hv60[valid]

        if np.std(pred_abs) == 0 or np.std(actual_abs) == 0:
            drop["n_dates_zero_var_model"] += 1
            continue

        ic_model = float(spearmanr(pred_abs, actual_abs)[0])
        if not np.isfinite(ic_model):
            drop["n_dates_zero_var_model"] += 1
            continue

        ic_base20 = None
        ic_base60 = None
        if np.std(hv20) > 0:
            ic_base20 = float(spearmanr(hv20, actual_abs)[0])
        else:
            drop["n_dates_zero_var_base20"] += 1
        if np.std(hv60) > 0:
            ic_base60 = float(spearmanr(hv60, actual_abs)[0])
        else:
            drop["n_dates_zero_var_base60"] += 1

        out["dates"].append(date)
        out["ic_model"].append(ic_model)
        out["ic_base20"].append(ic_base20)
        out["ic_base60"].append(ic_base60)
        out["delta_ic20"].append(ic_model - ic_base20 if ic_base20 is not None else None)
        out["delta_ic60"].append(ic_model - ic_base60 if ic_base60 is not None else None)
        out["sizes"].append(int(valid.sum()))

        # 次要 corr(|pred|, hist_vol)
        if np.std(hv20) > 0:
            out["corr_pred_vs_hv20_spearman"].append(float(spearmanr(pred_abs, hv20)[0]))
            out["corr_pred_vs_hv20_pearson"].append(float(pearsonr(pred_abs, hv20)[0]))
        if np.std(hv60) > 0:
            out["corr_pred_vs_hv60_spearman"].append(float(spearmanr(pred_abs, hv60)[0]))
            out["corr_pred_vs_hv60_pearson"].append(float(pearsonr(pred_abs, hv60)[0]))

    drop["n_dates_kept"] = len(out["dates"])
    out["drop_stats"] = drop
    return out


def _clean_series(series: list[float | None]) -> list[float]:
    return [float(x) for x in series if x is not None and np.isfinite(x)]


# --------------------------------------------------------------------------- #
# 20 交易日 circular block bootstrap（mean 的 95%CI 与单侧 p，H1: mean>0）
# --------------------------------------------------------------------------- #
def block_bootstrap_mean(series: list[float], reps: int, seed: int) -> dict[str, Any]:
    n = len(series)
    base: dict[str, Any] = {"bootstrap_reps": reps, "block_length_trading_days": BLOCK_LENGTH,
                            "bootstrap_seed": seed, "n_obs": n}
    arr = np.asarray(series, dtype=float)
    observed_mean = float(arr.mean())
    base["observed_mean"] = observed_mean
    base["abs_observed_mean"] = abs(observed_mean)
    base["std"] = float(arr.std(ddof=1)) if n > 1 else None
    if n < 2:
        base.update({"status": "insufficient", "ci95": None, "p_one_sided": None})
        return base
    if n < BLOCK_LENGTH:
        base.update({"status": "insufficient_for_block", "ci95": None, "p_one_sided": None})
        return base

    n_blocks = math.ceil(n / BLOCK_LENGTH)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(reps, n_blocks), endpoint=False)
    offsets = np.arange(BLOCK_LENGTH)
    pos = (starts[:, :, None] + offsets[None, None, :]) % n
    pos = pos.reshape(reps, -1)[:, :n]
    boot_means = arr[pos].mean(axis=1)
    ci_low = float(np.quantile(boot_means, 0.025))
    ci_high = float(np.quantile(boot_means, 0.975))
    p_one_sided = (1.0 + float(np.sum(boot_means <= 0.0))) / (reps + 1.0)
    base.update({
        "status": "ok",
        "ci95": [ci_low, ci_high],
        "p_one_sided": p_one_sided,
        "bootstrap_mean_of_mean": float(np.mean(boot_means)),
        "bootstrap_std_of_mean": float(np.std(boot_means)),
    })
    return base


# --------------------------------------------------------------------------- #
# 单宇宙汇总
# --------------------------------------------------------------------------- #
def summarize_universe(daily: dict[str, Any], reps: int, seed: int) -> dict[str, Any]:
    def mean_or_none(series):
        s = _clean_series(series)
        return float(np.mean(s)) if s else None

    def corr_summary(spearman_series, pearson_series):
        sp = _clean_series(spearman_series)
        pr = _clean_series(pearson_series)
        out = {
            "mean_spearman": float(np.mean(sp)) if sp else None,
            "mean_pearson": float(np.mean(pr)) if pr else None,
            "n_days": len(sp),
        }
        if len(sp) >= 2:
            out["spearman_bootstrap"] = block_bootstrap_mean(sp, reps, seed)
        return out

    ic_model_series = _clean_series(daily["ic_model"])
    ic_base20_series = _clean_series(daily["ic_base20"])
    ic_base60_series = _clean_series(daily["ic_base60"])
    delta20_series = _clean_series(daily["delta_ic20"])
    delta60_series = _clean_series(daily["delta_ic60"])

    return {
        "n_days_kept": daily["drop_stats"]["n_dates_kept"],
        "drop_stats": daily["drop_stats"],
        "cross_sectional_sizes": daily["sizes"],
        "ic_model": {
            "mean": mean_or_none(ic_model_series),
            "bootstrap": block_bootstrap_mean(ic_model_series, reps, seed),
        },
        "ic_base20": {
            "mean": mean_or_none(ic_base20_series),
            "bootstrap": block_bootstrap_mean(ic_base20_series, reps, seed),
        },
        "ic_base60": {
            "mean": mean_or_none(ic_base60_series),
            "bootstrap": block_bootstrap_mean(ic_base60_series, reps, seed),
        },
        "delta_ic20": {
            "mean": mean_or_none(delta20_series),
            "bootstrap": block_bootstrap_mean(delta20_series, reps, seed),
        },
        "delta_ic60": {
            "mean": mean_or_none(delta60_series),
            "bootstrap": block_bootstrap_mean(delta60_series, reps, seed),
        },
        "corr_pred_abs_vs_hv20": corr_summary(daily["corr_pred_vs_hv20_spearman"],
                                               daily["corr_pred_vs_hv20_pearson"]),
        "corr_pred_abs_vs_hv60": corr_summary(daily["corr_pred_vs_hv60_spearman"],
                                               daily["corr_pred_vs_hv60_pearson"]),
    }


# --------------------------------------------------------------------------- #
# 判定（严格按预注册判据，基于 hist_vol_20 的 ΔIC）
# --------------------------------------------------------------------------- #
def _decide(master: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    univs = {"master_pool_24": master, "new_universe": new}
    cond: dict[str, Any] = {}
    for u, s in univs.items():
        b = s["delta_ic20"]["bootstrap"]
        ci = b.get("ci95")
        mean = s["delta_ic20"]["mean"]
        cond[u] = {
            "mean_delta_ic": mean,
            "ci95": ci,
            "ci_low_gt0": ci is not None and ci[0] > 0,
            "mean_ge_002": mean is not None and mean >= DELTA_EFFECT_SIZE,
        }

    has_increment = all(
        cond[u]["ci_low_gt0"] and cond[u]["mean_ge_002"] for u in univs
    )
    no_increment = all(
        cond[u]["mean_delta_ic"] is not None and cond[u]["mean_delta_ic"] < DELTA_EFFECT_SIZE
        for u in univs
    )

    if has_increment:
        verdict = "模型有增量"
    elif no_increment:
        verdict = "无增量（基线已解释）"
    else:
        verdict = "无结论"

    return {
        "verdict": verdict,
        "per_universe": cond,
        "effect_size_threshold": DELTA_EFFECT_SIZE,
        "note": "基于主基线 hist_vol_20 的 ΔIC；hist_vol_60 仅作稳健性对照不用于判定。",
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-json", default=CANONICAL_RECORDS,
                        help="P4-B 逐笔记录 JSON（默认读取 scan_ic_20260912_145957.json）")
    parser.add_argument("--bootstrap-reps", type=int, default=DEFAULT_BOOTSTRAP_REPS)
    parser.add_argument("--bootstrap-seed", type=int, default=42)
    args = parser.parse_args()

    t0 = time.time()
    print(f"📂 读取逐笔记录: {args.records_json}")
    records = load_records(args.records_json)

    with open(args.records_json, encoding="utf-8") as f:
        src = json.load(f)
    sampling = src.get("sampling", {})
    master_pool_codes = src.get("master_pool_codes", [])

    universe_summary: dict[str, Any] = {}
    augmented_all: dict[str, list[dict[str, Any]]] = {}
    for u in ("master_pool_24", "new_universe"):
        recs = records.get(u, [])
        print(f"   {u}: 逐笔记录 {len(recs):,} 条 → 附加 hist_vol 点内字段 …")
        aug, meta = augment_records(recs)
        augmented_all[u] = aug
        daily = daily_paired_ic(aug)
        summary = summarize_universe(daily, args.bootstrap_reps, args.bootstrap_seed)
        summary["hist_vol_meta"] = meta
        summary["n_records"] = len(recs)
        universe_summary[u] = summary
        print(f"      截面日保留 {daily['drop_stats']['n_dates_kept']} / "
              f"总 {daily['drop_stats']['n_dates_total']}；hist_vol 缺失记录 {meta['n_records_missing_hist_vol']}")

    decision = _decide(universe_summary["master_pool_24"], universe_summary["new_universe"])

    output = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "walk_forward_oos records (P4-B canonical) + point-in-time hist_vol_20/60 "
                  "(close-to-close pct_change trailing std) + daily paired Spearman IC + "
                  "20d circular block bootstrap on ΔIC",
        "question": "模型的波动信号是否显著打败简单历史波动率基线（P4-C2）",
        "preregistration": "audits/NEXT-edge验证-P4C2预注册.md",
        "records_source": args.records_json,
        "params": {
            "horizon": HORIZON,
            "block_length_trading_days": BLOCK_LENGTH,
            "bootstrap_reps": args.bootstrap_reps,
            "min_cross_sectional": MIN_CROSS_SECTIONAL,
            "delta_effect_size": DELTA_EFFECT_SIZE,
            "hist_vol_windows": list(HIST_VOL_WINDOWS),
            "kline_start": KLINE_START,
            "return_definition": "close-to-close pct_change, trailing std ddof=1, includes day t",
        },
        "sampling": sampling,
        "master_pool_codes": master_pool_codes,
        "per_universe": universe_summary,
        "decision": decision,
        "per_trade_records": augmented_all,
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    os.makedirs("reports/h20d_evaluation", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/h20d_evaluation/scan_vol_baseline_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'='*70}")
    print(f"📊 P4-C2 波动基线对照完成 ({output['elapsed_seconds']:.0f}s)")
    print(f"{'='*70}")
    for u in ("master_pool_24", "new_universe"):
        s = universe_summary[u]
        print(f"\n【{u}】 逐笔 {s['n_records']:,}  截面日保留 {s['n_days_kept']}")
        for key, label in (("ic_model", "IC_model(|pred|,|act|)"),
                           ("ic_base20", "IC_base20(hist_vol_20,|act|)"),
                           ("ic_base60", "IC_base60(hist_vol_60,|act|)")):
            b = s[key]["bootstrap"]
            ci = b.get("ci95")
            ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "N/A"
            print(f"   {label:28s} mean = {b.get('observed_mean', float('nan')):+.4f}  "
                  f"95%CI = {ci_s}  单侧p = {b.get('p_one_sided', float('nan')):.4f}")
        for key, label in (("delta_ic20", "ΔIC20 = model − base20"),
                           ("delta_ic60", "ΔIC60 = model − base60")):
            b = s[key]["bootstrap"]
            ci = b.get("ci95")
            ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "N/A"
            print(f"   {label:28s} mean = {b.get('observed_mean', float('nan')):+.4f}  "
                  f"95%CI = {ci_s}  单侧p = {b.get('p_one_sided', float('nan')):.4f}")
        c20 = s["corr_pred_abs_vs_hv20"]
        c60 = s["corr_pred_abs_vs_hv60"]
        print(f"   corr(|pred|, hist_vol_20) 截面均值: Spearman={c20.get('mean_spearman')}  "
              f"Pearson={c20.get('mean_pearson')}  n_days={c20.get('n_days')}")
        print(f"   corr(|pred|, hist_vol_60) 截面均值: Spearman={c60.get('mean_spearman')}  "
              f"Pearson={c60.get('mean_pearson')}  n_days={c60.get('n_days')}")
    print(f"\n🎯 裁决: {decision['verdict']}")
    print(f"   逐宇宙条件: {json.dumps(decision['per_universe'], ensure_ascii=False)}")
    print(f"\n  📁 {out_path}")


if __name__ == "__main__":
    main()
