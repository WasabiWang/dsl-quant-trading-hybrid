#!/usr/bin/env python3
"""core/limit_distortion.py — 涨跌停数据失真处理 v4.5.12

A股涨跌停日的OHLCV数据存在失真：
- 涨停日: 买盘堆积 → 收盘价=涨停价，成交量不代表真实供需
- 跌停日: 卖盘堆积 → 收盘价=跌停价，无成交量≠无抛压
- 一字板: 开盘=收盘=涨停/跌停价 → 全天无真实交易

处理策略:
1. 标记涨停/跌停日 (is_limit_day)
2. 标记一字板 (is_one_shot_limit)
3. 计算失真系数 (distortion_factor: 0=完全失真, 1=正常)
4. 生成特征时应降权或排除失真日
"""
import numpy as np
import pandas as pd
from typing import Tuple


def detect_limit_days(
    data: pd.DataFrame,
    limit_rate: float = 0.10,
) -> pd.DataFrame:
    """检测涨跌停日并标记

    Args:
        data: 含 open, high, low, close, pct_chg 列的DataFrame
        limit_rate: 涨跌停幅度 (主板0.10, 创业板0.20, 科创板0.20, 北交所0.30)

    Returns:
        添加了 is_limit_up, is_limit_down, is_one_shot_limit, distortion_factor
    """
    df = data.copy()

    pct = df.get('pct_chg', pd.Series(0, index=df.index))
    # 若pct_chg为空，从OHLC推算
    if pct.abs().max() < 0.001:
        pct = df['close'].pct_change() * 100

    limit_threshold = limit_rate * 100 * 0.95  # 95%的涨跌幅视为涨停

    # 涨跌停标记
    df['is_limit_up'] = pct >= limit_threshold
    df['is_limit_down'] = pct <= -limit_threshold
    df['is_limit_day'] = df['is_limit_up'] | df['is_limit_down']

    # 一字板检测: 开盘=最高=最低=收盘 或 最高=最低
    df['is_one_shot_limit'] = df['is_limit_day'] & (
        (df['high'] == df['low']) |
        ((df['open'] == df['high']) & (df['open'] == df['low']))
    )

    # 失真系数
    df['distortion_factor'] = 1.0
    # 涨停日: 成交量失真，保留但降权
    df.loc[df['is_limit_up'], 'distortion_factor'] = 0.4
    # 跌停日: 同理
    df.loc[df['is_limit_down'], 'distortion_factor'] = 0.4
    # 一字板: 几乎完全失真
    df.loc[df['is_one_shot_limit'], 'distortion_factor'] = 0.1
    # 连续涨跌停: 进一步降权
    df['consecutive_limit_days'] = 0
    consecutive = 0
    for i in range(len(df)):
        if df['is_limit_day'].iloc[i]:
            consecutive += 1
        else:
            consecutive = 0
        df.iloc[i, df.columns.get_loc('consecutive_limit_days')] = consecutive
        if consecutive >= 3:
            df.iloc[i, df.columns.get_loc('distortion_factor')] = 0.05

    return df


def get_valid_training_mask(data: pd.DataFrame, min_factor: float = 0.3) -> pd.Series:
    """获取可用于训练的样本mask (失真系数 >= min_factor)"""
    if 'distortion_factor' not in data.columns:
        return pd.Series(True, index=data.index)
    return data['distortion_factor'] >= min_factor


def filter_distorted_samples(
    features: pd.DataFrame,
    label: pd.Series,
    distortion_factor: pd.Series = None,
    min_factor: float = 0.3,
) -> Tuple[pd.DataFrame, pd.Series]:
    """过滤涨跌停失真样本，用于模型训练

    Returns:
        (filtered_features, filtered_label)
    """
    if distortion_factor is None:
        if 'distortion_factor' in features.columns:
            distortion_factor = features['distortion_factor']
        else:
            return features, label

    mask = distortion_factor >= min_factor
    removed = (~mask).sum()
    if removed > 0:
        print(f"  ⚠️ 过滤涨跌停失真样本: {removed}/{len(features)} ({removed/len(features):.1%})")

    return features[mask], label[mask]


# 板块特殊阈值 (IPOs首日)
IPO_LIMIT = 0.44  # 新股首日最大涨幅44%


def get_board_limit_rate(symbol: str) -> float:
    """根据代码返回对应板块涨跌停幅度 (P1-6 fix: 支持ST检测)"""
    code = str(symbol).lstrip("'\"")
    try:
        from config.constants import is_st_stock
        if is_st_stock(code):
            return 0.05  # ST股5%涨跌幅
    except ImportError:
        pass
    if code.startswith("688"):
        return 0.20
    elif code.startswith("300") or code.startswith("301"):
        return 0.20
    elif code.startswith("8"):
        return 0.30
    elif code.startswith("60") or code.startswith("00"):
        return 0.10
    return 0.10
