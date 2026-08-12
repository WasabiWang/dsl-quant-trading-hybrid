"""
高级量化策略库 (v2.0 - 动态仓位 + 宏观过滤)
"""

import backtrader as bt
import numpy as np
import logging
import sys
import os

# 确保能导入 MacroAnalyst
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.analysts.macro_analyst import MacroAnalyst

logger = logging.getLogger(__name__)

class TrendVolatilityStrategy(bt.Strategy):
    params = (
        ('rsi_period', 14),
        ('rsi_low', 30),
        ('rsi_high', 70),
        ('bb_period', 20),
        ('bb_dev', 2.0),
        ('trend_ma', 200),
        ('macro_threshold', 0.4),
    )

    def __init__(self):
        self.macro_analyst = MacroAnalyst()
        self.ma_trend = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.trend_ma)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.p.rsi_period)
        self.bb = bt.indicators.BollingerBands(self.data.close, period=self.p.bb_period, devfactor=self.p.bb_dev)
        self.atr = bt.indicators.ATR(self.data, period=14)
        self.order = None

    def get_sizing(self, volatility):
        base_capital = self.broker.getcash()
        risk_per_trade = base_capital * 0.01
        if volatility == 0: return 0
        return max(100, int(risk_per_trade / volatility))

    def next(self):
        if self.order:
            return

        # 宏观过滤
        macro_report = self.macro_analyst.analyze("600519.SH", {})
        macro_score = macro_report.get("macro_score", 0.5)
        
        if macro_score < self.p.macro_threshold:
            if self.position:
                self.order = self.close()
            return

        # 动态仓位
        position_size = self.get_sizing(self.atr[0])

        if not self.position:
            if self.data.close[0] > self.ma_trend[0] and self.rsi[0] < self.p.rsi_low:
                self.order = self.buy(size=position_size)
            elif self.data.close[0] > self.bb.lines.top[0]:
                self.order = self.buy(size=position_size)
        else:
            if self.rsi[0] > self.p.rsi_high or self.data.close[0] < self.bb.lines.mid[0]:
                self.order = self.close()
