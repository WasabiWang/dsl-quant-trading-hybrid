#!/usr/bin/env python3
"""core/psi_monitor.py — 特征稳定性监控 v4.5.12
Population Stability Index (PSI): 检测特征分布漂移，A股市场风格切换快，关键监控。
PSI < 0.1: 稳定 | 0.1-0.25: 轻微漂移 | > 0.25: 显著漂移
"""
import os, json, warnings
from datetime import datetime
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bin_values(base: pd.Series, test: pd.Series, bins: int = 10) -> tuple:
    """等频分箱，计算每箱占比"""
    combined = pd.concat([base.dropna(), test.dropna()])
    if len(combined) < bins * 2:
        return None, None
    quantiles = np.linspace(0, 1, bins + 1)
    boundaries = np.quantile(combined, quantiles)
    boundaries[0] = -np.inf
    boundaries[-1] = np.inf

    base_pct = np.histogram(base.dropna(), bins=boundaries)[0] / max(len(base.dropna()), 1)
    test_pct = np.histogram(test.dropna(), bins=boundaries)[0] / max(len(test.dropna()), 1)
    return base_pct, test_pct


def calculate_psi(base: pd.Series, test: pd.Series, bins: int = 10) -> float:
    """计算单个特征的PSI值"""
    base_pct, test_pct = _bin_values(base, test, bins)
    if base_pct is None:
        return 0.0
    eps = 1e-10
    psi_values = []
    for b, t in zip(base_pct, test_pct):
        b_clip = max(b, eps)
        t_clip = max(t, eps)
        psi_values.append((t_clip - b_clip) * np.log(t_clip / b_clip))
    return float(np.sum(psi_values))


def calculate_psi_matrix(
    features_base: pd.DataFrame,
    features_test: pd.DataFrame,
    common_cols: Optional[List[str]] = None,
) -> Dict:
    """计算所有特征列的PSI矩阵"""
    if common_cols is None:
        common_cols = [c for c in features_base.columns if c in features_test.columns
                       and c not in ('symbol', 'date', 'trade_date', 'code')]

    results = {}
    for col in common_cols:
        try:
            psi = calculate_psi(features_base[col], features_test[col])
            level = 'stable' if psi < 0.1 else ('moderate' if psi < 0.25 else 'significant')
            results[col] = {'psi': round(psi, 4), 'level': level}
        except Exception:
            results[col] = {'psi': 0.0, 'level': 'error'}

    return results


def monitor_stock_features(
    symbol: str,
    train_features: pd.DataFrame,
    recent_features: pd.DataFrame,
    threshold: float = 0.25,
) -> dict:
    """监控单只股票的特征漂移

    Returns:
        {
            'symbol': str,
            'psi_values': {feature_name: psi_value},
            'drifting_features': [features exceeding threshold],
            'alert': bool  # True if any feature exceeds threshold
        }
    """
    psi_matrix = calculate_psi_matrix(train_features, recent_features)
    drifting = [k for k, v in psi_matrix.items() if v['psi'] >= threshold]

    return {
        'symbol': symbol,
        'timestamp': datetime.now().isoformat(),
        'psi_values': psi_matrix,
        'drifting_features': drifting,
        'alert': len(drifting) > 0,
        'max_psi': max((v['psi'] for v in psi_matrix.values()), default=0),
    }


def generate_psi_report(
    symbols: List[str],
    train_data: Dict[str, pd.DataFrame],
    recent_data: Dict[str, pd.DataFrame],
    output_path: Optional[str] = None,
) -> List[dict]:
    """批量生成PSI报告"""
    results = []
    for sym in symbols:
        if sym not in train_data or sym not in recent_data:
            continue
        result = monitor_stock_features(sym, train_data[sym], recent_data[sym])
        results.append(result)

        level_emoji = '🔴' if result['alert'] else '🟢'
        print(f"{level_emoji} {sym}: PSI_max={result['max_psi']:.4f}, "
              f"漂移特征={len(result['drifting_features'])}")

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)

    return results


def run_psi_check():
    """P2-7/P2-13: PSI监控入口，供cron调用

    流程:
    1. 读取master_stock_pool.yaml获取标地列表
    2. 对每只标的加载训练期特征和近期特征
    3. 计算PSI漂移, 记录告警
    4. 显著漂移的标的自动入retrain_queue
    """
    try:
        import yaml
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(pool_path) as f:
            pool = yaml.safe_load(f)
        symbols = [s["symbol"] for s in pool.get("master_pool", []) if s.get("symbol")]
        print(f"📊 PSI监控: {len(symbols)}只标的")

        # P2-13: 从 calibration 加载近期精度数据间接推断特征漂移
        # 完整实现需要加载特征数据，此处用精度趋势作为代理指标
        calib_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        drifting_stocks = []
        if os.path.exists(calib_path):
            with open(calib_path) as f:
                calib = json.load(f)
            sa = calib.get("stock_accuracy", {})
            for code, info in sa.items():
                accs = info.get("accuracies", [])
                if len(accs) >= 10:
                    recent = sum(accs[-5:]) / 5
                    early = sum(accs[:5]) / 5
                    if early - recent > 0.05 and recent < 0.55:
                        drifting_stocks.append({
                            "symbol": code,
                            "name": info.get("name", code),
                            "accuracy_drop": round(early - recent, 3),
                            "current_accuracy": round(recent, 3)
                        })
        if drifting_stocks:
            print(f"🔴 {len(drifting_stocks)}只精度趋势显著下降，可能特征漂移")
            for s in drifting_stocks[:5]:
                print(f"    {s['symbol']} {s['name']}: acc降{s['accuracy_drop']:.0%}→{s['current_accuracy']:.1%}")
            # 自动入retrain_queue
            try:
                from core.retrain_queue_manager import get_retrain_queue_manager
                mgr = get_retrain_queue_manager()
                for s in drifting_stocks:
                    mgr.add_priority(s["symbol"], s.get("name", ""), priority="high",
                                     reason=f"PSI监测: 精度下降{s['accuracy_drop']:.0%}")
                print(f"📋 已将{drifting_stocks}只加入重训队列")
            except Exception:
                pass
        else:
            print("🟢 未检测到显著特征漂移")
        print(f"⏰ PSI检查完成: {datetime.now().strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"⚠️ PSI监控异常: {e}")


if __name__ == '__main__':
    run_psi_check()
    # 原自检保留
    np.random.seed(42)
    base = pd.DataFrame({
        'ma_5': np.random.randn(1000) * 2 + 0.5,
        'rsi': np.random.rand(1000) * 40 + 40,
        'volume_ratio': np.random.rand(1000) + 1,
    })
    test = pd.DataFrame({
        'ma_5': np.random.randn(500) * 2 + 1.0,  # 均值从0.5→1.0
        'rsi': np.random.rand(500) * 40 + 45,    # 均值从60→65
        'volume_ratio': np.random.rand(500) + 1.2,  # 均值从1.5→1.7
    })
    results = calculate_psi_matrix(base, test)
    for col, info in results.items():
        print(f"  {col}: PSI={info['psi']:.4f} [{info['level']}]")
    alerts = [c for c, i in results.items() if i['level'] == 'significant']
    print(f"\n{alerts if alerts else 'no alerts — PSI正常'}")
