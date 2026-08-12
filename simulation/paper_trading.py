"""
模拟交易引擎

用于在实盘前验证交易策略，支持：
- 纸交易（Paper Trading）
- 回测验证
- 信号追踪
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
import logging
import json
import os

logger = logging.getLogger(__name__)

SLIPPAGE_BPS = 5  # P1-FIX: 添加滑点模型(5bp默认)


class PaperTrade:
    """单笔模拟交易记录"""
    
    def __init__(
        self,
        symbol: str,
        action: str,  # BUY/SELL
        quantity: int,
        price: float,
        timestamp: datetime = None
    ):
        self.symbol = symbol
        self.action = action
        self.quantity = quantity
        self.price = price
        self.timestamp = timestamp or datetime.now()
        self.trade_id = f"{symbol}_{self.timestamp.strftime('%Y%m%d_%H%M%S')}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "action": self.action,
            "quantity": self.quantity,
            "price": self.price,
            "timestamp": self.timestamp.isoformat(),
            "total_value": self.quantity * self.price
        }


class PaperTradingEngine:
    """模拟交易引擎
    
    功能：
    1. 接收交易信号
    2. 模拟成交
    3. 追踪持仓
    4. 计算盈亏
    """
    
    def __init__(self, initial_capital: float = 100000.0, config: Optional[Dict[str, Any]] = None):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.config = config or {}
        
        # 持仓记录: {symbol: {"quantity": int, "avg_price": float}}
        self.positions: Dict[str, Dict[str, Any]] = {}
        
        # 交易历史
        self.trade_history: List[PaperTrade] = []
        
        # 交易统计
        self.stats = {
            "total_trades": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "total_commission": 0.0
        }
        
        # 日志目录
        self.log_dir = self.config.get("log_dir", "logs/paper_trading")
        os.makedirs(self.log_dir, exist_ok=True)

        self._buy_dates = {}  # {symbol: date_str} — P0-FIX: T+1 tracking
        
        logger.info(f"PaperTradingEngine initialized with capital: ¥{initial_capital:,.2f}")
    
    def execute_order(self, symbol: str, action: str, quantity: int, price: float, date: str = None, **kwargs) -> Dict[str, Any]:
        """执行模拟订单
        
        Args:
            symbol: 股票代码
            action: BUY/SELL
            quantity: 数量
            price: 价格
            date: 交易日期(YYYY-MM-DD)，用于T+1约束
            **kwargs: 其他参数
            
        Returns:
            交易结果
        """
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")
        # 验证订单
        validation = self._validate_order(symbol, action, quantity, price)
        if not validation["valid"]:
            logger.warning(f"订单验证失败: {validation['reason']}")
            return {"success": False, "reason": validation["reason"]}
        
        # 执行交易
        trade = PaperTrade(symbol, action, quantity, price)
        
        if action == "BUY":
            self._execute_buy(trade, date=date)
        elif action == "SELL":
            result = self._execute_sell(trade, date=date)
            # P0-FIX: T+1被拒绝时直接返回失败
            if isinstance(result, dict) and result.get("success") is False:
                return result
        
        # 记录交易
        self.trade_history.append(trade)
        self.stats["total_trades"] += 1
        if action == "BUY":
            self.stats["buy_trades"] += 1
        else:
            self.stats["sell_trades"] += 1
        
        # 保存日志
        self._save_trade_log(trade)
        
        logger.info(f"交易执行: {action} {quantity} {symbol} @ ¥{price}")
        
        return {
            "success": True,
            "trade": trade.to_dict(),
            "remaining_cash": self.cash,
            "positions": self.get_positions_summary()
        }
    
    def _execute_buy(self, trade: PaperTrade, date: str = None):
        """执行买入"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        total_cost = trade.quantity * trade.price
        commission = total_cost * 0.00025  # 万2.5佣金 (v4.5.12 fix: 与config/constants.py统一)
        slippage_cost = trade.quantity * trade.price * SLIPPAGE_BPS / 10000  # P1-FIX: 滑点

        self.cash -= (total_cost + commission + slippage_cost)
        self.stats["total_commission"] += commission

        # 更新持仓
        if trade.symbol in self.positions:
            pos = self.positions[trade.symbol]
            total_qty = pos["quantity"] + trade.quantity
            pos["avg_price"] = (pos["avg_price"] * pos["quantity"] + trade.price * trade.quantity) / total_qty
            pos["quantity"] = total_qty
        else:
            self.positions[trade.symbol] = {
                "quantity": trade.quantity,
                "avg_price": trade.price
            }

        self._buy_dates[trade.symbol] = date  # P0-FIX: 记录买入日期

    def _execute_sell(self, trade: PaperTrade, date: str = None):
        """执行卖出"""
        if date is None:
            date = datetime.now().strftime("%Y-%m-%d")

        # P0-FIX: T+1约束 — 当日买入不可当日卖出
        if trade.symbol in self._buy_dates and self._buy_dates[trade.symbol] == date:
            return {
                "success": False,
                "message": f"T+1约束: {trade.symbol}当日买入不可卖出",
                "symbol": trade.symbol,
                "signal": "SELL",
                "price": trade.price,
                "date": date,
            }

        total_value = trade.quantity * trade.price
        commission = total_value * 0.00025
        stamp_duty = total_value * 0.001  # 印花税千分之一
        slippage_cost = trade.quantity * trade.price * SLIPPAGE_BPS / 10000  # P1-FIX: 滑点
        
        self.cash += (total_value - commission - stamp_duty - slippage_cost)
        self.stats["total_commission"] += (commission + stamp_duty)
        
        # 更新持仓
        if trade.symbol in self.positions:
            pos = self.positions[trade.symbol]
            pos["quantity"] -= trade.quantity
            if pos["quantity"] <= 0:
                del self.positions[trade.symbol]
            # P0-FIX: 清除买入日期记录
            if trade.symbol in self._buy_dates:
                del self._buy_dates[trade.symbol]
    
    def _validate_order(self, symbol: str, action: str, quantity: int, price: float) -> Dict[str, Any]:
        """验证订单"""
        if action == "BUY":
            total_cost = quantity * price
            if total_cost > self.cash:
                return {"valid": False, "reason": f"资金不足: 需要¥{total_cost:.2f}, 可用¥{self.cash:.2f}"}
        
        elif action == "SELL":
            if symbol not in self.positions:
                return {"valid": False, "reason": f"无持仓: {symbol}"}
            
            pos = self.positions[symbol]
            if quantity > pos["quantity"]:
                return {"valid": False, "reason": f"持仓不足: 需要{quantity}, 持有{pos['quantity']}"}
        
        return {"valid": True}
    
    def get_positions_summary(self, current_prices: Dict[str, float] = None) -> Dict[str, Any]:
        """P0-FIX: 使用现价(非成本价)计算市值"""
        current_prices = current_prices or {}
        # P2-TODO: 涨跌停检查（暂不实现，需接入实时行情判断是否涨停/跌停）
        total_market_value = 0
        for symbol, pos in self.positions.items():
            price = current_prices.get(symbol, pos["avg_price"])  # fallback to cost
            total_market_value += pos["quantity"] * price
        
        return {
            "cash": self.cash,
            "positions_count": len(self.positions),
            "total_market_value": total_market_value,
            "total_assets": self.cash + total_market_value,
            "profit_loss": self.cash + total_market_value - self.initial_capital,
            "return_rate": (self.cash + total_market_value - self.initial_capital) / self.initial_capital * 100
        }
    
    def get_trade_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取交易历史"""
        return [trade.to_dict() for trade in self.trade_history[-limit:]]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            **self.stats,
            "positions_summary": self.get_positions_summary()
        }
    
    def _save_trade_log(self, trade: PaperTrade):
        """保存交易日志"""
        log_file = os.path.join(self.log_dir, f"{datetime.now().strftime('%Y%m%d')}.json")
        
        trades = []
        if os.path.exists(log_file):
            try:
                with open(log_file, 'r', encoding='utf-8') as f:
                    trades = json.load(f)
            except:
                trades = []
        
        trades.append(trade.to_dict())
        
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(trades, f, ensure_ascii=False, indent=2)
    
    def reset(self):
        """重置引擎"""
        self.cash = self.initial_capital
        self.positions.clear()
        self._buy_dates.clear()  # P0-FIX: 清除T+1记录
        self.trade_history.clear()
        self.stats = {
            "total_trades": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "total_commission": 0.0
        }
        logger.info("PaperTradingEngine reset")
