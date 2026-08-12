"""
模拟交易模块

包含：
- 纸交易引擎 (Paper Trading)
- 回测验证
- 信号追踪
"""

from .paper_trading import PaperTradingEngine, PaperTrade

__all__ = [
    "PaperTradingEngine",
    "PaperTrade"
]
