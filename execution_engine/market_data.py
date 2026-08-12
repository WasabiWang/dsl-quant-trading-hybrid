"""DSL v4.6.x — 实时行情客户端

抽象层: 统一价格获取接口, 支持麦蕊快照 + QMT实时推送(预留)
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from datetime import datetime
import logging
import os

logger = logging.getLogger(__name__)


class MarketDataClient(ABC):
    """行情客户端抽象基类"""

    @abstractmethod
    def get_price(self, symbol: str) -> Optional[float]:
        """获取当前价格"""
        ...

    @abstractmethod
    def get_batch_prices(self, symbols: List[str]) -> Dict[str, float]:
        """批量获取价格 {symbol: price}"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """连接状态"""
        ...

    def get_pre_close(self, symbol: str) -> Optional[float]:
        """获取昨收价 (用于涨跌停计算)"""
        return None


class MairuiSnapshotClient(MarketDataClient):
    """麦蕊API快照行情 (当前主力)"""

    def __init__(self):
        self._connected = True
        self._cache: Dict[str, Dict] = {}
        self._cache_ttl = 30  # 30秒缓存

    def is_connected(self) -> bool:
        return self._connected

    def get_price(self, symbol: str) -> Optional[float]:
        prices = self.get_batch_prices([symbol])
        return prices.get(symbol)

    def get_batch_prices(self, symbols: List[str]) -> Dict[str, float]:
        """麦蕊批量实时行情 (≤20只)"""
        result = {}
        now = datetime.now().timestamp()

        # 缓存处理
        fresh = []
        for s in symbols:
            c = self._cache.get(s)
            if c and now - c.get("ts", 0) < self._cache_ttl:
                result[s] = c["price"]
            else:
                fresh.append(s)

        if not fresh:
            return result

        try:
            from config.mairui_api_config import get_multi_stock_real
            batch = fresh[:20]
            real_list = get_multi_stock_real(batch)
            if isinstance(real_list, list):
                for item in real_list:
                    dm = str(item.get("dm", item.get("code", "")))
                    p = item.get("p") or item.get("current") or item.get("latest")
                    if dm and p:
                        price = float(p)
                        result[dm] = price
                        self._cache[dm] = {"price": price, "ts": now}

            # 剩余单只查询
            remaining = fresh[20:]
            for code in remaining:
                try:
                    from config.mairui_api_config import get_stock_real
                    data = get_stock_real(code)
                    if data and data.get("current_price", 0) > 0:
                        price = float(data["current_price"])
                        result[code] = price
                        self._cache[code] = {"price": price, "ts": now}
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[MairuiClient] 批量行情失败: {e}, 尝试akshare fallback")
            try:
                import akshare as ak
                df = ak.stock_zh_a_spot_em()
                for code in fresh:
                    row = df[df["代码"] == code]
                    if not row.empty:
                        price = float(row.iloc[0].get("最新价", 0))
                        if price > 0:
                            result[code] = price
                            self._cache[code] = {"price": price, "ts": now}
            except Exception as e2:
                logger.error(f"[MairuiClient] akshare fallback也失败: {e2}")

        return result

    def get_pre_close(self, symbol: str) -> Optional[float]:
        """从麦蕊获取昨收"""
        try:
            from config.mairui_api_config import get_kline_history
            klines = get_kline_history(symbol, period="d", adjust="qfq", limit=5)
            if klines and len(klines) >= 2:
                c = klines[-2].get("close") or klines[-2].get("close_price") or klines[-2].get("c")
                return float(c) if c else None
        except Exception:
            pass
        return None


class QMTRealtimeClient(MarketDataClient):
    """QMT xtdata 实时行情推送 (预留 — 待实盘环境测试)

    使用 xtdata.subscribe_whole_quote() 订阅全市场行情
    配合 on_data_update 回调实时更新价格
    """

    def __init__(self, xtdata_path: str = None):
        self._connected = False
        self._xt_data = None
        self._prices: Dict[str, float] = {}
        self._subscribed: bool = False

    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        try:
            from xtquant import xtdata
            self._xt_data = xtdata
            self._connected = True
            return True
        except ImportError:
            logger.warning("[QMTClient] xtquant not installed")
            return False
        except Exception as e:
            logger.error(f"[QMTClient] connect failed: {e}")
            return False

    def subscribe(self, symbols: List[str]):
        """订阅实时行情"""
        if not self._xt_data:
            return
        try:
            codes = []
            for s in symbols:
                if s.startswith("6") or s.startswith("5"):
                    codes.append(f"{s}.SH")
                else:
                    codes.append(f"{s}.SZ")
            if codes:
                self._xt_data.subscribe_whole_quote(codes)
                self._subscribed = True
                logger.info(f"[QMTClient] 已订阅 {len(codes)} 只股票实时行情")
        except Exception as e:
            logger.error(f"[QMTClient] 订阅行情失败: {e}")

    def get_price(self, symbol: str) -> Optional[float]:
        return self.get_batch_prices([symbol]).get(symbol)

    def get_batch_prices(self, symbols: List[str]) -> Dict[str, float]:
        result = {}
        if not self._xt_data:
            return result
        for s in symbols:
            price = self._prices.get(s)
            if price:
                result[s] = price
                continue
            try:
                code = f"{s}.SH" if (s.startswith("6") or s.startswith("5")) else f"{s}.SZ"
                data = self._xt_data.get_market_data_ex(
                    stock_list=[code], period="tick", count=1
                )
                if data and code in data:
                    last = data[code]
                    if hasattr(last, 'lastPrice'):
                        result[s] = float(last.lastPrice)
                    elif isinstance(last, dict) and 'lastPrice' in last:
                        result[s] = float(last['lastPrice'])
            except Exception:
                pass
        return result

    def get_pre_close(self, symbol: str) -> Optional[float]:
        """从QMT获取昨收"""
        if not self._xt_data:
            return None
        try:
            code = f"{symbol}.SH" if (symbol.startswith("6") or symbol.startswith("5")) else f"{symbol}.SZ"
            detail = self._xt_data.get_instrument_detail(code)
            if detail and hasattr(detail, 'PreClose'):
                return float(detail.PreClose)
            if detail and isinstance(detail, dict):
                return float(detail.get('PreClose', 0)) or None
        except Exception:
            pass
        return None


# ── 全局客户端 ──

_market_client: Optional[MarketDataClient] = None


def get_market_client() -> MarketDataClient:
    """获取全局行情客户端 (默认麦蕊, QMT优先)"""
    global _market_client
    if _market_client is None:
        # QMT环境检测
        if os.environ.get("DSL_BROKER") == "xtquant":
            client = QMTRealtimeClient()
            if client.connect():
                _market_client = client
                return _market_client
        _market_client = MairuiSnapshotClient()
    return _market_client
