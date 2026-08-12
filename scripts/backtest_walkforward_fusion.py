#!/usr/bin/env python3
"""
DSL v4.5.6 Walk-Forward OOS 三窗口信号融合评估

方法:
- 对每只标的在滚动窗口(WINDOW_DAYS=90)进行 Walk-Forward
- 每个窗口并行训练 3 个 LightGBM 模型 (H5D/H10D/H20D)
- 每只每日用 fused_signal() 多数投票得到融合信号
- 以 20日实际收益方向为真实标签评估精度
- hold 信号(0)不计入方向精度

融合规则:
  3个窗口投票 ≥2 看涨 → buy(1)
            ≥2 看跌 → sell(-1)
            分歧    → hold(0)
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
    return fetch_fundamentals_mairui(code)
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb
import warnings
warnings.filterwarnings("ignore")


# ====== 截面排名字段 ======
RANK_FEATURES = ['mom_5d', 'mom_10d', 'mom_20d', 'vol_ratio_20', 'vol_change', 'atr14', 'rsi14', 'amplitude', 'turnover']
RANK_COLS = ['rank_' + f for f in RANK_FEATURES]

# ====== 全局参数 ======
HORIZONS = [5, 10, 20]
WINDOW_DAYS = 90
BACKTEST_DAYS = 730
MIN_TRAIN_SAMPLES = 200
MIN_OOS_PREDICTIONS = 50
TIMEOUT_GLOBAL = 2700   # 3x 单窗口超时
MAX_WORKERS = 3


# ====== 融合信号 ======
def fused_signal(h5d_pred, h10d_pred, h20d_pred):
    """
    三窗口多数投票融合。
    h5d_pred/h10d_pred/h20d_pred: 预测的未来收益(正=涨, 负=跌)
    返回: 1(buy/涨), -1(sell/跌), 0(hold)
    """
    up_votes = sum(1 for p in [h5d_pred, h10d_pred, h20d_pred] if p > 0)
    down_votes = sum(1 for p in [h5d_pred, h10d_pred, h20d_pred] if p < 0)

    if up_votes >= 2:
        return 1
    elif down_votes >= 2:
        return -1
    else:
        return 0


# ====== Step 1: 预加载 ======
def preload_all_data():
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

            feats['_date'] = pd.to_datetime(df['date']).dt.date

            # 确保所有 horizon 的 target 都存在
            for h in HORIZONS:
                tcol = f"target_{h}d"
                if tcol not in feats.columns:
                    close = df["close"].astype(float)
                    feats[tcol] = close.shift(-h) / close - 1

            all_data[code] = {'feats': feats, 'name': name}
            loaded += 1
            print(f"  ✅ {code} {name}: {len(df)}天 → {len(feats.columns)}维特征")

        except Exception as e:
            print(f"  ❌ {code} {name}: 异常 {e}")
            failed += 1

    print(f"\n  📊 预加载完成: {loaded}只成功, {failed}只失败")
    return all_data


# ====== Step 1.5: 截面特征 ======
def compute_cross_sectional_ranks(all_data):
    print(f"\n{'='*60}")
    print(f"📊 Step 1.5: 计算 Cross-sectional Rank 特征...")
    print(f"{'='*60}")

    codes = list(all_data.keys())
    if len(codes) < 3:
        print(f"  ⚠️ 可用标的数量不足 ({len(codes)}), 跳过截面计算")
        return all_data

    for code in codes:
        for col in RANK_COLS:
            all_data[code]['feats'][col] = -1.0

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

    for feat in RANK_FEATURES:
        col = 'rank_' + feat
        rank_series = panel.groupby('_date')[feat].rank(pct=True, method='average')
        panel[col] = rank_series.fillna(-1.0)

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

    total_cells = len(codes) * len(RANK_COLS)
    non_default = 0
    for code in codes:
        feats = all_data[code]['feats']
        for col in RANK_COLS:
            non_default += (feats[col] != -1.0).sum()
    print(f"  Rank 特征覆盖率: {non_default}/{total_cells} cells")
    return all_data


# ====== 单模型训练 ======
def train_horizon_model(feats, fcols, train_idx, horizon):
    """训练单个窗口的 LightGBM 模型"""
    tcol = f"target_{horizon}d"
    train_data = feats.loc[train_idx]
    target_valid = train_data[tcol].notna()
    if target_valid.sum() < MIN_TRAIN_SAMPLES:
        return None

    X_tr = train_data.loc[target_valid, fcols].values
    y_tr = train_data.loc[target_valid, tcol].values
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
        return (model, scaler, selector)
    except Exception:
        return None


# ====== 单只股票的融合评估 ======
def evaluate_stock_fusion(code: str, name: str, feats: pd.DataFrame) -> dict:
    """
    对单只股票执行三窗口融合 Walk-Forward OOS 精度评估。
    """
    t0 = time.time()
    try:
        # 验证所有 horizon 的 target 列
        for h in HORIZONS:
            tcol = f"target_{h}d"
            if tcol not in feats.columns or feats[tcol].notna().sum() < MIN_TRAIN_SAMPLES:
                return None

        fcols = [c for c in feats.columns if not c.startswith("target_") and not c.startswith("_")]
        feats[fcols] = feats[fcols].fillna(0)

        all_idx = feats.index.tolist()
        if len(all_idx) < BACKTEST_DAYS + MIN_TRAIN_SAMPLES:
            return None

        bt_start = len(all_idx) - BACKTEST_DAYS
        bt_indices = all_idx[bt_start:]

        # 统计：每个预测记录 (fused_signal, actual_20d_direction)
        all_records = []  # list of (fused_signal, actual_dir)

        # 窗口滚动
        for w in range(0, len(bt_indices), WINDOW_DAYS):
            w_end = min(w + WINDOW_DAYS, len(bt_indices))
            test_dates = bt_indices[w:w_end]

            if len(test_dates) < 5:
                continue

            train_end_pos = all_idx.index(test_dates[0])
            train_idx = all_idx[:train_end_pos]

            if len(train_idx) < MIN_TRAIN_SAMPLES:
                continue

            # 顺序训练 3 个 horizon 模型 (避免嵌套线程池的SIGSEGV)
            models = {}  # {horizon: (model, scaler, selector)}
            for h in HORIZONS:
                try:
                    result = train_horizon_model(feats, fcols, train_idx, h)
                    if result is not None:
                        models[h] = result
                except Exception:
                    pass

            if len(models) < 3:
                continue  # 某个 horizon 训练失败, 跳过该窗口

            # 预测测试期
            for test_idx in test_dates:
                if test_idx not in feats.index:
                    continue
                row = feats.loc[test_idx]
                actual_20d_val = row.get("target_20d", np.nan)
                if pd.isna(actual_20d_val):
                    continue

                # 获取 3 个模型的预测
                preds = {}
                for h in HORIZONS:
                    model, scaler, selector = models[h]
                    try:
                        X_test = scaler.transform([row[fcols].values])
                        X_test_sel = selector.transform(X_test)
                        preds[h] = float(model.predict(X_test_sel)[0])
                    except Exception:
                        break
                if len(preds) < 3:
                    continue

                signal = fused_signal(preds[5], preds[10], preds[20])
                actual_dir = 1 if actual_20d_val > 0 else -1
                all_records.append((signal, actual_dir))

        # 计算指标
        if len(all_records) < MIN_OOS_PREDICTIONS:
            return None

        # 分类统计
        buy_correct = 0
        buy_wrong = 0
        sell_correct = 0
        sell_wrong = 0
        hold_count = 0

        for signal, actual_dir in all_records:
            if signal == 1:  # buy
                if actual_dir == 1:
                    buy_correct += 1
                else:
                    buy_wrong += 1
            elif signal == -1:  # sell
                if actual_dir == -1:
                    sell_correct += 1
                else:
                    sell_wrong += 1
            else:  # hold
                hold_count += 1

        total_non_hold = buy_correct + buy_wrong + sell_correct + sell_wrong
        correct = buy_correct + sell_correct
        wrong = buy_wrong + sell_wrong
        accuracy = correct / total_non_hold if total_non_hold > 0 else 0

        # 信号分布
        signal_dist = {
            "buy": buy_correct + buy_wrong,
            "sell": sell_correct + sell_wrong,
            "hold": hold_count,
        }
        hold_ratio = hold_count / len(all_records) if len(all_records) > 0 else 0

        return {
            "direction_accuracy": round(accuracy, 4),
            "total_predictions": total_non_hold,
            "total_records": len(all_records),
            "hold_count": hold_count,
            "hold_ratio": round(hold_ratio, 4),
            "correct": correct,
            "wrong": wrong,
            "buy_accuracy": round(buy_correct / (buy_correct + buy_wrong), 4) if (buy_correct + buy_wrong) > 0 else 0,
            "sell_accuracy": round(sell_correct / (sell_correct + sell_wrong), 4) if (sell_correct + sell_wrong) > 0 else 0,
            "signal_distribution": signal_dist,
        }

    except Exception as e:
        return None


# ====== 主流程 ======
def main():
    start_time = time.time()

    pipeline_config = {
        "p0_1_fundamentals": {
            "enabled": True,
            "total_stocks": len(CODES),
        },
        "p0_2_cross_sectional_rank": {
            "enabled": True,
            "features": len(RANK_FEATURES),
            "feature_names": RANK_FEATURES,
            "rank_column_names": RANK_COLS,
        }
    }

    # Step 1: 预加载
    all_data = preload_all_data()
    pipeline_config["p0_1_fundamentals"]["stocks_with_data"] = len(all_data)

    if len(all_data) < 3:
        print("\n❌ 有效标的数量不足!")
        return None, None, None

    # Step 1.5: 截面特征
    all_data = compute_cross_sectional_ranks(all_data)

    # Step 2: 融合 Walk-Forward 并行评估
    print(f"\n{'='*60}")
    print(f"🎯 Step 2: 三窗口融合 Walk-Forward OOS 并行评估...")
    print(f"{'='*60}")

    results = {}
    evaluated_count = 0
    failed_count = 0

    eval_list = [(code, all_data[code]['name'], all_data[code]['feats'])
                 for code in all_data]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for code, name, feats in eval_list:
            future = executor.submit(evaluate_stock_fusion, code, name, feats.copy())
            futures[future] = code

        try:
            for future in as_completed(futures, timeout=TIMEOUT_GLOBAL):
                code = futures[future]
                try:
                    result = future.result(timeout=5)
                    if result:
                        results[code] = result
                        evaluated_count += 1
                        acc = result["direction_accuracy"]
                        hr = result.get("hold_ratio", 0)
                        bar = "✅" if acc > 0.5 else "⚠️"
                        print(f"  {bar} {code} {NAMES.get(code, '')}: "
                              f"acc={acc:.2%} n={result['total_predictions']} "
                              f"hold={hr:.1%} "
                              f"buy_acc={result['buy_accuracy']:.2%} "
                              f"sell_acc={result['sell_accuracy']:.2%}")
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
        return None, None, None

    accuracies = [v["direction_accuracy"] for v in results.values()]
    total_preds = [v["total_predictions"] for v in results.values()]
    hold_ratios = [v.get("hold_ratio", 0) for v in results.values()]
    buy_accs = [v.get("buy_accuracy", 0) for v in results.values()]
    sell_accs = [v.get("sell_accuracy", 0) for v in results.values()]

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

    # 加载 H20D baseline (最新结果)
    h20d_baseline = None
    eval_dir = "reports/horizon_eval"
    h20d_files = sorted([f for f in os.listdir(eval_dir)
                         if f.startswith("evaluation_h20d_") and f.endswith(".json")])
    if h20d_files:
        with open(os.path.join(eval_dir, h20d_files[-1]), "r") as f:
            h20d_data = json.load(f)
        h20d_baseline = h20d_data.get("summary", {}).get("mean_accuracy", None)

    fusion_mean = float(np.mean(accuracies))
    improvement = round(fusion_mean - h20d_baseline, 4) if h20d_baseline else None

    summary = {
        "stocks_evaluated": evaluated_count,
        "stocks_failed": failed_count,
        "mean_accuracy": round(fusion_mean, 4),
        "median_accuracy": round(float(np.median(accuracies)), 4),
        "std_accuracy": round(float(np.std(accuracies)), 4),
        "min_accuracy": round(float(np.min(accuracies)), 4),
        "max_accuracy": round(float(np.max(accuracies)), 4),
        "stocks_above_50pct": sum(1 for a in accuracies if a > 0.50),
        "stocks_above_55pct": sum(1 for a in accuracies if a > 0.55),
        "stocks_above_60pct": sum(1 for a in accuracies if a > 0.60),
        "total_oos_predictions": int(np.sum(total_preds)),
        "mean_hold_ratio": round(float(np.mean(hold_ratios)), 4),
        "mean_buy_accuracy": round(float(np.mean(buy_accs)), 4),
        "mean_sell_accuracy": round(float(np.mean(sell_accs)), 4),
        "vs_h20d_baseline": h20d_baseline,
        "improvement_vs_h20d": improvement,
        "accuracy_distribution": dist,
        "elapsed_seconds": round(elapsed, 1),
    }

    output = {
        "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "method": "fusion_walk_forward_oos",
        "fusion_rule": "majority_voting_2_of_3",
        "ground_truth": "20d_direction",
        "pipeline_config": pipeline_config,
        "params": {
            "horizons": HORIZONS,
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
    out_path = f"reports/horizon_eval/evaluation_fusion_oos_{timestamp}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"📊 三窗口融合 Walk-Forward OOS 评估完成 ({elapsed:.0f}s)")
    print(f"{'='*60}")
    print(f"  评估标的: {summary['stocks_evaluated']}只 (失败{summary['stocks_failed']}只)")
    print(f"  OOS总预测: {summary['total_oos_predictions']:,} (非hold)")
    print(f"  均值精度: {summary['mean_accuracy']:.2%}")
    print(f"  中位数:   {summary['median_accuracy']:.2%}")
    print(f"  标准差:   {summary['std_accuracy']:.2%}")
    print(f"  最高:     {summary['max_accuracy']:.2%}")
    print(f"  最低:     {summary['min_accuracy']:.2%}")
    print(f"  >50%:     {summary['stocks_above_50pct']}只")
    print(f"  >55%:     {summary['stocks_above_55pct']}只")
    print(f"  >60%:     {summary['stocks_above_60pct']}只")
    print(f"  平均hold比率: {summary['mean_hold_ratio']:.1%}")
    print(f"  平均买入精度: {summary['mean_buy_accuracy']:.2%}")
    print(f"  平均卖出精度: {summary['mean_sell_accuracy']:.2%}")
    if improvement is not None:
        imp_str = f"+{improvement:.2%}" if improvement > 0 else f"{improvement:.2%}"
        print(f"  H20D基线均值: {h20d_baseline:.2%}")
        print(f"  融合增益:     {imp_str}")
    print(f"\n  精度分布:")
    for k, v in dist.items():
        bar = "█" * v
        pct_label = k.replace("_", "-").replace("below", "<").replace("plus", "+")
        print(f"    {pct_label}: {v:3d} {bar}")
    print(f"\n  📁 {out_path}")
    return output, out_path, results


# ====== 对比报告 ======
def generate_fusion_comparison(fusion_output, fusion_results):
    """生成融合 vs 单窗口对比报告"""
    print(f"\n{'='*60}")
    print(f"📝 生成融合 vs 单窗口对比报告...")
    print(f"{'='*60}")

    # 加载最新的单窗口结果
    eval_dir = "reports/horizon_eval"
    single_window = {}  # {horizon: {code: accuracy}}

    # 也加载对比用的 H5D/H10D per_stock 数据
    h5d_data = None
    h10d_data = None
    h20d_data = None

    for h in HORIZONS:
        files = sorted([f for f in os.listdir(eval_dir)
                        if f.startswith(f"evaluation_h{h}d_") and f.endswith(".json")])
        if files:
            with open(os.path.join(eval_dir, files[-1]), "r") as f:
                data = json.load(f)
            single_window[h] = {
                "mean_accuracy": data.get("summary", {}).get("mean_accuracy", 0),
                "median_accuracy": data.get("summary", {}).get("median_accuracy", 0),
                "per_stock": {code: info.get("direction_accuracy", 0)
                              for code, info in data.get("per_stock", {}).items()}
            }

    # 获取 H20D per_stock 的统计数据 (buy/sell acc)
    h20d_per_stock = {}
    if h20d_files := sorted([f for f in os.listdir(eval_dir)
                             if f.startswith("evaluation_h20d_") and f.endswith(".json")]):
        with open(os.path.join(eval_dir, h20d_files[-1]), "r") as f:
            h20d_data_full = json.load(f)
        for code, info in h20d_data_full.get("per_stock", {}).items():
            h20d_per_stock[code] = {
                "accuracy": info.get("direction_accuracy", 0),
                "total_predictions": info.get("total_predictions", 0),
                "tpr": info.get("true_positive_rate", 0),
                "tnr": info.get("true_negative_rate", 0),
            }

    # 融合各标的精度
    fusion_per_stock = {}
    for code, info in fusion_results.items():
        diff = None
        if code in h20d_per_stock:
            diff = round(info["direction_accuracy"] - h20d_per_stock[code]["accuracy"], 4)
        fusion_per_stock[code] = {
            "fusion_accuracy": info["direction_accuracy"],
            "h20d_accuracy": h20d_per_stock.get(code, {}).get("accuracy"),
            "delta_vs_h20d": diff,
            "total_non_hold": info["total_predictions"],
            "hold_ratio": info.get("hold_ratio", 0),
            "signal_distribution": info.get("signal_distribution", {}),
        }

    # 按融合增益排序
    sorted_by_improvement = sorted(
        [(code, v) for code, v in fusion_per_stock.items() if v["delta_vs_h20d"] is not None],
        key=lambda x: x[1]["delta_vs_h20d"], reverse=True
    )

    top10_improved = []
    for code, v in sorted_by_improvement[:10]:
        top10_improved.append({
            "code": code,
            "name": NAMES.get(code, code),
            "fusion_accuracy": v["fusion_accuracy"],
            "h20d_accuracy": v["h20d_accuracy"],
            "delta": v["delta_vs_h20d"],
        })

    top5_worsened = []
    for code, v in sorted_by_improvement[-5:]:
        if v["delta_vs_h20d"] < 0:
            top5_worsened.insert(0, {
                "code": code,
                "name": NAMES.get(code, code),
                "fusion_accuracy": v["fusion_accuracy"],
                "h20d_accuracy": v["h20d_accuracy"],
                "delta": v["delta_vs_h20d"],
            })

    # 信号分布统计
    total_signals = {"buy": 0, "sell": 0, "hold": 0}
    for code, v in fusion_results.items():
        sd = v.get("signal_distribution", {})
        total_signals["buy"] += sd.get("buy", 0)
        total_signals["sell"] += sd.get("sell", 0)
        total_signals["hold"] += sd.get("hold", 0)

    total_records = sum(total_signals.values())
    signal_ratios = {
        k: round(v / total_records, 4) if total_records > 0 else 0
        for k, v in total_signals.items()
    }

    # 单窗口均值对比
    h_comparison = {}
    for h in HORIZONS:
        hkey = f"h{h}d"
        if h in single_window:
            h_comparison[hkey] = {
                "mean_accuracy": single_window[h]["mean_accuracy"],
                "median_accuracy": single_window[h]["median_accuracy"],
            }

    fusion_mean = fusion_output["summary"]["mean_accuracy"]
    fusion_median = fusion_output["summary"]["median_accuracy"]

    comparison = {
        "report_metadata": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": "v4.5.6",
            "method": "三窗口融合 vs 单窗口对比",
            "fusion_rule": "majority_voting_2_of_3",
            "ground_truth": "20日实际收益方向",
        },
        "horizon_comparison": {
            **h_comparison,
            "fusion": {
                "mean_accuracy": fusion_mean,
                "median_accuracy": fusion_median,
                "method": "H5D+H10D+H20D majority voting",
            }
        },
        "vs_h20d_baseline": {
            "h20d_mean": fusion_output["summary"]["vs_h20d_baseline"],
            "fusion_mean": fusion_mean,
            "improvement": fusion_output["summary"]["improvement_vs_h20d"],
        },
        "per_stock_fusion": fusion_per_stock,
        "top10_improved": top10_improved,
        "top5_worsened": top5_worsened,
        "signal_distribution": {
            "raw_counts": total_signals,
            "ratios": signal_ratios,
        },
        "fusion_summary": fusion_output["summary"],
    }

    out_path = "reports/horizon_eval/fusion_comparison.json"
    with open(out_path, "w") as f:
        json.dump(comparison, f, ensure_ascii=False, indent=2)
    print(f"  📁 {out_path}")

    # 打印对比总结
    print(f"\n{'='*70}")
    print(f"📊 三窗口融合 vs 单窗口精度对比")
    print(f"{'='*70}")
    print(f"  {'指标':<12} {'H5D':<10} {'H10D':<10} {'H20D':<10} {'融合':<10}")
    print(f"  {'-'*52}")
    for h in HORIZONS:
        hkey = f"h{h}d"
        if hkey in h_comparison:
            v = h_comparison[hkey]
            print(f"  {'均值精度':<12} {v['mean_accuracy']:.2%}")

    print(f"  {'均值精度':<12} {'':<10} {'':<10} {'':<10} {fusion_mean:.2%}")
    print(f"  {'中位数':<12} {'':<10} {'':<10} {'':<10} {fusion_median:.2%}")
    if fusion_output["summary"]["improvement_vs_h20d"] is not None:
        imp = fusion_output["summary"]["improvement_vs_h20d"]
        imp_str = f"+{imp:.2%}" if imp > 0 else f"{imp:.2%}"
        print(f"  {'vs H20D增益':<12} {'':<10} {'':<10} {'':<10} {imp_str}")

    print(f"\n  📊 信号分布 (全体):")
    print(f"    买入: {total_signals['buy']:>5} ({signal_ratios['buy']:.1%})")
    print(f"    卖出: {total_signals['sell']:>5} ({signal_ratios['sell']:.1%})")
    print(f"    Hold: {total_signals['hold']:>5} ({signal_ratios['hold']:.1%})")

    print(f"\n  🏆 融合改善 Top 10:")
    for i, item in enumerate(top10_improved, 1):
        print(f"    {i:2d}. {item['code']} {item['name']}: "
              f"{item['h20d_accuracy']:.2%} → {item['fusion_accuracy']:.2%} ({item['delta']:+.4f})")

    if top5_worsened:
        print(f"\n  📉 融合恶化 Top 5:")
        for item in top5_worsened:
            print(f"    • {item['code']} {item['name']}: "
                  f"{item['h20d_accuracy']:.2%} → {item['fusion_accuracy']:.2%} ({item['delta']:+.4f})")

    return comparison


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="三窗口信号融合 Walk-Forward OOS 评估")
    parser.add_argument("--window-days", type=int, default=90, help="滚动窗口天数")
    parser.add_argument("--backtest-days", type=int, default=730, help="回测总天数")
    parser.add_argument("--max-workers", type=int, default=3, help="并行线程数")
    parser.add_argument("--timeout-global", type=int, default=2700, help="全局超时秒数")
    parser.add_argument("--generate-report", action="store_true", help="仅从已保存结果生成对比报告")
    args = parser.parse_args()

    # 覆盖全局参数
    WINDOW_DAYS = args.window_days
    BACKTEST_DAYS = args.backtest_days
    MAX_WORKERS = args.max_workers
    TIMEOUT_GLOBAL = args.timeout_global

    # 加载股票池
    with open("config/master_stock_pool.yaml") as f:
        raw_pool = yaml.safe_load(f)["master_pool"]
    pool = [s for s in raw_pool if "." not in s["symbol"]]
    CODES = [s["symbol"] for s in pool]
    NAMES = {s["symbol"]: s.get("name", s["symbol"]) for s in pool}

    if args.generate_report:
        # 仅生成对比报告
        eval_dir = "reports/horizon_eval"
        fusion_files = sorted([f for f in os.listdir(eval_dir)
                               if f.startswith("evaluation_fusion_") and f.endswith(".json")])
        if not fusion_files:
            print("❌ 未找到融合评估结果文件")
            sys.exit(1)

        with open(os.path.join(eval_dir, fusion_files[-1]), "r") as f:
            fusion_output = json.load(f)
        fusion_results = fusion_output.get("per_stock", {})

        if not fusion_results:
            print("❌ 融合评估结果无有效数据")
            sys.exit(1)

        comparison = generate_fusion_comparison(fusion_output, fusion_results)
        print(f"\n✅ 对比报告已生成")
    else:
        output, out_path, results = main()
        if output and results:
            comparison = generate_fusion_comparison(output, results)
            print(f"\n✅ 融合评估 + 对比报告完成!")
