"""DSL v4.5.9 — EasyTraderBroker: 东方财富券商接入 (STUB)

使用前需要:
1. 安装 easytrader: pip install easytrader
2. 配置东方财富账号
3. 配置验证码识别 (可选)

环境变量:
  EASYTRADER_USER=your_username
  EASYTRADER_PASSWORD=your_password
"""

import os, sys
from typing import Dict, List

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from execution_engine.brokers.base import BaseBroker, Order, Position, AccountInfo


class EasyTraderBroker(BaseBroker):
    """东方财富客户端券商"""

    broker_name = "easytrader"

    def __init__(self, user: str = None, password: str = None):
        self._user = user or os.getenv("EASYTRADER_USER", "")
        self._password = password or os.getenv("EASYTRADER_PASSWORD", "")
        self._connected = False
        self._trader = None

    def connect(self) -> bool:
        try:
            import easytrader
            self._trader = easytrader.use('ths')  # 同花顺
            # self._trader.prepare(user=self._user, password=self._password)
            self._connected = True
            return True
        except ImportError:
            print("[EasyTraderBroker] easytrader not installed. Run: pip install easytrader")
            return False
        except Exception as e:
            print(f"[EasyTraderBroker] connect failed: {e}")
            return False

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def get_positions(self) -> List[Position]:
        if not self._trader:
            return []
        # TODO: self._trader.position
        return []

    def get_cash(self) -> float:
        if not self._trader:
            return 0.0
        # TODO: self._trader.balance
        return 0.0

    def get_account(self) -> AccountInfo:
        return AccountInfo(
            broker_name="easytrader", account_id=self._user,
            total_value=0, cash=self.get_cash(), market_value=0,
        )

    def submit_order(self, order: Order) -> Dict:
        if not self._trader:
            return {"success": False, "error": "未连接"}
        # TODO: self._trader.buy() / self._trader.sell()
        return {"success": False, "error": "not implemented"}

    def cancel_order(self, order_id: str) -> bool:
        return False

    def get_order_status(self, order_id: str) -> Dict:
        return {"order_id": order_id, "status": "unknown"}

    def get_today_orders(self) -> List[Dict]:
        return []
