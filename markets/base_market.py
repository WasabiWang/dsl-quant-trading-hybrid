"""
市场抽象基类

定义统一的市场数据接口，所有市场实现必须继承此类
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class MarketData(ABC):
    """市场数据抽象基类"""
    
    @abstractmethod
    def get_price(self, symbol: str) -> float:
        """获取最新价格
        
        Args:
            symbol: 股票代码
            
        Returns:
            最新价格
        """
        pass
    
    @abstractmethod
    def get_history(self, symbol: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """获取历史K线数据
        
        Args:
            symbol: 股票代码
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            包含OHLCV数据的字典
        """
        pass
    
    @abstractmethod
    def get_realtime_quote(self, symbol: str) -> Dict[str, Any]:
        """获取实时行情
        
        Args:
            symbol: 股票代码
            
        Returns:
            包含买卖五档、成交量等数据的字典
        """
        pass
    
    @abstractmethod
    def get_market_info(self) -> Dict[str, Any]:
        """获取市场信息
        
        Returns:
            市场基本信息（交易时间、规则等）
        """
        pass
    
    def validate_symbol(self, symbol: str) -> bool:
        """验证股票代码格式
        
        Args:
            symbol: 股票代码
            
        Returns:
            是否有效
        """
        # 默认实现，子类可覆盖
        return bool(symbol and len(symbol) > 0)
    
    def normalize_symbol(self, symbol: str) -> str:
        """标准化股票代码
        
        Args:
            symbol: 原始股票代码
            
        Returns:
            标准化后的代码
        """
        # 默认实现，子类可覆盖
        return symbol.strip().upper()
