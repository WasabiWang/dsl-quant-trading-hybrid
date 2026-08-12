# -*- coding: utf-8 -*-
"""DSL Data SDK - 统一数据接口

从原始SDK重新导出所有公共函数，包括 normalize_symbol。
"""
import sys
import os

# 添加项目根目录到 sys.path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 从原始SDK导入所有公共函数
try:
    from dsl_data_sdk_original import (
        normalize_symbol,
        get_price,
    )
except ImportError:
    # 如果原始SDK不可用，提供基本实现
    def normalize_symbol(symbol: str) -> str:
        """标准化股票代码格式
        
        支持格式:
        - 000001.SZ -> sz000001
        - 600519.SH -> sh600519
        - 000001 -> sz000001 (6位数字，以0/3开头自动判断为深市)
        - 600519 -> sh600519 (6位数字，以6开头自动判断为沪市)
        """
        symbol = str(symbol).strip().upper()
        if symbol.startswith(('SH', 'SZ')):
            return symbol.lower()
        if symbol.endswith('.SZ'):
            return 'sz' + symbol[:-3]
        elif symbol.endswith('.SH'):
            return 'sh' + symbol[:-3]
        # 6位纯数字，自动判断市场
        if symbol.isdigit() and len(symbol) == 6:
            if symbol.startswith(('6', '9')):
                return 'sh' + symbol.lower()
            else:
                return 'sz' + symbol.lower()
        return symbol.lower()
    
    def get_price(symbol: str, market: str = 'cn') -> dict:
        """获取股票价格 — v4.6.4: 新浪数据源,适配sh/sz代码前缀"""
        try:
            norm = normalize_symbol(symbol)
            code = norm[2:] if norm.startswith(('sh','sz')) else symbol
            
            import akshare as ak
            try:
                df = ak.stock_zh_a_spot()
            except Exception:
                df = ak.stock_zh_a_spot_em()
            
            # 新浪代码格式: sh600519, sz000001
            codes = df['代码'].astype(str)
            row = df[(codes == code) | 
                     (codes.str.upper() == 'SH' + code) |
                     (codes.str.upper() == 'SZ' + code) |
                     (codes == 'sh' + code) |
                     (codes == 'sz' + code)]
            if not row.empty:
                r = row.iloc[0]
                return {
                    'symbol': symbol,
                    'name': r.get('名称', ''),
                    'price': float(r.get('最新价', 0)),
                    'high': float(r.get('最高', 0)),
                    'low': float(r.get('最低', 0)),
                    'open': float(r.get('今开', r.get('开盘', 0))),
                    'volume': float(r.get('成交量', 0)),
                    'amount': float(r.get('成交额', 0)),
                    'change_pct': float(r.get('涨跌幅', 0)),
                }
        except Exception as e:
            return {"error": f"get_price failed: {e}"}
        return {"error": "no data"}

__all__ = ['normalize_symbol', 'get_price']
