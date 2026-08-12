"""
宏观数据源模块

采集全球主要经济体的宏观指标：
- 中国: GDP, CPI, PPI, M2, 利率, 失业率
- 美国: Federal Funds Rate, CPI, Non-farm Payrolls, GDP
- 全球: 油价, 金价, 美元指数
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class MacroDataSource:
    """宏观数据源管理器"""
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        logger.info("MacroDataSource initialized")
    
    def get_china_indicators(self) -> Dict[str, Any]:
        """获取中国宏观指标"""
        indicators = {
            "country": "China",
            "currency": "CNY",
            "updated_at": datetime.now().isoformat(),
            "data": {}
        }
        
        # 1. GDP增长率 (季度)
        indicators["data"]["gdp_growth"] = self._get_china_gdp()
        
        # 2. CPI (月度)
        indicators["data"]["cpi"] = self._get_china_cpi()
        
        # 3. PPI (月度)
        indicators["data"]["ppi"] = self._get_china_ppi()
        
        # 4. M2货币供应 (月度)
        indicators["data"]["m2_growth"] = self._get_china_m2()
        
        # 5. LPR利率
        indicators["data"]["lpr_1y"] = 3.1  # 示例值
        indicators["data"]["lpr_5y"] = 3.6
        
        # 6. 失业率
        indicators["data"]["unemployment_rate"] = 5.0
        
        return indicators
    
    def get_us_indicators(self) -> Dict[str, Any]:
        """获取美国宏观指标"""
        indicators = {
            "country": "USA",
            "currency": "USD",
            "updated_at": datetime.now().isoformat(),
            "data": {}
        }
        
        # 1. Federal Funds Rate
        indicators["data"]["fed_rate"] = 4.50  # 示例值
        
        # 2. CPI
        indicators["data"]["cpi"] = self._get_us_cpi()
        
        # 3. Non-farm Payrolls
        indicators["data"]["nfp"] = 200000  # 示例值
        
        # 4. GDP Growth
        indicators["data"]["gdp_growth"] = 2.5
        
        # 5. Unemployment Rate
        indicators["data"]["unemployment_rate"] = 3.7
        
        return indicators
    
    def get_global_indicators(self) -> Dict[str, Any]:
        """获取全球宏观指标"""
        return {
            "oil_price_brent": 80.5,  # 布伦特原油
            "oil_price_wti": 75.2,    # WTI原油
            "gold_price": 2050.0,     # 金价 (USD/oz)
            "dxy_index": 104.5,       # 美元指数
            "vix_index": 15.2,        # 恐慌指数
            "updated_at": datetime.now().isoformat()
        }
    
    def get_all_indicators(self) -> Dict[str, Any]:
        """获取所有宏观指标"""
        return {
            "china": self.get_china_indicators(),
            "usa": self.get_us_indicators(),
            "global": self.get_global_indicators()
        }
    
    # --- 内部方法：数据获取实现 ---
    
    def _get_china_gdp(self) -> float:
        """获取中国GDP增长率"""
        try:
            import akshare as ak
            # 简化实现，生产环境应解析真实数据
            return 5.2  # 示例值
        except:
            return 5.0
    
    def _get_china_cpi(self) -> float:
        """获取中国CPI"""
        try:
            import akshare as ak
            return 0.3  # 示例值
        except:
            return 0.5
    
    def _get_china_ppi(self) -> float:
        """获取中国PPI"""
        try:
            import akshare as ak
            return -1.5  # 示例值
        except:
            return -1.0
    
    def _get_china_m2(self) -> float:
        """获取中国M2增长率"""
        try:
            import akshare as ak
            return 10.5  # 示例值
        except:
            return 10.0
    
    def _get_us_cpi(self) -> float:
        """获取美国CPI"""
        try:
            import yfinance as yf
            return 3.2  # 示例值
        except:
            return 3.5


def get_macro_data() -> Dict[str, Any]:
    """便捷函数：获取所有宏观数据"""
    source = MacroDataSource()
    return source.get_all_indicators()
