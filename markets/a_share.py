"""
A股市场数据实现

集成现有的stock-core模块
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from .base_market import MarketData

logger = logging.getLogger(__name__)


class AShareMarket(MarketData):
    """A股市场数据
    
    使用stock-core模块获取数据
    数据源优先级: akshare > eastmoney > tushare
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self._core_available = False
        self._init_core()
        
        logger.info("AShareMarket initialized")
    
    def _init_core(self):
        """初始化stock-core模块"""
        try:
            import sys
            import os
            
            # 尝试导入stock-core
            # 路径: ../../core/stock-core 或通过git submodule
            core_paths = [
                os.path.join(os.path.dirname(__file__), "../../core/stock-core"),
                os.path.join(os.path.dirname(__file__), "../../../alphaquant-backtest/core"),
            ]
            
            for path in core_paths:
                if os.path.exists(path):
                    sys.path.insert(0, path)
                    logger.info(f"stock-core路径: {path}")
                    self._core_available = True
                    break
            
            if not self._core_available:
                logger.warning("stock-core模块未找到，将使用简化模式")
                
        except Exception as e:
            logger.warning(f"stock-core初始化失败: {e}")
            self._core_available = False
    
    def get_price(self, symbol: str) -> float:
        """获取A股最新价格"""
        symbol = self.normalize_symbol(symbol)
        
        if self._core_available:
            try:
                from core.fetch_market_data import get_price
                return get_price(symbol)
            except Exception as e:
                logger.error(f"stock-core获取价格失败: {e}")
        
        # 降级：使用akshare直接获取
        return self._get_price_akshare(symbol)
    
    def get_history(self, symbol: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """获取A股历史数据"""
        symbol = self.normalize_symbol(symbol)
        
        if self._core_available:
            try:
                from core.fetch_market_data import get_history
                return get_history(symbol, start_date, end_date)
            except Exception as e:
                logger.error(f"stock-core获取历史数据失败: {e}")
        
        # 降级实现
        return self._get_history_akshare(symbol, start_date, end_date)
    
    def get_realtime_quote(self, symbol: str) -> Dict[str, Any]:
        """获取A股实时行情"""
        symbol = self.normalize_symbol(symbol)
        
        if self._core_available:
            try:
                from core.fetch_market_data import get_realtime
                return get_realtime(symbol)
            except Exception as e:
                logger.error(f"stock-core获取实时行情失败: {e}")
        
        return self._get_realtime_akshare(symbol)
    
    def get_market_info(self) -> Dict[str, Any]:
        """获取A股市场信息"""
        return {
            "market": "A股",
            "currency": "CNY",
            "timezone": "Asia/Shanghai",
            "trading_hours": {
                "open": "09:30",
                "close": "15:00",
                "break_start": "11:30",
                "break_end": "13:00"
            },
            "rules": {
                "t_rule": "T+1",
                "price_limit": "±10%",
                "st_limit": "±5%"
            }
        }
    
    def normalize_symbol(self, symbol: str) -> str:
        """标准化A股代码"""
        symbol = symbol.strip().upper()
        
        # 如果已经是标准格式 (600519.SH)，直接返回
        if '.' in symbol:
            return symbol
        
        # 纯数字代码，添加后缀
        if len(symbol) == 6:
            if symbol.startswith('6'):
                return f"{symbol}.SH"  # 上海
            elif symbol.startswith(('0', '3')):
                return f"{symbol}.SZ"  # 深圳
            elif symbol.startswith('8') or symbol.startswith('4'):
                return f"{symbol}.BJ"  # 北交所
        
        return symbol
    
    def validate_symbol(self, symbol: str) -> bool:
        """验证A股代码格式"""
        symbol = symbol.strip()
        if not symbol:
            return False
        
        # 6位数字
        if len(symbol) == 6 and symbol.isdigit():
            return True
        
        # 标准格式: XXXXXX.SH/SZ/BJ
        if '.' in symbol:
            parts = symbol.split('.')
            if len(parts) == 2 and len(parts[0]) == 6 and parts[0].isdigit():
                return parts[1] in ['SH', 'SZ', 'BJ']
        
        return False
    
    # --- 降级实现（akshare直接调用）---
    
    def _get_price_akshare(self, symbol: str) -> float:
        """使用akshare获取价格 — v4.6.4: 新浪优先, 东财降级"""
        try:
            import akshare as ak
            
            code = symbol.split('.')[0] if '.' in symbol else symbol
            
            # 主数据源: 新浪 (东财push2已封锁)
            try:
                df = ak.stock_zh_a_spot()
            except Exception:
                df = ak.stock_zh_a_spot_em()
            
            # 新浪代码格式: sh600519, sz000001 等
            codes = df['代码'].astype(str)
            row = df[(codes == code) | 
                     (codes.str.upper() == 'SH' + code) |
                     (codes.str.upper() == 'SZ' + code) |
                     (codes == 'sh' + code) |
                     (codes == 'sz' + code)]
            
            if not row.empty:
                return float(row['最新价'].iloc[0])
            
            return 0.0
            
        except Exception as e:
            logger.error(f"akshare获取价格失败: {e}")
            return 0.0
    
    def _get_history_akshare(self, symbol: str, start_date: str, end_date: str) -> Dict[str, Any]:
        """使用akshare获取历史数据 — v4.6.4: 东财优先, BaoStock兜底"""
        try:
            import akshare as ak
            import pandas as pd
            
            code = symbol.split('.')[0] if '.' in symbol else symbol
            df = None
            
            # 优先东财K线 (push2his可能仍部分可用)
            for attempt in range(2):
                try:
                    import time, random
                    time.sleep(random.uniform(0.5, 1.5))
                    df = ak.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=start_date.replace('-', ''),
                        end_date=end_date.replace('-', ''),
                        adjust="qfq"
                    )
                    if not df.empty:
                        break
                except Exception as e:
                    if attempt < 1:
                        logger.warning(f"东财K线失败, 尝试BaoStock...")
                    break
            
            # 降级: BaoStock
            if df is None or df.empty:
                logger.info(f"东财K线不可用, 使用BaoStock")
                import baostock as bs
                code_num = code.zfill(6)
                bs_code = f"sh.{code_num}" if code_num.startswith(('6','9')) else f"sz.{code_num}"
                
                lg = bs.login()
                try:
                    fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
                    rs = bs.query_history_k_data_plus(bs_code, fields,
                                                       start_date=start_date.replace('-',''),
                                                       end_date=end_date.replace('-',''),
                                                       frequency="d", adjustflag="2")
                    data_list = []
                    while (rs.error_code == '0') and rs.next():
                        data_list.append(rs.get_row_data())
                    if data_list:
                        df = pd.DataFrame(data_list, columns=fields.split(','))
                        for c in ['open','high','low','close','preclose','volume','amount','turn','pctChg']:
                            df[c] = pd.to_numeric(df[c], errors='coerce')
                        df.rename(columns={
                            'date':'日期','code':'股票代码','open':'开盘','high':'最高',
                            'low':'最低','close':'收盘','volume':'成交量','amount':'成交额',
                            'turn':'换手率','pctChg':'涨跌幅'
                        }, inplace=True)
                finally:
                    bs.logout()
            
            return {
                "symbol": symbol,
                "data": df.to_dict('records') if df is not None and not df.empty else [],
                "count": len(df) if df is not None else 0
            }
            
        except Exception as e:
            logger.error(f"akshare获取历史数据失败: {e}")
            return {"symbol": symbol, "data": [], "count": 0}
    
    def _get_realtime_akshare(self, symbol: str) -> Dict[str, Any]:
        """使用akshare获取实时行情（降级）"""
        try:
            import akshare as ak
            
            code = symbol.split('.')[0] if '.' in symbol else symbol
            
            df = ak.stock_zh_a_spot_em()
            row = df[df['代码'] == code]
            
            if not row.empty:
                return {
                    "symbol": symbol,
                    "price": float(row['最新价'].iloc[0]),
                    "change": float(row['涨跌幅'].iloc[0]),
                    "volume": int(row['成交量'].iloc[0]),
                    "amount": float(row['成交额'].iloc[0])
                }
            
            return {}
            
        except Exception as e:
            logger.error(f"akshare获取实时行情失败: {e}")
            return {}
