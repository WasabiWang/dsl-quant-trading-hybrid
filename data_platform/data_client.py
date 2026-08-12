#!/usr/bin/env python3
"""
统一数据访问入口 - 封装多个数据源
"""
import time
import json
from typing import Dict, List, Any, Optional
from common.cache import cache
from common.logger import get_logger
from common.http_client import HttpClient
from common.config import get_config

logger = get_logger("data_client")

class DataClient:
    """
    统一数据访问客户端
    封装东方财富、AKShare、Tushare、RSS等数据源
    """
    
    def __init__(self):
        self.cache = cache
        self.http = HttpClient()
        self.default_source = get_config('data_client.default_source', 'eastmoney')
        self.rate_limit = get_config('data_client.rate_limit.max_per_second', 10)
        self._request_times = []
    
    def _check_rate_limit(self):
        """限流检查"""
        now = time.time()
        # 清理1秒前的请求
        self._request_times = [t for t in self._request_times if now - t < 1]
        if len(self._request_times) >= self.rate_limit:
            time.sleep(0.1)
        self._request_times.append(now)
    
    def get_market_data(self, symbol: str, period: str = "1d",
                       start_time: Optional[str] = None, 
                       end_time: Optional[str] = None,
                       limit: int = 1000) -> Dict:
        """获取行情数据"""
        # 先查缓存
        cache_key = f"market_data:{symbol}:{period}:{limit}"
        cached = self.cache.get(cache_key)
        if cached:
            logger.info(f"行情数据命中缓存: {symbol}")
            return cached
        
        self._check_rate_limit()
        
        # 根据数据源调用
        if self.default_source == 'eastmoney':
            data = self._get_from_eastmoney(symbol, period, limit)
        elif self.default_source == 'akshare':
            data = self._get_from_akshare(symbol, period, limit)
        else:
            data = {"code": 400, "msg": f"未知数据源: {self.default_source}"}
        
        # 缓存结果
        if data.get("code") == 200:
            self.cache.set(cache_key, data, 60)  # 1分钟缓存
        
        return data
    
    def _get_from_eastmoney(self, symbol: str, period: str, limit: int) -> Dict:
        """从东方财富获取数据（简化实现）"""
        # 实际项目中应调用真实API
        return {
            "code": 200,
            "msg": "success",
            "data": [
                {"time": int(time.time()*1000), "open": 12.5, "high": 12.8, 
                 "low": 12.3, "close": 12.6, "volume": 1000000}
            ],
            "request_id": f"em_{symbol}_{int(time.time())}"
        }
    
    def _get_from_akshare(self, symbol: str, period: str, limit: int) -> Dict:
        """从AKShare获取数据（简化实现）"""
        return {
            "code": 200,
            "msg": "success",
            "data": [
                {"time": int(time.time()*1000), "open": 12.5, "high": 12.8,
                 "low": 12.3, "close": 12.6, "volume": 1000000}
            ],
            "request_id": f"ak_{symbol}_{int(time.time())}"
        }
    
    def get_fundamentals(self, symbol: str, fields: List[str]) -> Dict:
        """获取基本面数据"""
        cache_key = f"fundamentals:{symbol}:{','.join(fields)}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        self._check_rate_limit()
        
        # 简化实现
        data = {
            "code": 200,
            "msg": "success",
            "data": {
                "pe": 12.34,
                "pb": 1.56,
                "roe": 0.18,
                "eps": 2.34,
                "update_time": int(time.time() * 1000)
            }
        }
        
        self.cache.set(cache_key, data, 3600)  # 1小时缓存
        return data
    
    def get_news(self, symbol: str = None, limit: int = 10) -> Dict:
        """获取新闻数据"""
        cache_key = f"news:{symbol or 'all'}:{limit}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        
        # 简化实现
        data = {
            "code": 200,
            "msg": "success",
            "data": [
                {"title": "测试新闻1", "publish_time": int(time.time()*1000), "source": "测试"},
                {"title": "测试新闻2", "publish_time": int(time.time()*1000), "source": "测试"}
            ][:limit]
        }
        
        self.cache.set(cache_key, data, 300)  # 5分钟缓存
        return data

# 全局实例
data_client = DataClient()

if __name__ == "__main__":
    client = DataClient()
    # 测试
    print(client.get_market_data("600000", "1d"))
    print(client.get_fundamentals("600000", ["pe", "pb"]))
    print(client.get_news())