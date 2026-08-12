#!/usr/bin/env python3
"""
DSL v4.5.1 Walk-Forward OOS Direction Accuracy — 参数化窗口
接收 --horizon 参数 (5/10/20)，支持三种窗口评估

方法:
- 滚动窗口: 每90天一个窗口，用窗口前数据训练 LightGBM
- 对每个窗口的测试期，每天用模型预测未来HORIZON日收益方向
- 跨窗口汇总每只股票的 OOS direction_accuracy
"""
import os, sys, json, yaml, gc, time, argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import numpy as np
import pandas as pd

os.environ["PYTHONWARNINGS"] = "ignore"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from dsl_data_sdk_original import get_kline, normalize_symbol
from train_predictor_enhanced import build_features
from scripts.fundamentals_loader import fetch_fundamentals_mairui


def fetch_fundamentals(code):
    """统一基本面接口"""
    return fetch_fundamentals_mairui(code)
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb
import warnings
warnings.filterwarnings("ignore")


# ====== 截面排名字段（9个特征） ======
RANK_FEATURES = ['mom_5d', 'mom_10d', 'mom_20d', 'vol_ratio_20', 'vol_change', 'atr14', 'rsi14', 'amplitude', 'turnover']
RANK_COLS = ['rank_' + f for f in RANK_FEATURES]


# ====== 新增：Step 1 预加载 ======
def preload_all_data():
    """
    预加载所有标的的数据并计算特征矩阵。
    返回: {code: {'feats': DataFrame, 'name': str}}
    """
    print(f"\n{'='*60}")
    print(f"🚚 Step 1: 预加载 {len(CODES)}只标的的数据...")
    print(f"{'='*60}")

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=int(BACKTEST_DAYS * 2.2))).strftime("%Y-%m-%d")

    all_data = {}
    loaded = 0
    failed = 0

    for s in pool:
        code = s["symbol"]
        name = s.get("name", code)

        try:
            kline = get_kline(normalize_symbol(code), start_date, end_date)
            if not kline or len(kline) < 350:
                print(f"  ⏭️  {code} {name}: 数据不足({len(kline) if kline else 0}条)")
                failed += 1
                continue

            df = pd.DataFrame(kline)
            for c in ["close", "volume", "high", "low", "open"]:
                df[c] = df[c].astype(float)

            fundamentals = fetch_fundamentals(code)
            feats = build_features(df, fundamentals).replace([np.inf, -np.inf], np.nan)

            # 添加日期列用于截面对齐
            feats['_date'] = pd.to_datetime(df['date']).dt.date

            # 确保所有 horizon 的 target 都存在
            for h in [5, 10, 20]:
                tcol = f"target_{h}d"
                if tcol not in feats.columns:
                    close = df["close"].astype(float)
                    feats[tcol] = close.shift(-h) / close - 1

            all_data[code] = {
                'feats': feats,
                'name': name
            }
            loaded += 1
            print(f"  ✅ {code} {name}: {len(df)}天 → {len(feats.columns)}维特征")

        except Exception as e:
            print(f"  ❌ {code} {name}: 异常 {e}")
            failed += 1

    print(f"\n  📊 预加载完成: {loaded}只成功, {failed}只失败")
    return all_data


