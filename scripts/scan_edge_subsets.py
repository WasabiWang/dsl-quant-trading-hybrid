#!/usr/bin/env python3
"""
H20D walk-forward edge 子集扫描。

统计原则：
1. 完整复刻 backtest_walkforward_h20d_accuracy.py 的滚动训练/预测口径；
2. 保存每笔 OOS 预测；
3. 以交易日期为重采样单元，使用 20 个交易日 circular block bootstrap；
4. 前 70% 交易日筛选、后 30% 交易日验收；
5. 筛选期和验收期分别使用 Benjamini-Hochberg FDR(q=0.10)。

本脚本只读生产数据，并禁止底层 SDK 写缓存。唯一输出是
reports/h20d_evaluation/edge_subset_scan_*.json。
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
from typing import Any, Callable

import numpy as np
import pandas as pd
import yaml

os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# 按要求复用与基准脚本相同的导入。
from train_predictor_enhanced import build_features, fetch_fundamentals  # noqa: E402
from dsl_data_sdk_original import get_kline, normalize_symbol  # noqa: E402
from sklearn.feature_selection import SelectFromModel  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from scipy.stats import t as student_t  # noqa: E402
import lightgbm as lgb  # noqa: E402

# 生产数据只读：SDK 默认会把读取结果写回 cache；本任务显式关闭该副作用。
import dsl_data_sdk_original as _data_sdk  # noqa: E402
import scripts.fundamentals_loader as _fundamentals_loader  # noqa: E402

_data_sdk._write_cache = lambda key, data: None
_fundamentals_loader._save_file_cache = lambda: None

HORIZON = 20
BACKTEST_DAYS = 730
WINDOW_DAYS = 90
MIN_TRAIN_SAMPLES = 200
MIN_OOS_PREDICTIONS = 50
TIMEOUT_GLOBAL = 1200
MAX_WORKERS = 3
BLOCK_LENGTH = 20
DEFAULT_BOOTSTRAP_REPS = 4000
FDR_Q = 0.10
SCREEN_FRACTION = 0.70
VALIDATION_ALPHA = 0.10


def _load_pool() -> tuple[list[dict[str, Any]], dict[str, str]]:
    with open(PROJECT_ROOT / "config" / "master_stock_pool.yaml", encoding="utf-8") as f:
        raw_pool = yaml.safe_load(f)["master_pool"]
    pool = [s for s in raw_pool if "." not in str(s["symbol"])]
    for stock in pool:
        stock["symbol"] = str(stock["symbol"]).zfill(6)
    names = {s["symbol"]: s.get("name", s["symbol"]) for s in pool}
    return pool, names


def evaluate_stock(code: str, name: str) -> dict[str, Any]:
    """复刻基准脚本逻辑，并保留逐笔 OOS 预测。"""
    started = time.time()
    window_errors: list[str] = []
    try:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=int(BACKTEST_DAYS * 2.2))).strftime("%Y-%m-%d")
        kline = get_kline(normalize_symbol(code), start_date, end_date)
        if not kline or len(kline) < 350:
            return {"code": code, "name": name, "status": "insufficient_kline", "n_kline": len(kline or [])}

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
            return {"code": code, "name": name, "status": "insufficient_target"}

        feats[feature_cols] = feats[feature_cols].fillna(0)
        all_idx = feats.index.tolist()
        if len(all_idx) < MIN_TRAIN_SAMPLES + WINDOW_DAYS:
            return {"code": code, "name": name, "status": "insufficient_total_rows", "n_rows": len(all_idx)}

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
            return {
                "code": code,
                "name": name,
                "status": "insufficient_oos_predictions",
                "n_predictions": len(records),
                "window_errors": window_errors[:10],
            }
        return {
            "code": code,
            "name": name,
            "status": "ok",
            "n_predictions": len(records),
            "accuracy": float(np.mean([r["correct"] for r in records])),
            "elapsed_seconds": round(time.time() - started, 3),
            "window_errors": window_errors[:10],
            "records": records,
        }
    except Exception as exc:
        return {"code": code, "name": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        gc.collect()


def _date_arrays(records: list[dict[str, Any]], master_dates: list[str]) -> tuple[np.ndarray, np.ndarray]:
    date_pos = {date: i for i, date in enumerate(master_dates)}
    correct = np.zeros(len(master_dates), dtype=float)
    counts = np.zeros(len(master_dates), dtype=float)
    for record in records:
        position = date_pos.get(record["date"])
        if position is not None:
            correct[position] += int(record["correct"])
            counts[position] += 1
    return correct, counts


def block_bootstrap_metric(
    records: list[dict[str, Any]],
    master_dates: list[str],
    reps: int,
    seed: int,
) -> dict[str, Any]:
    """20 交易日 circular block bootstrap；p 值检验 H0: accuracy <= 0.5。"""
    n_predictions = len(records)
    stocks = sorted({r["code"] for r in records})
    base: dict[str, Any] = {
        "n_predictions": n_predictions,
        "n_stocks": len(stocks),
        "stocks": stocks,
        "accuracy": None,
        "block_bootstrap_95ci": None,
        "block_bootstrap_p_one_sided": None,
        "bootstrap_reps": reps,
        "block_length_trading_days": BLOCK_LENGTH,
        "p_value_null": "H0: accuracy <= 0.5; one-sided centered block bootstrap",
    }
    if n_predictions == 0 or not master_dates:
        base["status"] = "insufficient_data"
        return base

    observed = float(np.mean([r["correct"] for r in records]))
    daily_correct, daily_counts = _date_arrays(records, master_dates)
    n_dates = len(master_dates)
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
        base.update({"accuracy": observed, "status": "insufficient_bootstrap_draws", "valid_bootstrap_draws": int(len(boot_accuracy))})
        return base

    ci_low, ci_high = np.quantile(boot_accuracy, [0.025, 0.975])
    # 在 H0 下把 bootstrap 分布平移到 0.5；计算上尾概率。
    threshold = observed - 0.5
    centered_delta = boot_accuracy - observed
    p_value = (1.0 + float(np.sum(centered_delta >= threshold))) / (len(centered_delta) + 1.0)
    base.update(
        {
            "status": "ok",
            "accuracy": observed,
            "block_bootstrap_95ci": [float(ci_low), float(ci_high)],
            "block_bootstrap_p_one_sided": float(p_value),
            "valid_bootstrap_draws": int(len(boot_accuracy)),
            "n_unique_dates_with_predictions": int(np.sum(daily_counts > 0)),
            "n_master_dates": n_dates,
        }
    )
    base["cluster_robust"] = cluster_robust_metric(records)
    return base


def cluster_robust_metric(records: list[dict[str, Any]]) -> dict[str, Any]:
    """OLS 截距模型按股票聚类的 CR1 sandwich 区间（t, G-1 自由度）。"""
    if not records:
        return {"status": "insufficient_data"}
    values = np.asarray([r["correct"] for r in records], dtype=float)
    mean = float(values.mean())
    codes = np.asarray([r["code"] for r in records])
    unique_codes = np.unique(codes)
    clusters = len(unique_codes)
    if clusters < 2:
        return {"status": "not_available", "reason": "股票聚类数少于2", "n_clusters": clusters}
    score_sums = np.asarray([np.sum(values[codes == code] - mean) for code in unique_codes])
    variance = (clusters / (clusters - 1.0)) * float(np.sum(score_sums**2)) / (len(values) ** 2)
    se = math.sqrt(max(variance, 0.0))
    critical = float(student_t.ppf(0.975, df=clusters - 1))
    ci = [max(0.0, mean - critical * se), min(1.0, mean + critical * se)]
    p_value = float(student_t.sf((mean - 0.5) / se, df=clusters - 1)) if se > 0 else (0.0 if mean > 0.5 else 1.0)
    return {
        "status": "ok",
        "method": "OLS intercept-only CR1 sandwich, stock clusters, t(G-1)",
        "n_clusters": clusters,
        "standard_error": se,
        "ci_95": ci,
        "p_one_sided": p_value,
    }


def benjamini_hochberg(p_values: dict[str, float]) -> dict[str, float]:
    """返回单调化 BH adjusted p-values。"""
    ordered = sorted(p_values.items(), key=lambda item: item[1])
    m = len(ordered)
    adjusted: dict[str, float] = {}
    running = 1.0
    for rank_from_end in range(m - 1, -1, -1):
        hypothesis_id, p_value = ordered[rank_from_end]
        rank = rank_from_end + 1
        running = min(running, p_value * m / rank)
        adjusted[hypothesis_id] = float(min(1.0, running))
    return adjusted


def _metric_seed(base_seed: int, hypothesis_id: str, phase: str) -> int:
    # Python hash 会跨进程随机化，因此使用稳定字符和。
    token = f"{hypothesis_id}|{phase}"
    return int((base_seed + sum((i + 1) * ord(ch) for i, ch in enumerate(token))) % (2**32 - 1))


def _make_hypotheses(screen_records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    hypotheses: list[dict[str, Any]] = []
    for code in sorted({r["code"] for r in screen_records}):
        hypotheses.append(
            {
                "id": f"stock:{code}",
                "family": "stock",
                "label": code,
                "definition": {"code_equals": code},
                "predicate": lambda r, code=code: r["code"] == code,
            }
        )

    magnitudes = np.asarray([abs(float(r["predicted_return"])) for r in screen_records], dtype=float)
    quantiles = np.quantile(magnitudes, [0.25, 0.50, 0.75]).tolist()
    q25, q50, q75 = quantiles
    confidence_defs = [
        ("q1", "最低预测幅度四分位", None, q25, lambda x: x <= q25),
        ("q2", "次低预测幅度四分位", q25, q50, lambda x: q25 < x <= q50),
        ("q3", "次高预测幅度四分位", q50, q75, lambda x: q50 < x <= q75),
        ("q4", "最高预测幅度四分位", q75, None, lambda x: x > q75),
    ]
    for qid, label, lower, upper, magnitude_predicate in confidence_defs:
        hypotheses.append(
            {
                "id": f"confidence:{qid}",
                "family": "prediction_magnitude_quartile",
                "label": label,
                "definition": {
                    "absolute_predicted_return_lower_exclusive": lower,
                    "absolute_predicted_return_upper_inclusive": upper,
                    "thresholds_derived_from": "screening_period_only",
                },
                "predicate": lambda r, p=magnitude_predicate: p(abs(float(r["predicted_return"]))),
            }
        )

    for quarter in range(1, 5):
        hypotheses.append(
            {
                "id": f"calendar_quarter:Q{quarter}",
                "family": "calendar_quarter_of_year",
                "label": f"日历Q{quarter}",
                "definition": {"calendar_quarter_of_year": quarter},
                "predicate": lambda r, quarter=quarter: ((int(r["date"][5:7]) - 1) // 3 + 1) == quarter,
            }
        )

    scan_notes = {
        "prediction_magnitude_quartile_thresholds": {
            "q25": q25,
            "q50": q50,
            "q75": q75,
            "absolute_value": True,
            "derived_from": "screening_period_only",
        },
        "calendar_quarter_interpretation": "可跨年份复现的季节子集：Q1/Q2/Q3/Q4，而非一次性的YYYY-Q标签",
        "market_regime": {
            "status": "skipped",
            "reason": "所要求复用的输入仅含个股K线与最新基本面快照，没有独立、时点一致的宽基市场regime序列；个股vol_regime不能冒充市场regime。",
            "hypotheses_added": 0,
        },
    }
    return hypotheses, scan_notes


def _public_hypothesis(hypothesis: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in hypothesis.items() if key != "predicate"}


def run_scan(bootstrap_reps: int, seed: int) -> tuple[dict[str, Any], Path]:
    started = time.time()
    pool, names = _load_pool()
    print(f"🚀 H20D edge 子集扫描：{len(pool)}只，日期块={BLOCK_LENGTH}交易日，bootstrap B={bootstrap_reps}")
    print("   统计设计：前70%交易日筛选 + 后30%交易日独立验收；两阶段均BH-FDR q=0.10")

    stock_results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(evaluate_stock, s["symbol"], s.get("name", s["symbol"])): s["symbol"] for s in pool}
        try:
            for future in as_completed(futures, timeout=TIMEOUT_GLOBAL):
                code = futures[future]
                try:
                    result = future.result(timeout=5)
                except Exception as exc:
                    result = {"code": code, "name": names.get(code, code), "status": "future_error", "error": f"{type(exc).__name__}: {exc}"}
                stock_results[code] = result
                if result.get("status") == "ok":
                    print(f"  ✅ {code} {names.get(code, '')}: acc={result['accuracy']:.2%}, n={result['n_predictions']}")
                else:
                    print(f"  ⏭️ {code} {names.get(code, '')}: {result.get('status')}")
        except TimeoutError:
            for future, code in futures.items():
                if code not in stock_results:
                    stock_results[code] = {"code": code, "name": names.get(code, code), "status": "global_timeout"}

    valid_results = [result for result in stock_results.values() if result.get("status") == "ok"]
    records = sorted(
        [record for result in valid_results for record in result["records"]],
        key=lambda record: (record["date"], record["code"]),
    )
    if not records:
        raise RuntimeError("无有效逐笔 OOS 预测，无法执行统计推断")

    all_dates = sorted({r["date"] for r in records})
    split_at = int(math.floor(len(all_dates) * SCREEN_FRACTION))
    if split_at < BLOCK_LENGTH or len(all_dates) - split_at < BLOCK_LENGTH:
        raise RuntimeError("交易日期不足，无法执行70/30分离及20日块重采样")
    screen_dates = all_dates[:split_at]
    validation_dates = all_dates[split_at:]
    screen_set = set(screen_dates)
    validation_set = set(validation_dates)
    screen_records = [r for r in records if r["date"] in screen_set]
    validation_records = [r for r in records if r["date"] in validation_set]

    hypotheses, scan_notes = _make_hypotheses(screen_records)
    output_hypotheses: list[dict[str, Any]] = []
    screen_p_values: dict[str, float] = {}

    for hypothesis in hypotheses:
        subset = [r for r in screen_records if hypothesis["predicate"](r)]
        metric = block_bootstrap_metric(
            subset,
            screen_dates,
            bootstrap_reps,
            _metric_seed(seed, hypothesis["id"], "screening"),
        )
        p_value = metric.get("block_bootstrap_p_one_sided")
        screen_p_values[hypothesis["id"]] = float(p_value) if p_value is not None else 1.0
        public = _public_hypothesis(hypothesis)
        public["screening"] = metric
        output_hypotheses.append(public)

    screen_adjusted = benjamini_hochberg(screen_p_values)
    candidates: list[str] = []
    by_id = {item["id"]: item for item in output_hypotheses}
    raw_by_id = {item["id"]: item for item in hypotheses}
    for hypothesis_id, item in by_id.items():
        item["screening"]["bh_fdr_adjusted_p"] = screen_adjusted[hypothesis_id]
        item["screening"]["passes_bh_fdr_q_0_10"] = bool(
            item["screening"].get("accuracy") is not None
            and item["screening"]["accuracy"] > 0.5
            and screen_adjusted[hypothesis_id] <= FDR_Q
        )
        if item["screening"]["passes_bh_fdr_q_0_10"]:
            candidates.append(hypothesis_id)

    validation_p_values: dict[str, float] = {}
    for hypothesis_id in candidates:
        hypothesis = raw_by_id[hypothesis_id]
        subset = [r for r in validation_records if hypothesis["predicate"](r)]
        metric = block_bootstrap_metric(
            subset,
            validation_dates,
            bootstrap_reps,
            _metric_seed(seed, hypothesis_id, "validation"),
        )
        by_id[hypothesis_id]["validation"] = metric
        p_value = metric.get("block_bootstrap_p_one_sided")
        validation_p_values[hypothesis_id] = float(p_value) if p_value is not None else 1.0

    validation_adjusted = benjamini_hochberg(validation_p_values) if validation_p_values else {}
    stable: list[str] = []
    for item in output_hypotheses:
        hypothesis_id = item["id"]
        if hypothesis_id not in candidates:
            item["validation"] = {
                "status": "not_tested",
                "reason": "筛选期未通过BH-FDR q=0.10；验收期按预注册流程不查看该子集",
            }
            item["stable_edge"] = False
            continue
        metric = item["validation"]
        adjusted_p = validation_adjusted[hypothesis_id]
        metric["bh_fdr_adjusted_p_among_screened_candidates"] = adjusted_p
        reproduced = bool(metric.get("accuracy") is not None and metric["accuracy"] > 0.5 and adjusted_p <= VALIDATION_ALPHA)
        metric["reproduced_at_fdr_q_0_10"] = reproduced
        item["stable_edge"] = reproduced
        if reproduced:
            stable.append(hypothesis_id)

    overall_full = block_bootstrap_metric(records, all_dates, bootstrap_reps, _metric_seed(seed, "overall", "full"))
    overall_screen = block_bootstrap_metric(screen_records, screen_dates, bootstrap_reps, _metric_seed(seed, "overall", "screening"))
    overall_validation = block_bootstrap_metric(validation_records, validation_dates, bootstrap_reps, _metric_seed(seed, "overall", "validation"))

    failures = [
        {key: value for key, value in result.items() if key != "records"}
        for result in stock_results.values()
        if result.get("status") != "ok"
    ]
    per_stock_run = {
        code: {key: value for key, value in result.items() if key != "records"}
        for code, result in sorted(stock_results.items())
    }
    now = datetime.now()
    output_path = PROJECT_ROOT / "reports" / "h20d_evaluation" / f"edge_subset_scan_{now.strftime('%Y%m%d_%H%M%S')}.json"
    output = {
        "schema_version": "1.0",
        "generated_at": now.isoformat(timespec="seconds"),
        "method": "h20d_walk_forward_edge_subset_scan",
        "parameters": {
            "horizon": HORIZON,
            "backtest_days": BACKTEST_DAYS,
            "window_days": WINDOW_DAYS,
            "min_train_samples": MIN_TRAIN_SAMPLES,
            "min_oos_predictions": MIN_OOS_PREDICTIONS,
            "block_bootstrap_length_trading_days": BLOCK_LENGTH,
            "bootstrap_reps": bootstrap_reps,
            "bootstrap_seed": seed,
            "screen_fraction_by_unique_trading_dates": SCREEN_FRACTION,
            "screening_fdr_q": FDR_Q,
            "validation_fdr_q": VALIDATION_ALPHA,
            "direction_rule": "predicted_return > 0 equals actual_return > 0",
        },
        "inference_guardrails": {
            "bernoulli_iid_ci_used": False,
            "bootstrap_unit": "交易日期块；同一天的全部股票记录共同进入/离开样本",
            "bootstrap_scheme": "20交易日 circular moving-block bootstrap",
            "cluster_cross_check": "OLS截距模型，按股票CR1 sandwich，t(G-1)",
            "selection_validation_separation": "所有阈值和候选均只由前70%日期确定；后30%仅测试筛选候选",
            "validation_reproduction_rule": "accuracy>0.5 且候选集合内BH-FDR adjusted p<=0.10",
        },
        "date_split": {
            "n_all_unique_dates": len(all_dates),
            "n_screening_dates": len(screen_dates),
            "n_validation_dates": len(validation_dates),
            "screening_start": screen_dates[0],
            "screening_end": screen_dates[-1],
            "validation_start": validation_dates[0],
            "validation_end": validation_dates[-1],
        },
        "scan_notes": scan_notes,
        "summary": {
            "n_stocks_requested": len(pool),
            "n_stocks_evaluated": len(valid_results),
            "n_stock_failures": len(failures),
            "n_predictions": len(records),
            "n_hypotheses_scanned": len(hypotheses),
            "n_screening_fdr_significant": len(candidates),
            "screening_fdr_significant_subsets": candidates,
            "n_validation_reproduced": len(stable),
            "stable_edge_subsets": stable,
            "conclusion": (
                "存在通过筛选期FDR校正且在独立验收期经FDR复现的edge子集"
                if stable
                else "不存在通过筛选期FDR校正且在独立验收期经FDR复现的edge子集"
            ),
            "elapsed_seconds": round(time.time() - started, 3),
        },
        "overall": {
            "full_sample": overall_full,
            "screening_period": overall_screen,
            "validation_period": overall_validation,
        },
        "hypotheses": output_hypotheses,
        "stock_run_summary": per_stock_run,
        "failures": failures,
        "prediction_records": records,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, allow_nan=False)

    ci = overall_full["block_bootstrap_95ci"]
    cluster_ci = overall_full["cluster_robust"]["ci_95"]
    print("\n" + "=" * 72)
    print("📊 H20D edge 子集扫描完成")
    print("=" * 72)
    print(f"  结论：{output['summary']['conclusion']}")
    print(
        f"  假设：扫描 {len(hypotheses)} 个；筛选期FDR显著 {len(candidates)} 个；"
        f"验收期复现 {len(stable)} 个"
    )
    print(
        f"  全样本准确率：{overall_full['accuracy']:.2%}；"
        f"20日日期块bootstrap 95%CI=[{ci[0]:.2%}, {ci[1]:.2%}]"
    )
    print(f"  股票cluster-robust 95%CI=[{cluster_ci[0]:.2%}, {cluster_ci[1]:.2%}]")
    print(f"  市场regime：跳过（{scan_notes['market_regime']['reason']}）")
    print(f"  📁 {output_path.relative_to(PROJECT_ROOT)}")
    return output, output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描 H20D walk-forward 的稳定 edge 子集")
    parser.add_argument("--bootstrap-reps", type=int, default=DEFAULT_BOOTSTRAP_REPS)
    parser.add_argument("--seed", type=int, default=20260912)
    args = parser.parse_args()
    if args.bootstrap_reps < 2000:
        parser.error("--bootstrap-reps 必须 >= 2000")
    run_scan(args.bootstrap_reps, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
