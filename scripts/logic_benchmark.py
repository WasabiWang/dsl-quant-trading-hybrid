import subprocess
import json
import pandas as pd
import numpy as np

class LogicBenchmark:
    """
    狙击逻辑对比基准测试
    对比：传统均线拐头 vs 布林带缩口起爆
    """
    def __init__(self, symbol="sz000063", years=1):
        self.symbol = symbol
        self.data = self._load_data(years)

    def _load_data(self, years):
        print(f"📥 正在拉取 {self.symbol} 真实数据 ({years}年)...")
        url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={self.symbol}&scale=240&ma=5&datalen={250 * years}"
        try:
            res = subprocess.run(['curl', '-s', "--noproxy", "*", url], capture_output=True, text=True, timeout=10)
            data = json.loads(res.stdout.replace('null', '""'))
            df = pd.DataFrame(data)
            df['day'] = pd.to_datetime(df['day'])
            df.set_index('day', inplace=True)
            df['close'] = df['close'].astype(float)
            df['volume'] = df['volume'].astype(int)
            return df
        except: return pd.DataFrame()

    def run_baseline(self):
        """现状逻辑：站稳20日线 + 均线拐头"""
        print("🏃 正在运行现状逻辑 (Baseline: MA20)...")
        wins, trades = 0, 0
        for i in range(20, len(self.data)):
            price = self.data['close'].iloc[i]
            ma20_now = self.data['close'].iloc[i-20:i].mean()
            ma20_prev = self.data['close'].iloc[i-25:i-5].mean()
            
            # 信号：站稳且拐头
            if price > ma20_now and ma20_now > ma20_prev:
                # 模拟：持有5天后卖出看结果
                if i + 5 < len(self.data):
                    trades += 1
                    if self.data['close'].iloc[i+5] > price:
                        wins += 1
        return trades, wins

    def run_bb_squeeze(self):
        """优化逻辑：布林带缩口 + 突破中轨 + 放量"""
        print("🚀 正在运行布林带缩口逻辑 (BB Squeeze)...")
        wins, trades = 0, 0
        for i in range(30, len(self.data)):
            close = self.data['close'].iloc[i-20:i]
            ma20 = close.mean()
            std = close.std()
            upper = ma20 + 2 * std
            lower = ma20 - 2 * std
            
            # 条件1：缩口 (带宽处于近期低位，这里简化处理)
            bandwidth = (upper - lower) / ma20
            prev_bandwidth = (self.data['close'].iloc[i-40:i-20].mean() + 2*self.data['close'].iloc[i-40:i-20].std() - (self.data['close'].iloc[i-40:i-20].mean() - 2*self.data['close'].iloc[i-40:i-20].std())) / self.data['close'].iloc[i-40:i-20].mean()
            
            # 条件2：突破中轨 + 放量
            price = self.data['close'].iloc[i]
            vol = self.data['volume'].iloc[i]
            ma_vol = self.data['volume'].iloc[i-5:i].mean()
            
            if price > ma20 and vol > ma_vol * 1.2 and bandwidth < prev_bandwidth * 1.1:
                if i + 5 < len(self.data):
                    trades += 1
                    if self.data['close'].iloc[i+5] > price:
                        wins += 1
        return trades, wins

if __name__ == "__main__":
    bm = LogicBenchmark(symbol="sz000063", years=1)
    if not bm.data.empty:
        t1, w1 = bm.run_baseline()
        t2, w2 = bm.run_bb_squeeze()
        
        print("\n📊 逻辑对比报告 (持有5日胜率):")
        print(f"- 现状逻辑 (MA20):   交易 {t1} 次, 胜率 {(w1/t1*100) if t1 else 0:.1f}%")
        print(f"- 优化逻辑 (BB):     交易 {t2} 次, 胜率 {(w2/t2*100) if t2 else 0:.1f}%")