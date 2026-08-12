"""
个人投资者专用：网格高胜率策略 (Grid High-Win-Rate Strategy)

特点：
1. 不预测方向，只应对波动。
2. 胜率极高，通过分批建仓降低风险。
3. 适合个人投资者：无需盯盘，自动化套利。
"""

import backtrader as bt

class GridHighWinRateStrategy(bt.Strategy):
    params = (
        ('grid_size', 0.03),      # 网格间距：每跌 3% 补一次仓
        ('take_profit', 0.05),    # 止盈目标：整体盈利 5% 离场
        ('bb_period', 20),        # 布林带周期
    )

    def __init__(self):
        self.bb = bt.indicators.BollingerBands(self.data.close, period=self.p.bb_period, devfactor=2.0)
        self.buy_prices = []  # 记录每一笔买入的成本

    def notify_order(self, order):
        if order.status == order.Completed:
            if order.isbuy():
                self.buy_prices.append(order.executed.price)
            elif order.issell():
                self.buy_prices = [] # 清仓后重置

    def next(self):
        if not self.position:
            # 初始建仓：价格跌破布林带下轨
            if self.data.close[0] < self.bb.lines.bot[0]:
                self.buy(size=100)
        else:
            current_avg = sum(self.buy_prices) / len(self.buy_prices)
            
            # 补仓逻辑：相比上一笔买入价下跌 grid_size
            if self.data.close[0] < self.buy_prices[-1] * (1 - self.p.grid_size):
                self.buy(size=100)
            
            # 止盈逻辑：当前价格高于均价 take_profit
            if self.data.close[0] > current_avg * (1 + self.p.take_profit):
                self.sell(size=len(self.buy_prices) * 100)
