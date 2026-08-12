#!/usr/bin/env python3
"""麦蕊智数 MyData 数据源适配器

复用 config/mairui_api_config.py 中已有的完善接口（含重试、限流、30+API），
提供 predictor 兼容的数据加载方法。

已存在的麦蕊配置:
  - config/settings.py: MAIRUI_LICENCE, BASE_URL, 限流参数
  - config/mairui_api_config.py: 30+ API接口 + 3次指数退避重试
"""

import os
import sys
import logging
import pandas as pd
import numpy as np
from typing import Optional, List, Dict
from datetime import datetime

# macOS 系统代理绕过（依赖 config/mairui_api_config 的已被修复，此处双重保险）
from common.proxy_bypass import clean_env_proxies
clean_env_proxies()

logger = logging.getLogger(__name__)

# 确保能导入config模块
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from config.mairui_api_config import (
        request_api, get_stock_real, get_multi_stock_real,
        get_all_stock_list, get_stock_five_level,
        get_ma, get_macd, get_boll, get_kdj, get_indicator,
        get_kline_history as _get_kline_history,
        get_financial_indicators, get_balance_sheet,
        get_income_statement, get_cashflow_statement,
        get_industry_concept_tree, get_category_stocks,
        get_stock_concepts, get_capital_structure,
        get_top_holders, get_top_flow_holders, get_holder_count,
        LICENCE,
    )
    MAIRUI_AVAILABLE = True
except ImportError as e:
    logger.warning(f"麦蕊API配置导入失败: {e}")
    MAIRUI_AVAILABLE = False
    LICENCE = None


def is_available() -> bool:
    """检查麦蕊数据源是否可用"""
    return MAIRUI_AVAILABLE and LICENCE is not None


def _get_licence() -> Optional[str]:
    """Return the configured licence without exposing it in logs."""
    return LICENCE if is_available() else None


def _coerce_trade_datetime(series: pd.Series) -> pd.Series:
    """Parse only date-like strings; reject numeric indicator values such as 1063."""
    text = series.astype(str).str.strip()
    date_like = text.str.match(
        r"^\d{4}[-/]?\d{2}[-/]?\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$",
        na=False,
    )
    parsed = pd.Series(pd.NaT, index=series.index, dtype="datetime64[ns]")
    parsed.loc[date_like] = pd.to_datetime(text.loc[date_like], errors="coerce")
    return parsed


# =========================================================================
# 历史K线数据（原始OHLCV接口）
# =========================================================================

