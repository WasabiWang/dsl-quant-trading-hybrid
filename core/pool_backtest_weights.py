#!/usr/bin/env python3
"""
pool_backtest_weights.py - 权重分配策略
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

def equal_weight(selected_stocks: List[str],
                current_prices: Dict[str, float],
                stock_data: Dict[str, pd.DataFrame],
                current_date: Any) -> Dict[str, float]:
    """
    等权重分配
    
    Args:
        selected_stocks: 选中的股票
        current_prices: 当前价格
        stock_data: 股票数据
        current_date: 当前日期
        
    Returns:
        权重字典
    """
    n = len(selected_stocks)
    if n == 0:
        return {}
    
    weight = 1.0 / n
    return {stock: weight for stock in selected_stocks}

def momentum_weight(selected_stocks: List[str],
                   current_prices: Dict[str, float],
                   stock_data: Dict[str, pd.DataFrame],
                   current_date: Any) -> Dict[str, float]:
    """
    动量权重分配
    
    Args:
        selected_stocks: 选中的股票
        current_prices: 当前价格
        stock_data: 股票数据
        current_date: 当前日期
        
    Returns:
        权重字典
    """
    if not selected_stocks:
        return {}
    
    momentum_scores = {}
    
    for symbol in selected_stocks:
        df = stock_data[symbol]
        if len(df) < 60:
            continue
        
        idx = df.index.get_loc(current_date)
        if idx >= 20:
            price_20 = df.iloc[idx-20]['close']
            price_60 = df.iloc[idx-60]['close'] if idx >= 60 else price_20
            
            ret_20 = (current_prices[symbol] / price_20 - 1) * 100
            ret_60 = (current_prices[symbol] / price_60 - 1) * 100
            
            momentum_score = ret_20 * 0.6 + ret_60 * 0.4
            momentum_scores[symbol] = max(momentum_score, 0.1)  # 确保为正
    
    if not momentum_scores:
        return equal_weight(selected_stocks, current_prices, stock_data, current_date)
    
    # 归一化权重
    total_score = sum(momentum_scores.values())
    weights = {symbol: score/total_score for symbol, score in momentum_scores.items()}
    
    return weights

def inverse_volatility_weight(selected_stocks: List[str],
                            current_prices: Dict[str, float],
                            stock_data: Dict[str, pd.DataFrame],
                            current_date: Any) -> Dict[str, float]:
    """
    逆波动率权重分配
    
    Args:
        selected_stocks: 选中的股票
        current_prices: 当前价格
        stock_data: 股票数据
        current_date: 当前日期
        
    Returns:
        权重字典
    """
    if not selected_stocks:
        return {}
    
    volatility_scores = {}
    
    for symbol in selected_stocks:
        df = stock_data[symbol]
        if len(df) < 20:
            continue
        
        idx = df.index.get_loc(current_date)
        if idx >= 20:
            # 计算过去20日的波动率
            returns = df.iloc[idx-19:idx+1]['close'].pct_change().dropna()
            if len(returns) > 0:
                volatility = returns.std() * np.sqrt(252)  # 年化波动率
                volatility_scores[symbol] = 1 / (volatility + 0.01)  # 加小常数避免除零
    
    if not volatility_scores:
        return equal_weight(selected_stocks, current_prices, stock_data, current_date)
    
    # 归一化权重
    total_score = sum(volatility_scores.values())
    weights = {symbol: score/total_score for symbol, score in volatility_scores.items()}
    
    return weights

def risk_parity_weight(selected_stocks: List[str],
                      current_prices: Dict[str, float],
                      stock_data: Dict[str, pd.DataFrame],
                      current_date: Any) -> Dict[str, float]:
    """
    风险平价权重分配（简化版）
    
    Args:
        selected_stocks: 选中的股票
        current_prices: 当前价格
        stock_data: 股票数据
        current_date: 当前日期
        
    Returns:
        权重字典
    """
    if len(selected_stocks) < 2:
        return equal_weight(selected_stocks, current_prices, stock_data, current_date)
    
    # 计算协方差矩阵（简化）
    returns_data = []
    valid_stocks = []
    
    for symbol in selected_stocks:
        df = stock_data[symbol]
        if len(df) < 60:
            continue
        
        idx = df.index.get_loc(current_date)
        if idx >= 60:
            # 获取过去60日的收益率
            prices = df.iloc[idx-59:idx+1]['close'].values
            returns = np.diff(prices) / prices[:-1]
            returns_data.append(returns)
            valid_stocks.append(symbol)
    
    if len(valid_stocks) < 2:
        return equal_weight(selected_stocks, current_prices, stock_data, current_date)
    
    # 计算协方差矩阵
    returns_matrix = np.array(returns_data)
    cov_matrix = np.cov(returns_matrix)
    
    # 计算风险贡献
    try:
        # 使用逆方差作为初始权重
        variances = np.diag(cov_matrix)
        inv_variances = 1 / (variances + 1e-8)
        weights = inv_variances / inv_variances.sum()
        
        # 迭代优化（简化版）
        for _ in range(10):
            # 计算风险贡献
            risk_contributions = weights * (cov_matrix @ weights)
            total_risk = risk_contributions.sum()
            
            # 调整权重使风险贡献相等
            target_contributions = total_risk / len(weights)
            adjustment = target_contributions / (risk_contributions + 1e-8)
            weights = weights * adjustment
            weights = weights / weights.sum()
        
        return dict(zip(valid_stocks, weights))
    
    except Exception:
        return equal_weight(selected_stocks, current_prices, stock_data, current_date)