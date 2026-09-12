#!/usr/bin/env python3
"""
P4-B 截面 IC 验证：模型预测收益率是否含截面排序信息（即使二元方向命中率≈50%）。

方法（预注册判据锁定，见 audits/NEXT-edge验证-P4B预注册.md）：
  1. 统一 walk-forward：同一份代码跑主池 24 只 与 新宇宙（沪深300 抽样，排除主池），
     参数与 backtest_walkforward_h20d_accuracy.py 完全一致：
       HORIZON=20 / BACKTEST_DAYS=730 / WINDOW_DAYS=90 /
       MIN_TRAIN_SAMPLES=200 / MIN_OOS_PREDICTIONS=50；
  2. 保留逐笔记录 {date, code, predicted_return, actual_return}；
  3. 逐日计算截面 Spearman rank IC；当日横截面样本数 <5 的日期剔除并报告剔除数；
  4. 20 交易日 circular block bootstrap（B=4000）给出 mean IC 的 95%CI 与单侧 p；
     并给出 ICIR = mean IC / std IC 的块重采样 95%CI（判据第 4 条：不与 0 重合）；
  5. 次要指标（报告不用于主判定）：top − bottom 三分位实际收益价差；
  6. 主池与新宇宙并列对照。

只读生产数据；唯一新建输出 reports/h20d_evaluation/scan_ic_*.json。
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from train_predictor_enhanced import build_features, fetch_fundamentals  # noqa: E402
from dsl_data_sdk_original import get_kline, normalize_symbol  # noqa: E402
from sklearn.feature_selection import SelectFromModel  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from scipy.stats import spearmanr  # noqa: E402
import lightgbm as lgb  # noqa: E402

# 生产数据只读：SDK 默认把读取结果写回 cache；本任务显式关闭该副作用。
import dsl_data_sdk_original as _data_sdk  # noqa: E402
import scripts.fundamentals_loader as _fundamentals_loader  # noqa: E402

_data_sdk._write_cache = lambda key, data: None
_fundamentals_loader._save_file_cache = lambda: None

HORIZON = 20
BACKTEST_DAYS = 730
WINDOW_DAYS = 90
MIN_TRAIN_SAMPLES = 200
MIN_OOS_PREDICTIONS = 50
TIMEOUT_GLOBAL = 2400
MAX_WORKERS = 3
BLOCK_LENGTH = 20
DEFAULT_BOOTSTRAP_REPS = 4000
MIN_CROSS_SECTIONAL = 5          # 当日横截面样本数 <5 的日期剔除
EFFECT_SIZE_THRESHOLD = 0.01     # |mean IC| 效应量阈值


def _load_master_pool() -> tuple[list[str], dict[str, str]]:
    with open(PROJECT_ROOT / "config" / "master_stock_pool.yaml", encoding="utf-8") as f:
        raw_pool = yaml.safe_load(f)["master_pool"]
    pool = [s for s in raw_pool if "." not in str(s["symbol"])]
    codes = [str(s["symbol"]).zfill(6) for s in pool]
    names = {str(s["symbol"]).zfill(6): s.get("name", s["symbol"]) for s in pool}
    return codes, names


def _fetch_index_codes(symbol: str) -> tuple[list[str], str]:
    """用 akshare 取宽基指数成分股代码（中证指数官方口径）。"""
    import akshare as ak
    df = ak.index_stock_cons_csindex(symbol=symbol)
    codes = [str(c).zfill(6) for c in df["成分券代码"]]
    as_of = str(df["日期"].iloc[0])
    return codes, as_of


def _has_enough_kline(code: str, min_rows: int = 350) -> bool:
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=int(BACKTEST_DAYS * 2.2))).strftime("%Y-%m-%d")
    try:
        kline = get_kline(normalize_symbol(code), start_date, end_date)
    except Exception:
        return False
    return bool(kline) and len(kline) >= min_rows


def build_universe(
    index_symbols: list[str],
    exclude: set[str],
    sample_size: int,
    seed: int,
) -> tuple[list[str], dict[str, Any]]:
    """构建新宇宙：取指数成分 → 排除主池 → 过滤历史不足 → 固定种子抽样。"""
    meta: dict[str, Any] = {
        "index_symbols": index_symbols,
        "sample_size": sample_size,
        "seed": seed,
        "excluded_master_pool": sorted(exclude),
    }
    candidates: dict[str, str] = {}
    index_meta: dict[str, Any] = {}
    for sym in index_symbols:
        codes, as_of = _fetch_index_codes(sym)
        index_meta[sym] = {"n_constituents": len(codes), "as_of": as_of}
        for c in codes:
            if c not in exclude and c not in candidates:
                candidates[c] = sym
    meta["index_meta"] = index_meta
    meta["n_candidates_before_filter"] = len(candidates)

    insufficient: list[str] = []
    kept: list[str] = []
    for c in sorted(candidates):
        if _has_enough_kline(c):
            kept.append(c)
        else:
            insufficient.append(c)
    meta["n_insufficient_history"] = len(insufficient)
    meta["insufficient_history_codes"] = insufficient
    meta["n_candidates_after_filter"] = len(kept)

    rng = np.random.default_rng(seed)
    if sample_size > len(kept):
        sample_size = len(kept)
    sampled = list(rng.choice(sorted(kept), size=sample_size, replace=False))
    meta["sampled_codes"] = sampled
    return sampled, meta


def evaluate_stock(code: str, name: str) -> dict[str, Any]:
    """复刻基准脚本滚动训练/预测口径，保留逐笔 {date, code, predicted_return, actual_return}。"""
    window_errors: list[str] = []
    try:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=int(BACKTEST_DAYS * 2.2))).strftime("%Y-%m-%d")
        kline = get_kline(normalize_symbol(code), start_date, end_date)
        if not kline or len(kline) < 350:
            return {"code": code, "name": name, "status": "insufficient_kline",
                    "reason": f"kline行数={len(kline or [])}<350"}

        df = pd.DataFrame(kline)
        if "date" not in df.columns:
            return {"code": code, "name": name, "status": "missing_date_column"}
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[df["date"].notna()].sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
        for column in ["close", "volume", "high", "low", "open"]:
            df[column] = df[column].astype(float)

        fundamentals = fetch_fundamentals(code)
        feats = build_features(df, fundamentals).replace([np.inf, -np.inf], np.nan)
        target_col = f"target_{HORIZON}d"
        feature_cols = [c for c in feats.columns if not c.startswith("target_")]
        if target_col not in feats.columns or feats[target_col].notna().sum() < MIN_TRAIN_SAMPLES:
            return {"code": code, "name": name, "status": "insufficient_target",
                    "reason": "target_20d 有效样本 < MIN_TRAIN_SAMPLES"}

        feats[feature_cols] = feats[feature_cols].fillna(0)
        all_idx = feats.index.tolist()
        if len(all_idx) < MIN_TRAIN_SAMPLES + WINDOW_DAYS:
            return {"code": code, "name": name, "status": "insufficient_total_rows",
                    "reason": f"总行数={len(all_idx)}"}

        bt_start = len(all_idx) - BACKTEST_DAYS
        bt_indices = all_idx[bt_start:]
        records: list[dict[str, Any]] = []

        for window_start in range(0, len(bt_indices), WINDOW_DAYS):
            window_end = min(window_start + WINDOW_DAYS, len(bt_indices))
            test_dates = bt_indices[window_start:window_end]
            if len(test_dates) < 5:
                continue

            train_end_pos = all_idx.index(test_dates[0])
            train_idx = all_idx[:train_end_pos]
            if len(train_idx) < MIN_TRAIN_SAMPLES:
                continue

            train_data = feats.loc[train_idx]
            target_valid = train_data[target_col].notna()
            if int(target_valid.sum()) < MIN_TRAIN_SAMPLES:
                continue

            x_train = train_data.loc[target_valid, feature_cols].values
            y_train = train_data.loc[target_valid, target_col].values
            split = int(len(x_train) * 0.8)
            x_train_80, y_train_80 = x_train[:split], y_train[:split]

            try:
                scaler = StandardScaler()
                x_scaled = scaler.fit_transform(x_train_80)
                selector = SelectFromModel(
                    lgb.LGBMRegressor(n_estimators=50, random_state=42, verbose=-1),
                    threshold="median",
                    max_features=40,
                )
                x_selected = selector.fit_transform(x_scaled, y_train_80)
                model = lgb.LGBMRegressor(
                    n_estimators=200,
                    max_depth=6,
                    learning_rate=0.03,
                    random_state=42,
                    verbose=-1,
                    n_jobs=1,
                )
                model.fit(x_selected, y_train_80)

                for test_idx in test_dates:
                    row = feats.loc[test_idx]
                    if pd.isna(row[target_col]):
                        continue
                    x_test = scaler.transform([row[feature_cols].values])
                    predicted_return = float(model.predict(selector.transform(x_test))[0])
                    actual_return = float(row[target_col])
                    records.append(
                        {
                            "date": df.loc[test_idx, "date"].strftime("%Y-%m-%d"),
                            "code": code,
                            "predicted_return": round(predicted_return, 6),
                            "actual_return": round(actual_return, 6),
                        }
                    )
            except Exception as exc:
                window_errors.append(f"window={window_start}: {type(exc).__name__}: {exc}")
                continue

        if len(records) < MIN_OOS_PREDICTIONS:
            return {"code": code, "name": name, "status": "insufficient_oos_predictions",
                    "reason": f"OOS预测数={len(records)}<{MIN_OOS_PREDICTIONS}",
                    "n_predictions": len(records), "window_errors": window_errors[:10]}

        return {
            "code": code,
            "name": name,
            "status": "ok",
            "n_predictions": len(records),
            "records": records,
            "window_errors": window_errors[:10],
        }
    except Exception as exc:
        return {"code": code, "name": name, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}


def compute_daily_ic(records: list[dict[str, Any]]) -> dict[str, Any]:
    """逐日计算截面 Spearman rank IC。返回 {dates, ic_values, drop_stats}。"""
    df = pd.DataFrame(records)
    if df.empty:
        return {"dates": [], "ic_values": [], "drop_stats": {
            "n_dates_total": 0, "n_dates_lt5": 0, "n_dates_zero_variance": 0,
            "n_dates_nan_ic": 0, "n_dates_kept": 0, "cross_sectional_sizes": []}}

    dates: list[str] = []
    ic_values: list[float] = []
    sizes: list[int] = []
    n_lt5 = 0
    n_zero_var = 0
    n_nan = 0
    n_total = 0

    for date, grp in df.groupby("date", sort=True):
        n_total += 1
        n = len(grp)
        if n < MIN_CROSS_SECTIONAL:
            n_lt5 += 1
            continue
        pred = grp["predicted_return"].values.astype(float)
        actual = grp["actual_return"].values.astype(float)
        if np.std(pred) == 0 or np.std(actual) == 0:
            n_zero_var += 1
            continue
        ic = spearmanr(pred, actual)[0]
        if not np.isfinite(ic):
            n_nan += 1
            continue
        dates.append(date)
        ic_values.append(float(ic))
        sizes.append(n)

    return {
        "dates": dates,
        "ic_values": ic_values,
        "drop_stats": {
            "n_dates_total": n_total,
            "n_dates_lt5": n_lt5,
            "n_dates_zero_variance": n_zero_var,
            "n_dates_nan_ic": n_nan,
            "n_dates_kept": len(dates),
            "cross_sectional_sizes": sizes,
        },
    }


def block_bootstrap_mean_ic(ic_values: list[float], reps: int, seed: int) -> dict[str, Any]:
    """20 交易日 circular block bootstrap：mean IC 的 95%CI、单侧 p、ICIR 的 95%CI。"""
    n = len(ic_values)
    base: dict[str, Any] = {
        "bootstrap_reps": reps,
        "block_length_trading_days": BLOCK_LENGTH,
        "bootstrap_seed": seed,
        "n_daily_ic": n,
    }
    if n < 2:
        base.update({"status": "insufficient_dates", "mean_ic": None, "mean_ic_95ci": None,
                     "icir": None, "icir_95ci": None})
        return base

    ic_arr = np.asarray(ic_values, dtype=float)
    observed_mean = float(ic_arr.mean())
    observed_std = float(ic_arr.std(ddof=1))
    observed_icir = observed_mean / observed_std if observed_std > 1e-12 else float("nan")
    base["mean_ic"] = observed_mean
    base["std_ic"] = observed_std
    base["icir"] = observed_icir
    base["abs_mean_ic"] = abs(observed_mean)

    if n < BLOCK_LENGTH:
        base.update({"status": "insufficient_dates_for_block", "mean_ic_95ci": None,
                     "mean_ic_p_one_sided": None, "icir_95ci": None})
        return base

    n_blocks = math.ceil(n / BLOCK_LENGTH)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(reps, n_blocks), endpoint=False)
    offsets = np.arange(BLOCK_LENGTH)
    sampled_positions = (starts[:, :, None] + offsets[None, None, :]) % n
    sampled_positions = sampled_positions.reshape(reps, -1)[:, :n]

    boot_samples = ic_arr[sampled_positions]          # (reps, n)
    boot_means = boot_samples.mean(axis=1)
    boot_stds = boot_samples.std(axis=1, ddof=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        boot_icirs = np.where(boot_stds > 1e-12, boot_means / boot_stds, np.nan)

    valid_icir = boot_icirs[np.isfinite(boot_icirs)]
    if len(valid_icir) < max(100, int(reps * 0.9)):
        icir_ci = None
    else:
        icir_ci = [float(np.quantile(valid_icir, 0.025)), float(np.quantile(valid_icir, 0.975))]

    mean_ci_low = float(np.quantile(boot_means, 0.025))
    mean_ci_high = float(np.quantile(boot_means, 0.975))
    # 单侧 p（H1: mean IC > 0）
    p_one_sided = (1.0 + float(np.sum(boot_means <= 0.0))) / (reps + 1.0)

    base.update({
        "status": "ok",
        "mean_ic_95ci": [mean_ci_low, mean_ci_high],
        "mean_ic_p_one_sided": p_one_sided,
        "bootstrap_mean_mean_ic": float(np.mean(boot_means)),
        "bootstrap_std_mean_ic": float(np.std(boot_means)),
        "icir_95ci": icir_ci,
        "icir_valid_draws": int(len(valid_icir)),
    })
    return base


def tertile_spread(records: list[dict[str, Any]]) -> dict[str, Any]:
    """次要指标：top − bottom 三分位实际收益价差（pooled 与 逐日均值）。"""
    df = pd.DataFrame(records)
    if df.empty or len(df) < 3:
        return {"pooled_top_mean": None, "pooled_bottom_mean": None, "pooled_spread": None,
                "daily_mean_spread": None, "n_days": 0}

    # pooled：全部记录按预测收益率排序三等分
    sorted_df = df.sort_values("predicted_return").reset_index(drop=True)
    idx_groups = np.array_split(np.arange(len(sorted_df)), 3)
    bottom_mean = float(sorted_df.iloc[idx_groups[0]]["actual_return"].mean())
    mid_mean = float(sorted_df.iloc[idx_groups[1]]["actual_return"].mean())
    top_mean = float(sorted_df.iloc[idx_groups[2]]["actual_return"].mean())
    pooled_spread = top_mean - bottom_mean

    # 逐日：每日按预测收益率三等分，top − bottom 价差，再跨日平均
    daily_spreads: list[float] = []
    n_days = 0
    for date, grp in df.groupby("date", sort=True):
        if len(grp) < 3:
            continue
        g = grp.sort_values("predicted_return").reset_index(drop=True)
        parts = np.array_split(np.arange(len(g)), 3)
        daily_spreads.append(float(g.iloc[parts[2]]["actual_return"].mean())
                             - float(g.iloc[parts[0]]["actual_return"].mean()))
        n_days += 1

    daily_mean_spread = float(np.mean(daily_spreads)) if daily_spreads else None

    return {
        "pooled_top_mean": top_mean,
        "pooled_mid_mean": mid_mean,
        "pooled_bottom_mean": bottom_mean,
        "pooled_spread": pooled_spread,
        "daily_mean_spread": daily_mean_spread,
        "n_days": n_days,
    }


def _summarize_universe(label: str, results: dict[str, dict[str, Any]],
                        bootstrap_reps: int, seed: int) -> dict[str, Any]:
    ok = {c: v for c, v in results.items() if v["status"] == "ok"}
    all_records: list[dict[str, Any]] = []
    for v in ok.values():
        all_records.extend(v["records"])

    failed: dict[str, int] = {}
    for v in results.values():
        if v["status"] != "ok":
            key = v.get("status", "unknown")
            failed[key] = failed.get(key, 0) + 1

    ic_result = compute_daily_ic(all_records)
    boot = block_bootstrap_mean_ic(ic_result["ic_values"], bootstrap_reps, seed)
    tertile = tertile_spread(all_records)

    return {
        "label": label,
        "n_stocks_attempted": len(results),
        "n_stocks_ok": len(ok),
        "n_failed_or_skipped": len(results) - len(ok),
        "failure_distribution": failed,
        "failure_details": {
            c: {"status": v["status"], "reason": v.get("reason", v.get("window_errors", ""))}
            for c, v in results.items() if v["status"] != "ok"
        },
        "total_predictions": len(all_records),
        "cross_sectional": ic_result["drop_stats"],
        "bootstrap": boot,
        "tertile_spread": tertile,
        "per_stock": {
            c: {"name": v.get("name", c), "n_predictions": v.get("n_predictions")}
            for c, v in ok.items()
        },
    }


def _decide(master: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """严格按预注册判据裁决。"""
    m = master["bootstrap"]
    n = new["bootstrap"]
    m_ci = m.get("mean_ic_95ci")
    n_ci = n.get("mean_ic_95ci")
    m_icir_ci = m.get("icir_95ci")
    n_icir_ci = n.get("icir_95ci")

    def ci_val(ci, idx):
        return ci[idx] if ci is not None else None

    # 条件 1/2：CI 下限 > 0
    c1 = m_ci is not None and m_ci[0] > 0
    c2 = n_ci is not None and n_ci[0] > 0
    # 条件 3：效应量 |mean IC| >= 0.01（两池同查）
    c3_master = m.get("abs_mean_ic") is not None and m["abs_mean_ic"] >= EFFECT_SIZE_THRESHOLD
    c3_new = n.get("abs_mean_ic") is not None and n["abs_mean_ic"] >= EFFECT_SIZE_THRESHOLD
    c3 = c3_master and c3_new
    # 条件 4：ICIR 块重采样 CI 不与 0 重合（CI 整体 > 0）
    c4_master = m_icir_ci is not None and m_icir_ci[0] > 0
    c4_new = n_icir_ci is not None and n_icir_ci[0] > 0
    c4 = c4_master and c4_new

    any_upper_lt0 = (m_ci is not None and m_ci[1] < 0) or (n_ci is not None and n_ci[1] < 0)
    generalization_failure = c1 and not c2

    if any_upper_lt0:
        verdict = "无 edge（任一 CI 上界 < 0，负向信号）"
    elif generalization_failure:
        verdict = "无 edge（条件1通过但条件2不通过，泛化不成立）"
    elif c1 and c2 and c3 and c4:
        verdict = "有 edge"
    else:
        verdict = "无结论"

    return {
        "verdict": verdict,
        "conditions": {
            "c1_master_ci_low_gt0": bool(c1),
            "c2_new_ci_low_gt0": bool(c2),
            "c3_effect_size_master": bool(c3_master),
            "c3_effect_size_new": bool(c3_new),
            "c3_effect_size_both": bool(c3),
            "c4_icir_robust_master": bool(c4_master),
            "c4_icir_robust_new": bool(c4_new),
            "c4_icir_robust_both": bool(c4),
        },
        "any_ci_upper_lt0": bool(any_upper_lt0),
        "generalization_failure": bool(generalization_failure),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", nargs="+", default=["000300"],
                        help="宽基指数代码（akshare index_stock_cons_csindex），默认沪深300")
    parser.add_argument("--sample-size", type=int, default=80, help="新宇宙抽样规模（>=60）")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    parser.add_argument("--bootstrap-reps", type=int, default=DEFAULT_BOOTSTRAP_REPS)
    parser.add_argument("--bootstrap-seed", type=int, default=42)
    args = parser.parse_args()

    t0 = time.time()
    master_codes, master_names = _load_master_pool()
    exclude = set(master_codes)
    print(f"🚀 P4-B 截面 IC 验证")
    print(f"   主池 {len(master_codes)} 只; 指数 {args.index}; 抽样 {args.sample_size} 只; seed={args.seed}")

    sampled_codes, universe_meta = build_universe(args.index, exclude, args.sample_size, args.seed)
    print(f"   新宇宙候选: {universe_meta['n_candidates_before_filter']} → "
          f"过滤后 {universe_meta['n_candidates_after_filter']} → 抽样 {len(sampled_codes)}")
    new_names = {c: c for c in sampled_codes}

    all_targets = [(c, master_names.get(c, c), "master") for c in master_codes] + \
                  [(c, new_names.get(c, c), "new") for c in sampled_codes]
    master_results: dict[str, dict[str, Any]] = {}
    new_results: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(evaluate_stock, c, n): (c, g) for c, n, g in all_targets}
        done = 0
        try:
            for future in as_completed(futures, timeout=TIMEOUT_GLOBAL):
                code, group = futures[future]
                try:
                    result = future.result(timeout=5)
                except Exception as exc:
                    result = {"code": code, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}
                (master_results if group == "master" else new_results)[code] = result
                done += 1
                if done % 20 == 0 or done == len(all_targets):
                    print(f"   [{done}/{len(all_targets)}] 完成")
        except TimeoutError:
            print(f"   ⏰ 全局超时；已完成 {done}/{len(all_targets)}")

    master_summary = _summarize_universe("master_pool_24", master_results,
                                         args.bootstrap_reps, args.bootstrap_seed)
    new_summary = _summarize_universe("new_universe", new_results,
                                      args.bootstrap_reps, args.bootstrap_seed)
    decision = _decide(master_summary, new_summary)

    output = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "walk_forward_oos + cross-sectional Spearman rank IC + 20d circular block bootstrap",
        "question": "模型预测收益率是否含截面排序信息（P4-B）",
        "preregistration": "audits/NEXT-edge验证-P4B预注册.md",
        "params": {
            "horizon": HORIZON,
            "window_days": WINDOW_DAYS,
            "backtest_days": BACKTEST_DAYS,
            "min_train_samples": MIN_TRAIN_SAMPLES,
            "min_oos_predictions": MIN_OOS_PREDICTIONS,
            "max_workers": MAX_WORKERS,
            "block_length_trading_days": BLOCK_LENGTH,
            "bootstrap_reps": args.bootstrap_reps,
            "min_cross_sectional": MIN_CROSS_SECTIONAL,
            "effect_size_threshold": EFFECT_SIZE_THRESHOLD,
        },
        "sampling": universe_meta,
        "master_pool_codes": sorted(master_codes),
        "comparison": {
            "master_pool_24": master_summary,
            "new_universe": new_summary,
        },
        "decision": decision,
        "per_trade_records": {
            "master_pool_24": [r for v in master_results.values() if v["status"] == "ok" for r in v["records"]],
            "new_universe": [r for v in new_results.values() if v["status"] == "ok" for r in v["records"]],
        },
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    os.makedirs("reports/h20d_evaluation", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/h20d_evaluation/scan_ic_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'='*70}")
    print(f"📊 P4-B 截面 IC 验证完成 ({output['elapsed_seconds']:.0f}s)")
    print(f"{'='*70}")
    for key in ("master_pool_24", "new_universe"):
        s = output["comparison"][key]
        b = s["bootstrap"]
        ci = b.get("mean_ic_95ci")
        ci_s = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]" if ci else "N/A"
        icir_ci = b.get("icir_95ci")
        icir_ci_s = f"[{icir_ci[0]:+.4f}, {icir_ci[1]:+.4f}]" if icir_ci else "N/A"
        print(f"\n【{s['label']}】 股票ok={s['n_stocks_ok']}/{s['n_stocks_attempted']} "
              f"预测笔数={s['total_predictions']:,}")
        print(f"   mean IC = {b.get('mean_ic', float('nan')):+.4f}  std IC = {b.get('std_ic', float('nan')):.4f}  "
              f"ICIR = {b.get('icir', float('nan')):+.4f}")
        print(f"   mean IC 20日块bootstrap 95%CI = {ci_s}  单侧p = {b.get('mean_ic_p_one_sided', float('nan')):.4f}")
        print(f"   ICIR 95%CI = {icir_ci_s}")
        print(f"   截面日期: 剔除<5={s['cross_sectional']['n_dates_lt5']} "
              f"零方差={s['cross_sectional']['n_dates_zero_variance']} "
              f"保留={s['cross_sectional']['n_dates_kept']}")
        t = s["tertile_spread"]
        print(f"   三分位 top-bottom 价差: pooled={t['pooled_spread']}  daily均值={t['daily_mean_spread']}")
    print(f"\n🎯 裁决: {decision['verdict']}")
    print(f"   条件对照: {json.dumps(decision['conditions'], ensure_ascii=False)}")
    print(f"\n  📁 {out_path}")


if __name__ == "__main__":
    main()