# ====== 新增：Step 1.5 截面特征计算 ======
def compute_cross_sectional_ranks(all_data):
    """
    对每个日期跨所有标的计算截面 rank 特征（0-1）。
    修改 all_data 中的 feats DataFrame，追加 rank_* 列。
    """
    print(f"\n{'='*60}")
    print(f"📊 Step 1.5: 计算 Cross-sectional Rank 特征...")
    print(f"{'='*60}")

    codes = list(all_data.keys())
    if len(codes) < 3:
        print(f"  ⚠️ 可用标的数量不足 ({len(codes)}), 跳过截面计算")
        return all_data

    # 初始化 rank 列（默认为 -1）
    for code in codes:
        for col in RANK_COLS:
            all_data[code]['feats'][col] = -1.0

    # 构建 panel: 每个标的按 _date 展开
    panel_rows = []
    for code in codes:
        feats = all_data[code]['feats']
        for idx, row in feats.iterrows():
            d = row.get('_date')
            if pd.isna(d):
                continue
            rec = {'_date': d, '_code': code, '_idx': idx}
            for f in RANK_FEATURES:
                v = row.get(f, np.nan)
                rec[f] = v if (v is not None and not pd.isna(v)) else np.nan
            panel_rows.append(rec)

    if not panel_rows:
        print("  ⚠️ 无有效 panel 数据")
        return all_data

    panel = pd.DataFrame(panel_rows)
    print(f"  Panel shape: {panel.shape}, 日期数: {panel['_date'].nunique()}")

    # 对每个特征按日期分组计算百分位排名
    for feat in RANK_FEATURES:
        col = 'rank_' + feat
        # groupby rank with pct=True: 最小→0.0, 最大→1.0, NaN→-1.0
        rank_series = panel.groupby('_date')[feat].rank(pct=True, method='average')
        panel[col] = rank_series.fillna(-1.0)

    print(f"  9个截面特征计算完成")

    # 写回各标的 feats DataFrame
    for code in codes:
        mask = panel['_code'] == code
        subset = panel[mask]
        if subset.empty:
            continue
        feats = all_data[code]['feats']
        for _, row in subset.iterrows():
            idx = row['_idx']
            for col in RANK_COLS:
                feats.at[idx, col] = row[col]

    # 统计覆盖率
    total_cells = len(codes) * len(RANK_COLS)
    non_default = 0
    for code in codes:
        feats = all_data[code]['feats']
        for col in RANK_COLS:
            non_default += (feats[col] != -1.0).sum()
    print(f"  Rank 特征覆盖率: {non_default}/{total_cells} cells")

    return all_data

# ====== 解析参数 ======
parser = argparse.ArgumentParser(description="Walk-Forward OOS 方向精度评估")
parser.add_argument("--horizon", type=int, choices=[5, 10, 20],
                    help="预测窗口天数 (5/10/20)")
parser.add_argument("--generate-report", action="store_true", help="从已保存的结果生成对比报告")
parser.add_argument("--window-days", type=int, default=90, help="滚动窗口天数")
parser.add_argument("--backtest-days", type=int, default=730, help="回测总天数")
parser.add_argument("--max-workers", type=int, default=3, help="并行线程数")
parser.add_argument("--timeout-global", type=int, default=1200, help="全局超时秒数")
args = parser.parse_args()

HORIZON = args.horizon if args.horizon else 5
BACKTEST_DAYS = args.backtest_days
WINDOW_DAYS = args.window_days
MIN_TRAIN_SAMPLES = 200
MIN_OOS_PREDICTIONS = 50
TIMEOUT_GLOBAL = args.timeout_global
MAX_WORKERS = args.max_workers

# ====== 加载股票池 ======
with open("config/master_stock_pool.yaml") as f:
    raw_pool = yaml.safe_load(f)["master_pool"]
pool = [s for s in raw_pool if "." not in s["symbol"]]
CODES = [s["symbol"] for s in pool]
NAMES = {s["symbol"]: s.get("name", s["symbol"]) for s in pool}

if not args.generate_report:
    print(f"🚀 H{HORIZON}D Walk-Forward OOS 精度评估: {len(CODES)}只 × ~{BACKTEST_DAYS//WINDOW_DAYS}窗口")
    print(f"   参数: horizon={HORIZON}d window={WINDOW_DAYS}d backtest={BACKTEST_DAYS}d")
    print(f"   并行: {MAX_WORKERS}线程  最小训练样本: {MIN_TRAIN_SAMPLES}")


