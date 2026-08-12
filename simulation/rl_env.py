"""
强化学习交易环境 (Gym Wrapper)

让 RL Agent 在历史数据中学习
"""

import gymnasium as gym
from gymnasium import spaces
import numpy as np
import pandas as pd
from typing import Dict, Any

class StockTradingEnv(gym.Env):
    """股票交易强化学习环境"""
    
    metadata = {'render_modes': ['human']}
    
    def __init__(self, df: pd.DataFrame, initial_balance=100000):
        super().__init__()
        
        self.df = df
        self.initial_balance = initial_balance
        self.reward_range = (float('-inf'), float('inf'))
        
        # 动作空间: 0=持有, 1=买入, 2=卖出
        self.action_space = spaces.Discrete(3)
        
        # 观察空间: [余额, 持仓量, 收盘价, 成交量, SMA20, RSI, ...]
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(7,), dtype=np.float32
        )
        
        self.reset()
    
    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.balance = self.initial_balance
        self.shares_held = 0
        self.current_step = 0
        self.net_worth = self.initial_balance
        self.max_net_worth = self.initial_balance
        self.done = False
        
        return self._next_observation(), {}
    
    def _next_observation(self):
        # 确保数据不包含 NaN 或 Inf
        obs = np.array([
            self.balance,
            self.shares_held,
            self.df.iloc[self.current_step]['Close'],
            self.df.iloc[self.current_step]['Volume'],
            self.df.iloc[self.current_step]['SMA20'] if 'SMA20' in self.df.columns else 0,
            self.df.iloc[self.current_step]['RSI'] if 'RSI' in self.df.columns else 50,
            self.net_worth
        ], dtype=np.float32)
        
        # 替换可能的 NaN/Inf
        obs = np.nan_to_num(obs, nan=0.0, posinf=0.0, neginf=0.0)
        return obs
    
    def step(self, action):
        self._take_action(action)
        self.current_step += 1
        
        if self.current_step >= len(self.df) - 1:
            self.done = True
        
        current_price = self.df.iloc[self.current_step]['Close']
        self.net_worth = self.balance + self.shares_held * current_price
        
        # 计算基准表现（假设一开始就全仓买入）
        initial_shares = self.initial_balance / self.df.iloc[0]['Close']
        benchmark_worth = initial_shares * current_price
        
        # 奖励函数：超额收益 (Alpha)
        # 只有跑赢基准，奖励才是正的
        reward = (self.net_worth - benchmark_worth) / self.initial_balance
        
        # 增加“饥饿感”：如果落后于基准，额外惩罚
        if self.net_worth < benchmark_worth:
            reward -= 0.05
            
        self.max_net_worth = max(self.max_net_worth, self.net_worth)
        return self._next_observation(), float(reward), self.done, False, {}
    
    def _take_action(self, action):
        current_price = self.df.iloc[self.current_step]['Close']
        
        if action == 1:  # Buy
            max_shares = self.balance / current_price
            shares_bought = max_shares // 100 * 100  # A股/港股 100股整数倍
            if shares_bought > 0:
                self.balance -= shares_bought * current_price
                self.shares_held += shares_bought
                
        elif action == 2:  # Sell
            if self.shares_held > 0:
                self.balance += self.shares_held * current_price
                self.shares_held = 0
