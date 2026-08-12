"""DSL v4.5.9 — PaperBroker: wraps existing PaperTrader for the broker interface."""

import os, sys
from typing import Dict, List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from execution_engine.brokers.base import BaseBroker, Order, Position, AccountInfo


class PaperBroker(BaseBroker):
    """纸交易券商 — 包装现有 PaperTrader，实现 BaseBroker 接口"""

    broker_name = "paper"

    def __init__(self):
        self._connected = False
        self._pt = None

    def connect(self) -> bool:
        from scripts.paper_trader import PaperTrader
        self._pt = PaperTrader()
        self._connected = True
        return True

    def disconnect(self) -> None:
        self._connected = False
        self._pt = None

    def is_connected(self) -> bool:
        return self._connected and self._pt is not None

    def get_positions(self) -> List[Position]:
        if not self._pt:
            return []
        s = self._pt.get_portfolio_summary()
        return [
            Position(
                symbol=p["stock_code"] if "stock_code" in p else p.get("symbol", ""),
                quantity=p.get("quantity", 0),
                avg_cost=p.get("avg_cost", 0),
                current_price=p.get("current_price", 0),
                market_value=p.get("market_value", 0),
                pnl=p.get("pnl", 0),
                pnl_pct=p.get("pnl_pct", 0),
                market=p.get("market", "A"),
            )
            for p in s.get("positions", [])
        ]

    def get_cash(self) -> float:
        if not self._pt:
            return 0.0
        return self._pt._get_float("current_cash", 0.0)

    def get_account(self) -> AccountInfo:
        if not self._pt:
            return AccountInfo(broker_name="paper", account_id="paper",
                               total_value=0, cash=0, market_value=0)
        s = self._pt.get_portfolio_summary()
        positions = self.get_positions()
        return AccountInfo(
            broker_name="paper",
            account_id="paper",
            total_value=s.get("total_value", 0),
            cash=s.get("current_cash", 0),
            market_value=sum(p.market_value for p in positions),
            positions=positions,
            pnl=s.get("total_return_pct", 0) / 100 * 1_000_000 if s.get("total_return_pct") else 0,
            pnl_pct=s.get("total_return_pct", 0),
        )

    def submit_order(self, order: Order) -> Dict:
        if not self._pt:
            return {"success": False, "error": "未连接"}
        result = self._pt.execute_trade(
            market="A",
            stock_code=order.symbol,
            action=order.action.upper(),
            price=order.price,
            quantity=order.quantity,
            reason=order.reason,
            order_id=order.order_id or f"paper_{order.symbol}_{order.action}",
        )
        return result

    def cancel_order(self, order_id: str) -> bool:
        return False  # 纸交易不支持撤单

    def get_order_status(self, order_id: str) -> Dict:
        return {"order_id": order_id, "status": "unknown"}

    def get_today_orders(self) -> List[Dict]:
        if not self._pt:
            return []
        ledger = self._pt.load_ledger()
        return ledger.get("trade_history", [])