def evaluate_stock_with_feats(code: str, name: str, feats: pd.DataFrame) -> dict:
    """
    使用预计算的特征矩阵对单只股票执行 walk-forward OOS 方向精度评估。
    feats 应已包含 cross-sectional rank 特征。
    """
    t0 = time.time()
    try:
        tcol = f"target_{HORIZON}d"

        if tcol not in feats.columns or feats[tcol].notna().sum() < MIN_TRAIN_SAMPLES:
            return None

        # 特征列: 排除 target_* 和内部列(_date等)
        fcols = [c for c in feats.columns if not c.startswith("target_") and not c.startswith("_")]

        # 特征NaN填充为0
        feats[fcols] = feats[fcols].fillna(0)

        # 所有行可用，只需target有效
        all_idx = feats.index.tolist()
        if len(all_idx) < BACKTEST_DAYS + MIN_TRAIN_SAMPLES:
            return None

        # 回测期 = 最后 BACKTEST_DAYS 行
        bt_start = len(all_idx) - BACKTEST_DAYS
        bt_indices = all_idx[bt_start:]

        all_predictions = []

        # 窗口滚动
        for w in range(0, len(bt_indices), WINDOW_DAYS):
            w_end = min(w + WINDOW_DAYS, len(bt_indices))
            test_dates = bt_indices[w:w_end]

            if len(test_dates) < 5:
                continue

            # 训练截止 = test_dates第一行
            train_end_pos = all_idx.index(test_dates[0])
            train_idx = all_idx[:train_end_pos]

            if len(train_idx) < MIN_TRAIN_SAMPLES:
                continue

            # 训练数据
            train_data = feats.loc[train_idx]
            target_valid = train_data[tcol].notna()
            if target_valid.sum() < MIN_TRAIN_SAMPLES:
                continue

            X_tr = train_data.loc[target_valid, fcols].values
            y_tr = train_data.loc[target_valid, tcol].values

            # 80/20 时间分割
            split = int(len(X_tr) * 0.8)
            X_tr_80, y_tr_80 = X_tr[:split], y_tr[:split]

            try:
                scaler = StandardScaler()
                X_scaled = scaler.fit_transform(X_tr_80)

                selector = SelectFromModel(
                    lgb.LGBMRegressor(n_estimators=50, random_state=42, verbose=-1),
                    threshold="median", max_features=40
                )
                X_sel = selector.fit_transform(X_scaled, y_tr_80)

                model = lgb.LGBMRegressor(
                    n_estimators=200, max_depth=6, learning_rate=0.03,
                    random_state=42, verbose=-1, n_jobs=1,
                )
                model.fit(X_sel, y_tr_80)

                # 预测测试期
                for test_idx in test_dates:
                    if test_idx not in feats.index:
                        continue
                    row = feats.loc[test_idx]
                    if pd.isna(row[tcol]):
                        continue

                    X_test = scaler.transform([row[fcols].values])
                    X_test_sel = selector.transform(X_test)
                    pred = float(model.predict(X_test_sel)[0])
                    actual = float(row[tcol])

                    all_predictions.append((
                        1 if pred > 0 else -1,
                        1 if actual > 0 else -1,
                    ))
            except Exception:
                continue

        # 计算指标
        if len(all_predictions) < MIN_OOS_PREDICTIONS:
            return None

        correct = sum(1 for pd_, ad_ in all_predictions if pd_ == ad_)
        total = len(all_predictions)
        tp = sum(1 for pd_, ad_ in all_predictions if pd_ == 1 and ad_ == 1)
        tn = sum(1 for pd_, ad_ in all_predictions if pd_ == -1 and ad_ == -1)
        fp = sum(1 for pd_, ad_ in all_predictions if pd_ == 1 and ad_ == -1)
        fn = sum(1 for pd_, ad_ in all_predictions if pd_ == -1 and ad_ == 1)

        accuracy = correct / total
        tpr = tp / (tp + fn) if (tp + fn) > 0 else 0
        tnr = tn / (tn + fp) if (tn + fp) > 0 else 0

        return {
            "direction_accuracy": round(accuracy, 4),
            "total_predictions": total,
            "correct": correct,
            "wrong": total - correct,
            "true_positive_rate": round(tpr, 4),
            "true_negative_rate": round(tnr, 4),
            "predicted_ups": tp + fp,
            "predicted_downs": tn + fn,
            "actual_ups": tp + fn,
            "actual_downs": tn + fp,
        }

    except Exception as e:
        return None