def get_history_kline(symbol: str, period: str = "d",
                      start_date: str = None, end_date: str = None,
                      limit: int = None, adjust: str = "f") -> Optional[pd.DataFrame]:
    """获取历史K线数据
    
    通过麦蕊的 hsstock/history K线接口获取，返回标准化的OHLCV数据。
    
    Args:
        symbol: 股票代码，如 '600519.SH' 或 '600519'
        period: 周期 d=日线, w=周线, m=月线, 5/15/30/60=分钟线
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        limit: 获取最新N条
        adjust: 复权类型 f=前复权, n=不复权, b=后复权
    
    Returns:
        DataFrame: date, open, high, low, close, volume, amount
    """
    if not MAIRUI_AVAILABLE:
        return None
    
    sym = _normalize_symbol(symbol)
    
    try:
        data = _get_kline_history(
            sym,
            period=period,
            adjust=adjust,
            start_date=start_date.replace('-', '') if start_date else None,
            end_date=end_date.replace('-', '') if end_date else None,
            limit=limit,
        )
        
        if data and isinstance(data, list):
            df = pd.DataFrame(data)
            if df.empty:
                return None
            
            # 标准化列名
            col_map = {
                't': 'date', 'o': 'open', 'h': 'high', 'l': 'low',
                'c': 'close', 'v': 'volume', 'a': 'amount',
                'pc': 'prev_close', 'sf': 'suspension_flag',
                'tt': 'date', 'op': 'open', 'hi': 'high', 'lo': 'low',
                'cl': 'close', 'vo': 'volume', 'am': 'amount'
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            
            if 'date' in df.columns:
                df['date'] = _coerce_trade_datetime(df['date'])
                df = df.dropna(subset=['date'])
                if df.empty:
                    logger.warning(f"麦蕊K线日期字段无有效交易日 {symbol}")
                    return None
                df = df.set_index('date')
                df = df.sort_index()
                if start_date:
                    start_ts = pd.to_datetime(start_date.replace('-', ''), errors="coerce")
                    if pd.notna(start_ts):
                        df = df[df.index >= start_ts]
                if end_date:
                    end_ts = pd.to_datetime(end_date.replace('-', ''), errors="coerce")
                    if pd.notna(end_ts):
                        df = df[df.index <= end_ts]
            else:
                logger.warning(f"麦蕊K线缺少日期字段 {symbol}: columns={list(df.columns)}")
                return None
            
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount', 'prev_close', 'suspension_flag']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            required = ['open', 'high', 'low', 'close', 'volume']
            missing = [col for col in required if col not in df.columns]
            if missing:
                logger.warning(f"麦蕊K线缺少OHLCV字段 {symbol}: missing={missing}")
                return None
            df = df.dropna(subset=['close'])
            if df.empty:
                return None
            
            return df
    except Exception as e:
        logger.error(f"获取历史K线失败 {symbol}: {e}")
    
    return None


# =========================================================================
# 实时交易数据
# =========================================================================

def get_realtime_quote(symbol: str) -> Optional[dict]:
    """获取实时行情（复用已有接口）"""
    if not MAIRUI_AVAILABLE:
        return None
    
    sym = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    try:
        return get_stock_real(sym)
    except Exception as e:
        logger.error(f"获取实时行情失败 {symbol}: {e}")
    return None


def get_realtime_quotes_batch(symbols: List[str]) -> Optional[List[dict]]:
    """批量获取实时行情（最多20只）"""
    if not MAIRUI_AVAILABLE:
        return None
    
    syms = [s.replace('.SH', '').replace('.SZ', '').replace('.BJ', '') for s in symbols[:20]]
    try:
        return get_multi_stock_real(syms)
    except Exception as e:
        logger.error(f"批量获取实时行情失败: {e}")
    return None


# =========================================================================
# 基本面/财务数据
# =========================================================================

def get_fundamentals(symbol: str, limit: int = 4) -> Optional[pd.DataFrame]:
    """获取财务主要指标"""
    if not MAIRUI_AVAILABLE:
        return None
    
    sym = _normalize_symbol(symbol)
    try:
        data = get_financial_indicators(sym, limit=limit)
        if data and isinstance(data, list):
            return pd.DataFrame(data)
        elif data and isinstance(data, dict):
            return pd.DataFrame([data])
    except Exception as e:
        logger.error(f"获取财务指标失败 {symbol}: {e}")
    return None


def get_basic_info(symbol: str) -> Optional[dict]:
    """获取个股基本信息"""
    # 麦蕊没有单独的info接口，用实时行情+财务指标组合
    if not MAIRUI_AVAILABLE:
        return None
    
    sym = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    try:
        quote = get_stock_real(sym)
        return quote
    except Exception as e:
        logger.error(f"获取基本信息失败 {symbol}: {e}")
    return None


# =========================================================================
# 技术指标
# =========================================================================

def get_history_macd(symbol: str, period: str = "d",
                     start_date: str = None, limit: int = None) -> Optional[pd.DataFrame]:
    """获取历史MACD数据（复用已有接口）"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = _normalize_symbol(symbol)
    try:
        params = {}
        if start_date:
            params["start_date"] = start_date.replace('-', '')
        data = get_macd(sym, period=period, limit=limit, **params)
        if data and isinstance(data, list):
            return pd.DataFrame(data)
    except Exception as e:
        logger.error(f"获取MACD失败 {symbol}: {e}")
    return None


def get_history_kdj(symbol: str, period: str = "d",
                    start_date: str = None, limit: int = None) -> Optional[pd.DataFrame]:
    """获取历史KDJ数据（复用已有接口）"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = _normalize_symbol(symbol)
    try:
        params = {}
        if start_date:
            params["start_date"] = start_date.replace('-', '')
        data = get_kdj(sym, period=period, limit=limit, **params)
        if data and isinstance(data, list):
            return pd.DataFrame(data)
    except Exception as e:
        logger.error(f"获取KDJ失败 {symbol}: {e}")
    return None


def get_history_boll(symbol: str, period: str = "d",
                     start_date: str = None, limit: int = None) -> Optional[pd.DataFrame]:
    """获取历史BOLL数据（复用已有接口）"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = _normalize_symbol(symbol)
    try:
        params = {}
        if start_date:
            params["start_date"] = start_date.replace('-', '')
        data = get_boll(sym, period=period, limit=limit, **params)
        if data and isinstance(data, list):
            return pd.DataFrame(data)
    except Exception as e:
        logger.error(f"获取BOLL失败 {symbol}: {e}")
    return None


def get_history_ma(symbol: str, period: str = "d",
                   start_date: str = None, limit: int = None) -> Optional[pd.DataFrame]:
    """获取历史MA数据（复用已有接口）"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = _normalize_symbol(symbol)
    try:
        params = {}
        if start_date:
            params["start_date"] = start_date.replace('-', '')
        data = get_ma(sym, period=period, limit=limit, **params)
        if data and isinstance(data, list):
            return pd.DataFrame(data)
    except Exception as e:
        logger.error(f"获取MA失败 {symbol}: {e}")
    return None


# =========================================================================
# 资金流/涨跌停
# =========================================================================

def get_capital_flow(symbol: str, start_date: str = None,
                     end_date: str = None, limit: int = None) -> Optional[dict]:
    """获取资金流向数据"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = _normalize_symbol(symbol)
    try:
        params = {}
        if start_date:
            params["st"] = start_date.replace('-', '')
        if end_date:
            params["et"] = end_date.replace('-', '')
        if limit:
            params["lt"] = limit
        data = request_api("stock_capital_flow", stock_code=sym, **params)
        return data
    except Exception as e:
        logger.error(f"获取资金流失败 {symbol}: {e}")
    return None


def get_stop_price(symbol: str) -> Optional[dict]:
    """获取涨跌停价格"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = _normalize_symbol(symbol)
    try:
        data = request_api("stock_stop_price", stock_code=sym) if "stock_stop_price" in dir() else None
        return data
    except Exception as e:
        logger.error(f"获取涨跌停价格失败 {symbol}: {e}")
    return None


# =========================================================================
# 行业/概念
# =========================================================================

def get_industry_tree() -> Optional[list]:
    """获取行业/概念树"""
    if not MAIRUI_AVAILABLE:
        return None
    try:
        return get_industry_concept_tree()
    except Exception as e:
        logger.error(f"获取行业概念树失败: {e}")
    return None


def query_stock_concepts(symbol: str) -> Optional[list]:
    """查询股票所属行业/概念"""
    if not MAIRUI_AVAILABLE:
        return None
    sym = symbol.replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    try:
        from config.mairui_api_config import get_stock_concepts as _get_concepts
        return _get_concepts(sym)
    except Exception as e:
        logger.error(f"查询股票概念失败 {symbol}: {e}")
    return None


# =========================================================================
# 适配器：将麦蕊数据转为 predictor 兼容格式
# =========================================================================

def load_stock_data_mairui(symbol: str, start_date: str = None,
                           end_date: str = None) -> Optional[pd.DataFrame]:
    """从麦蕊API加载股票数据，格式兼容 predictor.data_loader
    
    Args:
        symbol: 股票代码，如 '600519.SH'
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
    
    Returns:
        DataFrame: open, high, low, close, volume, amount 等
    """
    if not MAIRUI_AVAILABLE:
        return None
    
    # 获取历史K线
    df = get_history_kline(symbol, period='d', start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        return None
    
    # 尝试合并财务指标
    try:
        fund_df = get_fundamentals(symbol, limit=1)
        if fund_df is not None and not fund_df.empty:
            # 将最新财务指标作为常量列添加
            latest = fund_df.iloc[-1] if isinstance(fund_df, pd.DataFrame) else fund_df
            if isinstance(latest, pd.Series):
                for key in ['pe', 'pb', 'roe', 'eps']:
                    if key in latest.index:
                        df[key] = pd.to_numeric(latest[key], errors='coerce')
    except Exception as e:
        logger.debug(f"财务指标合并跳过: {e}")
    
    return df


# =========================================================================
# 工具函数
# =========================================================================

def _normalize_symbol(symbol: str) -> str:
    """标准化股票代码为麦蕊格式 (如 600519.SH)"""
    sym = symbol.strip().upper()
    if '.' in sym:
        return sym
    # 纯数字自动补市场后缀
    if sym.startswith(('6', '9')):
        return f"{sym}.SH"
    elif sym.startswith(('0', '3')):
        return f"{sym}.SZ"
    elif sym.startswith(('4', '8')):
        return f"{sym}.BJ"
    return sym


# =========================================================================
# 测试
# =========================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("📊 麦蕊智数 MyData 数据源适配器测试")
    print("=" * 60)
    
    if is_available():
        print(f"✅ Licence: {LICENCE[:8]}...")
        
        # 测试实时行情
        quote = get_realtime_quote("600519")
        if quote:
            print(f"✅ 实时行情(贵州茅台): {quote.get('current_price', 'N/A')}")
        else:
            print("⚠️ 实时行情获取失败（可能非交易时间）")
        
        # 测试财务指标
        fund = get_fundamentals("600519.SH", limit=1)
        if fund is not None:
            print(f"✅ 财务指标: {len(fund)}条")
        else:
            print("⚠️ 财务指标获取失败")
        
        # 测试技术指标
        macd = get_history_macd("600519.SH", period='d', limit=5)
        if macd is not None:
            print(f"✅ MACD: {len(macd)}条")
        else:
            print("⚠️ MACD获取失败")
    else:
        print("❌ 麦蕊数据源不可用")
