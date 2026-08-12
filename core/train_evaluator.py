#!/usr/bin/env python3
"""
core/train_evaluator.py — 训练模型评估器 v4.5.1
P1改进: 统一输出R²、方向准确率、夏普比率等核心评估指标到训练报告

用法:
    from core.train_evaluator import evaluate_model, save_training_report
    
    metrics = evaluate_model(model, X_test, y_test, df_test)
    save_training_report(symbol, metrics, "reports/predictor/")
"""

import json
import os
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional
from datetime import datetime
from sklearn.metrics import r2_score, mean_squared_error, mean_absolute_error
import logging

logger = logging.getLogger(__name__)


def evaluate_model(model, X_test: np.ndarray, y_test: np.ndarray,
                   df_test: pd.DataFrame = None,
                   feature_names: list = None) -> Dict[str, Any]:
    """全面评估模型性能
    
    Args:
        model: 训练好的模型 (需有 predict 方法)
        X_test: 测试集特征
        y_test: 测试集标签 (实际值)
        df_test: 测试集原始DataFrame (可选, 用于计算夏普等)
        feature_names: 特征名称列表
        
    Returns:
        评估指标字典
    """
    metrics = {"timestamp": datetime.now().isoformat()}
    
    try:
        y_pred = model.predict(X_test)
        
        # 基础回归指标
        metrics["r2"] = round(float(r2_score(y_test, y_pred)), 4)
        metrics["mse"] = round(float(mean_squared_error(y_test, y_pred)), 6)
        metrics["rmse"] = round(float(np.sqrt(mean_squared_error(y_test, y_pred))), 4)
        metrics["mae"] = round(float(mean_absolute_error(y_test, y_pred)), 4)
        
        # 方向准确率 (预测涨跌方向是否正确)
        if len(y_test) > 1:
            actual_dir = np.sign(np.diff(y_test, prepend=y_test[0]))
            pred_dir = np.sign(np.diff(y_pred, prepend=y_pred[0]))
            direction_match = (actual_dir == pred_dir).sum()
            metrics["direction_accuracy"] = round(float(direction_match / len(y_test)), 4)
        else:
            metrics["direction_accuracy"] = 0.0
        
        # 样本外夏普比率 (如果提供了原始DataFrame)
        if df_test is not None and len(y_pred) > 0:
            returns = pd.Series(y_pred).pct_change().dropna()
            if len(returns) > 1 and returns.std() > 0:
                sharpe = float(np.sqrt(252) * returns.mean() / returns.std())
                metrics["sharpe_ratio"] = round(sharpe, 4)
            else:
                metrics["sharpe_ratio"] = 0.0
        
        # 最大回撤
        if len(y_pred) > 1:
            cummax = np.maximum.accumulate(y_pred)
            drawdowns = (y_pred - cummax) / np.maximum(cummax, 1e-9)
            metrics["max_drawdown"] = round(float(np.min(drawdowns)), 4)
        else:
            metrics["max_drawdown"] = 0.0
        
        # 预测偏差统计
        errors = y_test - y_pred
        metrics["mean_error"] = round(float(np.mean(errors)), 4)
        metrics["error_std"] = round(float(np.std(errors)), 4)
        metrics["prediction_bias"] = round(float(np.mean(y_pred) - np.mean(y_test)), 4)
        
        # 特征重要性 (如果模型支持)
        if hasattr(model, 'feature_importances_') and feature_names:
            importances = model.feature_importances_
            top_idx = np.argsort(importances)[-10:][::-1]
            metrics["top_features"] = [
                {"name": feature_names[i], "importance": round(float(importances[i]), 4)}
                for i in top_idx if i < len(feature_names)
            ]
        
        # 数据信息
        metrics["test_samples"] = len(y_test)
        metrics["feature_count"] = X_test.shape[1] if len(X_test.shape) > 1 else 1
        
    except Exception as e:
        logger.error(f"模型评估失败: {e}")
        metrics["error"] = str(e)
    
    return metrics


