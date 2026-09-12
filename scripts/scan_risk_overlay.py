#!/usr/bin/env python3
"""
P4-C 风险信息验证：模型信号是否"含风险信息"（知道自己不知道）→ 可作为仓位缩放/风控门控叠层。

预注册判据（锁定，见 audits/NEXT-edge验证-P4C预注册.md）：
  C1 个体波动预测：日度截面 Spearman IC 于 |predicted_return| 与 |actual_return| 之间。
      成功 = 主池24只 与 新宇宙(>=60只) 的 20交易日块 bootstrap 95%CI 下限均 >0，且 |mean IC| >= 0.02。
  C2 市场风险门控：每日截面 σ_pred(t) = std(predicted_return) 与 σ_actual(t) = std(actual_return)
      做 Spearman 相关。成功 = 20交易日块 bootstrap 95%CI 下限 >0，且 |ρ| >= 0.10。
  判定：有风险信息 = C1 且 C2 均满足；无风险信息 = 任一 CI 上界<0 或任一主检验点估计方向为负；
        无结论 = 其他。

数据来源（与 P1/P4-A/P4-B 完全一致，保证直接对照）：
  默认读取 P4-B 的逐笔记录 reports/h20d_evaluation/scan_ic_20260912_145957.json 的
  per_trade_records.{master_pool_24,new_universe}，即 {date, code, predicted_return, actual_return}。
  该记录由同一份 walk-forward 管线（backtest_walkforward_h20d_accuracy.py 参数：
  HORIZON=20/BACKTEST_DAYS=730/WINDOW_DAYS=90/MIN_TRAIN_SAMPLES=200/MIN_OOS_PREDICTIONS=50）
  与相同抽样（沪深300成分、排除主池、seed=42、80只）生成。
  --rerun 时复用 scripts/scan_ic.py 的 evaluate_stock/build_universe（其内部调用
  build_features/fetch_fundamentals/get_kline/normalize_symbol）重新生成记录。

只读生产数据；唯一新建输出 reports/h20d_evaluation/scan_risk_overlay_*.json。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import warnings
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

from scipy.stats import spearmanr  # noqa: E402

HORIZON = 20
BACKTEST_DAYS = 730
WINDOW_DAYS = 90
MIN_TRAIN_SAMPLES = 200
MIN_OOS_PREDICTIONS = 50
BLOCK_LENGTH = 20
DEFAULT_BOOTSTRAP_REPS = 4000      # >= 2000（与 P4-B 一致）
MIN_CROSS_SECTIONAL = 5            # 当日横截面样本数 <5 的日期剔除
C1_EFFECT_SIZE = 0.02              # |mean IC| 阈值
C2_EFFECT_SIZE = 0.10              # |ρ| 阈值
ANNUALIZATION = 252

CANONICAL_RECORDS = "reports/h20d_evaluation/scan_ic_20260912_145957.json"


# --------------------------------------------------------------------------- #
# 数据读取 / 重跑
# --------------------------------------------------------------------------- #
def load_records_from_json(path: str) -> dict[str, list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pr = data.get("per_trade_records")
    if not pr:
        raise ValueError(f"{path} 缺少 per_trade_records")
    out: dict[str, list[dict[str, Any]]] = {}
    for key in ("master_pool_24", "new_universe"):
        out[key] = [dict(r) for r in pr.get(key, [])]
    return out


def rerun_records(index_symbols: list[str], sample_size: int, seed: int) -> dict[str, list[dict[str, Any]]]:
    """复用 scan_ic.py 的管线重新生成逐笔记录（同一代码、同参数、同抽样）。"""
    import scripts.scan_ic as scan_ic  # noqa: E402
    master_codes, master_names = scan_ic._load_master_pool()
    exclude = set(master_codes)
    sampled_codes, universe_meta = scan_ic.build_universe(index_symbols, exclude, sample_size, seed)

    all_targets = [(c, master_names.get(c, c), "master") for c in master_codes] + \
                  [(c, c, "new") for c in sampled_codes]
    master_results: dict[str, dict[str, Any]] = {}
    new_results: dict[str, dict[str, Any]] = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
    with ThreadPoolExecutor(max_workers=scan_ic.MAX_WORKERS) as executor:
        futures = {executor.submit(scan_ic.evaluate_stock, c, n): (c, g) for c, n, g in all_targets}
        done = 0
        try:
            for future in as_completed(futures, timeout=scan_ic.TIMEOUT_GLOBAL):
                code, group = futures[future]
                try:
                    result = future.result(timeout=5)
                except Exception:
                    result = {"code": code, "status": "error"}
                (master_results if group == "master" else new_results)[code] = result
                done += 1
        except TimeoutError:
            print(f"   ⏰ 全局超时；已完成 {done}/{len(all_targets)}")

    return {
        "master_pool_24": [r for v in master_results.values() if v.get("status") == "ok"
                           for r in v.get("records", [])],
        "new_universe": [r for v in new_results.values() if v.get("status") == "ok"
                         for r in v.get("records", [])],
    }


# --------------------------------------------------------------------------- #
# C1 / C2 计算
# --------------------------------------------------------------------------- #
def compute_c1(records: list[dict[str, Any]]) -> dict[str, Any]:
    """日度截面 Spearman IC 于 |predicted_return| 与 |actual_return| 之间。"""
    df = pd.DataFrame(records)
    if df.empty:
        return {"ic_values": [], "drop_stats": {"n_dates_total": 0, "n_dates_lt5": 0,
                 "n_dates_zero_variance": 0, "n_dates_nan_ic": 0, "n_dates_kept": 0}}

    ic_values: list[float] = []
    n_total = n_lt5 = n_zero_var = n_nan = 0
    for date, grp in df.groupby("date", sort=True):
        n_total += 1
        if len(grp) < MIN_CROSS_SECTIONAL:
            n_lt5 += 1
            continue
        a_pred = np.abs(grp["predicted_return"].values.astype(float))
        a_act = np.abs(grp["actual_return"].values.astype(float))
        if np.std(a_pred) == 0 or np.std(a_act) == 0:
            n_zero_var += 1
            continue
        ic = spearmanr(a_pred, a_act)[0]
        if not np.isfinite(ic):
            n_nan += 1
            continue
        ic_values.append(float(ic))
    return {
        "ic_values": ic_values,
        "drop_stats": {
            "n_dates_total": n_total, "n_dates_lt5": n_lt5,
            "n_dates_zero_variance": n_zero_var, "n_dates_nan_ic": n_nan,
            "n_dates_kept": len(ic_values),
        },
    }


def compute_c2(records: list[dict[str, Any]]) -> dict[str, Any]:
    """每日截面 σ_pred(t)=std(pred) 与 σ_actual(t)=std(actual) 的 Spearman 相关（及逐日序列供 bootstrap）。"""
    df = pd.DataFrame(records)
    if df.empty:
        return {"sigma_pred": [], "sigma_actual": [], "rho": None, "n_days": 0}

    sigma_pred: list[float] = []
    sigma_actual: list[float] = []
    for date, grp in df.groupby("date", sort=True):
        if len(grp) < MIN_CROSS_SECTIONAL:
            continue
        sigma_pred.append(float(np.std(grp["predicted_return"].values.astype(float))))
        sigma_actual.append(float(np.std(grp["actual_return"].values.astype(float))))

    n_days = len(sigma_pred)
    rho = None
    if n_days >= 2 and np.std(sigma_pred) > 0 and np.std(sigma_actual) > 0:
        rho = float(spearmanr(sigma_pred, sigma_actual)[0])
    return {"sigma_pred": sigma_pred, "sigma_actual": sigma_actual, "rho": rho, "n_days": n_days}


# --------------------------------------------------------------------------- #
# 20 交易日 circular block bootstrap
# --------------------------------------------------------------------------- #
def _circular_block_positions(n: int, n_blocks: int, block_len: int, reps: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n, size=(reps, n_blocks), endpoint=False)
    offsets = np.arange(block_len)
    pos = (starts[:, :, None] + offsets[None, None, :]) % n
    return pos.reshape(reps, -1)[:, :n]          # (reps, n)


def block_bootstrap_mean(series: list[float], reps: int, seed: int) -> dict[str, Any]:
    """对时间序列做 20 交易日 circular block bootstrap，返回 mean 的 95%CI 与单侧 p(H1: mean>0)。"""
    n = len(series)
    base: dict[str, Any] = {"bootstrap_reps": reps, "block_length_trading_days": BLOCK_LENGTH,
                            "bootstrap_seed": seed, "n_obs": n}
    arr = np.asarray(series, dtype=float)
    observed_mean = float(arr.mean())
    base["observed_mean"] = observed_mean
    base["abs_observed_mean"] = abs(observed_mean)
    if n < 2:
        base.update({"status": "insufficient", "ci95": None, "p_one_sided": None})
        return base
    if n < BLOCK_LENGTH:
        base.update({"status": "insufficient_for_block", "ci95": None, "p_one_sided": None})
        return base

    n_blocks = math.ceil(n / BLOCK_LENGTH)
    pos = _circular_block_positions(n, n_blocks, BLOCK_LENGTH, reps, seed)
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


def block_bootstrap_spearman(x: list[float], y: list[float], reps: int, seed: int) -> dict[str, Any]:
    """对配对序列 (x,y) 做 20 交易日 circular block bootstrap，返回 Spearman ρ 的 95%CI 与单侧 p(H1: ρ>0)。"""
    n = len(x)
    base: dict[str, Any] = {"bootstrap_reps": reps, "block_length_trading_days": BLOCK_LENGTH,
                            "bootstrap_seed": seed, "n_obs": n}
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    if n < 2:
        base.update({"status": "insufficient", "ci95": None, "p_one_sided": None})
        return base
    observed = float(spearmanr(xa, ya)[0]) if (np.std(xa) > 0 and np.std(ya) > 0) else float("nan")
    base["observed_rho"] = observed
    base["abs_observed_rho"] = abs(observed) if np.isfinite(observed) else None
    if n < BLOCK_LENGTH:
        base.update({"status": "insufficient_for_block", "ci95": None, "p_one_sided": None})
        return base

    n_blocks = math.ceil(n / BLOCK_LENGTH)
    pos = _circular_block_positions(n, n_blocks, BLOCK_LENGTH, reps, seed)
    boot_rhos = np.full(reps, np.nan)
    for i in range(reps):
        xi = xa[pos[i]]
        yi = ya[pos[i]]
        if np.std(xi) == 0 or np.std(yi) == 0:
            continue
        r = spearmanr(xi, yi)[0]
        if np.isfinite(r):
            boot_rhos[i] = r

    valid = boot_rhos[np.isfinite(boot_rhos)]
    if len(valid) < max(100, int(reps * 0.9)):
        base.update({"status": "insufficient_valid_draws", "n_valid": int(len(valid)),
                     "ci95": None, "p_one_sided": None})
        return base
    ci_low = float(np.quantile(valid, 0.025))
    ci_high = float(np.quantile(valid, 0.975))
    p_one_sided = (1.0 + float(np.sum(valid <= 0.0))) / (len(valid) + 1.0)
    base.update({
        "status": "ok",
        "n_valid": int(len(valid)),
        "ci95": [ci_low, ci_high],
        "p_one_sided": p_one_sided,
        "bootstrap_mean_rho": float(np.mean(valid)),
    })
    return base


# --------------------------------------------------------------------------- #
# 次要经济指标：等权多头敞口 + C1/C2 缩放
# --------------------------------------------------------------------------- #
def _perf(period_ret: np.ndarray, periods_per_year: float) -> dict[str, Any]:
    """period_ret 为非重叠 HORIZON 期收益序列；按 periods_per_year=252/HORIZON 年化。"""
    r = np.asarray(period_ret, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return {"annualized_vol": None, "max_drawdown": None, "sharpe": None,
                "total_return": None, "n_periods": int(len(r))}
    ann_vol = float(r.std(ddof=1) * math.sqrt(periods_per_year))
    sharpe = float(r.mean() / r.std(ddof=1) * math.sqrt(periods_per_year)) if r.std(ddof=1) > 1e-12 else float("nan")
    equity = np.cumprod(1.0 + r)
    peak = np.maximum.accumulate(equity)
    mdd = float(((equity - peak) / peak).min())
    total_return = float(equity[-1] - 1.0)
    return {"annualized_vol": ann_vol, "max_drawdown": mdd, "sharpe": sharpe,
            "total_return": total_return, "n_periods": int(len(r))}


def secondary_economic(records: list[dict[str, Any]]) -> dict[str, Any]:
    """等权多头基准 vs C1(个股权重反比于|pred|) vs C2(整体敞口反比于σ_pred) 的波动/回撤/Sharpe。

    收益序列按非重叠 HORIZON(=20) 交易日块构造：每第 20 个交易日作为再平衡日，
    取该日逐笔 actual_return(=target_20d 未来20日收益)作为该块组合收益，
    从而得到非重叠的 20 日组合收益序列（约 252/HORIZON 期/年）。
    """
    df = pd.DataFrame(records)
    if df.empty:
        return {"baseline": {}, "c1_overlay": {}, "c2_overlay": {}, "note": "empty"}

    # C1 权重 floor：取全宇宙 |pred| 的 1% 分位，避免除零爆炸
    abs_pred_all = np.abs(df["predicted_return"].values.astype(float))
    floor = float(np.quantile(abs_pred_all, 0.01))
    floor = max(floor, 1e-6)

    dates = sorted(df["date"].unique())
    rebalance_dates = dates[::HORIZON]          # 非重叠：每隔 20 个交易日再平衡一次

    r_base: list[float] = []
    r_c1: list[float] = []
    sigma_pred: list[float] = []
    used_dates: list[str] = []
    for d in rebalance_dates:
        grp = df[df["date"] == d]
        if len(grp) < MIN_CROSS_SECTIONAL:
            continue
        pred = grp["predicted_return"].values.astype(float)
        actual = grp["actual_return"].values.astype(float)
        w = 1.0 / np.maximum(np.abs(pred), floor)
        w = w / w.sum()
        r_base.append(float(actual.mean()))
        r_c1.append(float((w * actual).sum()))
        sigma_pred.append(float(np.std(pred)))
        used_dates.append(d)

    r_base = np.asarray(r_base)
    r_c1 = np.asarray(r_c1)
    sigma_pred = np.asarray(sigma_pred)

    # C2 时间门控：k(t) ∝ 1/σ_pred(t)，跨再平衡日均值=1（平均敞口不变）
    inv = 1.0 / np.maximum(sigma_pred, 1e-9)
    k = inv / inv.mean()
    r_c2 = k * r_base

    periods_per_year = ANNUALIZATION / HORIZON
    baseline = _perf(r_base, periods_per_year)
    c1 = _perf(r_c1, periods_per_year)
    c2 = _perf(r_c2, periods_per_year)

    # 相对变化（次要，不用于主判定）
    def delta(overlay: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in ("annualized_vol", "max_drawdown", "sharpe"):
            bv = base.get(key)
            ov = overlay.get(key)
            if bv is None or ov is None or not np.isfinite(bv) or not np.isfinite(ov):
                out[key + "_delta"] = None
                continue
            if key == "max_drawdown":  # MDD 用相对变化，因基准可能为 0
                out[key + "_delta"] = None if abs(bv) < 1e-12 else float((ov - bv) / abs(bv))
            else:
                out[key + "_delta"] = float(ov - bv)
        return out

    return {
        "n_rebalance_periods": int(len(r_base)),
        "period_days": HORIZON,
        "periods_per_year": periods_per_year,
        "first_date": used_dates[0] if used_dates else None,
        "last_date": used_dates[-1] if used_dates else None,
        "c1_weight_floor": floor,
        "c2_gate_mean_k": float(k.mean()) if len(k) else None,
        "baseline": baseline,
        "c1_overlay": c1,
        "c2_overlay": c2,
        "c1_vs_baseline": delta(c1, baseline),
        "c2_vs_baseline": delta(c2, baseline),
        "note": "收益为非重叠 HORIZON=20 交易日组合收益(每第20个交易日再平衡),按 252/20 期/年年化; "
                "C1=个股权重反比于|pred|(截面再分配,各期权重和=1); C2=整体敞口反比于σ_pred(t)(时间门控,跨期均值=1); "
                "Sharpe 假设无风险利率=0。次要指标不用于主判定。",
    }


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def _decide(c1: dict[str, dict[str, Any]], c2: dict[str, dict[str, Any]]) -> dict[str, Any]:
    univs = ["master_pool_24", "new_universe"]

    point_estimates: dict[str, float] = {}
    ci_uppers: list[float] = []
    ci_lowers: dict[str, float] = {}

    for u in univs:
        b1 = c1[u]["bootstrap"]
        b2 = c2[u]["bootstrap"]
        point_estimates[f"c1_{u}_mean_ic"] = b1.get("observed_mean")
        point_estimates[f"c2_{u}_rho"] = b2.get("observed_rho")
        if b1.get("ci95"):
            ci_uppers.append(b1["ci95"][1]); ci_lowers[f"c1_{u}"] = b1["ci95"][0]
        if b2.get("ci95"):
            ci_uppers.append(b2["ci95"][1]); ci_lowers[f"c2_{u}"] = b2["ci95"][0]

    # C1 成功条件（两宇宙同时）：CI 下限>0 且 |mean IC|>=0.02
    c1_ok = {}
    for u in univs:
        b = c1[u]["bootstrap"]
        c1_ok[u] = (b.get("ci95") is not None and b["ci95"][0] > 0
                    and b.get("abs_observed_mean") is not None and b["abs_observed_mean"] >= C1_EFFECT_SIZE)
    c1_pass = all(c1_ok.values())

    # C2 成功条件（两宇宙同时，与 C1 口径一致）：CI 下限>0 且 |ρ|>=0.10
    c2_ok = {}
    for u in univs:
        b = c2[u]["bootstrap"]
        c2_ok[u] = (b.get("ci95") is not None and b["ci95"][0] > 0
                    and b.get("abs_observed_rho") is not None and b["abs_observed_rho"] >= C2_EFFECT_SIZE)
    c2_pass = all(c2_ok.values())

    # 无风险信息：任一 CI 上界<0，或任一主检验点估计方向为负
    any_upper_lt0 = any((v < 0) for v in ci_uppers) if ci_uppers else False
    any_point_negative = any((v is not None and v < 0) for v in point_estimates.values())

    if any_upper_lt0 or any_point_negative:
        verdict = "无风险信息"
    elif c1_pass and c2_pass:
        verdict = "有风险信息"
    else:
        verdict = "无结论"

    return {
        "verdict": verdict,
        "c1_success_both_universes": bool(c1_pass),
        "c2_success_both_universes": bool(c2_pass),
        "c1_success_per_universe": c1_ok,
        "c2_success_per_universe": c2_ok,
        "any_ci_upper_lt0": bool(any_upper_lt0),
        "any_point_estimate_negative": bool(any_point_negative),
        "point_estimates": point_estimates,
        "effect_size_thresholds": {"c1_abs_mean_ic": C1_EFFECT_SIZE, "c2_abs_rho": C2_EFFECT_SIZE},
        "note": "C2 亦按两宇宙同时满足判定（与 C1 口径一致，且与『只报主池』禁止条款一致）。",
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records-json", default=CANONICAL_RECORDS,
                        help="P4-B 逐笔记录 JSON（默认读取 scan_ic_20260912_145957.json）")
    parser.add_argument("--rerun", action="store_true",
                        help="复用 scan_ic.py 管线重新生成记录（而非读取缓存）")
    parser.add_argument("--index", nargs="+", default=["000300"])
    parser.add_argument("--sample-size", type=int, default=80)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--bootstrap-reps", type=int, default=DEFAULT_BOOTSTRAP_REPS)
    parser.add_argument("--bootstrap-seed", type=int, default=42)
    args = parser.parse_args()

    t0 = time.time()

    if args.rerun:
        print("🔄 复用 scan_ic.py 管线重新生成逐笔记录 …")
        records = rerun_records(args.index, args.sample_size, args.seed)
        source = f"rerun(index={args.index},sample={args.sample_size},seed={args.seed})"
    else:
        print(f"📂 读取逐笔记录: {args.records_json}")
        records = load_records_from_json(args.records_json)
        source = args.records_json

    c1: dict[str, dict[str, Any]] = {}
    c2: dict[str, dict[str, Any]] = {}
    secondary: dict[str, dict[str, Any]] = {}
    for u in ("master_pool_24", "new_universe"):
        recs = records.get(u, [])
        print(f"   {u}: 逐笔记录 {len(recs):,} 条")
        c1_raw = compute_c1(recs)
        c1[u] = {
            "drop_stats": c1_raw["drop_stats"],
            "bootstrap": block_bootstrap_mean(c1_raw["ic_values"], args.bootstrap_reps, args.bootstrap_seed),
        }
        c2_raw = compute_c2(recs)
        c2[u] = {
            "n_days": c2_raw["n_days"],
            "observed_rho": c2_raw["rho"],
            "bootstrap": block_bootstrap_spearman(c2_raw["sigma_pred"], c2_raw["sigma_actual"],
                                                  args.bootstrap_reps, args.bootstrap_seed),
        }
        secondary[u] = secondary_economic(recs)

    decision = _decide(c1, c2)

    output = {
        "schema_version": 1,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "walk_forward_oos records + daily cross-sectional Spearman( |pred|,|actual| ) for C1; "
                  "cross-sectional std Spearman for C2; 20d circular block bootstrap",
        "question": "模型信号是否含风险信息（P4-C，仓位缩放/风控门控叠层）",
        "preregistration": "audits/NEXT-edge验证-P4C预注册.md",
        "records_source": source,
        "params": {
            "horizon": HORIZON, "window_days": WINDOW_DAYS, "backtest_days": BACKTEST_DAYS,
            "min_train_samples": MIN_TRAIN_SAMPLES, "min_oos_predictions": MIN_OOS_PREDICTIONS,
            "block_length_trading_days": BLOCK_LENGTH, "bootstrap_reps": args.bootstrap_reps,
            "min_cross_sectional": MIN_CROSS_SECTIONAL, "c1_effect_size": C1_EFFECT_SIZE,
            "c2_effect_size": C2_EFFECT_SIZE, "annualization": ANNUALIZATION,
        },
        "c1": c1,
        "c2": c2,
        "secondary_economic": secondary,
        "decision": decision,
        "elapsed_seconds": round(time.time() - t0, 1),
    }

    os.makedirs("reports/h20d_evaluation", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/h20d_evaluation/scan_risk_overlay_{timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    # 简明打印
    print(f"\n{'='*70}")
    print(f"📊 P4-C 风险信息验证完成 ({output['elapsed_seconds']:.0f}s)")
    print(f"{'='*70}")
    for u in ("master_pool_24", "new_universe"):
        b1 = c1[u]["bootstrap"]
        ci1 = b1.get("ci95")
        b2 = c2[u]["bootstrap"]
        ci2 = b2.get("ci95")
        ci1_s = f"[{ci1[0]:+.4f}, {ci1[1]:+.4f}]" if ci1 else "N/A"
        ci2_s = f"[{ci2[0]:+.4f}, {ci2[1]:+.4f}]" if ci2 else "N/A"
        print(f"\n【{u}】 逐笔 {len(records.get(u, [])):,}  截面日(保留) {c1[u]['drop_stats']['n_dates_kept']}")
        print(f"   C1 mean IC(|pred|,|act|) = {b1.get('observed_mean', float('nan')):+.4f}  "
              f"95%CI = {ci1_s}  单侧p = {b1.get('p_one_sided', float('nan')):.4f}")
        print(f"   C2 ρ(σ_pred,σ_actual) = {b2.get('observed_rho', float('nan')):+.4f}  "
              f"95%CI = {ci2_s}  单侧p = {b2.get('p_one_sided', float('nan')):.4f}")
        s = secondary[u]
        print(f"   次要: 基准 annVol={s['baseline'].get('annualized_vol', float('nan')):.4f} "
              f"MDD={s['baseline'].get('max_drawdown', float('nan')):.4f} Sharpe={s['baseline'].get('sharpe', float('nan')):.4f}")
        print(f"         C1叠层 annVol={s['c1_overlay'].get('annualized_vol', float('nan')):.4f} "
              f"MDD={s['c1_overlay'].get('max_drawdown', float('nan')):.4f} Sharpe={s['c1_overlay'].get('sharpe', float('nan')):.4f}")
        print(f"         C2叠层 annVol={s['c2_overlay'].get('annualized_vol', float('nan')):.4f} "
              f"MDD={s['c2_overlay'].get('max_drawdown', float('nan')):.4f} Sharpe={s['c2_overlay'].get('sharpe', float('nan')):.4f}")
    print(f"\n🎯 裁决: {decision['verdict']}")
    print(f"   C1 两宇宙满足={decision['c1_success_both_universes']}  "
          f"C2 两宇宙满足={decision['c2_success_both_universes']}")
    print(f"   任一CI上界<0={decision['any_ci_upper_lt0']}  任一点估计为负={decision['any_point_estimate_negative']}")
    print(f"\n  📁 {out_path}")


if __name__ == "__main__":
    main()
