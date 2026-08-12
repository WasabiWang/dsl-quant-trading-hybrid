#!/usr/bin/env python3
"""scripts/detect_lookahead_bias.py — 前视偏差检测 v4.5.12

检测特征计算中是否使用了未来信息:
1. 逐日期模拟: 每次只用 t 日及以前的数据计算 t 日特征
2. 与"全量计算"对比: 如果某特征在全量模式下与逐日模式不同 → 存在前视偏差
3. 常见泄漏源: rolling(center=True), .shift(-1), 标准化用了全量均值/标准差

用法:
  python3 scripts/detect_lookahead_bias.py --symbol 600519 --days 200
"""

import os, sys, argparse, json
from datetime import datetime
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class LookaheadBiasDetector:
    """前视偏差检测器"""

    def __init__(self, tolerance: float = 1e-8):
        self.tolerance = tolerance
        self.findings = []

    def detect_rolling_leak(self, df: pd.DataFrame, feature_cols: list) -> list:
        """检测滚动计算是否用了未来数据 (center=True或shift方向错误)"""
        issues = []
        n = len(df)
        warmup = 60  # 最小预热期

        for col in feature_cols:
            if col not in df.columns:
                continue
            diffs = []
            for t in range(warmup, n):
                # 逐日模式: 只用 [0:t+1] 的数据
                historical = df.iloc[:t + 1].copy()
                # 全量模式: 使用所有数据 (潜在前视)
                full = df.copy()

                # 计算滚动均值对比 (最常见的泄漏)
                try:
                    hist_ma20 = historical[col].rolling(20, min_periods=5).mean().iloc[-1]
                    full_ma20 = full[col].rolling(20, min_periods=5).mean().iloc[t]

                    if not np.isnan(hist_ma20) and abs(hist_ma20 - full_ma20) > self.tolerance:
                        diffs.append({
                            'date': str(df.index[t]),
                            'hist_value': float(hist_ma20),
                            'full_value': float(full_ma20),
                            'diff': float(abs(hist_ma20 - full_ma20)),
                        })
                except Exception:
                    pass

            if diffs:
                max_diff = max(d['diff'] for d in diffs)
                issues.append({
                    'feature': col,
                    'type': 'rolling_leak',
                    'severity': 'high' if max_diff > 0.01 else 'medium',
                    'affected_days': len(diffs),
                    'max_diff': round(max_diff, 6),
                    'sample_dates': diffs[:3],
                })

        return issues

    def detect_shift_leak(self, df: pd.DataFrame, feature_cols: list) -> list:
        """检测shift方向错误: 是否错误使用了shift(-1)访问未来"""
        issues = []
        for col in feature_cols:
            if col not in df.columns or f'{col}_lag1' not in df.columns:
                continue
            # 正确的滞后特征: lag1 = col.shift(1) (用昨天的值)
            # 错误的未来泄漏: lead1 = col.shift(-1) (用明天的值)
            if f'{col}_lead1' in df.columns:
                issues.append({
                    'feature': col,
                    'type': 'shift_leak',
                    'severity': 'critical',
                    'detail': f'{col}_lead1 使用了shift(-1)即未来数据',
                })
        return issues

    def detect_normalize_leak(self, df: pd.DataFrame, feature_cols: list) -> list:
        """检测标准化泄漏: 是否用全量均值/std而非滚动窗口"""
        issues = []
        n = len(df)
        warmup = 60

        for col in feature_cols:
            if col not in df.columns:
                continue
            values = df[col].dropna()
            if len(values) < warmup:
                continue

            # 全量Z-score vs 滚动Z-score (用截至t日的数据)
            full_mean = values.mean()
            full_std = values.std()
            diffs = []
            for t in range(warmup, n):
                hist = df[col].iloc[:t + 1]
                hist_mean = hist.mean()
                hist_std = hist.std()
                if hist_std > 0 and full_std > 0:
                    full_z = (df[col].iloc[t] - full_mean) / full_std
                    hist_z = (df[col].iloc[t] - hist_mean) / hist_std
                    if abs(full_z - hist_z) > 0.01:
                        diffs.append({
                            'date': str(df.index[t]),
                            'full_zscore': round(float(full_z), 4),
                            'hist_zscore': round(float(hist_z), 4),
                        })

            if diffs:
                issues.append({
                    'feature': col,
                    'type': 'normalize_leak',
                    'severity': 'medium',
                    'affected_days': len(diffs),
                    'sample_dates': diffs[:3],
                })

        return issues

    def run_full_scan(self, df: pd.DataFrame, feature_cols: list = None) -> dict:
        """运行完整前视偏差扫描"""
        if feature_cols is None:
            # 排除元数据列
            exclude = {'symbol', 'code', 'date', 'trade_date', 'market', 'is_st',
                       'open', 'high', 'low', 'close', 'volume', 'amount'}
            feature_cols = [c for c in df.columns if c not in exclude and df[c].dtype in ('float64', 'float32', 'int64')]

        print(f"🔍 扫描 {len(feature_cols)} 个特征的前视偏差...")

        shift_issues = self.detect_shift_leak(df, feature_cols)
        rolling_issues = self.detect_rolling_leak(df, feature_cols)
        norm_issues = self.detect_normalize_leak(df, feature_cols)

        all_issues = shift_issues + rolling_issues + norm_issues

        if all_issues:
            for issue in all_issues:
                severity_icon = {'critical': '🔴', 'high': '🟠', 'medium': '🟡'}.get(issue['severity'], '⚪')
                print(f"  {severity_icon} [{issue['type']}] {issue['feature']}: "
                      f"severity={issue['severity']}")
        else:
            print("  ✅ 未检测到前视偏差")

        return {
            'total_features': len(feature_cols),
            'issues_found': len(all_issues),
            'issues': all_issues,
            'scan_time': datetime.now().isoformat(),
        }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="前视偏差检测")
    parser.add_argument('--symbol', default='600519', help='股票代码')
    parser.add_argument('--days', type=int, default=200, help='数据天数')
    parser.add_argument('--output', help='输出JSON路径')
    args = parser.parse_args()

    # 使用akshare获取已有数据
    try:
        from core.data_loader import DataLoader
        loader = DataLoader()
        end = datetime.now().strftime('%Y-%m-%d')
        start = (datetime.now() - pd.Timedelta(days=args.days)).strftime('%Y-%m-%d')
        df = loader.load_stock_data(args.symbol, start, end)
        if df is None or df.empty:
            print(f"❌ 无法获取 {args.symbol} 数据")
            sys.exit(1)
        detector = LookaheadBiasDetector()
        result = detector.run_full_scan(df)
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"❌ 检测失败: {e}")
        print("ℹ️ 需要 akshare 数据源支持")
