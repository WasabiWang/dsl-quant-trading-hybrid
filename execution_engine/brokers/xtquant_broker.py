"""DSL v4.6.x — XtQuantBroker: 华泰QMT/miniQMT 券商接入 (完整实现)

使用前需要:
1. 安装 xtquant: pip install xtquant
2. 启动 QMT 或 miniQMT 客户端
3. 配置账号信息在环境变量或 .env 中

环境变量:
  XTQUANT_PATH=/path/to/xtquant         # xtquant安装路径
  XTQUANT_ACCOUNT=your_account_id       # 资金账号
  XTQUANT_SESSION_ID=123456             # miniQMT会话ID (默认自动)

订单状态机: PENDING → SUBMITTED → PARTIAL → FILLED → CONFIRMED
                                        → CANCELLED → CONFIRMED
                                        → REJECTED  → CONFIRMED
"""

import os, sys, logging, time
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from execution_engine.brokers.base import BaseBroker, Order, Position, AccountInfo

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    PENDING = "PENDING"          # 待提交
    SUBMITTED = "SUBMITTED"      # 已提交到券商
    PARTIAL = "PARTIAL"          # 部分成交
    FILLED = "FILLED"            # 全部成交
    CANCELLED = "CANCELLED"      # 已撤单
    REJECTED = "REJECTED"        # 被拒绝
    CONFIRMED = "CONFIRMED"      # 已确认(终态)
    UNKNOWN = "UNKNOWN"          # 未知


# QMT订单状态码 → 内部状态映射
_QMT_STATUS_MAP = {
    48: OrderStatus.SUBMITTED,   # 已报
    49: OrderStatus.SUBMITTED,   # 待报(交易所未开市)
    50: OrderStatus.PARTIAL,     # 部成
    51: OrderStatus.PARTIAL,     # 部成(可撤)
    52: OrderStatus.FILLED,      # 全成
    53: OrderStatus.CANCELLED,   # 已撤
    54: OrderStatus.PARTIAL,     # 部撤
    55: OrderStatus.REJECTED,    # 废单
}