def walk_forward_evaluate(X: np.ndarray, y: np.ndarray,
                         n_splits: int = 3,
                         model_class=None,
                         **model_kwargs) -> Dict[str, Any]:
    """Walk-Forward 时序交叉验证 (Gap 3)

    使用 expanding window 逐步扩大训练集，更真实地模拟实际交易场景。
    替代单一 80/20 划分，避免时间点选择偏差。

    Args:
        X: 特征矩阵 (按时间顺序)
        y: 目标向量
        n_splits: 分割次数 (默认3)
        model_class: 模型类 (默认 LightGBM)
        **model_kwargs: 模型参数

    Returns:
        {"mean_accuracy": float, "fold_accuracies": [...], "stable": bool}
    """
    import lightgbm as lgb

    if model_class is None:
        model_class = lgb.LGBMRegressor

    if len(X) < 150 or n_splits < 2:
        return {
            "mean_accuracy": 0.0,
            "fold_accuracies": [],
            "stable": False,
            "error": "样本不足 (需要>=150)"
        }

    n = len(X)
    fold_accuracies = []
    fold_r2 = []

    # Expanding window: 每次用前k份训练，第k+1份测试
    for split in range(1, n_splits + 1):
        test_start = int(n * (split / (n_splits + 1)))
        if test_start >= n - 20:
            break

        X_tr = X[:test_start]
        y_tr = y[:test_start]
        X_te = X[test_start:]
        y_te = y[test_start:]

        if len(X_tr) < 100 or len(X_te) < 20:
            continue

        try:
            defaults = dict(n_estimators=100, max_depth=4, num_leaves=31,
                          learning_rate=0.025, random_state=42, verbose=-1, n_jobs=-1)
            defaults.update(model_kwargs)
            model = model_class(**defaults)
            model.fit(X_tr, y_tr)
            y_pred = model.predict(X_te)

            # 方向精度
            dir_acc = np.mean((y_pred > 0) == (y_te > 0))
            fold_accuracies.append(round(float(dir_acc), 4))

            # R²
            try:
                from sklearn.metrics import r2_score
                r2 = r2_score(y_te, y_pred)
                fold_r2.append(round(float(r2), 4))
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Walk-Forward fold {split} 失败: {e}")

    if not fold_accuracies:
        return {
            "mean_accuracy": 0.0,
            "fold_accuracies": [],
            "stable": False,
            "error": "所有fold失败"
        }

    mean_acc = float(np.mean(fold_accuracies))
    # 稳定性: 各fold精度标准差 < 5%
    stable = float(np.std(fold_accuracies)) < 0.05 if len(fold_accuracies) > 1 else False

    return {
        "mean_accuracy": round(mean_acc, 4),
        "fold_accuracies": fold_accuracies,
        "mean_r2": round(float(np.mean(fold_r2)), 4) if fold_r2 else None,
        "stable": stable,
        "n_folds": len(fold_accuracies),
    }


def quality_check(metrics: Dict[str, Any], wf_result: Dict[str, Any] = None) -> Dict[str, Any]:
    """质量检查 — 判断模型是否达到上线标准
    
    Returns:
        {"passed": bool, "warnings": [...], "errors": [...]}
    """
    result = {"passed": True, "warnings": [], "errors": []}
    
    r2 = metrics.get("r2", 0)
    acc = metrics.get("direction_accuracy", 0)
    sharpe = metrics.get("sharpe_ratio", 0)
    samples = metrics.get("test_samples", 0)
    features = metrics.get("feature_count", 0)
    
    # R²检查
    if r2 < 0:
        result["errors"].append(f"R²={r2}<0, 模型为无效模型(不如瞎猜)")
        result["passed"] = False
    elif r2 < 0.1:
        result["warnings"].append(f"R²={r2}偏低, 模型解释力不足")
    
    # 方向准确率检查
    if acc < 0.53:
        result["warnings"].append(f"方向准确率={acc:.1%}<53%, 低于最低阈值")
        result["passed"] = False
    elif acc < 0.55:
        result["warnings"].append(f"方向准确率={acc:.1%}接近阈值, 建议持续监控")
    
    # 夏普比率检查
    if sharpe < 0:
        result["warnings"].append(f"夏普比率={sharpe}<0, 策略可能亏钱")
    
    # 过拟合检查: 特征数/样本数不应该太高
    if samples > 0 and features / samples > 0.1:
        result["warnings"].append(
            f"特征数({features})/样本数({samples})={features/samples:.2f}>0.1, 存在过拟合风险"
        )

    # Phase 4: Walk-Forward 验证检查 (Gap 3)
    if wf_result and not wf_result.get("error"):
        wf_acc = wf_result.get("mean_accuracy", 0)
        wf_stable = wf_result.get("stable", False)
        if wf_acc < 0.50:
            result["errors"].append(
                f"Walk-Forward CV精度={wf_acc:.1%}<50%, 模型不可靠"
            )
            result["passed"] = False
        elif wf_acc < 0.53:
            result["warnings"].append(
                f"Walk-Forward CV精度={wf_acc:.1%}偏低 (<53%)"
            )
        if not wf_stable:
            result["warnings"].append(
                f"Walk-Forward CV不稳定 (σ>=5%), 跨时期表现波动大"
            )

    return result


