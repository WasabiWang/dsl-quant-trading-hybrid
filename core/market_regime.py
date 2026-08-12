"""
市场体制识别器 (Market Regime Detector)

功能：
1. 判断当前是牛市、熊市还是震荡市。
2. 评估市场热度（Volatility）。
3. 为上层策略推荐最适合的“武器”。
"""

import backtrader as bt

class MarketRegimeDetector(bt.Analyzer):
    params = (
        ('trend_ma', 200),      # 牛熊分界线
        ('adx_period', 14),     # 趋势强度
        ('adx_threshold', 25),  # 趋势与震荡的分水岭
    )

    def __init__(self):
        self.ma_trend = bt.indicators.SimpleMovingAverage(self.data.close, period=self.p.trend_ma)
        self.adx = bt.indicators.AverageDirectionalMovementIndex(self.data, period=self.p.adx_period)
        self.regime = "UNKNOWN"

    def next(self):
        close = self.data.close[0]
        ma = self.ma_trend[0]
        adx = self.adx[0]

        # 1. 判定大势 (牛/熊)
        is_bull_market = close > ma
        trend_strength = "STRONG" if adx > self.p.adx_threshold else "WEAK"

        # 2. 综合判定体制
        if is_bull_market and trend_strength == "STRONG":
            self.regime = "BULL_RUN"        # 牛市主升浪
        elif not is_bull_market and trend_strength == "STRONG":
            self.regime = "BEAR_CRASH"      # 熊市主跌浪
        else:
            self.regime = "SIDEWAYS"        # 震荡市 (大部分时间的常态)

    def get_regime(self):
        return self.regime

    def get_suggested_strategy(self):
        """根据体制推荐策略"""
        if self.regime == "BULL_RUN":
            return "TrendFollower"  # 趋势跟踪：拿住不动
        elif self.regime == "BEAR_CRASH":
            return "CashIsKing"     # 现金为王：空仓避险
        else:
            return "GridArbitrage"  # 网格套利：高抛低吸 (最适合个人投资者)