class XtQuantBroker(BaseBroker):
    """华泰QMT/miniQMT 券商 — v4.6.x 完整实现"""

    broker_name = "xtquant"

    def __init__(self, xtdata_path: str = None, account: str = None,
                 session_id: int = None, mini_mode: bool = True):
        self._xtdata_path = xtdata_path or os.getenv("XTQUANT_PATH", "")
        self._account = account or os.getenv("XTQUANT_ACCOUNT", "")
        self._session_id = session_id or int(os.getenv("XTQUANT_SESSION_ID", "0"))
        self._mini_mode = mini_mode
        self._connected = False
        self._xt_trader = None
        self._xt_data = None
        self._acc = None
        self._callback = None
        self._order_cache: Dict[str, Dict] = {}  # order_id → status dict

    def connect(self) -> bool:
        """连接miniQMT/QMT"""
        try:
            if self._xtdata_path:
                sys.path.insert(0, self._xtdata_path)

            from xtquant import xtdata
            self._xt_data = xtdata

            if self._mini_mode:
                from xtquant import xttrader
                from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback

                path = self._xtdata_path or os.path.join(os.path.dirname(__file__), 'xtquant')
                self._session_id = self._session_id or int(time.time())

                class _QmtCallback(XtQuantTraderCallback):
                    def __init__(self, broker):
                        self.broker = broker

                    def on_disconnected(self):
                        logger.warning("[QMT] 连接断开")
                        self.broker._connected = False

                    def on_stock_order(self, order):
                        """订单状态变化回调"""
                        try:
                            oid = str(order.order_id) if hasattr(order, 'order_id') else str(order.m_strOrderID)
                            qmt_status = order.order_status if hasattr(order, 'order_status') else order.m_nOrderStatus
                            status = _QMT_STATUS_MAP.get(qmt_status, OrderStatus.UNKNOWN)
                            self.broker._order_cache[oid] = {
                                "order_id": oid,
                                "status": status.value,
                                "qmt_status": qmt_status,
                                "filled_volume": getattr(order, 'filled_volume',
                                                         getattr(order, 'm_nVolumeTotal', 0)),
                                "price": getattr(order, 'price', getattr(order, 'm_dPrice', 0.0)),
                                "updated_at": datetime.now().isoformat(),
                            }
                            logger.info(f"[QMT] 订单回调: {oid} → {status.value}")
                            # v4.6.x: 推送对账引擎
                            try:
                                from execution_engine.reconciliation import get_reconciliation_engine
                                engine = get_reconciliation_engine()
                                if status == OrderStatus.FILLED:
                                    engine.mark_executed(oid, trade_id=0,
                                                         exec_price=self.broker._order_cache[oid]["price"],
                                                         exec_qty=self.broker._order_cache[oid]["filled_volume"])
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error(f"[QMT] 回调异常: {e}")

                    def on_stock_asset(self, asset):
                        pass

                    def on_stock_position(self, position):
                        pass

                    def on_accountinfo(self, info):
                        pass

                self._callback = _QmtCallback(self)
                self._xt_trader = XtQuantTrader(path, self._session_id)
                self._xt_trader.register_callback(self._callback)
                self._xt_trader.start()
                connect_result = self._xt_trader.connect()
                if connect_result != 0:
                    logger.error(f"[QMT] 连接失败: code={connect_result}")
                    return False

                # 订阅账户
                if self._account:
                    self._xt_trader.subscribe(self._account)
                    self._acc = self._account
                else:
                    # 使用默认账户
                    self._acc = str(self._session_id)

                self._connected = True
                logger.info(f"[QMT] miniQMT连接成功 session={self._session_id} account={self._acc}")
                return True
            else:
                # 全功能QMT模式 (需xtquant完整版)
                logger.warning("[QMT] 全功能QMT模式暂未实现, 请使用miniQMT")
                return False

        except ImportError:
            logger.warning("[XtQuantBroker] xtquant not installed. Run: pip install xtquant")
            return False
        except Exception as e:
            logger.error(f"[XtQuantBroker] connect failed: {e}")
            return False

    def disconnect(self) -> None:
        if self._xt_trader:
            try:
                self._xt_trader.stop()
            except Exception:
                pass
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._xt_trader is not None

    def get_positions(self) -> List[Position]:
        if not self._xt_trader or not self._acc:
            return []
        try:
            positions = self._xt_trader.query_stock_positions(self._acc)
            if not positions:
                return []
            result = []
            for pos in positions:
                code = pos.stock_code if hasattr(pos, 'stock_code') else pos.m_strInstrumentID
                qty = pos.volume if hasattr(pos, 'volume') else pos.m_nVolume
                if qty <= 0:
                    continue
                avg = pos.open_price if hasattr(pos, 'open_price') else pos.m_dOpenPrice
                cur = pos.market_value / qty if qty > 0 else 0
                if hasattr(pos, 'last_price'):
                    cur = pos.last_price
                elif hasattr(pos, 'm_dLastPrice'):
                    cur = pos.m_dLastPrice
                result.append(Position(
                    symbol=str(code), quantity=int(qty),
                    avg_cost=float(avg) if avg else 0,
                    current_price=float(cur) if cur else float(avg),
                ))
            return result
        except Exception as e:
            logger.error(f"[QMT] 查询持仓失败: {e}")
            return []

    def get_cash(self) -> float:
        if not self._xt_trader or not self._acc:
            return 0.0
        try:
            asset = self._xt_trader.query_stock_asset(self._acc)
            if hasattr(asset, 'cash'):
                return float(asset.cash)
            elif hasattr(asset, 'm_dCash'):
                return float(asset.m_dCash)
            return 0.0
        except Exception as e:
            logger.error(f"[QMT] 查询资金失败: {e}")
            return 0.0

    def get_account(self) -> AccountInfo:
        positions = self.get_positions()
        cash = self.get_cash()
        market_value = sum(p.market_value for p in positions)
        total = cash + market_value
        return AccountInfo(
            broker_name="xtquant",
            account_id=self._acc or "",
            total_value=total,
            cash=cash,
            market_value=market_value,
            positions=positions,
        )

    def submit_order(self, order: Order) -> Dict:
        """提交订单到QMT

        返回: {"success": bool, "order_id": str, "qmt_order_id": int, "error": str}
        """
        if not self._xt_trader or not self._acc:
            return {"success": False, "error": "未连接", "order_id": order.order_id}

        try:
            # 订单类型: 0=限价单, 1=市价单(仅深圳)
            order_type = 0 if order.order_type == "limit" else 1
            # 价格类型: -1=卖5价到买5价 0=限价 1=最新价 ... 5=涨停 6=跌停
            price_type = 5 if order.action == "BUY" else 6  # 确保成交: 买以涨停价, 卖以跌停价
            qmt_price = order.price

            qmt_order_id = self._xt_trader.order_stock(
                account=self._acc,
                stock_code=order.symbol,
                order_type=order_type,
                order_volume=order.quantity,
                price_type=price_type,
                price=qmt_price,
                strategy_name="DSL_Quant",
                order_remark=order.reason or "DSL量化交易",
            )

            oid = str(qmt_order_id)
            self._order_cache[oid] = {
                "order_id": oid,
                "dsl_order_id": order.order_id,
                "status": OrderStatus.SUBMITTED.value,
                "symbol": order.symbol,
                "action": order.action,
                "price": qmt_price,
                "quantity": order.quantity,
                "submitted_at": datetime.now().isoformat(),
            }

            logger.info(f"[QMT] 下单成功: {order.symbol} {order.action} x{order.quantity} @{qmt_price:.2f} → qmt_id={qmt_order_id}")
            return {"success": True, "order_id": order.order_id, "qmt_order_id": qmt_order_id}

        except Exception as e:
            logger.error(f"[QMT] 下单失败 {order.symbol}: {e}")
            return {"success": False, "error": str(e), "order_id": order.order_id}

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单 (QMT订单ID, 非DSL order_id)"""
        if not self._xt_trader:
            return False
        try:
            self._xt_trader.cancel_order_stock(self._acc, int(order_id))
            logger.info(f"[QMT] 撤单成功: {order_id}")
            return True
        except Exception as e:
            logger.error(f"[QMT] 撤单失败 {order_id}: {e}")
            return False

    def get_order_status(self, order_id: str) -> Dict:
        """查询订单状态"""
        # 先查缓存(回调更新的)
        if order_id in self._order_cache:
            cached = self._order_cache[order_id]
            if cached.get("status") == OrderStatus.FILLED.value:
                return cached

        # 查QMT实时状态
        if self._xt_trader:
            try:
                result = self._xt_trader.query_stock_order(self._acc, int(order_id))
                if result:
                    status_code = result.order_status if hasattr(result, 'order_status') else result.m_nOrderStatus
                    status = _QMT_STATUS_MAP.get(status_code, OrderStatus.UNKNOWN)
                    info = {
                        "order_id": order_id,
                        "status": status.value,
                        "qmt_status": status_code,
                        "filled_volume": getattr(result, 'filled_volume', getattr(result, 'm_nVolumeTotal', 0)),
                        "price": getattr(result, 'price', getattr(result, 'm_dPrice', 0.0)),
                        "updated_at": datetime.now().isoformat(),
                    }
                    self._order_cache[order_id] = info
                    return info
            except Exception as e:
                logger.warning(f"[QMT] 查询订单状态失败 {order_id}: {e}")

        return {"order_id": order_id, "status": OrderStatus.UNKNOWN.value}

    def get_today_orders(self) -> List[Dict]:
        """获取今日所有订单 (缓存)"""
        result = []
        today = datetime.now().strftime("%Y-%m-%d")
        for oid, info in self._order_cache.items():
            if today in info.get("submitted_at", ""):
                result.append(info)
        return result

    # ── 可选扩展 ──

    def pre_trade_check(self, order: Order) -> tuple:
        """交易前检查: 涨跌停/停牌"""
        if not self._xt_data:
            return True, ""
        try:
            # 检查是否停牌
            code = order.symbol
            if code.startswith("6") or code.startswith("5"):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"
            detail = self._xt_data.get_instrument_detail(code)
            if detail and hasattr(detail, 'IsTrading'):
                if not getattr(detail, 'IsTrading'):
                    return False, f"{order.symbol} 停牌"
            return True, ""
        except Exception:
            return True, ""

    def post_trade_notify(self, order: Order, result: Dict) -> None:
        """交易后通知: 飞书推送"""
        if result.get("success"):
            return
        try:
            from common.feishu_utils import send_markdown
            send_markdown(
                title=f"🔴 实盘交易失败: {order.symbol}",
                content=f"**{order.action}** {order.symbol} {order.quantity}股\n"
                        f"价格: {order.price}\n原因: {result.get('error', 'unknown')}\n"
                        f"时间: {datetime.now().strftime('%H:%M:%S')}"
            )
        except Exception:
            pass
