#!/usr/bin/env python3
"""
情绪面分析师 - 计算个股市场情绪得分
对接资金流、龙虎榜、北向资金、舆情等多维度数据
"""
import os
import sys
try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class SentimentAnalyst:
    def __init__(self):
        pass

    def calculate_stock_sentiment(self, symbol: str, days: int = 3) -> float:
        """
        计算个股情绪综合得分(0-10分)
        维度：资金流(40%) + 龙虎榜(30%) + 北向资金(30%)
        """
        total_score = 0.0
        weight_sum = 0.0
        
        try:
            # 1. 近期资金流得分(40%)
            fund_flow_score = self._get_fund_flow_score(symbol, days)
            total_score += fund_flow_score * 0.4
            weight_sum += 0.4
            
            # 2. 龙虎榜得分(30%)
            lhb_score = self._get_lhb_score(symbol, days)
            total_score += lhb_score * 0.3
            weight_sum += 0.3
            
            # 3. 北向资金得分(30%)
            north_flow_score = self._get_north_flow_score(symbol, days)
            total_score += north_flow_score * 0.3
            weight_sum += 0.3
            
        except Exception as e:
            print(f"计算{symbol}情绪得分失败: {e}")
        
        if weight_sum == 0:
            return 5.0
        
        return round(total_score / weight_sum, 2)

    def _get_fund_flow_score(self, symbol: str, days: int = 3) -> float:
        """获取个股资金流得分(0-10分)"""
        try:
            import time
            time.sleep(0.3)
            # 获取个股资金流数据
            market = "sh" if symbol.startswith("6") else "sz"
            stock_flow_df = ak.stock_individual_fund_flow(stock=symbol, market=market)
            if not stock_flow_df.empty:
                recent_flow = stock_flow_df.head(days)
                net_inflow_days = len(recent_flow[recent_flow['主力净流入-净额'].astype(float) > 0])
                avg_flow_rate = recent_flow['主力净流入-净占比'].astype(float).mean()
                # 连续净流入得分更高
                score = min(10, max(0, net_inflow_days * 2 + avg_flow_rate))
                return round(score, 2)
        except Exception as e:
            print(f"获取{symbol}资金流失败: {e}")
        return 5.0

    def _get_lhb_score(self, symbol: str, days: int = 3) -> float:
        """获取个股龙虎榜得分(0-10分)"""
        try:
            import time
            time.sleep(0.3)
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            lhb_df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
            stock_lhb = lhb_df[lhb_df['代码'] == symbol]
            if not stock_lhb.empty:
                # 机构净买入越多得分越高
                net_buy = stock_lhb['机构净额'].sum()
                if net_buy > 100000000: # 净买入1亿以上
                    return 10.0
                elif net_buy > 50000000: # 净买入5000万以上
                    return 8.0
                elif net_buy > 0: # 净买入
                    return 7.0
                elif net_buy < -100000000: # 净卖出1亿以上
                    return 2.0
                else: # 净卖出
                    return 4.0
        except Exception as e:
            print(f"获取{symbol}龙虎榜数据失败: {e}")
        return 5.0

    def _get_north_flow_score(self, symbol: str, days: int = 3) -> float:
        """获取北向资金持股变化得分(0-10分)"""
        try:
            import time
            time.sleep(0.3)
            # 获取北向资金持股数据
            market = "沪股通" if symbol.startswith("6") else "深股通"
            north_hold_df = ak.stock_hsgt_hold_stock_em(market=market)
            stock_hold = north_hold_df[north_hold_df['代码'] == symbol]
            if not stock_hold.empty:
                change_ratio = float(stock_hold.iloc[0]['持股变动比例'])
                # 北向增持越多得分越高
                score = min(10, max(0, 5 + change_ratio * 2))
                return round(score, 2)
        except Exception as e:
            print(f"获取{symbol}北向资金数据失败: {e}")
        return 5.0
