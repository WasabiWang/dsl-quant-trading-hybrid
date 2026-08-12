#!/usr/bin/env python3
"""
evaluate_h20d.py — DSL h20d (20日预测) LightGBM 模型方向精度评估

对 master_stock_pool.yaml 中每只标的做滚动回测：
  1. 获取历史K线（至少800天，保证足够样本）
  2. 复用 batch_predict.py 的 build_h20d_features() 做特征工程
  3. 对每个时间点滚动预测未来20日收益方向
  4. 对比实际方向计算 direction_accuracy

输出:
  reports/h20d_evaluation/evaluation_h20d_{timestamp}.json  — 完整评估报告
  reports/h20d_evaluation/h20d_vs_h5d_comparison.json       — h20d vs h5d 精度对比
"""
import os, sys, json, gc, warnings
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
import pandas as pd
import yaml
import joblib

warnings.filterwarnings("ignore")

POOL_PATH = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
REPORT_DIR = os.path.join(PROJECT_ROOT, "reports", "h20d_evaluation")
os.makedirs(REPORT_DIR, exist_ok=True)

# 从 batch_predict.py 复用 h20d 特征工程
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
from batch_predict import build_h20d_features
from dsl_data_sdk_original import get_kline


def get_stock_pool() -> list:
    """从 master_stock_pool.yaml 读取标的列表"""
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    stocks = []
    seen = set()
    for s in data.get("master_pool", []):
        sym = s.get("symbol", "")
        if sym and sym not in seen:
            seen.add(sym)
            stocks.append({
                "symbol": sym,
                "name": s.get("name", sym),
                "tier": s.get("tier", ""),
                "h20d_acc": s.get("h20d_acc", None),
            })
    return stocks


def load_h20d_model(code: str):
    """加载单只标的的 h20d 模型组件"""
    base = os.path.join(MODELS_DIR, code)
    model_path = os.path.join(base, "lightgbm_20d.pkl")
    scaler_path = os.path.join(base, "scaler_20d.pkl")
    selector_path = os.path.join(base, "selector_20d.pkl")
    if not all(os.path.exists(p) for p in [model_path, scaler_path, selector_path]):
        return None
    try:
        return {
            "model": joblib.load(model_path),
            "scaler": joblib.load(scaler_path),
            "selector": joblib.load(selector_path),
        }
    except Exception as e:
        print(f"  ⚠️ 加载模型失败 {code}: {e}")
        return None


def load_h5d_accuracy(code: str) -> float:
    """从 h5d 模型的 eval JSON 读取 direction_accuracy（ensemble_voting 优先）"""
    base = os.path.join(MODELS_DIR, code)
    # 优先 ensemble_voting
    ev_path = os.path.join(base, "ensemble_voting_evaluation.json")
    if os.path.exists(ev_path):
        try:
            with open(ev_path) as f:
                data = json.load(f)
            return data.get("direction_accuracy", None)
        except Exception:
            pass
    # 降级到 lightgbm
    lgb_path = os.path.join(base, "lightgbm_evaluation.json")
    if os.path.exists(lgb_path):
        try:
            with open(lgb_path) as f:
                data = json.load(f)
            return data.get("direction_accuracy", None)
        except Exception:
            pass
    return None


