#!/usr/bin/env python3
"""
dsl_engine.py - DSL量化交易引擎核心模块 v4.5.12

功能：
1. DSL语法设计（原语、验证器）
2. DSL执行引擎（JSON→Backtrader）
3. 涨跌停价格限制回测
4. A股特色指标扩展
5. P0: 滑点模型 & 常量统一管理
"""

import json
import ast
import pandas as pd
import numpy as np
from typing import Dict, Tuple
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# P0: 导入统一常量 — 所有交易参数唯一来源 config/constants.py
from config.constants import (
    COMMISSION_RATE, STAMP_TAX_RATE, MIN_COMMISSION, MIN_TRADE_UNIT,
    LIMIT_RATES, SLIPPAGE_BPS, get_slippage_bps, MIN_VOLUME_RATIO_FOR_FILL
)

class DSLValidator:
    """DSL语法验证器"""
    
    @staticmethod
    def validate_strategy_json(strategy_json: Dict) -> Tuple[bool, str]:
        """
        验证策略JSON格式 + 语义

        检查: 必填字段 / indicator type合法性 / params类型 /
              condition字符串可解析性 / signal action合法性

        Returns:
            (是否有效, 错误信息)
        """
        required_fields = ['name', 'symbol', 'timeframe', 'indicators', 'signals']

        for field in required_fields:
            if field not in strategy_json:
                return False, f"缺少必要字段: {field}"

        # 验证时间框架
        valid_timeframes = {'daily', 'weekly', 'monthly', '60m', '30m', '15m', '5m'}
        tf = strategy_json.get('timeframe', '')
        if tf not in valid_timeframes:
            return False, f"非法timeframe: '{tf}'，有效值: {sorted(valid_timeframes)}"

        # 验证指标
        if not isinstance(strategy_json['indicators'], list):
            return False, "indicators必须是列表"
        valid_primitives = set(DSLValidator.get_available_primitives().keys())
        for i, ind in enumerate(strategy_json['indicators']):
            if not isinstance(ind, dict):
                return False, f"indicators[{i}]必须是字典"
            ind_type = ind.get('type', '')
            if ind_type not in valid_primitives:
                return False, f"indicators[{i}]非法类型: '{ind_type}'，有效值: {sorted(valid_primitives)}"
            params = ind.get('params', {})
            if 'period' in params:
                p = params['period']
                if not isinstance(p, int) or p < 1:
                    return False, f"indicators[{i}].params.period必须为正整数，实际: {p}"

        # 验证信号
        if not isinstance(strategy_json['signals'], list):
            return False, "signals必须是列表"
        if len(strategy_json['signals']) == 0:
            return False, "signals不能为空"
        valid_actions = {'buy', 'sell', 'hold'}
        for i, sig in enumerate(strategy_json['signals']):
            if not isinstance(sig, dict):
                return False, f"signals[{i}]必须是字典"
            condition = sig.get('condition', '')
            if not condition or not isinstance(condition, str):
                return False, f"signals[{i}]缺少condition字符串"
            # 检查condition是否引用已知的indicator名称
            indicator_names = {ind.get('name', '') for ind in strategy_json['indicators']}
            for ind_name in indicator_names:
                if ind_name and ind_name in condition:
                    break
            else:
                # 检查是否是内置条件原语
                if not any(condition.startswith(p) for p in
                          ['cross_above(', 'cross_below(', 'greater_than(', 'less_than(']):
                    if condition not in valid_primitives:
                        return False, (
                            f"signals[{i}].condition '{condition}' 未引用任何indicators name "
                            f"且不是已知原语. indicators names: {indicator_names}"
                        )
            action = sig.get('action', '')
            if action not in valid_actions:
                return False, f"signals[{i}].action非法: '{action}'，有效值: {sorted(valid_actions)}"

        return True, "验证通过"
    
    @staticmethod
    def get_available_primitives() -> Dict[str, Dict]:
        """获取可用的DSL原语"""
        return {
            # 技术指标原语
            'ma': {
                'description': '移动平均线',
                'params': ['period'],
                'returns': 'float'
            },
            'ema': {
                'description': '指数移动平均线',
                'params': ['period'],
                'returns': 'float'
            },
            'rsi': {
                'description': '相对强弱指数',
                'params': ['period'],
                'returns': 'float'
            },
            'macd': {
                'description': 'MACD指标',
                'params': ['fast', 'slow', 'signal'],
                'returns': 'tuple'
            },
            'bollinger': {
                'description': '布林带',
                'params': ['period', 'std'],
                'returns': 'tuple'
            },
            'volume_ratio': {
                'description': '成交量比率',
                'params': ['period'],
                'returns': 'float'
            },
            'turnover_rate': {
                'description': '换手率',
                'params': [],
                'returns': 'float'
            },
            
            # A股特色指标
            'consecutive_limit_up': {
                'description': '连板天数',
                'params': [],
                'returns': 'int'
            },
            'turnover_rate_spike': {
                'description': '换手率异动',
                'params': ['threshold'],
                'returns': 'bool'
            },
            'net_capital_flow': {
                'description': '主力资金净流入(万元)',
                'params': ['period'],
                'returns': 'float'
            },
            'north_flow_in': {
                'description': '北向资金当日净流入(亿元)',
                'params': [],
                'returns': 'float'
            },
            'dragon_tiger_flag': {
                'description': '龙虎榜上榜标记',
                'params': [],
                'returns': 'bool'
            },
            'limit_up_distortion': {
                'description': '涨停失真标记',
                'params': [],
                'returns': 'bool'
            },
            
            # 逻辑原语
            'cross_above': {
                'description': '上穿',
                'params': ['series1', 'series2'],
                'returns': 'bool'
            },
            'cross_below': {
                'description': '下穿',
                'params': ['series1', 'series2'],
                'returns': 'bool'
            },
            'greater_than': {
                'description': '大于',
                'params': ['value1', 'value2'],
                'returns': 'bool'
            },
            'less_than': {
                'description': '小于',
                'params': ['value1', 'value2'],
                'returns': 'bool'
            },
            
            # 状态原语
            'hold_position': {
                'description': '持有仓位',
                'params': [],
                'returns': 'bool'
            },
            'entry_signal': {
                'description': '入场信号',
                'params': ['condition'],
                'returns': 'bool'
            },
            'exit_signal': {
                'description': '出场信号',
                'params': ['condition'],
                'returns': 'bool'
            }
        }

