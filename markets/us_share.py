"""
美股市场数据实现

数据源: Yahoo Finance (yfinance) + Alpha Vantage
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from .base_market import MarketData

logger = logging.getLogger(__name__)


class USShareMarket(MarketData):
    """美股市场数据
    
    数据源:
    - Yahoo Finance (主要)
    - Alpha Vantage (备用，需要API密钥)
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.alpha_vantage_key = self.config.get('alpha_vantage_key')
        logger.info("USShareMarket initialized")
    
    def get_price(self, symbol: str) -> float:
        """获取美股最新价格"""
        symbol = self.normalize_symbol(symbol)
        
        # 主要：yfinance
        try:
            import yfinance as yf
            
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return info.get('currentPrice', 0.0)
            
        except Exception as e:
            logger.warning(f"yfinance获取美股价格失败: {e}")
        
        # 备用：Alpha Vantage
        if self.alpha_vantage_key:
            return self._get_price_alpha_vantage(symbol)
        
        return 0.0
    
    def get_history(self, symbol: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """获取美股历史数据"""
        symbol = self.normalize_symbol(symbol)
        
        try:
            import yfinance as yf
            
            ticker = yf.Ticker(symbol)
            df = ticker.history(start=start_date, end=end_date)
            
            return {
                "symbol": symbol,
                "data": df.reset_index().to_dict('records') if not df.empty else [],
                "count": len(df),
                "currency": "USD"
            }
            
        except Exception as e:
            logger.error(f"yfinance获取美股历史数据失败: {e}")
            return {"symbol": symbol, "data": [], "count": 0, "currency": "USD"}
    
    def get_realtime_quote(self, symbol: str) -> Dict[str, Any]:
        """获取美股实时行情"""
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
                "pe_ratio": info.get('trailingPE', 0),
                "currency": "USD"
            }
            
        except Exception as e:
            logger.error(f"yfinance获取美股实时行情失败: {e}")
            return {}
    
    def get_market_info(self) -> Dict[str, Any]:
        """获取美股市场信息"""
        return {
            "market": "美股",
            "currency": "USD",
            "timezone": "America/New_York",
            "trading_hours": {
                "open": "09:30",
                "close": "16:00",
                "pre_market": "04:00-09:30",
                "after_hours": "16:00-20:00"
            },
            "rules": {
                "t_rule": "T+0",
                "price_limit": "无涨跌停限制（有熔断机制）",
                "circuit_breaker": "7%/13%/20% 三级熔断"
            }
        }
    
    def normalize_symbol(self, symbol: str) -> str:
        """标准化美股代码"""
        symbol = symbol.strip().upper()
        
        # 美股代码通常不需要后缀
        # 但需要处理特殊字符
        return symbol.replace('.', '-').replace('/', '-')
    
    def validate_symbol(self, symbol: str) -> bool:
        """验证美股代码格式"""
        symbol = symbol.strip()
        if not symbol:
            return False
        
        # 美股代码: 1-5个字母（部分含数字）
        if len(symbol) <= 5:
            return True
        
        return False
    
    # --- 备用实现（Alpha Vantage）---
    
    def _get_price_alpha_vantage(self, symbol: str) -> float:
        """使用Alpha Vantage获取美股价格（备用）"""
        try:
            import requests
            
            url = "https://www.alphavantage.co/query"
            params = {
                "function": "GLOBAL_QUOTE",
                "symbol": symbol,
                "apikey": self.alpha_vantage_key
            }
            
            response = requests.get(url, params=params, timeout=10)
            if response.status_code == 200:
                data = response.json()
                quote = data.get('Global Quote', {})
                price = quote.get('05. price', 0)
                return float(price)
            
            return 0.0
            
        except Exception as e:
            logger.error(f"Alpha Vantage获取美股价格失败: {e}")
            return 0.0