def evaluate_h20d_stock(code: str, bundle: dict) -> dict:
    """
    对单只标的执行 h20d 滚动回测评估。
    
    流程:
      1. 获取长周期 K 线 (≥700天)
      2. 用 build_h20d_features() 计算 54 维特征
      3. 对每个可用的时间点 t (需要至少 250 天 lookback)：
         a. 用截至 t 的滚动窗口做预测（用完整历史特征）
         b. 计算未来20日实际收益 close[t+20] / close[t] - 1
         c. 预测符号 vs 实际符号 → 方向判断
      4. 汇总 direction_accuracy
    """
    # 获取更多数据（至少800天，留足特征窗口和未来20日偏移）
    # 请求更多历史K线（至少1500天，充分覆盖滚动窗口）
    try:
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=1500)).strftime("%Y-%m-%d")
        raw = get_kline(code, start, end)
        if not raw or len(raw) < 500:
            return {"error": f"数据不足: {len(raw) if raw else 0}天 (< 500)"}
        df = pd.DataFrame(raw)
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
    except Exception as e:
        return {"error": f"K线获取失败: {e}"}
    
    if len(df) < 500:
        return {"error": f"数据不足: {len(df) if df is not None else 0}天"}
    
    df = df.sort_values("date").reset_index(drop=True)
    
    # 计算特征矩阵
    features = build_h20d_features(df)
    if features.empty:
        return {"error": "特征计算为空"}
    
    # 计算未来20日实际收益
    close = df["close"].astype(float).values
    future_return_20d = np.full(len(close), np.nan)
    for i in range(len(close) - 20):
        future_return_20d[i] = close[i + 20] / close[i] - 1
    
    # 找到第一个所有必要指标都非 NaN 的行
    # 特征需要最大窗口约 250 天（52周high等），加上额外 margin
    min_window = max(260, len(close) // 3)  # 至少260天或1/3数据
    
    model = bundle["model"]
    scaler = bundle["scaler"]
    selector = bundle["selector"]
    
    expected_features = scaler.mean_.shape[0]
    feature_array = features.values  # (T, N)
    
    predictions = []
    actuals = []
    timestamps = []
    
    # 滚动预测
    for t in range(min_window, len(close) - 20):
        # 取截至 t 的特征
        raw = feature_array[t].reshape(1, -1)
        
        # 检查特征维度并处理
        if raw.shape[1] != expected_features:
            if raw.shape[1] > expected_features:
                raw = raw[:, :expected_features]
            else:
                pad = np.zeros((1, expected_features - raw.shape[1]))
                raw = np.hstack([raw, pad])
        
        # 检查 NaN
        if np.any(np.isnan(raw)):
            continue
        
        try:
            X_scaled = scaler.transform(raw)
            X_selected = selector.transform(X_scaled)
            pred = model.predict(X_selected)[0]
        except Exception:
            continue
        
        actual = future_return_20d[t]
        if np.isnan(actual):
            continue
        
        predictions.append(float(pred))
        actuals.append(float(actual))
        timestamps.append(df.iloc[t]["date"])
    
    if len(predictions) < 100:
        return {"error": f"有效预测不足: {len(predictions)}次 (< 100)"}
    
    # 计算方向精度
    pred_sign = np.array([1 if p > 0 else 0 for p in predictions])
    actual_sign = np.array([1 if a > 0 else 0 for a in actuals])
    correct = int(np.sum(pred_sign == actual_sign))
    total = len(predictions)
    accuracy = round(correct / total, 4)
    
    return {
        "direction_accuracy": accuracy,
        "total_predictions": total,
        "correct": correct,
        "wrong": total - correct,
        "first_date": str(timestamps[0]),
        "last_date": str(timestamps[-1]),
    }


def compute_accuracy_distribution(results: dict) -> dict:
    """计算精度分布"""
    accs = [r["direction_accuracy"] for r in results.values() if "direction_accuracy" in r]
    return {
        "below_45": sum(1 for a in accs if a < 0.45),
        "45_50": sum(1 for a in accs if 0.45 <= a < 0.50),
        "50_55": sum(1 for a in accs if 0.50 <= a < 0.55),
        "55_60": sum(1 for a in accs if 0.55 <= a < 0.60),
        "60_plus": sum(1 for a in accs if a >= 0.60),
    }


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("=" * 60)
    print(f"🔮 h20d 方向精度评估 (LightGBM 20d)")
    print(f"评估时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 1. 读取股票池
    stocks = get_stock_pool()
    print(f"\n📋 股票池: {len(stocks)} 只标的")
    
    # 2. 加载模型
    results = {}
    h5d_accuracies = {}
    evaluated = 0
    failed = 0
    
    for stock in stocks:
        code = stock["symbol"]
        name = stock["name"]
        print(f"\n{'─' * 50}")
        print(f"📈 {code:6s} {name:8s} (tier={stock.get('tier','?')})")
        
        # 加载h5d精度（参考对比）
        h5d_acc = load_h5d_accuracy(code)
        if h5d_acc is not None:
            h5d_accuracies[code] = round(h5d_acc, 4)
            print(f"   h5d精度参考: {h5d_acc:.2%}")
        
        # 加载h20d模型
        bundle = load_h20d_model(code)
        if bundle is None:
            print(f"   ⏭️  无h20d模型，跳过")
            failed += 1
            results[code] = {
                "symbol": code,
                "name": name,
                "error": "无h20d模型",
            }
            continue
        
        # 评估
        result = evaluate_h20d_stock(code, bundle)
        if "error" in result:
            print(f"   ❌ {result['error']}")
            failed += 1
            results[code] = {
                "symbol": code,
                "name": name,
                "error": result["error"],
            }
        else:
            evaluated += 1
            acc = result["direction_accuracy"]
            print(f"   ✅ direction_accuracy: {acc:.2%}  ({result['correct']}/{result['total_predictions']})")
            result["symbol"] = code
            result["name"] = name
            results[code] = result
        
        # 清理内存
        gc.collect()
    
    # 3. 汇总统计
    valid_results = {k: v for k, v in results.items() if "direction_accuracy" in v}
    accs = [v["direction_accuracy"] for v in valid_results.values()]
    
    summary = {}
    if accs:
        summary = {
            "mean_accuracy": round(float(np.mean(accs)), 4),
            "median_accuracy": round(float(np.median(accs)), 4),
            "std_accuracy": round(float(np.std(accs)), 4),
            "min_accuracy": round(float(np.min(accs)), 4),
            "max_accuracy": round(float(np.max(accs)), 4),
            "stocks_above_50": sum(1 for a in accs if a >= 0.50),
            "stocks_above_55": sum(1 for a in accs if a >= 0.55),
            "stocks_above_60": sum(1 for a in accs if a >= 0.60),
        }
    
    dist = compute_accuracy_distribution(valid_results)
    
    report = {
        "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_stocks": len(stocks),
        "evaluated": evaluated,
        "failed": failed,
        "h5d_min_lookback_days": 260,
        "results": results,
        "summary": summary,
        "accuracy_distribution": dist,
    }
    
    # 4. 写入评估报告
    report_path = os.path.join(REPORT_DIR, f"evaluation_h20d_{timestamp}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n{'=' * 60}")
    print(f"📄 评估报告已保存: {report_path}")
    print(f"   ✓ 成功: {evaluated} / 失败: {failed}")
    if accs:
        print(f"   📊 mean_accuracy: {summary['mean_accuracy']:.2%}")
        print(f"   📊 median_accuracy: {summary['median_accuracy']:.2%}")
        print(f"   📊 min: {summary['min_accuracy']:.2%} / max: {summary['max_accuracy']:.2%}")
        print(f"   📊 精度≥50%: {summary['stocks_above_50']}只")
        print(f"   📊 精度≥55%: {summary['stocks_above_55']}只")
        print(f"   📊 精度≥60%: {summary['stocks_above_60']}只")
        print(f"   📊 分布: {dist}")
    
    # 5. 写入 h20d vs h5d 对比表
    comparison = []
    for code in results:
        r = results[code]
        name = r.get("name", code)
        h20d_acc = r.get("direction_accuracy", None)
        h5d_acc = h5d_accuracies.get(code, None)
        h20d_label = f"{h20d_acc:.2%}" if h20d_acc is not None else "N/A"
        h5d_label = f"{h5d_acc:.2%}" if h5d_acc is not None else "N/A"
        
        if h20d_acc is not None and h5d_acc is not None:
            diff = round(h20d_acc - h5d_acc, 4)
            better = "h20d" if diff > 0 else ("h5d" if diff < 0 else "tie")
        else:
            diff = None
            better = "N/A"
        
        comparison.append({
            "symbol": code,
            "name": name,
            "h20d_accuracy": h20d_acc,
            "h5d_accuracy": h5d_acc,
            "diff": diff,
            "better": better,
            "h20d_predictions": r.get("total_predictions", 0),
        })
    
    # 排序：按差值降序（h20d 优势最大在前）
    comparison.sort(key=lambda x: x["diff"] if x["diff"] is not None else -999, reverse=True)
    
    comp_result = {
        "evaluated_at": report["evaluated_at"],
        "total_stocks": len(comparison),
        "h20d_better": sum(1 for c in comparison if c["better"] == "h20d"),
        "h5d_better": sum(1 for c in comparison if c["better"] == "h5d"),
        "tie": sum(1 for c in comparison if c["better"] == "tie"),
        "comparison": comparison,
    }
    
    comp_path = os.path.join(REPORT_DIR, "h20d_vs_h5d_comparison.json")
    with open(comp_path, "w", encoding="utf-8") as f:
        json.dump(comp_result, f, indent=2, ensure_ascii=False)
    print(f"📄 h20d vs h5d 对比表: {comp_path}")
    
    print("=" * 60)


if __name__ == "__main__":
    main()
