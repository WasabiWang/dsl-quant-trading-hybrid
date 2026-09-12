#!/usr/bin/env python3
"""
P4-A 泛化验证：h20d 微弱方向信号是否存在于主池之外？

要回答的问题：
  主池 24 只 h20d 方向准确率 51.54%（sd 5.49%，20日块 bootstrap CI [48.44%, 54.52%]）。
  这个微弱信号是普适的，还是这 24 只被选出来的结果（幸存者偏差）？

方法：
  1. 用 akshare 取宽基指数（默认沪深300）成分股，排除 master_stock_pool.yaml 的 24 只，
     按固定随机种子抽样 60–100 只（每只 ≥350 交易日历史），构建"扩展宇宙"；
  2. 完整复刻 backtest_walkforward_h20d_accuracy.py 的滚动训练/预测口径
     （HORIZON=20 / BACKTEST_DAYS=730 / WINDOW_DAYS=90 / MIN_TRAIN_SAMPLES=200 /
      MIN_OOS_PREDICTIONS=50），并保留逐笔预测记录 {date, code, predicted_return,
      actual_return, correct}；
  3. 以交易日期为重采样单元，做 20 交易日 circular block bootstrap（B≥2000）；
  4. 与主池 24 只并列对比（同口径：同一脚本、同一评估函数、同一 bootstrap）。

只读生产数据；唯一输出 reports/h20d_evaluation/scan_generalization_*.json。
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
import lightgbm as lgb  # noqa: E402

# 生产数据只读：关闭 SDK / fundamentals 的缓存写回副作用。
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
    """构建扩展宇宙：取指数成分 → 排除主池 → 过滤历史不足 → 固定种子抽样。"""
    meta: dict[str, Any] = {
        "index_symbols": index_symbols,
        "sample_size": sample_size,
        "seed": seed,
        "excluded_master_pool": sorted(exclude),
    }
    candidates: dict[str, str] = {}  # code -> index symbol
    index_meta: dict[str, Any] = {}
    for sym in index_symbols:
        codes, as_of = _fetch_index_codes(sym)
        index_meta[sym] = {"n_constituents": len(codes), "as_of": as_of}
        for c in codes:
            if c not in exclude and c not in candidates:
                candidates[c] = sym
    meta["index_meta"] = index_meta
    meta["n_candidates_before_filter"] = len(candidates)

    # 过滤历史不足（逐只读本地/麦蕊 kline）
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
    """复刻基准脚本逻辑，并保留逐笔 OOS 预测记录。"""
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
                    correct = int((predicted_return > 0) == (actual_return > 0))
                    records.append(
                        {
                            "date": df.loc[test_idx, "date"].strftime("%Y-%m-%d"),
                            "code": code,
                            "predicted_return": predicted_return,
                            "actual_return": actual_return,
                            "correct": correct,
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
            "accuracy": float(np.mean([r["correct"] for r in records])),
            "records": records,
            "window_errors": window_errors[:10],
        }
    except Exception as exc:
        return {"code": code, "name": name, "status": "error", "reason": f"{type(exc).__name__}: {exc}"}


def _date_arrays(records: list[dict[str, Any]], master_dates: list[str]) -> tuple[np.ndarray, np.ndarray]:
    date_pos = {d: i for i, d in enumerate(master_dates)}
    correct = np.zeros(len(master_dates), dtype=float)
    counts = np.zeros(len(master_dates), dtype=float)
    for r in records:
        p = date_pos.get(r["date"])
        if p is not None:
            correct[p] += int(r["correct"])
            counts[p] += 1
    return correct, counts


def block_bootstrap_metric(records: list[dict[str, Any]], reps: int, seed: int) -> dict[str, Any]:
    """20 交易日 circular block bootstrap，重采样单元=交易日期。"""
    n_predictions = len(records)
    stocks = sorted({r["code"] for r in records})
    base: dict[str, Any] = {
        "n_predictions": n_predictions,
        "n_stocks": len(stocks),
        "bootstrap_reps": reps,
        "block_length_trading_days": BLOCK_LENGTH,
        "bootstrap_seed": seed,
    }
    if n_predictions == 0:
        base.update({"status": "insufficient_data", "accuracy": None, "block_bootstrap_95ci": None})
        return base

    observed = float(np.mean([r["correct"] for r in records]))
    base["accuracy"] = observed

    master_dates = sorted({r["date"] for r in records})
    daily_correct, daily_counts = _date_arrays(records, master_dates)
    n_dates = len(master_dates)
    if n_dates < BLOCK_LENGTH:
        base.update({"status": "insufficient_dates", "n_master_dates": n_dates,
                     "block_bootstrap_95ci": None})
        return base

    n_blocks = math.ceil(n_dates / BLOCK_LENGTH)
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n_dates, size=(reps, n_blocks), endpoint=False)
    offsets = np.arange(BLOCK_LENGTH)
    sampled_positions = (starts[:, :, None] + offsets[None, None, :]) % n_dates
    sampled_positions = sampled_positions.reshape(reps, -1)[:, :n_dates]
    boot_correct = daily_correct[sampled_positions].sum(axis=1)
    boot_counts = daily_counts[sampled_positions].sum(axis=1)
    valid = boot_counts > 0
    boot_accuracy = boot_correct[valid] / boot_counts[valid]
    if len(boot_accuracy) < max(100, int(reps * 0.9)):
        base.update({"status": "insufficient_bootstrap_draws",
                     "valid_bootstrap_draws": int(len(boot_accuracy)),
                     "block_bootstrap_95ci": None})
        return base

    ci_low, ci_high = np.quantile(boot_accuracy, [0.025, 0.975])
    threshold = observed - 0.5
    centered_delta = boot_accuracy - observed
    p_value = (1.0 + float(np.sum(centered_delta >= threshold))) / (len(centered_delta) + 1.0)
    base.update({
        "status": "ok",
        "block_bootstrap_95ci": [float(ci_low), float(ci_high)],
        "block_bootstrap_p_one_sided": float(p_value),
        "valid_bootstrap_draws": int(len(boot_accuracy)),
        "n_master_dates": n_dates,
        "n_unique_dates_with_predictions": int(np.sum(daily_counts > 0)),
        "bootstrap_mean": float(np.mean(boot_accuracy)),
        "bootstrap_std": float(np.std(boot_accuracy)),
    })
    return base


def _per_stock_distribution(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    accs = [v["accuracy"] for v in results.values() if v["status"] == "ok"]
    if not accs:
        return {"n": 0}
    arr = np.asarray(accs, dtype=float)
    return {
        "n": int(len(arr)),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "std_sample_ddof1": float(arr.std(ddof=1)),
        "std_pop_ddof0": float(arr.std(ddof=0)),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "above_50pct": int(np.sum(arr > 0.5)),
        "above_55pct": int(np.sum(arr > 0.55)),
        "above_60pct": int(np.sum(arr > 0.60)),
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

    boot = block_bootstrap_metric(all_records, bootstrap_reps, seed)
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
        "overall_accuracy": float(np.mean([r["correct"] for r in all_records])) if all_records else None,
        "bootstrap": boot,
        "per_stock_distribution": _per_stock_distribution(ok),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", nargs="+", default=["000300"],
                        help="宽基指数代码（akshare index_stock_cons_csindex），默认沪深300")
    parser.add_argument("--sample-size", type=int, default=80, help="抽样规模（60–100）")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    parser.add_argument("--bootstrap-reps", type=int, default=DEFAULT_BOOTSTRAP_REPS)
    parser.add_argument("--bootstrap-seed", type=int, default=42)
    args = parser.parse_args()

    t0 = time.time()
    master_codes, master_names = _load_master_pool()
    exclude = set(master_codes)
    print(f"🚀 P4-A 泛化验证")
    print(f"   主池 {len(master_codes)} 只; 指数 {args.index}; 抽样 {args.sample_size} 只; seed={args.seed}")

    sampled_codes, universe_meta = build_universe(args.index, exclude, args.sample_size, args.seed)
    print(f"   扩展宇宙候选: {universe_meta['n_candidates_before_filter']} → "
          f"过滤后 {universe_meta['n_candidates_after_filter']} → 抽样 {len(sampled_codes)}")
    new_names = {c: c for c in sampled_codes}

    # —— 并行评估：主池 24 + 扩展宇宙 ——
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

    # —— 汇总 ——
    master_summary = _summarize_universe("master_pool_24", master_results,
                                         args.bootstrap_reps, args.bootstrap_seed)
    new_summary = _summarize_universe("new_universe", new_results,
                                      args.bootstrap_reps, args.bootstrap_seed)

    output = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "walk_forward_oos + 20d circular block bootstrap",
        "question": "h20d 微弱方向信号是否存在于主池之外（幸存者偏差检验）",
        "params": {
            "horizon": HORIZON,
            "window_days": WINDOW_DAYS,
            "backtest_days": BACKTEST_DAYS,
            "min_train_samples": MIN_TRAIN_SAMPLES,
            "min_oos_predictions": MIN_OOS_PREDICTIONS,
            "max_workers": MAX_WORKERS,
            "block_length_trading_days": BLOCK_LENGTH,
            "bootstrap_reps": args.bootstrap_reps,
        },
        "sampling": universe_meta,
        "master_pool_codes": sorted(master_codes),
        "comparison": {
            "master_pool_24": master_summary,
            "new_universe": new_summary,
        },
        "per_stock_new_universe": {
            c: {"name": new_names.get(c, c), "status": v["status"],
                "accuracy": v.get("accuracy"), "n_predictions": v.get("n_predictions"),
                "reason": v.get("reason")}
            for c, v in new_results.items()
        },
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    os.makedirs("reports/h20d_evaluation", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/h20d_evaluation/scan_generalization_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'='*70}")
    print(f"📊 泛化验证完成 ({output['elapsed_seconds']:.0f}s)")
    print(f"{'='*70}")
    for key in ("master_pool_24", "new_universe"):
        s = output["comparison"][key]
        boot = s["bootstrap"]
        ci = boot.get("block_bootstrap_95ci")
        ci_s = f"[{ci[0]:.4f}, {ci[1]:.4f}]" if ci else "N/A"
        d = s["per_stock_distribution"]
        print(f"\n【{s['label']}】 股票ok={s['n_stocks_ok']}/{s['n_stocks_attempted']} "
              f"预测笔数={s['total_predictions']:,}")
        print(f"   整体准确率: {s['overall_accuracy']:.4%}  20日块bootstrap95%CI={ci_s}")
        if d.get("n"):
            print(f"   逐股: mean={d['mean']:.4%} median={d['median']:.4%} "
                  f"sd(ddof1)={d['std_sample_ddof1']:.4%} min={d['min']:.4%} max={d['max']:.4%}")
        if s["failure_distribution"]:
            print(f"   失败/跳过: {s['failure_distribution']}")
    print(f"\n  📁 {out_path}")


if __name__ == "__main__":
    main()
