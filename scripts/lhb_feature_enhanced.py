#!/usr/bin/env python3
"""
增强版龙虎榜特征处理模块
"""
import pandas as pd
import numpy as np
import akshare as ak
import ssl

# 禁用SSL验证
ssl._create_default_https_context = ssl._create_unverified_context

def get_lhb_data(start_date, end_date):
    """获取龙虎榜数据"""
    try:
        lhb_df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
        return lhb_df
    except Exception as e:
        print(f"获取龙虎榜数据失败: {e}")
        return None

def add_lhb_features(df, symbol, lhb_cache=None):
    """添加龙虎榜特征及衍生指标"""
    try:
        # 获取最近2年的龙虎榜数据
        end_date = pd.Timestamp.now().strftime("%Y%m%d")
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=365*2)).strftime("%Y%m%d")
        
        # 使用缓存或者重新获取
        if lhb_cache is not None:
            lhb_df = lhb_cache
        else:
            lhb_df = get_lhb_data(start_date, end_date)
        
        if lhb_df is not None and len(lhb_df) > 0:
            print(f"✅ 获取龙虎榜数据成功，共{len(lhb_df)}条数据")
            
            # 尝试不同的日期列名（兼容不同版本的akshare）
            date_column = None
            code_column = None
            buy_column = None
            sell_column = None
            net_column = None
            turnover_column = None
            
            # 查找可能的列名（根据实际返回的列名调整）
            for col in lhb_df.columns:
                col_lower = col.lower()
                if '日期' in col or 'date' in col_lower or '时间' in col_lower or '上榜日' in col or 'trade_date' in col_lower:
                    date_column = col
                if '代码' in col or 'symbol' in col_lower or 'code' in col_lower or 'stock_code' in col_lower:
                    code_column = col
                if '买入' in col or 'buy' in col_lower or '龙虎榜买入额' in col or 'buy_amount' in col_lower:
                    buy_column = col
                if '卖出' in col or 'sell' in col_lower or '龙虎榜卖出额' in col or 'sell_amount' in col_lower:
                    sell_column = col
                if '净额' in col or 'net' in col_lower or '龙虎榜净买额' in col or 'net_amount' in col_lower:
                    net_column = col
                if '成交额' in col or 'volume' in col_lower or 'amount' in col_lower or '总成交额' in col or 'trade_amount' in col_lower:
                    turnover_column = col
            
            print(f"   识别列名: 日期={date_column}, 代码={code_column}, 买入={buy_column}, 卖出={sell_column}, 净额={net_column}, 成交额={turnover_column}")
            
            if date_column and code_column:
                # 转换日期列
                lhb_df[date_column] = pd.to_datetime(lhb_df[date_column])
                
                # 筛选该股票的龙虎榜数据
                stock_lhb = lhb_df[lhb_df[code_column].astype(str) == str(symbol)]
                
                if len(stock_lhb) > 0:
                    # 按日期聚合
                    agg_dict = {}
                    if buy_column:
                        agg_dict[buy_column] = 'sum'
                    if sell_column:
                        agg_dict[sell_column] = 'sum'
                    if net_column:
                        agg_dict[net_column] = 'sum'
                    if turnover_column:
                        agg_dict[turnover_column] = 'sum'
                    
                    # 添加计数
                    agg_dict[date_column] = 'count'
                    
                    lhb_by_date = stock_lhb.groupby(date_column).agg(agg_dict)
                    
                    # 重命名列
                    rename_dict = {}
                    if buy_column:
                        rename_dict[buy_column] = 'lhb_buy_amount'
                    if sell_column:
                        rename_dict[sell_column] = 'lhb_sell_amount'
                    if net_column:
                        rename_dict[net_column] = 'lhb_net_amount'
                    if turnover_column:
                        rename_dict[turnover_column] = 'lhb_total_turnover'
                    rename_dict[date_column] = 'lhb_count'
                    
                    lhb_by_date = lhb_by_date.rename(columns=rename_dict)
                    
                    # 合并到主数据
                    df = df.join(lhb_by_date, how='left')
                    
                    print(f"✅ 加入龙虎榜特征，共{len(stock_lhb)}条记录")
                else:
                    print(f"⚠️  股票{symbol}无龙虎榜记录")
            else:
                print(f"⚠️  无法识别必要的列名")
        else:
            print(f"⚠️  龙虎榜数据获取失败或为空")
    except Exception as e:
        print(f"⚠️  添加龙虎榜特征失败: {e}")
    
    # 填充缺失值
    lhb_cols = ['lhb_buy_amount', 'lhb_sell_amount', 'lhb_net_amount', 'lhb_count', 'lhb_total_turnover']
    for col in lhb_cols:
        if col not in df.columns:
            df[col] = 0
    
    # 计算龙虎榜衍生指标
    total_amount = (df['volume'] * df['close']).replace(0, 1)
    # 1. 净买额占当日总成交额比例
    df['lhb_net_ratio'] = df['lhb_net_amount'] / total_amount
    # 2. 买入卖出比例（资金力度）
    df['lhb_buy_sell_ratio'] = df['lhb_buy_amount'] / df['lhb_sell_amount'].replace(0, 1)
    # 3. 龙虎榜成交额占总成交额比例
    df['lhb_turnover_ratio'] = df['lhb_total_turnover'] / total_amount
    # 4. 龙虎榜出现标记（1表示当日上榜，0表示未上榜）
    df['lhb_appeared'] = (df['lhb_count'] > 0).astype(int)
    # 5. 连续上榜天数特征
    df['lhb_consecutive_days'] = df['lhb_appeared'].groupby((df['lhb_appeared'] == 0).cumsum()).cumsum()
    # 6. 龙虎榜净买额滚动7天/15天/30天总和
    df['lhb_net_7d'] = df['lhb_net_amount'].rolling(7, min_periods=1).sum()
    df['lhb_net_15d'] = df['lhb_net_amount'].rolling(15, min_periods=1).sum()
    df['lhb_net_30d'] = df['lhb_net_amount'].rolling(30, min_periods=1).sum()
    
    # 填充NaN
    df = df.fillna(0)
    
    print(f"✅ 龙虎榜衍生特征计算完成: 净买占比、买卖比、成交额占比、上榜标记、连续上榜天数、滚动净买额")
    return df

if __name__ == "__main__":
    # 测试
    test_df = pd.DataFrame({'date': pd.date_range('2023-01-01', '2024-04-20')})
    test_df = test_df.set_index('date')
    test_df['close'] = np.random.randn(len(test_df)) * 10 + 100
    test_df['volume'] = np.random.randint(10000, 1000000, len(test_df))
    
    test_df = add_lhb_features(test_df, '000001')
    print(test_df[[col for col in test_df.columns if 'lhb' in col]].head(20))
