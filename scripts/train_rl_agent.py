#!/usr/bin/env python3
"""
强化学习训练脚本

利用 Stable-Baselines3 (PPO 算法) 在历史数据上训练交易 Agent。
"""

import sys
import os
import logging
import pandas as pd
from stable_baselines3 import PPO
from stable_baselines3.common.env_util import make_vec_env

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data_sources.data_manager import DataManager
from simulation.rl_env import StockTradingEnv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def train_rl_agent(symbol: str = "300750.SZ", years: int = 5):
    print("=" * 60)
    print(f"🤖 开始 RL 训练 (高波动模式): {symbol}")
    print("=" * 60)

    # 1. 准备数据
    dm = DataManager()
    df = dm.get_historical_data(symbol, years=years)
    
    # 尝试加载本地高波动数据
    local_csv_map = {
        "300750.SZ": "300750_catl.csv",
        "300059.SZ": "300059_eastmoney.csv",
        "CATL": "300750_catl.csv", 
        "EASTMONEY": "300059_eastmoney.csv"
    }
    
    if df.empty or symbol in local_csv_map:
        csv_name = local_csv_map.get(symbol, "600519_realistic.csv")
        local_csv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", csv_name)
        if os.path.exists(local_csv):
            logger.info(f"加载本地高波动数据集: {csv_name}")
            df = pd.read_csv(local_csv)
            df['Date'] = pd.to_datetime(df['Date'])
            df.set_index('Date', inplace=True)
            dm._add_indicators(df)
    
    if df.empty:
        logger.error("数据不足，无法训练 RL")
        return

    # 2. 创建环境
    env = make_vec_env(lambda: StockTradingEnv(df), n_envs=1)

    # 3. 训练模型 (PPO) - 进阶版
    logger.info("正在训练进阶版 PPO 模型... (10万步训练)")
    model = PPO("MlpPolicy", env, verbose=1, learning_rate=3e-4, gamma=0.99, n_steps=2048)
    model.learn(total_timesteps=100000)
    
    # 4. 保存模型
    model_path = f"models/rl_{symbol.replace('.', '_')}.zip"
    os.makedirs("models", exist_ok=True)
    model.save(model_path)
    logger.info(f"模型已保存至: {model_path}")

if __name__ == "__main__":
    train_rl_agent("600519.SH", years=5)
