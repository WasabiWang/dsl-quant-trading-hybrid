#!/usr/bin/env python3
"""DSL v4.5.1 模拟盘回测 — 使用模型池预测器 (v2)"""
import sys, os, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd, numpy as np
from core.pool_backtest import PoolBacktestEngine
from core.pool_predictor import PoolPredictor
from dsl_data_sdk_original import normalize_symbol
import akshare as ak
import warnings
warnings.filterwarnings('ignore')

# 加载股票池
pool_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config', 'stock_pool.yaml')
with open(pool_path) as f:
    pool_cfg = yaml.safe_load(f)

predictor = PoolPredictor()

test_stocks = []
for tier in ['core', 'cycle']:
    for s in pool_cfg['tiers'][tier]['stocks'][:4]:
        test_stocks.append((s['code'], s['name'], tier))

print(f"{'='*60}")
print(f"DSL v4.5.1 模型池模拟盘回测")
print(f"{'='*60}")
print(f"🧠 模型池: {len(predictor.models)}个 | 测试标的: {len(test_stocks)}只")
print(f"回测周期: 2026-04-01 至 2026-04-26")
print(f"策略: 周频调仓 + 3bp滑点 + 次日开盘价")
print()

# 加载历史数据 + 模型预测信号
stock_data = {}
stock_names = {}
for code, name, tier in test_stocks:
    try:
        sym = f"sh{code}" if code.startswith('6') else f"sz{code}"
        df = ak.stock_zh_a_daily(symbol=sym, start_date="20260301", end_date="20260427", adjust="qfq")
        if len(df) < 30: continue
        df = df.rename(columns={'开盘':'open','最高':'high','最低':'low','收盘':'close','成交量':'volume','成交额':'amount'})
        for col in ['open','high','low','close']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date').dropna(subset=['close'])
        
        symbol = normalize_symbol(f"{code}.{'SH' if code.startswith('6') else 'SZ'}")
        stock_data[symbol] = df
        stock_names[symbol] = name
        
        # 模型预测
        pred = predictor.predict(code, df, name)
        print(f"📊 {code} {name:<8s} [{tier:5s}] | {pred['signal']:4s} | "
              f"预测收益:{pred['predicted_return']:+.4f} | "
              f"置信:{pred['confidence']:.0%} | {pred['mode']}")
    except Exception as e:
        print(f"❌ {code} {name}: {e}")

if len(stock_data) < 2:
    print("\n❌ 数据不足"); sys.exit(1)

print(f"\n{'='*60}")
print("🚀 启动回测")
print(f"{'='*60}")
eng = PoolBacktestEngine(500000)
eng.load_stock_data(stock_data)
eng.set_rebalance_schedule('weekly', '2026-04-01')

def model_selection(available, prices, data, date, max_n):
    """基于模型预测评分选股"""
    scored = []
    for sym in available:
        df = data[sym]
        code = sym[2:] if sym.startswith(('sh','sz')) else sym
        pred = predictor.predict(code, df)
        scored.append((sym, pred['score']))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:max_n]]

def model_weight(selected, prices, data, date):
    """基于模型预测评分分配权重"""
    scores = {}
    for sym in selected:
        code = sym[2:] if sym.startswith(('sh','sz')) else sym
        pred = predictor.predict(code, data[sym])
        scores[sym] = max(pred['score'], 3)
    total = sum(scores.values())
    return {s: scores[s]/total for s in selected}

result = eng.run_backtest(
    model_selection, model_weight,
    max_positions=4, slippage_bps=3.0, use_next_day_open=True
)

print(f"\n{'='*60}")
print(f"📊 回测结果")
print(f"{'='*60}")
print(f"初始资金: {result['initial_capital']:,.0f}")
print(f"最终价值: {result['final_value']:,.0f}")
print(f"总收益率: {result['total_return']:.2%}")
print(f"最大回撤: {result['max_drawdown']:.2%}")
print(f"交易次数: {result['total_trades']}")
