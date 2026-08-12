"""
实盘模拟持仓管理器
核心功能：模拟真实交易，记录持仓、交易记录、绩效统计
全程使用本地JSON存储，无数据库依赖，轻量高效
"""
import json
import os
import sys
import time
from datetime import datetime, date
from typing import List, Dict, Optional
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.trading_cost_calculator import trading_cost_calculator
from core.circuit_breaker import circuit_breaker
class PortfolioManager:
    def __init__(self, data_path: str = "../data/simulation_portfolio.json"):
        self.data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), data_path)
        self._init_data()
    
    def _init_data(self):
        """初始化数据文件"""
        if not os.path.exists(os.path.dirname(self.data_path)):
            os.makedirs(os.path.dirname(self.data_path))
        if not os.path.exists(self.data_path):
            init_data = {
                "initial_capital": 100000.0,  # 初始资金10万
                "current_capital": 100000.0,
                "cash": 100000.0,
                "positions": {},  # 持仓：{code: {"quantity": 0, "avg_cost": 0, "holding_days": 0}}
                "trade_history": [],  # 交易历史
                "daily_stats": [],  # 每日绩效统计
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat()
            }
            self._save_data(init_data)
    
    def _load_data(self) -> Dict:
        """加载数据"""
        with open(self.data_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _save_data(self, data: Dict):
        """保存数据"""
        data['updated_at'] = datetime.now().isoformat()
        with open(self.data_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def buy(self, code: str, name: str, price: float, amount: int, trade_date: str = None, market: str = "A") -> bool:
        """
        模拟买入
        :param code: 股票代码
        :param name: 股票名称
        :param price: 买入价格
        :param amount: 买入数量（100的整数倍）
        :param trade_date: 交易日期，默认今天
        :param market: 市场类型：A=A股，HK=港股
        :return: 是否买入成功
        """
        if trade_date is None:
            trade_date = date.today().isoformat()
        amount = int(amount / 100) * 100  # 取整为100的整数倍
        if amount < 100:
            return False  # 最小买入100股
        # 熔断检查
        trading_allowed, reason = circuit_breaker.is_trading_allowed()
        if not trading_allowed:
            print(f"❌ 交易被熔断拦截：{reason}")
            return False
        # 单票持仓限制检查
        total_capital = self._load_data()['current_capital']
        position_allowed, position_reason = circuit_breaker.check_position_limit(code, price*amount, total_capital)
        if not position_allowed:
            print(f"❌ 交易被持仓限制拦截：{position_reason}")
            return False
        # 计算真实交易成本
        cost_detail = trading_cost_calculator.calculate_trading_cost(market, "buy", price, amount)
        total_pay = cost_detail["net_amount"]  # 实际总支出（含所有成本和滑点）
        actual_price = cost_detail["actual_price"]
        
        data = self._load_data()
        if data['cash'] < total_pay:
            return False  # 资金不足
        # 扣除实际支出资金
        data['cash'] -= total_pay
        # 更新持仓：成本按实际成交价格（含滑点）计算
        if code in data['positions']:
            pos = data['positions'][code]
            new_quantity = pos['quantity'] + amount
            new_avg_cost = (pos['quantity'] * pos['avg_cost'] + amount * actual_price) / new_quantity
            pos['quantity'] = new_quantity
            pos['avg_cost'] = new_avg_cost
            pos['max_profit'] = max(pos.get('max_profit', 0.0), 0.0)  # 初始化最高盈利
        else:
            data['positions'][code] = {
                "name": name,
                "quantity": amount,
                "avg_cost": actual_price,
                "holding_days": 0,
                "first_buy_date": date,
                "max_profit": 0.0  # 记录持仓期间最高盈利，用于移动止盈
            }
        # 记录交易
        trade_record = {
            "date": trade_date,
            "type": "buy",
            "code": code,
            "name": name,
            "price": price,
            "actual_price": actual_price,
            "amount": amount,
            "total_cost": cost_detail["total_cost"],
            "stamp_tax": cost_detail["stamp_tax"],
            "commission": cost_detail["commission"],
            "transfer_fee": cost_detail["transfer_fee"],
            "slippage_cost": cost_detail["slippage_cost"],
            "total_pay": total_pay,
            "market": market
        }
        data['trade_history'].append(trade_record)
        self._save_data(data)
        # 记录交易成功
        circuit_breaker.record_trade_result(True)
        circuit_breaker.update_trade_count()
        return True
    
    def sell(self, code: str, price: float, amount: int = None, trade_date: str = None, market: str = "A") -> bool:
        """
        模拟卖出
        :param code: 股票代码
        :param price: 卖出价格
        :param amount: 卖出数量，默认全部卖出
        :param trade_date: 交易日期，默认今天
        :param market: 市场类型：A=A股，HK=港股
        :return: 是否卖出成功
        """
        if trade_date is None:
            trade_date = date.today().isoformat()
        data = self._load_data()
        # 熔断检查
        trading_allowed, reason = circuit_breaker.is_trading_allowed()
        if not trading_allowed:
            print(f"❌ 交易被熔断拦截：{reason}")
            return False
        if code not in data['positions']:
            return False  # 无持仓
        pos = data['positions'][code]
        if amount is None or amount >= pos['quantity']:
            amount = pos['quantity']
            del data['positions'][code]
        else:
            pos['quantity'] -= amount
        # 计算真实卖出收入（扣除所有成本和滑点）
        cost_detail = trading_cost_calculator.calculate_trading_cost(market, "sell", price, amount)
        total_income = cost_detail["net_amount"]  # 实际总收入
        actual_price = cost_detail["actual_price"]
        profit = (actual_price - pos['avg_cost']) * amount
        # 增加资金
        data['cash'] += total_income
        # 记录交易
        trade_record = {
            "date": trade_date,
            "type": "sell",
            "code": code,
            "name": pos.get('name', code),
            "price": price,
            "actual_price": actual_price,
            "amount": amount,
            "total_cost": cost_detail["total_cost"],
            "stamp_tax": cost_detail["stamp_tax"],
            "commission": cost_detail["commission"],
            "transfer_fee": cost_detail["transfer_fee"],
            "slippage_cost": cost_detail["slippage_cost"],
            "total_income": total_income,
            "profit": round(profit, 2),
            "profit_rate": round(profit / (pos['avg_cost'] * amount) * 100, 2),
            "market": market
        }
        data['trade_history'].append(trade_record)
        self._save_data(data)
        return True
    
    def update_daily_stats(self, current_prices: Dict[str, float], date: str = None):
        """
        更新每日绩效统计
        :param current_prices: 持仓股最新价格 {code: price}
        :param date: 统计日期，默认今天
        """
        if date is None:
            date = date.today().isoformat()
        data = self._load_data()
        # 计算持仓总市值
        position_value = 0.0
        position_profit = 0.0
        for code, pos in data['positions'].items():
            price = current_prices.get(code, pos['avg_cost'])
            value = pos['quantity'] * price
            position_value += value
            position_profit += (price - pos['avg_cost']) * pos['quantity']
            pos['holding_days'] += 1
        # 更新总资产
        data['current_capital'] = data['cash'] + position_value
        # 计算收益率
        total_profit = data['current_capital'] - data['initial_capital']
        total_return = total_profit / data['initial_capital']
        # 计算最大回撤（简化计算）
        max_capital = max([s.get('total_capital', data['initial_capital']) for s in data['daily_stats']] + [data['current_capital']])
        drawdown = (max_capital - data['current_capital']) / max_capital if max_capital != 0 else 0
        # 更新熔断机制的今日回撤
        circuit_breaker.update_drawdown(drawdown)
        # 计算胜率
        win_trades = [t for t in data['trade_history'] if t.get('profit', 0) > 0]
        total_trades = len([t for t in data['trade_history'] if t['type'] == 'sell'])
        win_rate = len(win_trades) / total_trades if total_trades > 0 else 0
        # 计算盈亏比
        if total_trades > 0:
            avg_win = sum([t['profit'] for t in win_trades]) / len(win_trades) if win_trades else 0
            loss_trades = [t for t in data['trade_history'] if t.get('profit', 0) <= 0 and t['type'] == 'sell']
            avg_loss = abs(sum([t['profit'] for t in loss_trades]) / len(loss_trades)) if loss_trades else 0
            profit_loss_ratio = avg_win / avg_loss if avg_loss != 0 else 0
        else:
            profit_loss_ratio = 0
        # 记录统计
        data['daily_stats'].append({
            "date": date,
            "total_capital": data['current_capital'],
            "cash": data['cash'],
            "position_value": position_value,
            "total_profit": total_profit,
            "total_return": total_return,
            "position_count": len(data['positions']),
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "max_drawdown": drawdown
        })
        self._save_data(data)
    
    def get_portfolio_summary(self) -> Dict:
        """获取账户总览数据"""
        data = self._load_data()
        total_profit = data['current_capital'] - data['initial_capital']
        total_return = total_profit / data['initial_capital']
        position_value = data['current_capital'] - data['cash']
        position_ratio = position_value / data['current_capital'] if data['current_capital'] != 0 else 0
        win_trades = [t for t in data['trade_history'] if t.get('profit', 0) > 0]
        total_trades = len([t for t in data['trade_history'] if t['type'] == 'sell'])
        win_rate = len(win_trades) / total_trades if total_trades > 0 else 0
        stats = {
            "initial_capital": data['initial_capital'],
            "current_capital": data['current_capital'],
            "cash": data['cash'],
            "position_value": position_value,
            "position_ratio": position_ratio,
            "total_profit": total_profit,
            "total_return": total_return,
            "position_count": len(data['positions']),
            "total_trades": total_trades,
            "win_rate": win_rate
        }
        if len(data['daily_stats']) > 0:
            latest = data['daily_stats'][-1]
            stats['max_drawdown'] = latest.get('max_drawdown', 0)
            stats['profit_loss_ratio'] = latest.get('profit_loss_ratio', 0)
        return stats
    
    def get_positions(self) -> List[Dict]:
        """获取当前持仓列表"""
        data = self._load_data()
        positions = []
        for code, pos in data['positions'].items():
            positions.append({
                "code": code,
                "name": pos['name'],
                "quantity": pos['quantity'],
                "avg_cost": pos['avg_cost'],
                "holding_days": pos['holding_days']
            })
        return positions
    
    def get_trade_history(self, limit: int = 20) -> List[Dict]:
        """获取最近交易记录"""
        data = self._load_data()
        return sorted(data['trade_history'], key=lambda x: x['date'], reverse=True)[:limit]
    
    def get_daily_performance(self, limit: int = 30) -> List[Dict]:
        """获取最近30天绩效数据"""
        data = self._load_data()
        return sorted(data['daily_stats'], key=lambda x: x['date'], reverse=True)[:limit]
