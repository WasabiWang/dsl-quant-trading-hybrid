"""
core/production_signal.py — 生产信号评分模块

统一存储 F1-F4 多因子评分逻辑，供回测引擎和 morning_decision 共用。
消除回测与生产使用不同评分系统的问题。

因子架构:
  F1 动量因子 (30%) — 当日涨跌幅, 有天花板(±2%→±0.8分)
  F2 ML确认因子 (25%) — daily_predict的方向一致性和置信度
  F3 量价因子 (10%) — 成交量异动(放量涨>缩量涨)
  F4 风险调整 (10%) — 黑天鹅/回撤/波动率惩罚
  基线 5.0 分 (剩余25%为基线)

P0: 回测和生产共用同一套评分逻辑
"""

from typing import Dict, Optional


def compute_alpha_score(
    symbol: str,
    change_pct: float,
    volume_ratio: float = 0.0,
    ml_pred: Optional[dict] = None,
    risk_position_ratio: float = 0.8,
    black_swan_active: bool = False,
) -> dict:
    """F1-F4 多因子评分 — 唯一的生产评分函数

    Args:
        symbol: 股票代码
        change_pct: 当日涨跌幅(%)，如 +1.5 表示涨1.5%
        volume_ratio: 量比(当日成交量/均量)，>1 放量，<1 缩量
        ml_pred: ML预测结果 {signal, confidence, ...} 或 None
        risk_position_ratio: 风控仓位比例 (0~1)
        black_swan_active: 是否黑天鹅模式

    Returns:
        dict 包含 total_score, action_signal, pred_signal 和因子明细
    """
    chg = change_pct

    # F1: 动量因子 (有天花板, ±2%封顶)
    chg_capped = max(-2.0, min(2.0, chg))
    f1_momentum = chg_capped / 2.5  # ±2% → ±0.8分

    # F2: ML确认因子
    f2_ml_confirm = 0.0  # 默认中性
    ml_signal = ml_pred.get('signal', 'hold') if ml_pred else 'hold'
    ml_conf = ml_pred.get('confidence', 0) if ml_pred else 0
    ml_dir = 1 if ml_signal == 'buy' else -1 if ml_signal == 'sell' else 0
    momentum_dir = 1 if chg > 0 else -1 if chg < 0 else 0

    if ml_pred:
        if ml_signal == 'hold':
            f2_ml_confirm = 0.0  # ML中性, 不加分不扣分
        elif momentum_dir == 0:
            # P0-FIX: 股票未涨未跌(如盘前/开盘前数据), 无方向可印证或矛盾
            # 不惩罚ML信号, 用半权重正向分(动量缺失时ML信号价值降低)
            f2_ml_confirm = ml_conf * ml_dir * 0.5  # conf=0.6 buy → +0.3
        elif ml_dir == momentum_dir:
            # ML方向与动量一致: 买入看涨/卖出看跌 → 加分
            f2_ml_confirm = ml_conf * ml_dir * 1.5  # conf=0.6 buy → +0.9
        else:
            # ML方向与动量矛盾: 动量涨但ML说sell → 严重降分
            f2_ml_confirm = -0.8  # 惩罚方向矛盾
            # 同时打折动量因子
            f1_momentum *= 0.5
    # else: 无ML数据, f2_ml_confirm=0, 不惩罚

    # F3: 量价因子
    f3_volume = 0.0
    if volume_ratio > 0:
        if chg > 0 and volume_ratio > 1.5:
            f3_volume = 0.3  # 放量上涨
        elif chg > 0 and volume_ratio < 0.7:
            f3_volume = -0.1  # 缩量上涨(可持续性差)
        elif chg < 0 and volume_ratio > 1.5:
            f3_volume = -0.3  # 放量下跌
        elif chg < 0 and volume_ratio < 0.7:
            f3_volume = 0.1  # 缩量下跌(抛压轻)

    # F4: 风险调整因子
    f4_risk = 0.0
    if black_swan_active and risk_position_ratio < 0.5:
        f4_risk = -0.3  # 黑天鹅压制下整体降分
    elif risk_position_ratio < 0.7:
        f4_risk = -0.15  # 中等风险降分

    # 综合评分 (基线5.0 + 加权因子)
    raw_score = 5.0 + f1_momentum * 0.30 + f2_ml_confirm * 0.25 + f3_volume * 0.10 + f4_risk * 0.10
    score = round(max(0, min(10, raw_score)), 1)

    # 信号生成: 基于综合评分
    # P1-FIX: 原阈值使"增持"(≥6.5)/"减持"(≤3.5)在合理输入范围内永不可达
    # 实测: 涨+2%+ML买0.7→评分5.53; 跌-2%+ML卖0.7→评分4.47
    # 调整后: 涨+2%+ML买0.7→评分5.53→关注; 涨+1%+ML买0.6→评分5.35→关注
    if score >= 5.5:
        action = "增持"
    elif score >= 5.0:
        action = "关注"
    elif score >= 4.5:
        action = "中性"
    elif score >= 3.8:
        action = "减持"
    else:
        action = "回避"

    # pred_signal: 供plan_trades使用, 综合ML+动量
    if ml_pred and ml_signal != 'hold' and ml_conf >= 0.58:
        pred_signal = ml_signal  # ML高信度优先
    elif score >= 6.5:
        pred_signal = "buy"   # 综合评分高但ML无高信度
    elif score <= 3.5:
        pred_signal = "sell"  # 综合评分低
    else:
        pred_signal = "hold"

    return {
        "symbol": symbol,
        "total_score": score,
        "action_signal": action,
        "pred_signal": pred_signal,
        "raw_factors": {
            "f1_momentum": round(f1_momentum, 3),
            "f2_ml_confirm": round(f2_ml_confirm, 3),
            "f3_volume": round(f3_volume, 3),
            "f4_risk": round(f4_risk, 3),
            "ml_signal": ml_signal if ml_pred else "none",
            "ml_conf": round(ml_conf, 2) if ml_pred else 0,
        },
    }