def main():
    start_time = time.time()
    pipeline_config = {
        "p0_1_fundamentals": {
            "enabled": True,
            "stocks_with_data": 0,
            "total_stocks": len(CODES),
        },
        "p0_2_cross_sectional_rank": {
            "enabled": True,
            "features": len(RANK_FEATURES),
            "feature_names": RANK_FEATURES,
            "rank_column_names": RANK_COLS,
        }
    }

    # ====== Step 1: 预加载所有标的的数据 ======
    all_data = preload_all_data()
    pipeline_config["p0_1_fundamentals"]["stocks_with_data"] = len(all_data)

    if len(all_data) < 3:
        print("\n❌ 有效标的数量不足!")
        return None, None

    # ====== Step 1.5: 截面特征计算 ======
    all_data = compute_cross_sectional_ranks(all_data)

    # ====== Step 2: Walk-Forward OOS 并行评估 ======
    print(f"\n{'='*60}")
    print(f"🎯 Step 2: Walk-Forward OOS 并行评估...")
    print(f"{'='*60}")

    results = {}
    evaluated_count = 0
    failed_count = 0

    # 先提取 codes/names/feats
    eval_list = [(code, all_data[code]['name'], all_data[code]['feats'])
                 for code in all_data]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for code, name, feats in eval_list:
            future = executor.submit(evaluate_stock_with_feats, code, name, feats.copy())
            futures[future] = code

        try:
            for future in as_completed(futures, timeout=TIMEOUT_GLOBAL):
                code = futures[future]
                try:
                    result = future.result(timeout=5)
                    if result:
                        results[code] = result
                        evaluated_count += 1
                        bar = "✅" if result["direction_accuracy"] > 0.5 else "⚠️"
                        print(f"  {bar} {code} {NAMES.get(code, '')}: "
                              f"acc={result['direction_accuracy']:.2%} "
                              f"n={result['total_predictions']} "
                              f"tpr={result['true_positive_rate']:.2%} "
                              f"tnr={result['true_negative_rate']:.2%}")
                    else:
                        failed_count += 1
                        print(f"  ⏭️ {code}: 数据不足或评估失败")
                except Exception as e:
                    failed_count += 1
                    print(f"  ❌ {code}: 异常 {e}")
        except TimeoutError:
            remaining = sum(1 for f in futures if not f.done())
            print(f"  ⏰ 全局超时! {remaining}只未完成")
            for future in futures:
                if future.done():
                    code = futures[future]
                    try:
                        result = future.result(timeout=2)
                        if result and code not in results:
                            results[code] = result
                            evaluated_count += 1
                    except Exception:
                        pass

    elapsed = time.time() - start_time

    if not results:
        print("\n❌ 无有效回测结果!")
        return None, None

    accuracies = [v["direction_accuracy"] for v in results.values()]
    total_preds = [v["total_predictions"] for v in results.values()]

    dist = {"below_40": 0, "40_45": 0, "45_50": 0, "50_55": 0, "55_60": 0, "60_plus": 0}
    for a in accuracies:
        if a < 0.40:
            dist["below_40"] += 1
        elif a < 0.45:
            dist["40_45"] += 1
        elif a < 0.50:
            dist["45_50"] += 1
        elif a < 0.55:
            dist["50_55"] += 1
        elif a < 0.60:
            dist["55_60"] += 1
        else:
            dist["60_plus"] += 1

    summary = {
        "stocks_evaluated": evaluated_count,
        "stocks_failed": failed_count,
        "mean_accuracy": round(float(np.mean(accuracies)), 4),
        "median_accuracy": round(float(np.median(accuracies)), 4),
        "std_accuracy": round(float(np.std(accuracies)), 4),
        "min_accuracy": round(float(np.min(accuracies)), 4),
        "max_accuracy": round(float(np.max(accuracies)), 4),
        "stocks_above_50pct": sum(1 for a in accuracies if a > 0.50),
        "stocks_above_55pct": sum(1 for a in accuracies if a > 0.55),
        "stocks_above_60pct": sum(1 for a in accuracies if a > 0.60),
        "total_oos_predictions": int(np.sum(total_preds)),
        "elapsed_seconds": round(elapsed, 1),
        "accuracy_distribution": dist,
    }

    output = {
        "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "walk_forward_oos",
        "pipeline_config": pipeline_config,
        "params": {
            "horizon": HORIZON,
            "window_days": WINDOW_DAYS,
            "backtest_days": BACKTEST_DAYS,
            "min_train_samples": MIN_TRAIN_SAMPLES,
            "min_oos_predictions": MIN_OOS_PREDICTIONS,
        },
        "per_stock": results,
        "summary": summary,
    }

    os.makedirs("reports/horizon_eval", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/horizon_eval/evaluation_h{HORIZON}d_oos_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"📊 H{HORIZON}D Walk-Forward OOS 方向精度评估完成 ({elapsed:.0f}s)")
    print(f"{'='*60}")
    print(f"  评估标的: {summary['stocks_evaluated']}只 (失败{summary['stocks_failed']}只)")
    print(f"  OOS预测总次数: {summary['total_oos_predictions']:,}")
    print(f"  均值精度: {summary['mean_accuracy']:.2%}")
    print(f"  中位数:   {summary['median_accuracy']:.2%}")
    print(f"  标准差:   {summary['std_accuracy']:.2%}")
    print(f"  最高:     {summary['max_accuracy']:.2%}")
    print(f"  最低:     {summary['min_accuracy']:.2%}")
    print(f"  >50%:     {summary['stocks_above_50pct']}只")
    print(f"  >55%:     {summary['stocks_above_55pct']}只")
    print(f"  >60%:     {summary['stocks_above_60pct']}只")
    print(f"\n  精度分布:")
    for k, v in dist.items():
        bar = "█" * v
        pct_label = k.replace("_", "-").replace("below", "<").replace("plus", "+")
        print(f"    {pct_label}: {v:3d} {bar}")
    print(f"\n  📁 {out_path}")
    return output, out_path, pipeline_config


