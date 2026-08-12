#!/usr/bin/env python3
"""本地历史数据加载器 - 从Desktop/stockday_wd加载10年A股数据
支持两种数据源:
1. stockday_wd: 按个股CSV存储，2016-2025，含基本面数据
2. stock_daily CSV: 聚合格式，5236只标的
"""
import os
import glob
import pandas as pd
import numpy as np
from pathlib import Path
from typing import Optional, List, Dict

# 数据源路径
STOCKDAY_DIR = os.path.expanduser("~/Desktop/stockday_wd/stockday_wd")
AGGREGATE_CSV = os.path.expanduser("~/Desktop/stock_daily_202604221637/stock_daily_202604221637.csv")


def list_available_symbols() -> List[str]:
    """列出所有可用标的"""
    if not os.path.exists(STOCKDAY_DIR):
        return []
    files = glob.glob(os.path.join(STOCKDAY_DIR, "*.csv"))
    symbols = [os.path.basename(f).replace('.csv', '') for f in files]
    return sorted(symbols)


def load_stock_data(symbol: str, start_date: str = None, end_date: str = None) -> Optional[pd.DataFrame]:
    """加载单只股票历史数据
    
    Args:
        symbol: 股票代码，如 '600519.SH', '000001.SZ'，也支持纯数字 '600519'
        start_date: 起始日期 'YYYY-MM-DD'，默认2016-01-01
        end_date: 结束日期 'YYYY-MM-DD'，默认今天
    
    Returns:
        DataFrame with columns: date, open, high, low, close, volume, amount, 
        pe_ttm, turn, mkt_cap, dividend_yield 等
    """
    if start_date is None:
        start_date = '2016-01-01'
    if end_date is None:
        end_date = pd.Timestamp.now().strftime('%Y-%m-%d')
    
    # 标准化代码格式
    sym = symbol.strip().upper()
    if '.' not in sym:
        # 纯数字，自动判断市场
        if sym.startswith(('6', '9')):
            sym = f"{sym}.SH"
        elif sym.startswith(('0', '3')):
            sym = f"{sym}.SZ"
        elif sym.startswith(('4', '8')):
            sym = f"{sym}.BJ"
    
    # 从stockday_wd加载
    csv_path = os.path.join(STOCKDAY_DIR, f"{sym}.csv")
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path, index_col=0, parse_dates=True)
            # 标准化列名
            col_map = {
                'OPEN': 'open', 'HIGH': 'high', 'LOW': 'low', 'CLOSE': 'close',
                'VOLUME': 'volume', 'AMT': 'amount',
                'PE_TTM': 'pe_ttm', 'TURN': 'turnover_rate',
                'MKT_CAP': 'market_cap', 'VAL_DIVIDENDYIELD3': 'dividend_yield',
                'YOY_OR': 'yoy_revenue', 'YOYOP': 'yoy_profit',
                'TOT_ASSETS': 'total_assets', 'TOT_LIAB': 'total_liabilities',
                'OPERATECASHFLOW_TTM1': 'ocf_ttm',
                'SHARE_PLEDGEDA_PCT': 'pledge_ratio',
                'RATING_AVG': 'analyst_rating',
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            
            # 过滤日期
            df = df.loc[start_date:end_date]
            
            # 去除无效行
            df = df.dropna(subset=['close'])
            
            if len(df) > 0:
                df.index.name = 'date'
                return df
        except Exception as e:
            print(f"❌ 加载 {sym} 失败: {e}")
    
    return None


def load_multi_stocks(symbols: List[str], start_date: str = '2016-01-01', 
                      end_date: str = None) -> Dict[str, pd.DataFrame]:
    """批量加载多只股票数据"""
    results = {}
    for sym in symbols:
        df = load_stock_data(sym, start_date, end_date)
        if df is not None and len(df) > 0:
            results[sym] = df
    return results


def get_data_summary() -> Dict:
    """获取数据概要"""
    symbols = list_available_symbols()
    total_size = 0
    date_ranges = []
    
    # 抽样检查时间范围
    sample_syms = ['600519.SH', '000001.SZ', '601318.SH', '000858.SZ', '600036.SH']
    for sym in sample_syms:
        df = load_stock_data(sym)
        if df is not None:
            date_ranges.append({
                'symbol': sym,
                'start': df.index.min().strftime('%Y-%m-%d'),
                'end': df.index.max().strftime('%Y-%m-%d'),
                'rows': len(df)
            })
    
    # 总数据量
    if os.path.exists(STOCKDAY_DIR):
        for f in glob.glob(os.path.join(STOCKDAY_DIR, "*.csv")):
            total_size += os.path.getsize(f)
    
    return {
        'total_symbols': len(symbols),
        'total_size_mb': round(total_size / 1024 / 1024, 0),
        'sample_date_ranges': date_ranges,
        'data_source': 'stockday_wd (Desktop)',
        'columns': 'open,high,low,close,volume,amount,pe_ttm,turnover_rate,market_cap,dividend_yield,yoy_revenue,yoy_profit,ocf_ttm,total_assets,total_liabilities,pledge_ratio,analyst_rating'
    }


if __name__ == "__main__":
    print("=" * 60)
    print("📊 本地A股历史数据概要")
    print("=" * 60)
    
    summary = get_data_summary()
    print(f"📍 数据路径: {STOCKDAY_DIR}")
    print(f"📊 标的数量: {summary['total_symbols']}")
    print(f"💾 数据总量: {summary['total_size_mb']} MB")
    print(f"📋 可用字段: {summary['columns']}")
    print()
    
    print("📅 时间范围抽样:")
    for item in summary['sample_date_ranges']:
        print(f"  {item['symbol']}: {item['start']} ~ {item['end']} ({item['rows']}行)")
    
    print()
    # 测试加载600519
    df = load_stock_data('600519.SH')
    if df is not None:
        print(f"✅ 600519.SH 贵州茅台 加载成功: {len(df)}行")
        print(f"   列: {list(df.columns)}")
        print(f"   最新5天:")
        print(df[['close', 'volume', 'pe_ttm', 'turnover_rate']].tail(5).to_string())
