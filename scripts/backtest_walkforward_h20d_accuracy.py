#!/usr/bin/env python3
"""
DSL v4.5.1 H20D Walk-Forward OOS Direction Accuracy
严格样本外方向精度统计 — 无交易模拟，无未来信息泄露

方法:
- 滚动窗口: 每90天一个窗口，用窗口前数据训练 LightGBM
- 对每个窗口的测试期，每天用模型预测未来20日收益方向
- 跨窗口汇总每只股票的 OOS direction_accuracy

对比: 全历史滚动精度 (train_predictor_enhanced.py 的 ens_acc 测试集精度)
"""
import os, sys, json, yaml, gc, time
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
from train_predictor_enhanced import build_features, fetch_fundamentals
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb
import warnings
warnings.filterwarnings("ignore")

# ====== 参数 ======
HORIZON = 20
BACKTEST_DAYS = 730       # 回测2年
WINDOW_DAYS = 90          # 每季度切换窗口
MIN_TRAIN_SAMPLES = 200   # 每只至少 200 训练样本才训练
MIN_OOS_PREDICTIONS = 50  # 每只至少 50 次 OOS 预测才计入
TIMEOUT_GLOBAL = 1200     # 全局超时秒数
MAX_WORKERS = 3           # 并行线程数

# ====== 加载股票池 ======
with open("config/master_stock_pool.yaml") as f:
    raw_pool = yaml.safe_load(f)["master_pool"]
pool = [s for s in raw_pool if "." not in s["symbol"]]
CODES = [s["symbol"] for s in pool]
NAMES = {s["symbol"]: s.get("name", s["symbol"]) for s in pool}
print(f"🚀 H20D Walk-Forward OOS 精度评估: {len(CODES)}只 × {BACKTEST_DAYS//WINDOW_DAYS}窗口")
print(f"   参数: horizon={HORIZON}d window={WINDOW_DAYS}d backtest={BACKTEST_DAYS}d")
print(f"   并行: {MAX_WORKERS}线程  最小训练样本: {MIN_TRAIN_SAMPLES}")


def evaluate_stock(code: str, name: str) -> dict:
    """对单只股票执行 walk-forward OOS 方向精度评估"""
    t0 = time.time()
    try:
        # 1. 拉取数据（4年+确保训练+回测数据充足）
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=int(BACKTEST_DAYS * 2.2))).strftime("%Y-%m-%d")

        kline = get_kline(normalize_symbol(code), start_date, end_date)
        if not kline or len(kline) < 350:
            return None

        df = pd.DataFrame(kline)
        for c in ["close", "volume", "high", "low", "open"]:
            df[c] = df[c].astype(float)

        # 2. 基本面
        fundamentals = fetch_fundamentals(code)

        # 3. 构建56维特征 + target_20d
        feats = build_features(df, fundamentals).replace([np.inf, -np.inf], np.nan)
        tcol = f"target_{HORIZON}d"
        fcols = [c for c in feats.columns if not c.startswith("target_")]

        if tcol not in feats.columns or feats[tcol].notna().sum() < MIN_TRAIN_SAMPLES:
            return None

        # 4. 特征NaN填充为0（滚动窗口初期无值→无偏离基线，LightGBM可自行处理但StandardScaler不能）
        feats[fcols] = feats[fcols].fillna(0)

        # 5. 所有行可用，只需target有效
        all_idx = feats.index.tolist()
        if len(all_idx) < BACKTEST_DAYS + MIN_TRAIN_SAMPLES:
            return None

        # 6. 回测期 = 最后 BACKTEST_DAYS 行
        bt_start = len(all_idx) - BACKTEST_DAYS
        bt_indices = all_idx[bt_start:]

        all_predictions = []
        next_report_pct = 10  # 进度报告

        # 7. 窗口滚动（约8个窗口）
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

            # 训练数据：所有行(tcol有效的才参与训练)
            train_data = feats.loc[train_idx]
            target_valid = train_data[tcol].notna()
            if target_valid.sum() < MIN_TRAIN_SAMPLES:
                continue

            X_tr = train_data.loc[target_valid, fcols].values
            y_tr = train_data.loc[target_valid, tcol].values

            # 80/20 按时间分割（与 backtest_walkforward.py 一致）
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

        # 8. 计算指标
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

    results = {}
    evaluated_count = 0
    failed_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for s in pool:
            code = s["symbol"]
            name = s.get("name", code)
            future = executor.submit(evaluate_stock, code, name)
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

    os.makedirs("reports/h20d_evaluation", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/h20d_evaluation/evaluation_h20d_oos_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"📊 H20D Walk-Forward OOS 方向精度评估完成 ({elapsed:.0f}s)")
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
    return output, out_path


if __name__ == "__main__":
    output, out_path = main()
