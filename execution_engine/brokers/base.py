"""DSL v4.5.9 — Abstract broker interface.

All broker implementations (paper, xtquant, easytrader) must implement this interface.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime


@dataclass
class Order:
    symbol: str
    action: str          # BUY / SELL
    price: float
    quantity: int
    order_type: str = "limit"   # limit / market
    reason: str = ""
    order_id: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float
    market_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    market: str = "A"

    def __post_init__(self):
        if not self.market_value:
            self.market_value = self.quantity * self.current_price
        if not self.pnl:
            self.pnl = self.quantity * (self.current_price - self.avg_cost)
        if not self.pnl_pct and self.avg_cost > 0:
            self.pnl_pct = round((self.current_price / self.avg_cost - 1) * 100, 2)


@dataclass
class AccountInfo:
    broker_name: str
    account_id: str
    total_value: float
    cash: float
    market_value: float
    positions: List[Position] = field(default_factory=list)
    pnl: float = 0.0
    pnl_pct: float = 0.0


class BaseBroker(ABC):
    """券商抽象基类 — 所有券商实现必须继承此类"""

    broker_name: str = "base"

    @abstractmethod
    def connect(self) -> bool:
        """建立连接，返回是否成功"""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """断开连接"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """检查连接状态"""
        ...

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """获取当前持仓"""
        ...

    @abstractmethod
    def get_cash(self) -> float:
        """获取可用资金"""
        ...

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取完整账户信息"""
        ...

    @abstractmethod
    def submit_order(self, order: Order) -> Dict:
        """提交订单，返回 {"order_id": str, "status": str, ...}"""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤销订单"""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict:
        """查询订单状态"""
        ...

    @abstractmethod
    def get_today_orders(self) -> List[Dict]:
        """获取今日所有订单"""
        ...

    # ── 可选钩子 ──

    def pre_trade_check(self, order: Order) -> tuple:
        """交易前检查，返回 (allowed: bool, reason: str)"""
        return True, ""

    def post_trade_notify(self, order: Order, result: Dict) -> None:
        """交易后通知（可被子类覆盖，如发送飞书消息）"""
        pass
