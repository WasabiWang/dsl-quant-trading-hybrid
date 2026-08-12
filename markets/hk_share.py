"""
港股市场数据实现

数据源: Eastmoney港股API + Yahoo Finance
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from .base_market import MarketData

logger = logging.getLogger(__name__)


class HKShareMarket(MarketData):
    """港股市场数据
    
    数据源:
    - 东方财富港股API
    - Yahoo Finance (yfinance)
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        logger.info("HKShareMarket initialized")
    
    def get_price(self, symbol: str) -> float:
        """获取港股最新价格"""
        symbol = self.normalize_symbol(symbol)
        
        # 尝试yfinance
        try:
            import yfinance as yf
            
            # 港股代码格式: 0700.HK
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return info.get('currentPrice', 0.0)
            
        except Exception as e:
            logger.warning(f"yfinance获取港股价格失败: {e}")
        
        # 降级：东方财富API
        return self._get_price_eastmoney(symbol)
    
    def get_history(self, symbol: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """获取港股历史数据"""
        symbol = self.normalize_symbol(symbol)
        
        try:
            import yfinance as yf
            import pandas as pd
            
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start_date, end=end_date)
            
            return {
                "symbol": symbol,
                "data": df.reset_index().to_dict('records') if not df.empty else [],
                "count": len(df),
                "currency": "HKD"
            }
            
        except Exception as e:
            logger.error(f"yfinance获取港股历史数据失败: {e}")
            return {"symbol": symbol, "data": [], "count": 0, "currency": "HKD"}
    
    def get_realtime_quote(self, symbol: str) -> Dict[str, Any]:
        """获取港股实时行情"""
        symbol = self.normalize_symbol(symbol)
        
        try:
            import yfinance as yf
            
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            return {
                "symbol": symbol,
                "price": info.get('currentPrice', 0.0),
                "change": info.get('regularMarketChangePercent', 0.0),
                "volume": info.get('volume', 0),
                "market_cap": info.get('marketCap', 0),
                "currency": "HKD"
            }
            
        except Exception as e:
            logger.error(f"yfinance获取港股实时行情失败: {e}")
            return {}
    
    def get_market_info(self) -> Dict[str, Any]:
        """获取港股市场信息"""
        return {
            "market": "港股",
            "currency": "HKD",
            "timezone": "Asia/Hong_Kong",
            "trading_hours": {
                "open": "09:30",
                "close": "16:00",
                "break_start": "12:00",
                "break_end": "13:00"
            },
            "rules": {
                "t_rule": "T+0",
                "price_limit": "无涨跌停限制",
                "min_tick": "取决于股价区间"
            }
        }
    
    def normalize_symbol(self, symbol: str) -> str:
        """标准化港股代码"""
        symbol = symbol.strip().upper()
        
        # 如果已经是标准格式 (0700.HK)，直接返回
        if '.' in symbol:
            return symbol
        
        # 纯数字代码，添加.HK后缀
        if symbol.isdigit():
            # 补齐5位
            symbol = symbol.zfill(5)
            return f"{symbol}.HK"
        
        return symbol
    
    def validate_symbol(self, symbol: str) -> bool:
        """验证港股代码格式"""
        symbol = symbol.strip()
        if not symbol:
            return False
        
        # 5位数字
        if len(symbol) == 5 and symbol.isdigit():
            return True
        
        # 标准格式: XXXXX.HK
        if '.' in symbol:
            parts = symbol.split('.')
            if len(parts) == 2 and len(parts[0]) == 5 and parts[0].isdigit():
                return parts[1] == 'HK'
        
        return False
    
    # --- 降级实现（东方财富API）---
    
    def _get_price_eastmoney(self, symbol: str) -> float:
        """使用东方财富API获取港股价格（降级）"""
        try:
            import requests
            
            # 东方财富港股API
            code = symbol.split('.')[0] if '.' in symbol else symbol
            url = f"http://push2.eastmoney.com/api/qt/stock/get"
            
            params = {
                "secid": f"116.{code}",  # 116=港股
                "fields": "f43,f57,f58,f169,f170,f46,f44,f51,f168,f47,f164,f163,f116,f60,f45,f52,f50,f48,f167,f117,f71,f161,f49,f530"
            }
            
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data.get('data'):
                    return float(data['data'].get('f43', 0))
            
            return 0.0
            
        except Exception as e:
            logger.error(f"东方财富API获取港股价格失败: {e}")
            return 0.0
