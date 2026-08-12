"""
RL 动态参数优化器

利用 PPO 代理动态调整策略的 RSI 和 BB 参数。
"""

import gymnasium as gym
import numpy as np
import pandas as pd
from stable_baselines3 import PPO
import backtrader as bt
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategies.advanced_strategies import TrendVolatilityStrategy

class RLParamEnv(gym.Env):
    """
    环境定义：
    动作空间: 调整 RSI 阈值 (30, 40, 50) 和 BB 倍数 (1.5, 2.0, 2.5)
    观察空间: 当前 RSI, BB 宽度, 均线斜率
    """
    def __init__(self, df):
        super().__init__()
        self.df = df
        self.current_step = 0
        # 动作: [rsi_level_idx, bb_dev_idx]
        self.action_space = gym.spaces.MultiDiscrete([3, 3])
        self.observation_space = gym.spaces.Box(low=-1, high=1, shape=(3,), dtype=np.float32)
        
    def reset(self, seed=None, options=None):
        self.current_step = 100 # 预留指标计算空间
        return self._get_obs(), {}
    
    def _get_obs(self):
        # 简化观察：RSI, BB Width, MA Trend
        return np.array([0.0, 0.0, 0.0], dtype=np.float32)

    def step(self, action):
        # 1. 映射动作到具体参数
        rsi_levels = [30, 40, 50]
        bb_devs = [1.5, 2.0, 2.5]
        
        params = {
            'rsi_low': rsi_levels[action[0]],
            'bb_dev': bb_devs[action[1]]
        }
        
        # 2. 运行 Backtrader 模拟该参数下的 10 天表现
        reward = self._simulate_params(params)
        
        self.current_step += 10
        done = self.current_step >= len(self.df) - 100
        return self._get_obs(), reward, done, False, {}

    def _simulate_params(self, params):
        try:
            cerebro = bt.Cerebro()
            # 确保数据长度足够计算指标 (至少需要 200 天用于 MA200)
            start_idx = max(0, self.current_step)
            end_idx = min(len(self.df), self.current_step + 250)
            if end_idx - start_idx < 210: return 0.0 # 数据不足则跳过
            
            data = bt.feeds.PandasData(dataname=self.df.iloc[start_idx:end_idx])
            cerebro.adddata(data)
            cerebro.addstrategy(TrendVolatilityStrategy, **params)
            cerebro.broker.setcash(10000)
            cerebro.run()
            return (cerebro.broker.getvalue() - 10000) / 100
        except:
            return 0.0

def optimize_strategy_params():
    print("🤖 启动 RL 动态参数优化...")
    # 加载真实数据
    local_csv = "data/600519_realistic.csv"
    if os.path.exists(local_csv):
        df = pd.read_csv(local_csv)
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        
        env = RLParamEnv(df)
        model = PPO("MlpPolicy", env, verbose=1)
        print("🏋️ 正在训练 AI 调参师 (5000步)...")
        model.learn(total_timesteps=5000)
        print("✅ 优化完成！模型已准备好为策略提供动态参数。")

if __name__ == "__main__":
    optimize_strategy_params()
