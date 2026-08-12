#!/usr/bin/env python3
"""
下载过去5年历史数据到本地，用于训练和后续分析
数据源：baostock（免费、稳定）
"""
import os
import sys
import baostock as bs
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import time

# 路径配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = f"{PROJECT_ROOT}/data/history"
os.makedirs(DATA_DIR, exist_ok=True)

# 股票池
STOCK_POOL = [
    {"symbol": "600760", "name": "中航沈飞", "market": "sh"},
    {"symbol": "000001", "name": "平安银行", "market": "sz"},
    {"symbol": "300750", "name": "宁德时代", "market": "sz"},
    {"symbol": "002594", "name": "比亚迪", "market": "sz"},
    {"symbol": "600519", "name": "贵州茅台", "market": "sh"},
    {"symbol": "000858", "name": "五粮液", "market": "sz"},
    {"symbol": "601318", "name": "中国平安", "market": "sh"},
    {"symbol": "000063", "name": "中兴通讯", "market": "sz"},
    {"symbol": "600036", "name": "招商银行", "market": "sh"}
]

# 行业指数（用于行业因子）
SECTOR_INDICES = [
    {"symbol": "000300", "name": "沪深300", "market": "sh"},
    {"symbol": "399001", "name": "深证成指", "market": "sz"},
    {"symbol": "399006", "name": "创业板指", "market": "sz"}
]

def download_stock_history(symbol, market, years=5):
    """下载单只股票的历史数据"""
    try:
        # baostock代码格式：sh.600760 或 sz.000001
        bs_code = f"{market}.{symbol}"
        
        # 计算日期
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=years*365)).strftime("%Y-%m-%d")
        
        print(f"📥 下载{symbol}({market}) {start_date} 至 {end_date} 的历史数据...")
        
        # 登录
        lg = bs.login()
        if lg.error_code != '0':
            print(f"❌ 登录失败: {lg.error_msg}")
            return None
        
        # 查询日线数据
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,open,high,low,close,volume,turn,pctChg",  # 涨跌幅
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2"  # 前复权
        )
        
        data_list = []
        while (rs.error_code == '0') & rs.next():
            data_list.append(rs.get_row_data())
        
        if len(data_list) == 0:
            print(f"⚠️  {symbol}无数据")
            bs.logout()
            return None
        
        df = pd.DataFrame(data_list, columns=rs.fields)
        
        # 数据类型转换
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'turn', 'pctChg']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 添加技术指标
        df = add_technical_indicators(df)
        
        # 保存到CSV
        csv_path = f"{DATA_DIR}/{symbol}_{market}_history.csv"
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        
        # 登出
        bs.logout()
        
        print(f"✅  {symbol}数据下载完成，共{len(df)}条，保存到{csv_path}")
        return df
        
    except Exception as e:
        print(f"❌ 下载{symbol}失败: {e}")
        return None

def add_technical_indicators(df):
    """添加技术指标"""
    # 确保数据按日期排序
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date')
    
    # 计算均线
    df['ma5'] = df['close'].rolling(window=5).mean()
    df['ma10'] = df['close'].rolling(window=10).mean()
    df['ma20'] = df['close'].rolling(window=20).mean()
    df['ma60'] = df['close'].rolling(window=60).mean()
    
    # 计算波动率
    df['volatility_20'] = df['pctChg'].rolling(window=20).std()
    
    # 计算动量
    df['momentum_5'] = df['close'].pct_change(periods=5)
    df['momentum_10'] = df['close'].pct_change(periods=10)
    df['momentum_20'] = df['close'].pct_change(periods=20)
    
    # 计算成交量变化率
    df['volume_change'] = df['volume'].pct_change()
    
    # 计算量价背离：价格涨但成交量跌，或价格跌但成交量涨
    df['price_up'] = df['pctChg'] > 0
    df['volume_up'] = df['volume_change'] > 0
    df['volume_price_divergence'] = df['price_up'] != df['volume_up']
    
    # 填充NaN
    df = df.ffill().fillna(0)
    
    return df

def download_sector_indices():
    """下载行业指数数据"""
    try:
        lg = bs.login()
        if lg.error_code != '0':
            return
        
        for sector in SECTOR_INDICES:
            bs_code = f"{sector['market']}.{sector['symbol']}"
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=5*365)).strftime("%Y-%m-%d")
            
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,code,open,high,low,close,volume,pctChg",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="2"
            )
            
            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())
            
            if len(data_list) > 0:
                df = pd.DataFrame(data_list, columns=rs.fields)
                csv_path = f"{DATA_DIR}/sector_{sector['symbol']}_history.csv"
                df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                print(f"✅  行业指数{sector['name']}下载完成，{len(df)}条")
        
        bs.logout()
    except Exception as e:
        print(f"❌ 下载行业指数失败: {e}")

def main():
    """主函数"""
    print("🚀 开始下载过去5年历史数据...")
    print(f"📁 数据将保存到: {DATA_DIR}")
    
    total = len(STOCK_POOL)
    success = 0
    
    # 下载股票数据
    for stock in STOCK_POOL:
        df = download_stock_history(stock['symbol'], stock['market'], years=5)
        if df is not None:
            success += 1
        time.sleep(1)  # 避免请求过快
    
    # 下载行业指数数据
    download_sector_indices()
    
    print(f"\n🎉 数据下载完成！成功{success}/{total}只股票")
    print(f"👉 所有数据已保存到{DATA_DIR}，可用于训练和后续分析")

if __name__ == "__main__":
    main()