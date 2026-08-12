import subprocess
import json
import pandas as pd
import numpy as np

def get_data(symbol, n=1000):
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen={n}"
    try:
        res = subprocess.run(['curl', '-s', "--noproxy", "*", url], capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout.replace('null', '""'))
        df = pd.DataFrame(data)
        df['day'] = pd.to_datetime(df['day'])
        df.set_index('day', inplace=True)
        for col in ['close', 'volume', 'high', 'low']:
            df[col] = df[col].astype(float)
        return df
    except: return pd.DataFrame()

def calculate_atr(df, period=14):
    h, l, c = df['high'], df['low'], df['close']
    tr = pd.concat([h-l, abs(h-c.shift()), abs(l-c.shift())], axis=1).max(axis=1)
    return tr.rolling(period).mean()

class SniperEngineV28:
    pass # Deprecated alias

class SniperEngine(SniperEngineV28):
    """兼容性别名"""
    pass
    """
    狙击手决策引擎 v2.8：支持多市场数据源 (A股/港股)
    """
    def __init__(self, watch_list=["sz000063", "hk00700"], market_sym="sh000300", data_fetcher=None):
        self.watch_list = watch_list
        self.market_df = get_data(market_sym, 1000) # A股大盘
        # 如果是港股列表，这里可以扩展为获取恒生指数
        self.data_fetcher = data_fetcher if data_fetcher else get_data

    def _check_sniper_setup(self, symbol):
        # 使用注入的数据源
        df_s = self.data_fetcher(symbol, 1000)
        if df_s.empty or self.market_df.empty: return None
        
        # 1. 准备指标
        df_s['ma20'] = df_s['close'].rolling(20).mean()
        df_s['ma5_vol'] = df_s['volume'].rolling(5).mean()
        df_s['atr'] = calculate_atr(df_s)
        std20 = df_s['close'].rolling(20).std()
        df_s['bw'] = (4 * std20) / df_s['ma20']
        df_s['bw_th'] = df_s['bw'].rolling(20).quantile(0.2)
        
        # 2. RS 强度 (10日)
        df_s['rs_val'] = df_s['close'].pct_change(10) - self.market_df['close'].pct_change(10)
        df_s['ma60'] = df_s['close'].rolling(60).mean()

        # 3. 遍历寻找符合“缩口起爆”且 RS > 0 的时机
        common_idx = df_s.index.intersection(self.market_df.index)
        for i in range(60, len(common_idx)):
            date = common_idx[i]
            s = df_s.loc[date]
            m = self.market_df.loc[date]
            
            # 1. 计算动态 RS 门槛 (过去 60 天前 30% 分位)
            rs_history = df_s['rs_val'].iloc[i-60:i].dropna()
            if len(rs_history) < 40: continue
            dynamic_rs_threshold = rs_history.quantile(0.7)
            is_extreme_rs = s['rs_val'] > dynamic_rs_threshold
            
            # 2. MA60 趋势软化 (±2% 边界)
            s_row = df_s.iloc[i]
            ma60_val = s_row['ma60']
            if pd.isna(ma60_val): continue
            is_uptrend = s['close'] > ma60_val
            is_near_ma60 = abs(s['close'] - ma60_val) / ma60_val <= 0.02
            
            # 3. 准入逻辑：要么顺势，要么在边界内且极强
            trend_ok = is_uptrend or (is_near_ma60 and is_extreme_rs)
            
            # 4. 基础缩口起爆逻辑
            is_squeeze = s['bw'] <= s['bw_th']
            is_breakout = (s['close'] > s['ma20']) and (s['volume'] > s['ma5_vol'])
            
            if trend_ok and is_squeeze and is_breakout:
                return {"Symbol": symbol, "Price": s['close'], "RS": s['rs_val'], "Signal": "🎯 v2.8 自适应共振"}
        
        return None

    def scan_market(self):
        print("🔍 狙击手引擎 v2.6：正在扫描 RS 共振机会...")
        targets = []
        for stock in self.watch_list:
            result = self._check_sniper_setup(stock)
            if result:
                targets.append(result)
                print(f"  ✅ 发现目标: {result['Symbol']} (RS: {result['RS']*100:.2f}%)")
        return targets

if __name__ == "__main__":
    engine = SniperEngineV26(watch_list=["sz002475", "sh600519"])
    engine.scan_market()