def save_training_report(symbol: str, metrics: Dict[str, Any],
                         quality: Dict[str, Any],
                         output_dir: str = "reports/predictor/",
                         model_path: str = "",
                         data_points: int = 0,
                         feature_count: int = 0,
                         models_trained: list = None,
                         models_saved: list = None) -> str:
    """保存完整的训练报告JSON
    
    Args:
        symbol: 股票代码
        metrics: 评估指标
        quality: 质量检查结果
        output_dir: 输出目录
        model_path: 模型保存路径
        data_points: 训练数据量
        feature_count: 特征数量
        models_trained: 训练过的模型列表
        models_saved: 保存的模型列表
        
    Returns:
        报告文件路径
    """
    os.makedirs(output_dir, exist_ok=True)
    
    report = {
        "symbol": symbol,
        "timestamp": datetime.now().isoformat(),
        "model_path": model_path,
        "data_points": data_points,
        "feature_count": feature_count,
        "models_trained": models_trained or [],
        "models_saved": models_saved or [],
        "metrics": metrics,
        "quality_check": quality,
        "test_samples": metrics.get("test_samples", 0),
    }
    
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = os.path.join(output_dir, f"training_{timestamp_str}.json")
    
    # 同时写入一个包含所有标的的汇总文件
    summary_path = os.path.join(output_dir, f"summary_{datetime.now().strftime('%Y%m%d')}.json")
    summary = {}
    if os.path.exists(summary_path):
        try:
            with open(summary_path, 'r') as f:
                summary = json.load(f)
        except Exception:
            pass
    
    summary[symbol] = {
        "r2": metrics.get("r2"),
        "direction_accuracy": metrics.get("direction_accuracy"),
        "sharpe_ratio": metrics.get("sharpe_ratio"),
        "quality_passed": quality.get("passed"),
        "warnings": quality.get("warnings", []),
        "errors": quality.get("errors", [])
    }
    
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    logger.info(f"训练报告已保存: {output_path}")
    logger.info(f"汇总报告已更新: {summary_path}")
    
    return output_path


def print_evaluation_report(symbol: str, metrics: Dict[str, Any],
                            quality: Dict[str, Any]):
    """打印格式化的评估报告"""
    print("\n" + "=" * 60)
    print(f"📊 模型评估报告 — {symbol}")
    print("=" * 60)
    
    r2 = metrics.get("r2", 0)
    acc = metrics.get("direction_accuracy", 0)
    sharpe = metrics.get("sharpe_ratio", 0)
    mdd = metrics.get("max_drawdown", 0)
    samples = metrics.get("test_samples", 0)
    features = metrics.get("feature_count", 0)
    bias = metrics.get("prediction_bias", 0)
    
    # 评分标记
    def grade(val, thresholds):
        if val >= thresholds[0]: return "🟢"
        elif val >= thresholds[1]: return "🟡"
        else: return "🔴"
    
    print(f"  R² (决定系数):      {r2:.4f}  {grade(r2, [0.3, 0.1])}")
    print(f"  方向准确率:          {acc:.2%}  {grade(acc, [0.55, 0.53])}")
    print(f"  样本外夏普比率:      {sharpe:.2f}  {grade(sharpe, [1.5, 0.5])}")
    print(f"  最大回撤:            {mdd:.2%}")
    print(f"  测试样本数:          {samples}")
    print(f"  特征数量:            {features}")
    print(f"  预测偏差:            {bias:.4f}")
    print(f"  RMSE:                {metrics.get('rmse', 'N/A')}")
    print(f"  MAE:                 {metrics.get('mae', 'N/A')}")
    
    if metrics.get("top_features"):
        print(f"\n  📌 Top 5 重要特征:")
        for i, f in enumerate(metrics["top_features"][:5], 1):
            print(f"    {i}. {f['name']}: {f['importance']:.4f}")
    
    print(f"\n  🏥 质量检查: {'✅ 通过' if quality['passed'] else '❌ 未通过'}")
    for w in quality.get("warnings", []):
        print(f"    ⚠️  {w}")
    for e in quality.get("errors", []):
        print(f"    ❌ {e}")
    
    print("=" * 60)