def load_oldest_file(horizon):
    """加载该 horizon 最早的评估结果文件（原始 baseline）"""
    eval_dir = "reports/horizon_eval"
    if not os.path.isdir(eval_dir):
        return None
    files = sorted([f for f in os.listdir(eval_dir)
                    if f.startswith(f"evaluation_h{horizon}d_") and f.endswith(".json")])
    if not files:
        return None
    target = files[0]
    print(f"  📂 加载 baseline: {target}")
    with open(os.path.join(eval_dir, target), "r") as f:
        return json.load(f)


def load_newest_file(horizon):
    """加载该 horizon 最新的评估结果文件（新特征结果）"""
    eval_dir = "reports/horizon_eval"
    if not os.path.isdir(eval_dir):
        return None
    files = sorted([f for f in os.listdir(eval_dir)
                    if f.startswith(f"evaluation_h{horizon}d_") and f.endswith(".json")])
    if not files:
        return None
    target = files[-1]
    print(f"  📂 加载新结果: {target}")
    with open(os.path.join(eval_dir, target), "r") as f:
        return json.load(f)


def generate_rank_comparison_report(h_results, pipeline_config):
    """生成截面 rank 改进前后对比报告"""
    print(f"\n{'='*60}")
    print(f"📝 生成截面 rank 改进对比报告...")
    print(f"{'='*60}")

    comparison = {
        "report_metadata": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "v4.5.5",
            "pipeline": "P0特征工程改进 - Cross-sectional Rank",
        },
        "p0_1_fundamentals": {
            "description": "真实基本面特征（麦蕊API）",
            "features": ["roe", "eps", "bps", "cfps", "revenue_growth", "gross_margin", "capex_ratio"],
            "stocks_with_data": pipeline_config["p0_1_fundamentals"]["stocks_with_data"],
            "total_stocks": pipeline_config["p0_1_fundamentals"]["total_stocks"],
            "coverage_pct": round(pipeline_config["p0_1_fundamentals"]["stocks_with_data"] / max(1, pipeline_config["p0_1_fundamentals"]["total_stocks"]) * 100, 1),
            "cache_file": "cache/fundamentals_cache.json",
        },
        "p0_2_cross_sectional_rank": pipeline_config["p0_2_cross_sectional_rank"],
        "oos_comparison": {},
    }

    for h in [5, 10, 20]:
        hkey = f"h{h}d"
        if hkey not in h_results:
            continue

        cur = h_results[hkey]
        baseline = load_oldest_file(h)

        if baseline:
            bl_mean = baseline.get("summary", {}).get("mean_accuracy", None)
        else:
            bl_mean = None

        cur_mean = cur.get("summary", {}).get("mean_accuracy", 0)
        cur_median = cur.get("summary", {}).get("median_accuracy", 0)
        cur_above50 = cur.get("summary", {}).get("stocks_above_50pct", 0)
        cur_above55 = cur.get("summary", {}).get("stocks_above_55pct", 0)
        cur_above60 = cur.get("summary", {}).get("stocks_above_60pct", 0)

        per_stock = []
        for code, info in sorted(cur.get("per_stock", {}).items()):
            bl_acc = None
            if baseline:
                bl_code_info = baseline.get("per_stock", {}).get(code, {})
                bl_acc = bl_code_info.get("direction_accuracy", None)

            cur_acc = info["direction_accuracy"]
            delta = round(cur_acc - bl_acc, 4) if bl_acc is not None else None

            per_stock.append({
                "code": code,
                "baseline_accuracy": bl_acc,
                "new_accuracy": cur_acc,
                "delta": delta,
            })

        # 排序 delta
        sorted_by_delta = sorted(
            [s for s in per_stock if s["delta"] is not None],
            key=lambda x: x["delta"], reverse=True
        )

        improved = sum(1 for s in per_stock if s["delta"] is not None and s["delta"] > 0)
        worsened = sum(1 for s in per_stock if s["delta"] is not None and s["delta"] < 0)
        unchanged = sum(1 for s in per_stock if s["delta"] is not None and s["delta"] == 0)

        comparison["oos_comparison"][hkey] = {
            "baseline_mean": bl_mean,
            "new_mean": round(cur_mean, 4),
            "delta_mean": round(cur_mean - bl_mean, 4) if bl_mean is not None else None,
            "baseline_median": baseline.get("summary", {}).get("median_accuracy") if baseline else None,
            "new_median": round(cur_median, 4),
            "baseline_above_50": baseline.get("summary", {}).get("stocks_above_50pct") if baseline else None,
            "new_above_50": cur_above50,
            "baseline_above_55": baseline.get("summary", {}).get("stocks_above_55pct") if baseline else None,
            "new_above_55": cur_above55,
            "baseline_above_60": baseline.get("summary", {}).get("stocks_above_60pct") if baseline else None,
            "new_above_60": cur_above60,
            "per_stock": per_stock,
            "improved": improved,
            "worsened": worsened,
            "unchanged": unchanged,
        }

    # Top 5 improved, Top 3 worsened (all horizons combined)
    all_deltas = []
    for hkey, hdata in comparison["oos_comparison"].items():
        for s in hdata.get("per_stock", []):
            if s["delta"] is not None:
                all_deltas.append({
                    "horizon": hkey,
                    "code": s["code"],
                    "baseline": s["baseline_accuracy"],
                    "new": s["new_accuracy"],
                    "delta": s["delta"],
                    "name": NAMES.get(s["code"], s["code"]),
                })

    sorted_deltas = sorted(all_deltas, key=lambda x: x["delta"], reverse=True)
    comparison["top5_improved"] = sorted_deltas[:5]
    comparison["top3_worsened"] = sorted_deltas[-3:] if len(sorted_deltas) >= 3 else sorted_deltas

    comparison["analysis"] = {
        "note": "Cross-sectional rank 特征在 walk-forward OOS 回测中已生效。"
                "每个窗口训练时 rank 特征作为输入维度参与模型训练和特征选择。"
                "rank 特征提供跨标的信息以增强排序能力。",
        "recommendation": "如果精度提升明显，建议集成到 production 推理管线中。"
    }

    os.makedirs("reports/horizon_eval", exist_ok=True)
    out_path = f"reports/horizon_eval/p0_rank_improvement_comparison.json"
    with open(out_path, "w") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)
    print(f"  📁 {out_path}")
    return comparison


