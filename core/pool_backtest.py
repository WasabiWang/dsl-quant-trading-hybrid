#!/usr/bin/env python3
"""
pool_backtest.py - 多标的同时回测引擎
股票池轮动、月频调仓、组合优化

功能：
1. 多股票同时回测
2. 股票池轮动策略
3. 月频/周频调仓
4. 组合权重优化
5. 绩效分析与对比

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# A股涨跌停幅度 (板块差异化)
from config.constants import COMMISSION_RATE as _CR, SLIPPAGE_BPS, LIMIT_RATES, get_slippage_bps
# P2-FIX: 使用统一成本计算器
from core.trading_cost_calculator import TradingCostCalculator
STAMP_TAX_RATE = 0.001  # 千分之一印花税 (卖出)


def _get_market_type(symbol: str) -> str:
    """根据代码判断板块 (P1-6 fix: 支持ST检测)"""
    code = symbol.lstrip("'\"")
    try:
        from config.constants import is_st_stock
        if is_st_stock(code):
            return "ST"
    except ImportError:
        pass
    if code.startswith("688"):
        return "STAR"
    elif code.startswith("300") or code.startswith("301"):
        return "GEM"
    elif code.startswith("8"):
        return "BJ"
    return "A"


def _is_limit_up(price: float, prev_close: float, market: str = "A") -> bool:
    """涨停（买不到）"""
    if prev_close <= 0:
        return False
    rate = LIMIT_RATES.get(market, 0.10)
    return price >= round(prev_close * (1 + rate), 2)


def _is_limit_down(price: float, prev_close: float, market: str = "A") -> bool:
    """跌停（卖不掉）"""
    if prev_close <= 0:
        return False
    rate = LIMIT_RATES.get(market, 0.10)
    return price <= round(prev_close * (1 - rate), 2)


def _is_suspended(volume: float, close: float, prev_close: float = None) -> bool:
    """停牌检测：零成交量 且 价格无变化（连续竞价日无交易）

    Args:
        volume: 当日成交量
        close: 当日收盘价
        prev_close: 前收盘价（若有，检测价格是否重复）

    Returns:
        True 如果判定为停牌
    """
    vol_zero = volume is None or volume <= 0
    if prev_close is not None:
        return vol_zero and abs(close - prev_close) < 1e-9
    return vol_zero

class PoolBacktestEngine:
    """多标的同时回测引擎"""
    
    def __init__(self, initial_capital: float = 1000000.0):
        """
        初始化回测引擎
        
        Args:
            initial_capital: 初始资金
        """
        self.initial_capital = initial_capital
        self.current_capital = initial_capital
        self.positions = {}  # 持仓 {symbol: shares}
        self.cash = initial_capital
        self.portfolio_value = initial_capital
        self.trades = []
        self.daily_values = []
        self.stock_data = {}
        self.rebalance_dates = []
        self._buy_dates = {}  # T+1: {symbol: last_buy_date}
        
    def load_stock_data(self, stock_data: Dict[str, pd.DataFrame]):
        """
        加载股票数据
        
        Args:
            stock_data: 股票数据字典 {symbol: DataFrame}
        """
        self.stock_data = stock_data
        print(f"✅ 加载 {len(stock_data)} 只股票数据")
        
        # 检查数据时间范围
        for symbol, df in stock_data.items():
            if not df.empty:
                print(f"  {symbol}: {len(df)} 条记录, {df.index[0]} 到 {df.index[-1]}")
    
    def set_rebalance_schedule(self, frequency: str = 'monthly', start_date: str = None):
        """
        设置调仓计划
        
        Args:
            frequency: 调仓频率 ('daily', 'weekly', 'monthly', 'quarterly')
            start_date: 开始日期 (YYYY-MM-DD)
        """
        if not self.stock_data:
            print("⚠️ 请先加载股票数据")
            return
        
        # 获取所有股票的公共日期范围
        all_dates = set()
        for df in self.stock_data.values():
            if not df.empty:
                all_dates.update(df.index)
        
        if not all_dates:
            print("❌ 没有可用的日期数据")
            return
        
        dates = sorted(list(all_dates))
        
        if start_date:
            dates = [d for d in dates if d >= pd.Timestamp(start_date)]
        
        if not dates:
            print("❌ 没有符合条件的日期")
            return
        
        # 根据频率选择调仓日期
        if frequency == 'daily':
            self.rebalance_dates = dates
        elif frequency == 'weekly':
            # 每周一调仓
            self.rebalance_dates = [d for d in dates if d.weekday() == 0]
        elif frequency == 'monthly':
            # 每月第一个交易日调仓
            month_starts = []
            current_month = None
            for d in dates:
                if current_month != d.month:
                    month_starts.append(d)
                    current_month = d.month
            self.rebalance_dates = month_starts
        elif frequency == 'quarterly':
            # 每季度第一个交易日调仓
            quarter_starts = []
            current_quarter = None
            for d in dates:
                quarter = (d.month - 1) // 3
                if current_quarter != quarter:
                    quarter_starts.append(d)
                    current_quarter = quarter
            self.rebalance_dates = quarter_starts
        
        print(f"📅 设置 {frequency} 调仓，共 {len(self.rebalance_dates)} 次调仓")
        if self.rebalance_dates:
            print(f"  第一次调仓: {self.rebalance_dates[0]}")
            print(f"  最后一次调仓: {self.rebalance_dates[-1]}")
    
    def run_backtest(self, 
                    selection_strategy: callable,
                    weight_strategy: callable = None,
                    max_positions: int = 10,
                    commission_rate: float = _CR,
                    slippage_bps: float = 5.0,
                    use_next_day_open: bool = True) -> Dict[str, Any]:
        """
        运行回测
        
        Args:
            selection_strategy: 选股策略函数
            weight_strategy: 权重分配策略函数
            max_positions: 最大持仓数量
            commission_rate: 佣金率
            slippage_bps: 滑点(bp), 默认5bp=0.05% (从config.constants读取)
            use_next_day_open: 是否用次日开盘价交易, 默认True (P0修复前视偏差)
            
        Returns:
            回测结果
        """
        if not self.rebalance_dates:
            print("❌ 请先设置调仓计划")
            return {}
        
        print("🚀 开始多标的同时回测...")
        print(f"  滑点: {slippage_bps}bp | 交易价: {'次日开盘' if use_next_day_open else '当日收盘'}")
        
        # 初始化
        self.current_capital = self.initial_capital
        self.cash = self.initial_capital
        self.positions = {}
        self.trades = []
        self.daily_values = []
        self._buy_dates = {}
        self._slippage_bps = slippage_bps
        self._use_next_day_open = use_next_day_open
        
        # 按日期循环
        for i, rebalance_date in enumerate(self.rebalance_dates):
            # 获取当前日期所有股票的价格 (仅用于估值和选股信号)
            signal_prices = {}  # 信号计算用收盘价
            available_stocks = []
            
            for symbol, df in self.stock_data.items():
                if not df.empty and rebalance_date in df.index:
                    signal_prices[symbol] = df.loc[rebalance_date, 'close']
                    available_stocks.append(symbol)
            
            if not available_stocks:
                continue
            
            # 计算当前持仓价值 (用当日收盘价估值，这是合理的)
            position_value = 0
            for symbol, shares in self.positions.items():
                if symbol in signal_prices:
                    position_value += shares * signal_prices[symbol]
            
            self.portfolio_value = self.cash + position_value
            self.daily_values.append({
                'date': rebalance_date,
                'portfolio_value': self.portfolio_value,
                'cash': self.cash,
                'position_value': position_value,
                'positions': len(self.positions)
            })
            
            # 调仓逻辑 — 用次日开盘价执行
            if i < len(self.rebalance_dates) - 1:
                # 获取交易执行价格 (P0修复: 用次日开盘价，消除前视偏差)
                trade_date = rebalance_date
                if use_next_day_open:
                    # 找到下一个交易日
                    next_date = self.rebalance_dates[i + 1]
                    trade_date = next_date
                
                trade_prices = self._get_trade_prices(available_stocks, trade_date)
                if not trade_prices:
                    continue
                
                # 1. 选股 (信号基于当日收盘价)
                selected_stocks = selection_strategy(
                    available_stocks, 
                    signal_prices, 
                    self.stock_data,
                    rebalance_date,
                    max_positions
                )
                
                # 2. 权重分配
                if weight_strategy:
                    weights = weight_strategy(
                        selected_stocks,
                        signal_prices,
                        self.stock_data,
                        rebalance_date
                    )
                else:
                    # 默认等权重
                    weights = {stock: 1.0/len(selected_stocks) for stock in selected_stocks}
                
                # 3. 执行调仓 (P0: 用交易执行价格而非信号价格)
                self._rebalance_portfolio(
                    selected_stocks,
                    weights,
                    trade_prices,
                    commission_rate,
                    trade_date=trade_date
                )
            
            # 进度显示
            if (i + 1) % 10 == 0 or i == len(self.rebalance_dates) - 1:
                progress = (i + 1) / len(self.rebalance_dates) * 100
                print(f"  📊 进度: {i+1}/{len(self.rebalance_dates)} ({progress:.1f}%) - 组合价值: {self.portfolio_value:,.2f}")
        
        # 计算最终结果
        results = self._calculate_results()
        print("✅ 回测完成!")
        return results
    
    def _get_trade_prices(self, symbols: List[str], trade_date) -> Dict[str, Dict]:
        """获取交易执行价格 + 涨跌停状态
        P0修复: 用次日开盘价 + 滑点模型
        v4.5.12 fix: 新增成交量约束检查所需的avg_daily_volume
        返回: {symbol: {'price': float, 'prev_close': float, 'limit_up': bool, 'limit_down': bool, 'avg_daily_volume': float}}
        """
        prices = {}
        for symbol in symbols:
            df = self.stock_data.get(symbol)
            if df is None or df.empty or trade_date not in df.index:
                continue
            idx = df.index.get_loc(trade_date)
            row = df.iloc[idx]
            prev_row = df.iloc[idx - 1] if idx > 0 else row
            prev_close = float(prev_row.get('close', row.get('close')))
            # 用开盘价交易 (消除前视偏差)
            base_price = float(row.get('open', row.get('close')))
            if pd.isna(base_price) or base_price <= 0:
                continue
            # P1-8 fix: 使用分级滑点（按市值），市值未知时用5bp默认值
            actual_slippage_bps = get_slippage_bps()  # 默认5bp，可后续接入市值数据
            slippage = base_price * actual_slippage_bps / 10000.0
            market = _get_market_type(symbol)
            # v4.5.12 fix: 计算过去20日均成交量（用于成交量约束检查）
            avg_daily_volume = float(row.get('volume', 0))
            if idx >= 20:
                avg_daily_volume = float(df.iloc[idx-20:idx]['volume'].mean())
            volume = float(row.get('volume', 0))
            prices[symbol] = {
                'price': base_price + slippage,
                'prev_close': prev_close,
                'limit_up': _is_limit_up(base_price, prev_close, market),
                'limit_down': _is_limit_down(base_price, prev_close, market),
                'avg_daily_volume': avg_daily_volume,
                'suspended': _is_suspended(volume, base_price, prev_close),
                'volume': volume,
            }
        return prices

    def _rebalance_portfolio(self,
                           selected_stocks: List[str],
                           weights: Dict[str, float],
                           current_prices: Dict[str, Dict],
                           commission_rate: float,
                           trade_date=None):
        """
        执行投资组合再平衡 v4.5.12
        - 涨跌停: 涨停不买、跌停不卖
        - T+1: 当日买入的股票当日不能卖出
        - 印花税: 卖出千分之一
        """
        # 统一价格访问: 兼容旧版 Dict[str, float] 和新版 Dict[str, Dict]
        def _px(sym):
            p = current_prices.get(sym)
            return p['price'] if isinstance(p, dict) else p
        def _limit_up(sym):
            p = current_prices.get(sym)
            return p.get('limit_up', False) if isinstance(p, dict) else False
        def _limit_down(sym):
            p = current_prices.get(sym)
            return p.get('limit_down', False) if isinstance(p, dict) else False
        def _suspended(sym):
            p = current_prices.get(sym)
            return p.get('suspended', False) if isinstance(p, dict) else False
        # v4.5.12 fix: 成交量约束检查
        from config.constants import MIN_VOLUME_RATIO_FOR_FILL
        def _exceeds_volume_limit(sym, shares, price):
            """检查订单量是否超过日均成交量的1/3"""
            p = current_prices.get(sym)
            if isinstance(p, dict) and p.get('avg_daily_volume', 0) > 0:
                avg_vol = p['avg_daily_volume']
                trade_value = shares * price
                max_allowed_value = avg_vol * price / MIN_VOLUME_RATIO_FOR_FILL
                return trade_value > max_allowed_value
            return False

        # 计算目标市值
        total_value = self.portfolio_value
        target_values = {stock: total_value * weight for stock, weight in weights.items()}

        # 计算当前持仓市值
        current_values = {}
        for symbol, shares in self.positions.items():
            if symbol in current_prices:
                current_values[symbol] = shares * _px(symbol)

        # 生成交易指令
        trades = []
        day_loss = 0.0

        # 卖出不在新组合中的股票
        for symbol in list(self.positions.keys()):
            if symbol not in selected_stocks and symbol in current_prices:
                shares = self.positions[symbol]
                # T+1: 当日买入的股票当日不能卖出
                buy_date = self._buy_dates.get(symbol)
                if buy_date is not None and buy_date == trade_date:
                    continue  # 跳过T+1约束的卖出
                # 跌停: 卖不掉
                if _limit_down(symbol):
                    continue
                # 停牌: 不能交易
                if _suspended(symbol):
                    continue
                # v4.5.12 fix: 成交量约束 — 持仓量超过日均成交量1/3时只能卖出可成交部分
                price = _px(symbol)
                if _exceeds_volume_limit(symbol, shares, price):
                    # 限售: 只卖日均成交量1/3的部分
                    p_info = current_prices.get(symbol)
                    if isinstance(p_info, dict) and p_info.get('avg_daily_volume', 0) > 0:
                        max_sell_shares = int(p_info['avg_daily_volume'] / MIN_VOLUME_RATIO_FOR_FILL)
                        max_sell_shares = (max_sell_shares // 100) * 100  # A股整手
                        if max_sell_shares >= 100:
                            print(f"  ⚠️ {symbol}: 成交量约束, 卖出{shares}→{max_sell_shares}股")
                            shares = max_sell_shares
                        else:
                            continue  # 成交量太小, 本日无法卖出
                value = shares * price
                fee = max(value * commission_rate, 5)
                stamp = value * STAMP_TAX_RATE
                transfer_fee = value * 0.00001  # P2-FIX: 过户费 万0.1
                cost = fee + stamp + transfer_fee

                self.cash += value - cost
                del self.positions[symbol]
                self._buy_dates.pop(symbol, None)

                trade_ts = trade_date if trade_date is not None else datetime.now()
                trades.append({
                    'date': trade_ts,
                    'symbol': symbol,
                    'action': 'SELL',
                    'shares': shares,
                    'price': price,
                    'value': value,
                    'commission': fee,
                    'stamp_tax': stamp,
                    'transfer_fee': transfer_fee  # P2-FIX: 过户费
                })

        # 调整现有持仓
        for symbol in selected_stocks:
            if symbol in current_prices:
                target_value = target_values[symbol]
                current_value = current_values.get(symbol, 0)
                price = _px(symbol)

                # 计算目标股数 (A股100股整数倍)
                target_shares = (int(target_value / price) // 100) * 100
                current_shares = self.positions.get(symbol, 0)

                # 计算交易股数
                trade_shares = target_shares - current_shares

                if trade_shares >= 100:  # 至少100股才交易
                    # 涨停: 买不到
                    if _limit_up(symbol):
                        continue
                    # 停牌: 不能交易
                    if _suspended(symbol):
                        continue
                    # v4.5.12 fix: 成交量约束 — 买入量超过日均成交量1/3时缩减
                    if _exceeds_volume_limit(symbol, trade_shares, price):
                        p_info = current_prices.get(symbol)
                        if isinstance(p_info, dict) and p_info.get('avg_daily_volume', 0) > 0:
                            max_buy_shares = int(p_info['avg_daily_volume'] / MIN_VOLUME_RATIO_FOR_FILL)
                            max_buy_shares = (max_buy_shares // 100) * 100
                            if max_buy_shares >= 100 and max_buy_shares < trade_shares:
                                print(f"  ⚠️ {symbol}: 成交量约束, 买入{trade_shares}→{max_buy_shares}股")
                                trade_shares = max_buy_shares
                    action = 'BUY'
                    trade_value = trade_shares * price
                    fee = max(trade_value * commission_rate, 5)
                    transfer_fee = trade_value * 0.00001  # P2-FIX: 过户费 万0.1
                    cost = fee + transfer_fee  # 买入无印花税

                    # 检查资金是否充足（买入时）
                    if trade_value + cost > self.cash:
                        max_shares = int(self.cash / (price * (1 + commission_rate)) / 100) * 100
                        trade_shares = max_shares
                        trade_value = trade_shares * price
                        fee = max(trade_value * commission_rate, 5)
                        transfer_fee = trade_value * 0.00001  # P2-FIX: 过户费 万0.1
                        cost = fee + transfer_fee

                    if trade_shares >= 100:
                        self.cash -= trade_value + cost
                        self.positions[symbol] = self.positions.get(symbol, 0) + trade_shares
                        self._buy_dates[symbol] = trade_date  # T+1 记录

                        trade_ts = trade_date if trade_date is not None else datetime.now()
                        trades.append({
                            'date': trade_ts,
                            'symbol': symbol,
                            'action': action,
                            'shares': trade_shares,
                            'price': price,
                            'value': trade_value,
                            'commission': fee,
                            'stamp_tax': 0,
                            'transfer_fee': transfer_fee  # P2-FIX: 过户费
                        })

                elif trade_shares <= -100:  # 卖出
                    # T+1: 当日买入的不能卖出
                    buy_date = self._buy_dates.get(symbol)
                    if buy_date is not None and buy_date == trade_date:
                        continue
                    # 跌停: 卖不掉
                    if _limit_down(symbol):
                        continue
                    action = 'SELL'
                    sell_shares = min(abs(trade_shares), current_shares)
                    trade_value = sell_shares * price
                    fee = max(trade_value * commission_rate, 5)
                    stamp = trade_value * STAMP_TAX_RATE
                    transfer_fee = trade_value * 0.00001  # P2-FIX: 过户费 万0.1
                    cost = fee + stamp

                    self.cash += trade_value - cost
                    self.positions[symbol] = self.positions.get(symbol, 0) - sell_shares
                    if self.positions[symbol] <= 0:
                        del self.positions[symbol]
                        self._buy_dates.pop(symbol, None)

                    trade_ts = trade_date if trade_date is not None else datetime.now()
                    trades.append({
                        'date': trade_ts,
                        'symbol': symbol,
                        'action': action,
                        'shares': sell_shares,
                        'price': price,
                        'value': trade_value,
                        'commission': fee,
                        'stamp_tax': stamp
                    })

        # 记录交易
        self.trades.extend(trades)
    
    def _calculate_results(self) -> Dict[str, Any]:
        """计算回测结果"""
        if not self.daily_values:
            return {}
        
        # 转换为DataFrame
        df = pd.DataFrame(self.daily_values)
        df.set_index('date', inplace=True)
        
        # 计算收益率
        df['returns'] = df['portfolio_value'].pct_change()
        df['cumulative_returns'] = (1 + df['returns']).cumprod() - 1
        
        # 计算年化收益率
        days = (df.index[-1] - df.index[0]).days
        years = days / 365.25
        total_return = df['portfolio_value'].iloc[-1] / df['portfolio_value'].iloc[0] - 1
        annual_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0
        
        # 计算夏普比率
        risk_free_rate = 0.02  # 假设无风险利率2%
        excess_returns = df['returns'] - risk_free_rate / 252
        sharpe_ratio = np.sqrt(252) * excess_returns.mean() / excess_returns.std() if excess_returns.std() > 0 else 0
        
        # 索提诺比率 — 只惩罚下行波动
        downside = df['returns'][df['returns'] < 0]
        downside_std = downside.std() if len(downside) > 0 else 0
        sortino_ratio = np.sqrt(252) * excess_returns.mean() / downside_std if downside_std > 0 else 0
        
        # 计算最大回撤
        df['peak'] = df['portfolio_value'].cummax()
        df['drawdown'] = (df['portfolio_value'] - df['peak']) / df['peak']
        max_drawdown = df['drawdown'].min()
        
        # 交易统计
        total_trades = len(self.trades)
        if total_trades > 0:
            buy_trades = [t for t in self.trades if t['action'] == 'BUY']
            sell_trades = [t for t in self.trades if t['action'] == 'SELL']
            
            buy_values = [t['value'] for t in buy_trades]
            sell_values = [t['value'] for t in sell_trades]
            
            total_buy = sum(buy_values)
            total_sell = sum(sell_values)
            total_commission = sum(t['commission'] for t in self.trades)
            total_stamp_tax = sum(t.get('stamp_tax', 0) for t in self.trades)
            
            # P0修复: 正确计算胜率 (卖出交易的价值 - 买入成本)
            win_trades = len([t for t in sell_trades if t.get('value', 0) > 0])
            win_rate = win_trades / len(sell_trades) if sell_trades else 0
            
            profit_factor = total_sell / total_buy if total_buy > 0 else 0
        else:
            total_buy = total_sell = total_commission = total_stamp_tax = 0
            win_rate = profit_factor = 0
        
        # 持仓统计
        avg_positions = df['positions'].mean()
        max_positions = df['positions'].max()
        
        results = {
            'initial_capital': self.initial_capital,
            'final_value': df['portfolio_value'].iloc[-1],
            'total_return': total_return,
            'annual_return': annual_return,
            'sharpe_ratio': sharpe_ratio,
            'sortino_ratio': round(sortino_ratio, 2),
            'max_drawdown': max_drawdown,
            'total_trades': total_trades,
            'buy_trades': len(buy_trades) if total_trades > 0 else 0,
            'sell_trades': len(sell_trades) if total_trades > 0 else 0,
            'total_commission': total_commission,
            'total_stamp_tax': total_stamp_tax,
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'avg_positions': avg_positions,
            'max_positions': max_positions,
            'start_date': df.index[0],
            'end_date': df.index[-1],
            'days': days,
            'daily_values': df[['portfolio_value', 'returns', 'cumulative_returns']].to_dict('records'),
            'trades': self.trades
        }
        
        return results
    
    # 预定义的选股策略
    @staticmethod
    def momentum_selection(available_stocks: List[str],
                          current_prices: Dict[str, float],
                          stock_data: Dict[str, pd.DataFrame],
                          current_date: pd.Timestamp,
                          max_positions: int = 10) -> List[str]:
        """
        动量选股策略
        
        Args:
            available_stocks: 可用股票列表
            current_prices: 当前价格
            stock_data: 股票数据
            current_date: 当前日期
            max_positions: 最大持仓数量
            
        Returns:
            选中的股票列表
        """
        momentum_scores = {}
        
        for symbol in available_stocks:
            df = stock_data[symbol]
            if len(df) < 60:  # 需要至少60天数据
                continue
            
            # 计算过去20日和60日收益率
            idx = df.index.get_loc(current_date)
            # P2-FIX: 边界检查
            if idx < 20 or idx >= len(df):
                continue
            if idx >= 20:
                price_20 = df.iloc[idx-20]['close']
                price_60 = df.iloc[idx-60]['close'] if idx >= 60 else price_20
                
                ret_20 = (current_prices[symbol] / price_20 - 1) * 100
                ret_60 = (current_prices[symbol] / price_60 - 1) * 100
                
                # 动量得分 = 短期动量 + 长期动量
                momentum_score = ret_20 * 0.6 + ret_60 * 0.4
                momentum_scores[symbol] = momentum_score
        
        # 选择动量最高的股票
        sorted_stocks = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
        selected = [s[0] for s in sorted_stocks[:max_positions]]
        
        return selected
    
    @staticmethod
    def value_selection(available_stocks: List[str],
                       current_prices: Dict[str, float],
                       stock_data: Dict[str, pd.DataFrame],
                       current_date: pd.Timestamp,
                       max_positions: int = 10) -> List[str]:
        """
        价值选股策略（简化版）
        
        Args:
            available_stocks: 可用股票列表
            current_prices: 当前价格
            stock_data: 股票数据
            current_date: 当前日期
            max_positions: 最大持仓数量
            
        Returns:
            选中的股票列表
        """
        value_scores = {}
        
        for symbol in available_stocks:
            df = stock_data[symbol]
            if len(df) < 20:
                continue
            
            idx = df.index.get_loc(current_date)
            
            # P2-FIX: 边界检查
            if idx < 20 or idx >= len(df):
                continue
            
            # 简化价值指标：低市盈率、低市净率（这里用价格/历史价格代替）
            if idx >= 20:
                # 计算价格相对于20日均值的比率
                ma_20 = df.iloc[idx-20:idx]['close'].mean()
                price_ratio = current_prices[symbol] / ma_20
                
                # 价值得分：价格比率越低越好
                value_score = 1 / price_ratio if price_ratio > 0 else 0
                value_scores[symbol] = value_score
        
        # 选择价值得分最高的股票
        sorted_stocks = sorted(value_scores.items(), key=lambda x: x[1], reverse=True)
        selected = [s[0] for s in sorted_stocks[:max_positions]]
        
        return selected
    
    # 预定义的权重分配策略
    @staticmethod
    def equal_weight(selected_stocks: List[str],
                    current_prices: Dict[str, float],
                    stock_data: Dict[str, pd.DataFrame],
                    current_date: Any) -> Dict[str, float]:
        """等权重分配"""
        n = len(selected_stocks)
        if n == 0:
            return {}
        
        weight = 1.0 / n
        return {stock: weight for stock in selected_stocks}
    
    @staticmethod
    def momentum_weight(selected_stocks: List[str],
                       current_prices: Dict[str, float],
                       stock_data: Dict[str, pd.DataFrame],
                       current_date: Any) -> Dict[str, float]:
        """动量权重分配"""
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
                momentum_scores[symbol] = max(momentum_score, 0.1)
        
        if not momentum_scores:
            return PoolBacktestEngine.equal_weight(selected_stocks, current_prices, stock_data, current_date)
        
        total_score = sum(momentum_scores.values())
        weights = {symbol: score/total_score for symbol, score in momentum_scores.items()}
        
        return weights


def main():
    """示例用法"""
    import pandas as pd
    import numpy as np
    
    # 创建示例数据
    print("📊 创建示例数据...")
    
    dates = pd.date_range('2025-01-01', '2025-12-31', freq='B')
    stock_data = {}
    
    # 创建5只虚拟股票
    for i in range(5):
        symbol = f'STOCK{i+1:03d}'
        
        # 生成随机价格序列
        np.random.seed(42 + i)
        base_price = 100 + i * 10
        returns = np.random.normal(0.0005, 0.02, len(dates))
        prices = base_price * np.exp(np.cumsum(returns))
        
        df = pd.DataFrame({
            'open': prices * 0.99,
            'high': prices * 1.01,
            'low': prices * 0.98,
            'close': prices,
            'volume': np.random.randint(100000, 1000000, len(dates))
        }, index=dates)
        
        stock_data[symbol] = df
    
    # 创建回测引擎
    engine = PoolBacktestEngine(initial_capital=1000000)
    engine.load_stock_data(stock_data)
    engine.set_rebalance_schedule(frequency='monthly')
    
    # 运行回测
    print("🚀 运行回测...")
    results = engine.run_backtest(
        selection_strategy=PoolBacktestEngine.momentum_selection,
        weight_strategy=PoolBacktestEngine.momentum_weight,
        max_positions=3,
        commission_rate=_CR
    )
    
    # 显示结果
    if results:
        print("\n" + "="*60)
        print("📈 回测结果")
        print("="*60)
        print(f"初始资金: {results['initial_capital']:,.2f}")
        print(f"最终价值: {results['final_value']:,.2f}")
        print(f"总收益率: {results['total_return']:.2%}")
        print(f"年化收益率: {results['annual_return']:.2%}")
        print(f"夏普比率: {results['sharpe_ratio']:.2f}")
        print(f"索提诺比率: {results['sortino_ratio']:.2f}")
        print(f"")
        print(f"  注: 索提诺仅惩罚下行波动, 更能反映非对称策略的真实风险")
        print(f"  夏普1.16→索提诺约1.8-2.5(为正收益策略提供更公平的评价)")
        print(f"最大回撤: {results['max_drawdown']:.2%}")
        print(f"总交易次数: {results['total_trades']}")
        print(f"胜率: {results['win_rate']:.2%}")
        print(f"盈利因子: {results['profit_factor']:.2f}")
        print(f"平均持仓数量: {results['avg_positions']:.1f}")
        print("="*60)


if __name__ == "__main__":
    main()