class DSLExecutor:
    """DSL执行引擎（JSON→回测）v4.5.12"""

    def __init__(self, initial_cash: float = 1000000):
        self.validator = DSLValidator()
        self.initial_cash = initial_cash
        self.commission_rate = COMMISSION_RATE
        self.stamp_tax = STAMP_TAX_RATE
        self.min_commission = MIN_COMMISSION
        self.min_trade_unit = MIN_TRADE_UNIT
        # v4.5.12 P1: 分级滑点 — 通过get_slippage_bps按市值动态计算
        self.slippage_bps = SLIPPAGE_BPS  # 默认值
        self._market_cap_cache = {}  # {symbol: market_cap} 缓存

    def _detect_market_type(self, symbol: str) -> str:
        """P2-FIX: 根据股票代码自动判断板块 (P1-6: 支持ST检测)"""
        code = symbol.strip("'\"")
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

    def _check_limit_price(self, price: float, prev_close: float, market: str = None, symbol: str = None) -> bool:
        """P2-FIX: 检查是否触及涨跌停限制，支持ST/北交所自动检测"""
        if pd.isna(prev_close) or prev_close == 0:
            return False
        # P2-FIX: 自动检测板块类型
        if market is None and symbol is not None:
            market = self._detect_market_type(symbol)
        if market is None:
            market = 'A'
        rate = LIMIT_RATES.get(market, 0.10)
        limit_up = prev_close * (1 + rate)
        limit_down = prev_close * (1 - rate)
        return price >= limit_up or price <= limit_down

    def _is_limit_up(self, price: float, prev_close: float, market: str = None, symbol: str = None) -> bool:
        """P2-FIX: 检查是否涨停（买不到），支持ST/北交所自动检测"""
        if pd.isna(prev_close) or prev_close == 0:
            return False
        # P2-FIX: 自动检测板块类型
        if market is None and symbol is not None:
            market = self._detect_market_type(symbol)
        if market is None:
            market = 'A'
        rate = LIMIT_RATES.get(market, 0.10)
        return price >= prev_close * (1 + rate)

    def _is_limit_down(self, price: float, prev_close: float, market: str = None, symbol: str = None) -> bool:
        """P2-FIX: 检查是否跌停（卖不掉），支持ST/北交所自动检测"""
        if pd.isna(prev_close) or prev_close == 0:
            return False
        # P2-FIX: 自动检测板块类型
        if market is None and symbol is not None:
            market = self._detect_market_type(symbol)
        if market is None:
            market = 'A'
        rate = LIMIT_RATES.get(market, 0.10)
        return price <= prev_close * (1 - rate)

    def _calculate_trading_cost(self, price: float, quantity: int, is_buy: bool = True,
                                symbol: str = None) -> float:
        """计算A股交易成本：佣金+印花税+分级滑点"""
        amount = price * quantity
        commission = max(amount * self.commission_rate, self.min_commission)
        stamp_tax = 0 if is_buy else amount * self.stamp_tax
        # v4.5.12 P1: 分级滑点 — 按市值动态计算
        mc = self._market_cap_cache.get(symbol) if symbol else None
        actual_slippage_bps = get_slippage_bps(mc)
        slippage = amount * actual_slippage_bps / 10000.0
        return commission + stamp_tax + slippage

    def _apply_a_share_rules(self, quantity: int, symbol: str = None,
                              price: float = None, volume: float = None, avg_volume: float = None) -> int:
        """A股最小交易单位: 主板/创业板100股, 科创板200股 + 成交量约束"""
        min_unit = self.min_trade_unit
        if symbol and symbol.startswith("688"):
            min_unit = 200
        adjusted = (quantity // min_unit) * min_unit
        adjusted = max(adjusted, min_unit)
        # P2-8: 成交量约束 — 订单金额不超过日均成交额的1/3
        if price and volume is not None and avg_volume and avg_volume > 0:
            max_qty_by_volume = int((avg_volume / MIN_VOLUME_RATIO_FOR_FILL) / price)
            if adjusted > max_qty_by_volume:
                adjusted = (max_qty_by_volume // min_unit) * min_unit
                adjusted = max(adjusted, min_unit)
        return adjusted

    # ═══════════════ 指标计算 ═══════════════

    def _compute_indicator(self, data: pd.DataFrame, ind_def: Dict, symbol: str = None) -> pd.Series:
        """根据DSL指标定义计算指标序列

        P2-11 fix: 使用 symbol 参数判断板块涨跌停幅度，消除0.099硬编码
        """
        ind_type = ind_def.get('type', '')
        params = ind_def.get('params', {})
        name = ind_def.get('name', ind_type)
        close = data.get('close', pd.Series(dtype=float))
        high = data.get('high', close)
        low = data.get('low', close)
        volume = data.get('volume', pd.Series(dtype=float))

        if ind_type == 'ma':
            return close.rolling(window=int(params.get('period', 5))).mean()
        elif ind_type == 'ema':
            return close.ewm(span=int(params.get('period', 12)), adjust=False).mean()
        elif ind_type == 'rsi':
            period = int(params.get('period', 14))
            delta = close.diff()
            gain = delta.clip(lower=0).rolling(period).mean()
            loss = (-delta.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, np.nan)
            return 100 - (100 / (1 + rs))
        elif ind_type == 'macd':
            fast = int(params.get('fast', 12))
            slow = int(params.get('slow', 26))
            signal = int(params.get('signal', 9))
            ema_fast = close.ewm(span=fast, adjust=False).mean()
            ema_slow = close.ewm(span=slow, adjust=False).mean()
            macd_line = ema_fast - ema_slow
            return macd_line - macd_line.ewm(span=signal, adjust=False).mean()
        elif ind_type == 'bollinger':
            period = int(params.get('period', 20))
            std_mul = float(params.get('std', 2))
            ma = close.rolling(period).mean()
            return ma + std_mul * close.rolling(period).std()
        elif ind_type == 'volume_ratio':
            period = int(params.get('period', 5))
            vol_ma = volume.rolling(period).mean()
            return volume / vol_ma.replace(0, np.nan)
        elif ind_type == 'turnover_rate':
            return data.get('turnover_rate', pd.Series(0, index=data.index))
        elif ind_type == 'consecutive_limit_up':
            # P2-11 fix: 使用 LIMIT_RATES 动态判断涨停，消除0.099硬编码
            market = self._detect_market_type(symbol) if symbol else 'A'
            limit_pct = LIMIT_RATES.get(market, 0.10)
            limit_up_series = (close / close.shift(1) - 1) >= (limit_pct - 0.001)
            result = pd.Series(0, index=data.index)
            cnt = 0
            for i in range(len(limit_up_series)):
                cnt = cnt + 1 if limit_up_series.iloc[i] else 0
                result.iloc[i] = cnt
            return result
        elif ind_type == 'turnover_rate_spike':
            tr = data.get('turnover_rate', pd.Series(0, index=data.index))
            tr_ma20 = tr.rolling(20).mean()
            threshold = float(params.get('threshold', 3.0))
            return (tr / tr_ma20.replace(0, np.nan)) > threshold
        elif ind_type == 'net_capital_flow':
            # 主力资金净流入：使用成交额×涨跌幅估算，实际应从Level2数据获取
            period = int(params.get('period', 5))
            amount = data.get('amount', volume * close)
            pct = data.get('pct_chg', close.pct_change())
            flow_est = amount * pct / 100
            return flow_est.rolling(window=period).mean()
        elif ind_type == 'north_flow_in':
            # 北向资金：从data中获取，如不存在返回0
            return data.get('north_flow', pd.Series(0.0, index=data.index))
        elif ind_type == 'dragon_tiger_flag':
            return data.get('dragon_tiger', pd.Series(False, index=data.index))
        elif ind_type == 'limit_up_distortion':
            # 涨停/跌停日的数据失真标记
            pct = data.get('pct_chg', pd.Series(0.0, index=data.index))
            # P2-11 fix: 使用动态涨跌停幅度，消除9.8硬编码
            market = self._detect_market_type(symbol) if symbol else 'A'
            limit_pct = LIMIT_RATES.get(market, 0.10)
            return abs(pct) >= (limit_pct * 100 - 0.2)
        else:
            return pd.Series(np.nan, index=data.index)

    # ═══════════════ 信号计算 ═══════════════

    def _evaluate_condition(self, indicators: Dict[str, pd.Series], condition_str: str, idx: int) -> bool:
        """评估DSL条件字符串，idx为当前数据位置"""
        if idx < 0 or idx >= len(next(iter(indicators.values())) if indicators else pd.Series()):
            return False

        def _val(name, offset=0):
            i = idx + offset
            if i < 0:
                i = 0
            s = indicators.get(name)
            if s is None or i >= len(s):
                return np.nan
            return float(s.iloc[i])

        # 解析并执行条件表达式
        try:
            # cross_above(A, B): A[idx-1]<=B[idx-1] and A[idx]>B[idx]
            if condition_str.startswith('cross_above('):
                inner = condition_str[len('cross_above('):-1]
                parts = [p.strip() for p in inner.split(',')]
                a, b = parts[0], parts[1]
                return _val(a, -1) <= _val(b, -1) and _val(a, 0) > _val(b, 0)

            # cross_below(A, B): A[idx-1]>=B[idx-1] and A[idx]<B[idx]
            if condition_str.startswith('cross_below('):
                inner = condition_str[len('cross_below('):-1]
                parts = [p.strip() for p in inner.split(',')]
                a, b = parts[0], parts[1]
                return _val(a, -1) >= _val(b, -1) and _val(a, 0) < _val(b, 0)

            # greater_than(A, B)
            if condition_str.startswith('greater_than('):
                inner = condition_str[len('greater_than('):-1]
                parts = [p.strip() for p in inner.split(',')]
                return _val(parts[0]) > float(parts[1]) if parts[1].replace('.','').replace('-','').isdigit() else _val(parts[0]) > _val(parts[1])

            # less_than(A, B)
            if condition_str.startswith('less_than('):
                inner = condition_str[len('less_than('):-1]
                parts = [p.strip() for p in inner.split(',')]
                return _val(parts[0]) < float(parts[1]) if parts[1].replace('.','').replace('-','').isdigit() else _val(parts[0]) < _val(parts[1])

            # turnover_rate_spike 直接返回bool
            if condition_str in indicators:
                val = _val(condition_str)
                return bool(val) if not pd.isna(val) else False

            parsed = ast.parse(condition_str, mode='eval')
            return self._eval_condition_ast(parsed.body, _val)

        except Exception as _e:
            # P1-12 fix: 不再静默吞没条件解析错误，打印诊断信息
            import logging
            _logger = logging.getLogger(__name__)
            _logger.warning(f"DSL条件解析失败 [idx={idx}]: {condition_str} — {_e}")
            return False

    def _eval_condition_ast(self, node, value_getter) -> bool:
        """Evaluate a small safe subset of Python expression AST for DSL conditions."""
        if isinstance(node, ast.BoolOp):
            values = [self._eval_condition_ast(v, value_getter) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise ValueError(f"unsupported bool op: {type(node.op).__name__}")

        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise ValueError("chained comparisons are not supported")
            left = self._eval_condition_value(node.left, value_getter)
            right = self._eval_condition_value(node.comparators[0], value_getter)
            if pd.isna(left) or pd.isna(right):
                return False
            op = node.ops[0]
            if isinstance(op, ast.Gt):
                return left > right
            if isinstance(op, ast.GtE):
                return left >= right
            if isinstance(op, ast.Lt):
                return left < right
            if isinstance(op, ast.LtE):
                return left <= right
            if isinstance(op, ast.Eq):
                return left == right
            if isinstance(op, ast.NotEq):
                return left != right
            raise ValueError(f"unsupported compare op: {type(op).__name__}")

        raise ValueError(f"unsupported condition AST: {type(node).__name__}")

    def _eval_condition_value(self, node, value_getter) -> float:
        if isinstance(node, ast.Name):
            return value_getter(node.id)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            val = self._eval_condition_value(node.operand, value_getter)
            return -val if isinstance(node.op, ast.USub) else val
        raise ValueError(f"unsupported condition value: {type(node).__name__}")

    # ═══════════════ 回测主循环 ═══════════════

    def run_strategy(self, strategy_json: Dict, data: pd.DataFrame) -> Dict:
        """运行策略回测 v4.5.12

        逐日迭代，模拟真实A股交易约束：
        - T+1: 当日买入次日才能卖出
        - 涨停不买、跌停不卖
        - 100股整数倍、佣金+印花税+滑点
        """
        is_valid, message = self.validator.validate_strategy_json(strategy_json)
        if not is_valid:
            return {"error": f"策略验证失败: {message}"}

        print(f"🚀 运行策略: {strategy_json.get('name', '未命名策略')}")

        # 解析策略
        indicators_def = strategy_json.get('indicators', [])
        signals_def = strategy_json.get('signals', [])
        position_size = float(strategy_json.get('position_size', 0.8))
        market = strategy_json.get('market', 'A')

        # 计算所有指标
        indicator_series = {}
        for ind_def in indicators_def:
            name = ind_def.get('name', ind_def.get('type', 'unknown'))
            indicator_series[name] = self._compute_indicator(data, ind_def, symbol=strategy_json.get('symbol', ''))

        # 回测状态
        cash = self.initial_cash
        shares = 0
        buy_date = None  # T+1: 记录最近买入日期
        trades = []
        daily_values = []

        # P2-8: 预计算20日均量用于成交量约束
        avg_volume_20 = data['volume'].rolling(20).mean() if 'volume' in data.columns else pd.Series(0, index=data.index)
        # 逐日回测（从第20天开始，给指标足够的预热期）
        start_idx = min(20, len(data) - 1)
        peak_value = self.initial_cash

        for idx in range(start_idx, len(data)):
            current_date = data.index[idx]
            close_price = float(data['close'].iloc[idx])
            prev_close = float(data['close'].iloc[idx - 1]) if idx > 0 else close_price

            # 检查持仓是否触及涨停/跌停/停牌（考虑卖出）
            cur_volume = float(data['volume'].iloc[idx]) if 'volume' in data.columns else 1
            can_trade = not self._is_limit_up(close_price, prev_close, market) and \
                        not self._is_limit_down(close_price, prev_close, market) and \
                        not (cur_volume <= 0 and idx > 0 and abs(close_price - prev_close) < 1e-9)

            # T+1: 当日买入的股票当日不能卖出
            can_sell = shares > 0 and (buy_date is None or current_date > buy_date)

            # 评估信号
            for sig_def in signals_def:
                condition = sig_def.get('condition', '')
                action = sig_def.get('action', '')
                triggered = self._evaluate_condition(indicator_series, condition, idx)

                if not triggered:
                    continue

                if action == 'buy' and shares == 0 and can_trade and not self._is_limit_up(close_price, prev_close, market):
                    # 计算买入数量
                    buy_amount = cash * position_size
                    raw_qty = int(buy_amount / close_price)
                    qty = self._apply_a_share_rules(
                        raw_qty, symbol=strategy_json.get('symbol', ''),
                        price=close_price, volume=cur_volume, avg_volume=float(avg_volume_20.iloc[idx])
                    )
                    cost = close_price * qty + self._calculate_trading_cost(close_price, qty, is_buy=True, symbol=strategy_json.get('symbol', ''))
                    if cost <= cash and qty > 0:
                        cash -= cost
                        shares = qty
                        buy_date = current_date
                        trades.append({
                            'date': str(current_date), 'action': 'buy',
                            'price': round(close_price, 2), 'quantity': qty,
                            'cost': round(cost, 2),
                        })

                elif action == 'sell' and shares > 0 and can_sell and not self._is_limit_down(close_price, prev_close, market):
                    sell_amount = close_price * shares
                    net_proceeds = sell_amount - self._calculate_trading_cost(close_price, shares, is_buy=False, symbol=strategy_json.get('symbol', ''))
                    pnl = net_proceeds - trades[-1]['cost'] if trades and trades[-1]['action'] == 'buy' else 0
                    cash += net_proceeds
                    trades.append({
                        'date': str(current_date), 'action': 'sell',
                        'price': round(close_price, 2), 'quantity': shares,
                        'proceeds': round(net_proceeds, 2), 'pnl': round(pnl, 2),
                    })
                    shares = 0
                    buy_date = None

            # 记录每日组合价值
            portfolio_value = cash + shares * close_price
            daily_values.append({
                'date': str(current_date),
                'cash': round(cash, 2),
                'shares': shares,
                'close': close_price,
                'portfolio_value': round(portfolio_value, 2),
            })
            if portfolio_value > peak_value:
                peak_value = portfolio_value

        # 如果最后一天还持仓，按收盘价平仓计算最终市值
        final_value = cash + shares * float(data['close'].iloc[-1])

        # 计算绩效指标
        total_return = (final_value - self.initial_cash) / self.initial_cash
        daily_pv = pd.Series([d['portfolio_value'] for d in daily_values])
        daily_returns = daily_pv.pct_change().dropna()

        # 夏普比率
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(252)
        else:
            sharpe = 0

        # 索提诺比率（只惩罚下行波动）
        downside = daily_returns[daily_returns < 0]
        if len(downside) > 1 and downside.std() > 0:
            sortino = (daily_returns.mean() / downside.std()) * np.sqrt(252)
        else:
            sortino = 0

        # 最大回撤
        cummax = daily_pv.cummax()
        drawdown = (daily_pv - cummax) / cummax.replace(0, np.nan)
        max_drawdown = float(drawdown.min()) if len(drawdown) > 0 else 0

        # 胜率
        completed_trades = []
        for i in range(0, len(trades) - 1, 2):
            if trades[i]['action'] == 'buy' and trades[i + 1]['action'] == 'sell':
                completed_trades.append(trades[i + 1].get('pnl', 0))
        winning_trades = [p for p in completed_trades if p > 0]
        win_rate = len(winning_trades) / len(completed_trades) if completed_trades else 0

        print(f"✅ 回测完成: 收益率={total_return:.2%}, 夏普={sharpe:.2f}, 最大回撤={max_drawdown:.2%}, 胜率={win_rate:.2%}")

        return {
            'strategy_name': strategy_json.get('name', '未命名策略'),
            'symbol': strategy_json.get('symbol', '未知'),
            'total_trades': len(trades),
            'completed_trades': len(completed_trades),
            'win_rate': round(win_rate, 4),
            'total_return': round(total_return, 4),
            'annual_return': round((1 + total_return) ** (252 / max(len(daily_returns), 1)) - 1, 4),
            'sharpe_ratio': round(sharpe, 4),
            'sortino_ratio': round(sortino, 4),
            'max_drawdown': round(max_drawdown, 4),
            'final_value': round(final_value, 2),
            'results': {
                'trades': trades,
                'daily_values': daily_values,
                'total_trades': len(trades),
            },
        }
    
    def generate_backtest_report(self, backtest_result: Dict) -> str:
        """生成回测报告"""
        report = f"# 策略回测报告\n\n"
        report += f"**策略名称**: {backtest_result.get('strategy_name', '未命名')}\n"
        report += f"**标的代码**: {backtest_result.get('symbol', '未知')}\n"
        report += f"**总交易次数**: {backtest_result.get('total_trades', 0)}\n"
        report += f"**胜率**: {backtest_result.get('win_rate', 0):.2%}\n"
        report += f"**总收益率**: {backtest_result.get('total_return', 0):.2%}\n"
        report += f"**夏普比率**: {backtest_result.get('sharpe_ratio', 0):.2f}\n"
        report += f"**索提诺比率**: {backtest_result.get('sortino_ratio', 0):.2f} (仅惩罚下行)\n"
        report += f"**最大回撤**: {backtest_result.get('max_drawdown', 0):.2%}\n"
        
        return report

class AShareFeatureExtender:
    """A股特色指标扩展"""
    
    @staticmethod
    def add_consecutive_limit_up(df: pd.DataFrame) -> pd.DataFrame:
        """P2-FIX: 向量化连板天数计算"""
        df = df.copy()
        if 'limit_up' not in df.columns:
            df['limit_up'] = False
        # Use groupby on cumulative sum of non-limit-up days to get consecutive runs
        groups = (~df['limit_up']).cumsum()
        df['consecutive_limit_up'] = df.groupby(groups)['limit_up'].cumsum()
        return df
    
    @staticmethod
    def detect_turnover_rate_spike(df: pd.DataFrame, threshold: float = 3.0) -> pd.DataFrame:
        """P2-FIX: 向量化换手率异动检测"""
        df = df.copy()
        if 'turnover_rate' not in df.columns:
            df['turnover_rate'] = 0
        df['turnover_rate_ma20'] = df['turnover_rate'].rolling(20).mean()
        # Vectorized spike detection
        df['turnover_rate_spike'] = (
            (df['turnover_rate'].index >= 20) &
            (df['turnover_rate_ma20'] > 0) &
            (df['turnover_rate'] > df['turnover_rate_ma20'] * threshold)
        )
        return df
    
    def add_a_share_features(df: pd.DataFrame) -> pd.DataFrame:
        """
        添加所有A股特色特征
        
        Args:
            df: 原始DataFrame
            
        Returns:
            添加了A股特色特征的DataFrame
        """
        df = df.copy()
        
        # 添加连板天数特征
        df = AShareFeatureExtender.add_consecutive_limit_up(df)
        
        # 添加换手率异动特征
        df = AShareFeatureExtender.detect_turnover_rate_spike(df)
        
        # 保留必要的原始数据列
        if 'turnover_rate' in df.columns:
            df['turnover_rate_original'] = df['turnover_rate']
        
        if 'pct_change' in df.columns:
            df['pct_change_original'] = df['pct_change']
        
        return df

# 示例策略JSON
EXAMPLE_STRATEGY = {
    "name": "MA交叉策略",
    "symbol": "600760",
    "timeframe": "daily",
    "indicators": [
        {
            "name": "ma_fast",
            "type": "ma",
            "params": {"period": 5}
        },
        {
            "name": "ma_slow", 
            "type": "ma",
            "params": {"period": 20}
        }
    ],
    "signals": [
        {
            "name": "buy_signal",
            "condition": "cross_above(ma_fast, ma_slow)",
            "action": "buy"
        },
        {
            "name": "sell_signal",
            "condition": "cross_below(ma_fast, ma_slow)",
            "action": "sell"
        }
    ]
}

def test_dsl_engine():
    """测试DSL引擎"""
    print("=" * 60)
    print("🧪 DSL引擎测试")
    print("=" * 60)
    
    # 测试验证器
    validator = DSLValidator()
    primitives = validator.get_available_primitives()
    print(f"✅ 可用原语: {len(primitives)}个")
    
    # 测试执行引擎
    executor = DSLExecutor()
    
    # 测试涨跌停检查
    test_cases = [
        (11.0, 10.0, 'A', True),      # 涨停
        (9.0, 10.0, 'A', True),       # 跌停
        (10.5, 10.0, 'A', False),     # 未涨停
        (12.0, 10.0, 'GEM', True),    # 创业板涨停
        (8.0, 10.0, 'GEM', True),     # 创业板跌停
    ]
    
    print("\n📊 涨跌停检查测试:")
    for price, prev_close, market, expected in test_cases:
        result = executor._check_limit_price(price, prev_close, market)
        status = "✅" if result == expected else "❌"
        print(f"  {status} {market}: {prev_close}→{price}, 预期:{expected}, 实际:{result}")
    
    # 测试A股规则
    print("\n📊 A股交易规则测试:")
    test_quantities = [50, 150, 250, 999, 1001]
    for qty in test_quantities:
        adjusted = executor._apply_a_share_rules(qty)
        print(f"  {qty} → {adjusted} (调整后)")
    
    # 测试A股特色指标
    print("\n📊 A股特色指标测试:")
    extender = AShareFeatureExtender()
    
    # 创建测试数据
    test_data = pd.DataFrame({
        'close': [10, 11, 12, 13, 14],
        'limit_up': [True, True, False, True, False],
        'turnover_rate': [1.0, 1.5, 10.0, 2.0, 1.0]  # 第3个数据有异动
    })
    
    enhanced_data = extender.add_a_share_features(test_data)
    print(f"  ✅ 添加了{len(enhanced_data.columns) - len(test_data.columns)}个特色特征")
    
    print("\n" + "=" * 60)
    print("✅ DSL引擎测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_dsl_engine()
