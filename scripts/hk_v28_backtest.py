import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from data_sources.hk_fetcher import fetch_hk_data
import pandas as pd
import numpy as np

def calculate_atr(df, period=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, abs(h-c.shift()), abs(l-c.shift())], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def run_hk_v28_backtest(symbol="hk00700", n=500):
    print(f"🚀 正在对 {symbol} 进行 v2.8 港股历史回测...\n")
    df = fetch_hk_data(symbol, n)
    if df.empty: 
        print("❌ 数据获取失败")
        return

    # 1. 准备 v2.8 指标
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    df['ma5_vol'] = df['volume'].rolling(5).mean()
    df['atr'] = calculate_atr(df)
    
    # 相对强度 (RS) - 简化：此处以大盘指数为基准，港股暂以自身均线代替趋势
    # 实际生产中应传入恒生指数数据
    df['rs_val'] = df['close'].pct_change(10) 
    
    std20 = df['close'].rolling(20).std()
    df['bw'] = (4 * std20) / df['ma20']
    df['bw_th'] = df['bw'].rolling(20).quantile(0.2)

    capital, shares, buy_price, highest = 100000.0, 0, 0, 0
    trades = []
    
    for i in range(60, len(df)):
        row = df.iloc[i]
        
        # 2. 卖出逻辑：RS 动态止盈
        if shares > 0:
            highest = max(highest, row['close'])
            # k 值随 RS 变化：1.5 - 3.0
            k = 1.5 + min(max(row['rs_val'] * 10, 0), 1.5)
            stop_price = highest - k * row['atr']
            
            if row['close'] < stop_price:
                revenue = shares * row['close']
                cost = shares * buy_price
                trades.append((revenue - cost) / cost)
                capital += revenue
                shares = 0
        
        # 3. 买入逻辑：v2.8 自适应
        elif shares == 0:
            rs_hist = df['rs_val'].iloc[i-60:i].dropna()
            if len(rs_hist) < 40: continue
            dyn_rs_th = rs_hist.quantile(0.7)
            is_extreme_rs = row['rs_val'] > dyn_rs_th
            
            ma60_val = row['ma60']
            is_uptrend = row['close'] > ma60_val
            is_near_ma60 = abs(row['close'] - ma60_val) / ma60_val <= 0.02
            trend_ok = is_uptrend or (is_near_ma60 and is_extreme_rs)
            
            is_squeeze = row['bw'] <= row['bw_th']
            is_breakout = (row['close'] > row['ma20']) and (row['volume'] > row['ma5_vol'])
            
            if trend_ok and is_squeeze and is_breakout:
                shares = int(capital // (row['close'] * 100)) * 100
                if shares > 0:
                    capital -= shares * row['close']
                    buy_price = row['close']
                    highest = row['close']

    # 4. 期末清算
    if shares > 0: capital += shares * df.iloc[-1]['close']
    
    # 5. 统计结果
    total_ret = (capital - 100000) / 100000
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 1
    pl_ratio = avg_win / avg_loss if avg_loss != 0 else 0
    
    print(f"📊 {symbol} 回测总结:")
    print(f"   - 总收益率:   {total_ret*100:.2f}%")
    print(f"   - 胜率:       {(len(wins)/len(trades)*100) if trades else 0:.1f}%")
    print(f"   - 盈亏比:     {pl_ratio:.2f}")
    print(f"   - 交易次数:   {len(trades)} 次")

if __name__ == "__main__":
    run_hk_v28_backtest("hk00700")