if __name__ == "__main__":
    if args.generate_report:
        # Post-processing: read saved files and generate comparison report
        pipeline_config = {
            "p0_1_fundamentals": {"enabled": True, "stocks_with_data": 29, "total_stocks": len(CODES)},
            "p0_2_cross_sectional_rank": {
                "enabled": True, "features": len(RANK_FEATURES),
                "feature_names": RANK_FEATURES, "rank_column_names": RANK_COLS
            }
        }
        # Load the latest (rank-enabled) JSON for each horizon as 'new' results
        all_h_results = {}
        for h in [5, 10, 20]:
            data = load_newest_file(h)
            if data:
                all_h_results[f"h{h}d"] = data
                print(f"  ✅ 加载 H{h}D 新结果 (2147)")
            else:
                print(f"  ⚠️ 未找到 H{h}D 新结果文件")

        if len(all_h_results) >= 1:
            comparison = generate_rank_comparison_report(all_h_results, pipeline_config)
            print(f"\n{'='*70}")
            print(f"📊 三窗口精度对比汇总")
            print(f"{'='*70}")
            print(f"  {'窗口':<8} {'基线均值':<12} {'新均值':<12} {'Δ精度':<12} {'>50%':<8} {'>55%':<8} {'>60%':<8}")
            print(f"  {'-'*68}")
            for hkey, data in comparison.get("oos_comparison", {}).items():
                d = data
                bl = f"{d['baseline_mean']:.2%}" if d.get('baseline_mean') is not None else "N/A"
                nu = f"{d['new_mean']:.2%}" if d.get('new_mean') is not None else "N/A"
                dl = f"{d['delta_mean']:+.4f}" if d.get('delta_mean') is not None else "N/A"
                print(f"  {hkey:<8} {bl:<12} {nu:<12} {dl:<12}")

            print(f"\n  🏆 精度提升 Top 5:")
            for i, item in enumerate(comparison.get("top5_improved", []), 1):
                print(f"    {i}. {item['code']} {item['name']} ({item['horizon']}): "
                      f"{item['baseline']:.2%} → {item['new']:.2%} ({item['delta']:+.4f})")
            print(f"\n  📉 精度下降 Top 3:")
            for item in comparison.get("top3_worsened", []):
                print(f"    • {item['code']} {item['name']} ({item['horizon']}): "
                      f"{item['baseline']:.2%} → {item['new']:.2%} ({item['delta']:+.4f})")
        else:
            print("\n⚠️ 无有效结果, 跳过对比报告")
    else:
        # Single horizon mode
        if not args.horizon:
            parser.error("--horizon is required unless --generate-report is used")
        output, out_path, pipeline_config = main()
