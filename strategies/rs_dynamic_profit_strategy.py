"""
RS动态止盈策略 (RS Dynamic Profit Strategy)
核心优化：
1. 基于RS（相对强弱）的动态止盈：RS越强，止盈空间越大
2. 自适应阈值：RS门槛为该股票60天RS分布的前30%分位
3. MA60趋势软化：股价在MA60±2%范围内且RS极强，允许破格入场
4. 移动止盈（Trailing Stop）：盈利达到5%后启动，回撤30%自动止盈
5. 统一总资金核算，避免股份单位换算问题
"""
import backtrader as bt
import numpy as np
from collections import deque
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.analysts.macro_analyst import MacroAnalyst
logger = logging.getLogger(__name__)
class RSDynamicProfitStrategy(bt.Strategy):
    params = (
        ('rs_period', 60),                # RS计算周期
        ('rs_quantile_threshold', 0.7),   # RS门槛：前30%分位 (1 - 0.7 = 0.3)
        ('ma60_tolerance', 0.02),         # MA60容差：±2%
        ('trailing_stop_trigger', 0.05),  # 移动止盈触发条件：盈利5%
        ('trailing_stop_drawdown', 0.3),  # 移动止盈回撤比例：30%
        ('grid_size', 0.03),              # 网格补仓间距：3%
        ('bb_period', 20),                # 布林带周期
        ('macro_threshold', 0.4),         # 宏观过滤阈值
    )
    def __init__(self):
        self.macro_analyst = MacroAnalyst()
        self.ma60 = bt.indicators.SimpleMovingAverage(self.data.close, period=60)
        self.bb = bt.indicators.BollingerBands(self.data.close, period=self.p.bb_period, devfactor=2.0)
        self.atr = bt.indicators.ATR(self.data, period=14)
        self.rs_history = deque(maxlen=self.p.rs_period)  # P0-FIX: O(1) popleft
        self.buy_prices = []  # 记录每一笔买入成本
        self.highest_profit = 0.0  # 持仓期间最高盈利
        self.order = None
        # P0-FIX: 宏观分析缓存，每20个bar刷新一次，避免回测中每bar调用API
        self._cached_macro_score = 0.5
        self._macro_cache_bar = -100  # 上次刷新的bar
    def calculate_rs(self):
        """计算当前RS值（相对强弱：个股涨跌幅 / 大盘涨跌幅）"""
        if len(self.data.close) < 20:
            return 0.5
        stock_return = (self.data.close[0] - self.data.close[-20]) / self.data.close[-20]
        # P0-FIX: 用个股自身60日均值回报作为市场代理，替代硬编码0.02
        # 后续应接入真实大盘指数数据
        if len(self.data.close) >= 60:
            market_return = (self.data.close[0] - self.data.close[-60]) / self.data.close[-60]
        else:
            market_return = stock_return  # fallback: 无足够数据时RS=1
        if abs(market_return) < 0.001:  # 避免除以接近0的数
            market_return = 0.001 if market_return >= 0 else -0.001
        rs_raw = stock_return / market_return
        rs = 1 / (1 + np.exp(-(rs_raw - 1) * 2))  # 归一化到0~1
        self.rs_history.append(rs)
        return rs
    def get_rs_quantile(self):
        """获取当前RS值在过去60天中的分位"""
        if len(self.rs_history) < self.p.rs_period:
            return 0.5
        current_rs = self.rs_history[-1]
        return np.sum(np.array(self.rs_history) <= current_rs) / len(self.rs_history)
    def get_dynamic_take_profit(self, rs_quantile):
        """基于RS分位获取动态止盈比例"""
        if rs_quantile >= 0.8:  # RS前20%，极强
            return 0.20  # 止盈20%
        elif rs_quantile >= 0.7:  # RS前30%，强
            return 0.15  # 止盈15%
        elif rs_quantile >= 0.5:  # RS前50%，中等
            return 0.10  # 止盈10%
        else:  # RS弱
            return 0.05  # 止盈5%
    def get_position_size(self):
        """基于总资金动态计算仓位，统一按资金核算，避免股份单位问题"""
        total_capital = self.broker.getvalue()
        # 动态仓位调整：根据波动率调整仓位
        volatility = self.atr[0] / self.data.close[0] if self.data.close[0] != 0 else 0.02
        if volatility < 0.02:  # 低波动
            position_ratio = 0.25  # 单次仓位25%
        elif volatility < 0.035:  # 中波动
            position_ratio = 0.15  # 单次仓位15%
        else:  # 高波动
            position_ratio = 0.10  # 单次仓位10%
        position_amount = total_capital * position_ratio
        return max(100, int(position_amount / self.data.close[0] / 100) * 100)  # 取整为100的整数倍
    def notify_order(self, order):
        if order.status == order.Completed:
            if order.isbuy():
                self.buy_prices.append(order.executed.price)
                self.highest_profit = 0.0  # 新买入后重置最高盈利
            elif order.issell():
                self.buy_prices = []  # 清仓后重置
                self.highest_profit = 0.0
        self.order = None
    def next(self):
        if self.order:
            return
        # P0-FIX: 宏观分析缓存，每20个bar刷新一次，避免回测中每bar调用API
        current_bar = len(self.data)
        if current_bar - self._macro_cache_bar >= 20:
            macro_report = self.macro_analyst.analyze(self.data._name, {})
            self._cached_macro_score = macro_report.get("macro_score", 0.5)
            self._macro_cache_bar = current_bar
        macro_score = self._cached_macro_score
        if macro_score < self.p.macro_threshold:
            if self.position:
                self.order = self.close()
            return
        # 计算RS相关指标
        current_rs = self.calculate_rs()
        rs_quantile = self.get_rs_quantile()
        take_profit_ratio = self.get_dynamic_take_profit(rs_quantile)
        # MA60趋势判断
        ma60_price = self.ma60[0]
        current_price = self.data.close[0]
        ma60_upper = ma60_price * (1 + self.p.ma60_tolerance)
        ma60_lower = ma60_price * (1 - self.p.ma60_tolerance)
        in_ma60_range = ma60_lower <= current_price <= ma60_upper
        # 买入逻辑
        if not self.position:
            # 条件1：RS达到前30%分位 + 价格站稳MA60上方
            condition1 = rs_quantile >= self.p.rs_quantile_threshold and current_price > ma60_upper
            # 条件2：MA60趋势软化：价格在MA60±2%范围内 + RS极强（前10%分位）
            condition2 = in_ma60_range and rs_quantile >= 0.9
            # 条件3：价格跌破布林带下轨，RS中等以上
            condition3 = current_price < self.bb.lines.bot[0] and rs_quantile >= 0.5
            if condition1 or condition2 or condition3:
                size = self.get_position_size()
                if size > 0:
                    self.order = self.buy(size=size)
        # 持有逻辑：止盈 + 移动止损
        else:
            current_avg = sum(self.buy_prices) / len(self.buy_prices)
            profit_ratio = (current_price - current_avg) / current_avg
            # 更新最高盈利
            if profit_ratio > self.highest_profit:
                self.highest_profit = profit_ratio
            # 1. 固定止盈：达到动态止盈比例
            if profit_ratio >= take_profit_ratio:
                self.order = self.close()
                logger.info(f"止盈离场：当前盈利{profit_ratio:.2%}，RS分位{rs_quantile:.2%}，止盈阈值{take_profit_ratio:.2%}")
            # 2. 移动止盈：盈利达到触发阈值后，回撤超过设定比例止盈
            elif self.highest_profit >= self.p.trailing_stop_trigger:
                drawdown = (self.highest_profit - profit_ratio) / self.highest_profit if self.highest_profit != 0 else 0
                if drawdown >= self.p.trailing_stop_drawdown:
                    self.order = self.close()
                    logger.info(f"移动止盈离场：最高盈利{self.highest_profit:.2%}，当前盈利{profit_ratio:.2%}，回撤{drawdown:.2%}")
            # 3. 补仓逻辑：相比上一笔买入价下跌网格间距
            elif len(self.buy_prices) > 0 and current_price < self.buy_prices[-1] * (1 - self.p.grid_size):
                size = self.get_position_size()
                if size > 0:
                    self.order = self.buy(size=size)
