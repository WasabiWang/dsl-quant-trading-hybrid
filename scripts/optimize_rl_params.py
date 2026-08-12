#!/usr/bin/env python3
"""
RL 模型超参数寻优 (Optuna)

自动寻找最适合茅台交易环境的 AI 学习率和步数。
"""

import optuna
import os
import sys
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stable_baselines3 import PPO
from simulation.rl_env import StockTradingEnv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def load_data():
    local_csv = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "600519_realistic.csv")
    df = pd.read_csv(local_csv)
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)
    return df

def objective(trial):
    # 1. 定义超参数搜索空间
    lr = trial.suggest_float("lr", 1e-5, 1e-3, log=True)
    n_steps = trial.suggest_categorical("n_steps", [512, 1024, 2048])
    gamma = trial.suggest_float("gamma", 0.9, 0.9999)
    
    df = load_data()
    env = StockTradingEnv(df)
    
    try:
        # 2. 训练模型 (简化版，快速评估)
        model = PPO("MlpPolicy", env, verbose=0, learning_rate=lr, n_steps=n_steps, gamma=gamma)
        model.learn(total_timesteps=10000) # 快速试运行
        
        # 3. 评估表现
        obs, _ = env.reset()
        done = False
        while not done:
            action, _ = model.predict(obs, deterministic=True)
            obs, reward, done, truncated, info = env.step(action)
        
        # 目标是最大化净资产
        return env.net_worth
    except:
        return 0.0

def run_optuna_search():
    print("🔍 启动 Optuna 参数寻优...")
    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=10) # 尝试 10 种组合

    print("\n✅ 寻优完成！最佳超参数组合:")
    for key, value in study.best_params.items():
        print(f"  {key}: {value}")
    print(f"最佳最终资金: ¥{study.best_value:,.2f}")

if __name__ == "__main__":
    run_optuna_search()
