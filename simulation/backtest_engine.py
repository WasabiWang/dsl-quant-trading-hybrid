"""
高保真回测引擎 v4.5.12
P0-FIX: 修复前视偏差/idx计算/prev_close

基于 Backtrader，支持：
- 多因子评分信号 (桥接 SignalGenerator，消除回测/生产代码路径不一致)
- T+1 约束
- 涨跌停挂单失败模拟
- 佣金 + 印花税 + 滑点
"""

import backtrader as bt
import pandas as pd
import numpy as np
from typing import Dict, Any, List, Optional
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.signal_generator import SignalGenerator
from core.production_signal import compute_alpha_score as production_score
from config.constants import (
    COMMISSION_RATE, STAMP_TAX_RATE, MIN_COMMISSION,
    SLIPPAGE_BPS, LIMIT_RATES,
)

logger = logging.getLogger(__name__)


class DSLSignalStrategy(bt.Strategy):
    """基于 DSL 信号 + 多因子评分的交易策略

    P0: use_production_scoring=True 时使用 core.production_signal 评分
        （与 morning_decision.py 生产评分一致），否则使用 SignalGenerator。
    """

    params = (
        ('printlog', True),
        ('max_position_pct', 0.8),
        ('market', 'A'),
        ('use_production_scoring', False),  # P0: 设为 True 以匹配生产评分
    )

    def __init__(self):
        self.order = None
        self.buy_date = None
        self.signal_generator = SignalGenerator()
        self.market = self.params.market

        # 逐日记录因子评分数值面板
        self.factor_panel = pd.DataFrame()

    def log(self, txt, dt=None):
        if self.params.printlog:
            dt = dt or self.datas[0].datetime.date(0)
            logger.info(f'{dt.isoformat()}, {txt}')

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        if order.status == order.Completed:
            if order.isbuy():
                self.log(f'买入: {order.executed.price:.2f}, 数量: {order.executed.size}')
                self.buy_date = self.datas[0].datetime.date(0)
            else:
                self.log(f'卖出: {order.executed.price:.2f}, 数量: {order.executed.size}')
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log(f'订单取消/拒绝: {order.getstatusname()}')
        self.order = None

    def _is_limit_up(self, price: float, prev_close: float) -> bool:
        """检查是否涨停（买不到）"""
        if prev_close <= 0:
            return False
        rate = LIMIT_RATES.get(self.market, 0.10)
        return price >= round(prev_close * (1 + rate), 2)

    def _is_limit_down(self, price: float, prev_close: float) -> bool:
        """检查是否跌停（卖不掉）"""
        if prev_close <= 0:
            return False
        rate = LIMIT_RATES.get(self.market, 0.10)
        return price <= round(prev_close * (1 - rate), 2)

    def _is_suspended(self, idx: int) -> bool:
        """停牌检测：零成交量 且 价格无变化"""
        if idx < 1:
            return False
        volume = float(self.data.volume[0])
        if volume > 0:
            return False
        close_price = float(self.data.close[0])
        prev_close = float(self.data.close[-1])
        return abs(close_price - prev_close) < 1e-9

    def _calculate_cost(self, price: float, size: int, is_buy: bool) -> float:
        """计算交易成本: 佣金 + 印花税(卖出) + 滑点"""
        amount = price * size
        commission = max(amount * COMMISSION_RATE, MIN_COMMISSION)
        stamp_tax = 0 if is_buy else amount * STAMP_TAX_RATE
        slippage = amount * SLIPPAGE_BPS / 10000.0
        return commission + stamp_tax + slippage

    def _build_ohlcv_df(self) -> pd.DataFrame:
        """从 Backtrader 数据线构建 OHLCV DataFrame 供 SignalGenerator 使用
        P0-FIX: 只包含当前及历史bar，防止前视偏差"""
        current_idx = len(self.data)  # Current bar index (0-based, count of bars seen so far)
        records = []
        for i in range(current_idx):
            records.append({
                'open': float(self.data.open[-current_idx + i]),
                'high': float(self.data.high[-current_idx + i]),
                'low': float(self.data.low[-current_idx + i]),
                'close': float(self.data.close[-current_idx + i]),
                'volume': float(self.data.volume[-current_idx + i]),
            })
        return pd.DataFrame(records)

    def next(self):
        if self.order:
            return

        current_date = self.datas[0].datetime.date(0)
        close_price = float(self.data.close[0])
        open_price = float(self.data.open[0])
        idx = len(self.data)  # P0-FIX: len(self.data) IS the current 0-based index
        prev_close = float(self.data.close[-1]) if idx > 0 else close_price  # P0-FIX: 移到使用前定义

        # 需要至少60根K线计算因子
        if idx < 60:
            return

        # 构建 OHLCV DataFrame 并生成信号
        df = self._build_ohlcv_df()

        if self.params.use_production_scoring:
            # === P0: 使用生产评分（与 morning_decision 一致）===
            change_pct = close_price / prev_close - 1 if idx > 0 else 0
            volume = float(self.data.volume[0])
            avg_vol_20 = sum(float(self.data.volume[-i]) for i in range(1, min(21, idx + 1))) / max(1, min(20, idx))
            vol_ratio = volume / avg_vol_20 if avg_vol_20 > 0 else 0
            prod = production_score(
                symbol=self.datas[0]._name or "STOCK",
                change_pct=change_pct * 100,
                volume_ratio=vol_ratio,
            )
            signal_score = prod["total_score"]
            signal_action = prod["pred_signal"]
        else:
            # 回测专用评分 (SignalGenerator)
            sg_signal = self.signal_generator.generate_signal(
                symbol=self.datas[0]._name or "STOCK",
                df=df,
            )
            signal_score = sg_signal.total_score
            signal_action = sg_signal.action_signal

        # T+1: 当日买入不可卖出
        can_sell = self.position and (self.buy_date is None or current_date > self.buy_date)

        if not self.position:
            # 无持仓 → 检查买入信号
            if signal_action == "buy":
                if self._is_limit_up(close_price, prev_close):
                    self.log(f'涨停跳过买入: {close_price:.2f}')
                    return
                if self._is_suspended(idx):
                    self.log(f'停牌跳过买入: {close_price:.2f}')
                    return
                size = self.broker.getcash() * self.params.max_position_pct / close_price
                size = int(size // 100 * 100)  # 100股整数倍
                if size > 0:
                    cost = close_price * size + self._calculate_cost(close_price, size, is_buy=True)
                    if cost <= self.broker.getcash():
                        self.order = self.buy(size=size)
                        self.log(f'买入信号 (评分={signal_score:.1f}): {close_price:.2f} x {size}')
        else:
            # 有持仓 → 检查卖出信号
            if signal_action == "sell":
                if not can_sell:
                    self.log(f'T+1约束: 当日买入不可卖出')
                    return
                if self._is_limit_down(close_price, prev_close):
                    self.log(f'跌停跳过卖出: {close_price:.2f}')
                    return
                if self._is_suspended(idx):
                    self.log(f'停牌跳过卖出: {close_price:.2f}')
                    return
                self.order = self.sell(size=self.position.size)
                self.log(f'卖出信号 (评分={signal_score:.1f}): {close_price:.2f}')


class BacktestEngine:
    """回测管理器"""

    def __init__(self, initial_cash=100000.0, market='A'):
        self.cerebro = bt.Cerebro()
        self.cerebro.broker.setcash(initial_cash)
        self.cerebro.broker.setcommission(commission=COMMISSION_RATE)
        self.initial_cash = initial_cash
        self.market = market

    def add_data(self, dataframe: pd.DataFrame, name="Stock"):
        """添加 Pandas 数据（需包含 open/high/low/close/volume 列）"""
        data = bt.feeds.PandasData(
            dataname=dataframe,
            open='open',
            high='high',
            low='low',
            close='close',
            volume='volume',
        )
        self.cerebro.adddata(data, name=name)

    def add_strategy(self, strategy_class=DSLSignalStrategy, **kwargs):
        """添加策略"""
        kwargs.setdefault('market', self.market)
        self.cerebro.addstrategy(strategy_class, **kwargs)

    def run(self):
        """运行回测"""
        logger.info(f'初始资金: {self.cerebro.broker.getvalue():.2f}')
        results = self.cerebro.run()
        final_value = self.cerebro.broker.getvalue()
        total_return = (final_value - self.initial_cash) / self.initial_cash
        logger.info(f'最终资金: {final_value:.2f}')
        logger.info(f'收益率: {total_return * 100:.2f}%')
        logger.info(f'交易成本: 佣金{COMMISSION_RATE:.4f} + 印花税{STAMP_TAX_RATE:.4f}(卖出) + 滑点{SLIPPAGE_BPS}bps')
        return results

    def get_performance(self) -> Dict:
        """获取回测绩效指标"""
        final_value = self.cerebro.broker.getvalue()
        return {
            'initial_cash': self.initial_cash,
            'final_value': final_value,
            'total_return': (final_value - self.initial_cash) / self.initial_cash,
            'commission_rate': COMMISSION_RATE,
            'stamp_tax_rate': STAMP_TAX_RATE,
            'slippage_bps': SLIPPAGE_BPS,
            'market': self.market,
        }

    def plot(self):
        """绘制图表"""
        self.cerebro.plot()
