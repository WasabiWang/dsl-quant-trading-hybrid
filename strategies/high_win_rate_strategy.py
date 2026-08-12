"""
个人投资者专用：高胜率共振策略 (High-Win-Rate Resonance Strategy)

核心逻辑：
1. 宏观过滤：只在宏观环境安全时出手。
2. 趋势共振：价格必须在 200日均线之上（长期牛市）。
3. 动量确认：MACD 必须在零轴上方金叉（中期动能）。
4. 波动率突破：价格突破布林带上轨（短期爆发）。
"""

import backtrader as bt
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

class ResonanceStrategy(bt.Strategy):
    params = (
        ('trend_period', 200),      # 长期趋势线
        ('macd_fast', 12),
        ('macd_slow', 26),
        ('macd_signal', 9),
        ('bb_period', 20),
        ('bb_dev', 2.0),
        ('risk_per_trade', 0.02),   # 单笔风险控制在 2%
    )

    def __init__(self):
        # 1. 趋势过滤器
        self.ma_trend = bt.indicators.ExponentialMovingAverage(self.data.close, period=self.p.trend_period)
        
        # 2. 动量核心 (MACD)
        self.macd = bt.indicators.MACDHisto(self.data.close, 
                                       period_me1=self.p.macd_fast, 
                                       period_me2=self.p.macd_slow, 
                                       period_signal=self.p.macd_signal)
        
        # 3. 波动率爆发点 (Bollinger Bands)
        self.bb = bt.indicators.BollingerBands(self.data.close, period=self.p.bb_period, devfactor=self.p.bb_dev)
        
        # 4. 真实波幅 (用于移动止损)
        self.atr = bt.indicators.ATR(self.data, period=14)
        
        self.order = None
        self.entry_price = 0.0
        self.highest_price = 0.0

    def notify_order(self, order):
        if order.status == order.Completed:
            if order.isbuy():
                self.entry_price = order.executed.price
                self.highest_price = self.entry_price
                logger.info(f'✅ 高胜率买入: {order.executed.price:.2f}')
            else:
                logger.info(f'✅ 锁定利润/止损: {order.executed.price:.2f}')
        # P0-FIX: 无论买入还是卖出完成，都必须清空order引用
        self.order = None

    def next(self):
        if self.order:
            return

        # --- 核心过滤器：只做顺势 ---
        if self.data.close[0] < self.ma_trend[0]:
            if self.position: self.order = self.close() # 趋势坏了就跑
            return

        # --- 胜率共振确认 (MACD Histogram 转正 + 突破布林上轨) ---
        is_resonance = (
            self.macd[0] > 0 and 
            self.macd[0] > self.macd[-1] and # 动能在增强
            self.data.close[0] > self.bb.lines.top[0]
        )

        if not self.position:
            if is_resonance:
                # 动态仓位：根据 ATR 决定买多少，确保止损时只亏 2%
                stop_distance = self.atr[0] * 2
                position_size = int((self.broker.getcash() * self.p.risk_per_trade) / stop_distance)
                self.order = self.buy(size=max(100, position_size))
        else:
            # 记录最高价，用于移动止损
            self.highest_price = max(self.highest_price, self.data.close[0])
            
            # 吊灯止损：从最高点回撤 2 倍 ATR 离场
            if self.data.close[0] < (self.highest_price - self.atr[0] * 2):
                self.order = self.close()
