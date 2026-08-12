#!/usr/bin/env python3
"""
参数寻优脚本 (Optuna)

自动寻找策略的最佳参数组合（如 SMA 周期、RSI 阈值等）。
"""

import optuna
import backtrader as bt
import pandas as pd
import logging
from simulation.backtest_engine import DSLSignalStrategy
from data_sources.data_manager import DataManager

logging.basicConfig(level=logging.INFO)

def objective(trial):
    # 1. 定义超参数搜索空间
    sma_period = trial.suggest_int('sma_period', 10, 50)
    rsi_upper = trial.suggest_int('rsi_upper', 60, 80)
    rsi_lower = trial.suggest_int('rsi_lower', 20, 40)

    # 2. 获取数据
    dm = DataManager()
    df = dm.get_historical_data("600519.SH", years=2) # 优化时用短周期加快速度
    
    # 3. 运行回测
    cerebro = bt.Cerebro()
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    # 将Optuna搜索的参数传递给策略
    cerebro.addstrategy(DSLSignalStrategy,
                        sma_period=sma_period,
                        rsi_upper=rsi_upper,
                        rsi_lower=rsi_lower)
    cerebro.broker.setcash(100000.0)
    
    try:
        results = cerebro.run()
        final_value = cerebro.broker.getvalue()
        return final_value
    except Exception as e:
        logging.warning(f"回测运行失败: {e}")
        return 0.0

def run_optimization():
    print("🔍 开始 Optuna 自动参数寻优...")
    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=20)

    print("✅ 优化完成！最佳参数:")
    print(study.best_params)
    print(f"最佳最终资金: ¥{study.best_value:,.2f}")

if __name__ == "__main__":
    run_optimization()
