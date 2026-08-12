import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from data_sources.hk_fetcher import fetch_hk_data
import pandas as pd
import numpy as np

# 港股核心精选池 (覆盖主要行业龙头，确保流动性与数据稳定性)
HK_WATCH_LIST = [
    "hk00700", "hk03690", "hk01810", "hk09988", "hk01024", "hk09618", "hk00981", "hk01211",
    "hk02318", "hk02359", "hk06618", "hk03606", "hk06098", "hk01928", "hk00005", "hk00939",
    "hk01299", "hk00388", "hk00941", "hk00175", "hk00285", "hk09999", "hk02015", "hk02269",
    "hk06690", "hk09961", "hk02400", "hk00003", "hk00016", "hk00267", "hk01113", "hk00144",
    "hk02020", "hk01997", "hk02331", "hk02313", "hk01810", "hk02899", "hk02600", "hk00857"
]

def calculate_atr(df, period=20):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, abs(h-c.shift()), abs(l-c.shift())], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def run_hk_market_scan():
    print("🇭🇰 DSL v2.8 港股全市场狙击扫描启动...\n")
    
    # 1. 恒指滤网
    hsi = fetch_hk_data("hkHSI", 100)
    if hsi.empty: return
    hsi_rs = hsi['close'].pct_change(10).iloc[-1]
    hsi_ok = hsi_rs > 0
    print(f"🌍 恒指环境: RS={hsi_rs*100:.2f}% | {'✅ 允许开仓' if hsi_ok else '⚠️ 系统避险'}\n")

    if not hsi_ok:
        print("💡 提示：恒指 RS 为负，系统判定为大环境避险期，已自动停止扫描。")
        return

    print("🔍 正在扫描核心成分股池 (v2.8 逻辑)...\n")
    targets = []
    
    for code in HK_WATCH_LIST:
        df = fetch_hk_data(code, 200)
        if df.empty or len(df) < 60: continue
        
        # 预处理指标
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        df['atr'] = calculate_atr(df, 20)
        df['rs_val'] = df['close'].pct_change(10) - hsi['close'].pct_change(10)
        
        std20 = df['close'].rolling(20).std()
        df['bw'] = (4 * std20) / df['ma20']
        df['bw_th'] = df['bw'].rolling(20).quantile(0.2)
        
        last = df.iloc[-1]
        rs_hist = df['rs_val'].iloc[-60:]
        if len(rs_hist) < 40: continue
        dyn_th = rs_hist.quantile(0.85)
        
        # v2.8 核心判定
        is_extreme_rs = last['rs_val'] > dyn_th
        is_squeeze = last['bw'] <= last['bw_th']
        is_breakout = last['close'] > last['ma20']
        is_uptrend = last['close'] > last['ma60']
        is_near_ma60 = abs(last['close'] - last['ma60']) / last['ma60'] <= 0.02
        trend_ok = is_uptrend or (is_near_ma60 and is_extreme_rs)
        
        if is_extreme_rs and is_squeeze and is_breakout and trend_ok:
            targets.append({
                "Code": code, 
                "Name": "港股龙头", 
                "Price": last['close'], 
                "RS": last['rs_val'],
                "ATR_Stop": last['atr'] * 1.5
            })

    if targets:
        print("🎯 发现符合 v2.8 狙击条件的标的：")
        print("-" * 50)
        print(f"{'代码':<10} {'现价':<10} {'RS强度':<10} {'初始止损距':<10}")
        print("-" * 50)
        for t in targets:
            print(f"{t['Code']:<10} {t['Price']:<10.2f} {t['RS']*100:<10.2f}% {t['ATR_Stop']:<10.2f}")
        print("-" * 50)
    else:
        print("⚠️ 当前核心池中暂无符合 v2.8 严格过滤条件的标的。")
        print("   (这说明市场目前要么处于普跌，要么龙头股正在蓄势，建议继续观望)")

if __name__ == "__main__":
    run_hk_market_scan()