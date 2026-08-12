"""
市场模块 - 多市场支持

支持A股、港股、美股统一接口
"""

from .base_market import MarketData
from .a_share import AShareMarket
from .hk_share import HKShareMarket
from .us_share import USShareMarket
from typing import Dict, Any, Optional

__all__ = [
    "MarketData",
    "AShareMarket",
    "HKShareMarket",
    "USShareMarket",
    "get_market",
    "detect_market"
]


def get_market(market_type: str = "auto", symbol: str = "", config: Optional[Dict[str, Any]] = None) -> MarketData:
    """获取市场实例
    
    Args:
        market_type: 市场类型 (a/hk/us/auto)
        symbol: 股票代码（用于自动检测）
        config: 配置字典
        
    Returns:
        MarketData实例
    """
    if market_type == "auto" and symbol:
        market_type = detect_market(symbol)
    
    markets = {
        "a": AShareMarket,
        "hk": HKShareMarket,
        "us": USShareMarket,
        "ashare": AShareMarket,
        "hkshare": HKShareMarket,
        "usshare": USShareMarket,
    }
    
    market_class = markets.get(market_type.lower())
    if not market_class:
        raise ValueError(f"未知市场类型: {market_type}，支持的类型: a/hk/us/auto")
    
    return market_class(config=config)


def detect_market(symbol: str) -> str:
    """根据股票代码自动检测市场
    
    Args:
        symbol: 股票代码
        
    Returns:
        市场类型: a/hk/us
    """
    symbol = symbol.strip().upper()
    
    # A股: 6位数字 或 XXXXXX.SH/SZ/BJ
    if '.' in symbol:
        suffix = symbol.split('.')[1]
        if suffix in ['SH', 'SZ', 'BJ']:
            return 'a'
        elif suffix == 'HK':
            return 'hk'
    
    # 纯数字判断
    if symbol.isdigit():
        # 港股: 4-5位数字（包括前导0）
        if 4 <= len(symbol) <= 5:
            return 'hk'
        # A股: 6位数字
        elif len(symbol) == 6:
            return 'a'
        # 其他数字代码：根据长度判断
        else:
            # 移除前导0后判断
            clean_symbol = symbol.lstrip('0')
            if 1 <= len(clean_symbol) <= 6:
                return 'a'  # 默认为A股
    
    # 默认美股（字母代码）
    return 'us'
