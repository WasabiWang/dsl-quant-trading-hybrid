import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from data_sources.hk_fetcher import fetch_hk_data
import pandas as pd
import numpy as np

def run_hk_morning_plan():
    print("🇭🇰 DSL 港股盘前交易预案 (模拟运行)\n")
    print("="*40)
    
    # 1. 获取恒生指数状态 (软滤网)
    hsi = fetch_hk_data("hkHSI", 100)
    if hsi.empty:
        print("❌ 恒指数据获取失败，建议检查网络。")
        return

    hsi['rs_market'] = hsi['close'].pct_change(10)
    current_hsi_rs = hsi['rs_market'].iloc[-1]
    market_status = "✅ 积极 (RS>0)" if current_hsi_rs > 0 else "⚠️ 谨慎 (RS<=0)"
    
    print(f"🌍 恒指环境 (HSI):")
    print(f"   - 最新收盘: {hsi['close'].iloc[-1]:.2f}")
    print(f"   - 10日相对强度: {current_hsi_rs*100:.2f}%")
    print(f"   - 系统判定: {market_status}\n")

    # 2. 扫描核心标的
    targets = {
        "腾讯控股": "hk00700",
        "美团-W": "hk03690",
        "小米集团": "hk01810"
    }
    
    print("🎯 核心标的狙击扫描 (v2.8-HK 逻辑):")
    for name, code in targets.items():
        df = fetch_hk_data(code, 200)
        if df.empty: continue
        
        # 计算 v2.8 指标
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        df['rs_val'] = df['close'].pct_change(10) - hsi['close'].pct_change(10)
        std20 = df['close'].rolling(20).std()
        df['bw'] = (4 * std20) / df['ma20']
        df['bw_th'] = df['bw'].rolling(20).quantile(0.2)
        
        last = df.iloc[-1]
        rs_hist = df['rs_val'].iloc[-60:]
        dyn_th = rs_hist.quantile(0.85)
        
        # 逻辑判定
        is_squeeze = last['bw'] <= last['bw_th']
        is_breakout = last['close'] > last['ma20']
        is_strong_rs = last['rs_val'] > dyn_th
        is_hsi_ok = current_hsi_rs > 0
        
        status = "观望"
        if is_hsi_ok and is_strong_rs and is_squeeze and is_breakout:
            status = "🎯 建议买入 (狙击点)"
        elif is_hsi_ok and is_strong_rs:
            status = "👀 重点关注 (待突破)"
            
        print(f"- {name} ({code}):")
        print(f"  现价: {last['close']:.2f} | RS: {last['rs_val']*100:.2f}% | 状态: {status}")

    print("\n" + "="*40)
    print("📅 预案总结: 请根据盘中 09:30 的实际成交量确认突破有效性。")

if __name__ == "__main__":
    run_hk_morning_plan()