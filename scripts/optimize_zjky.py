#!/usr/bin/env python3
"""
紫金矿业 (601899) 参数调优
=======================
针对共振策略进行参数网格搜索，找到最优参数组合
"""
import os, sys, json, time, warnings
warnings.filterwarnings('ignore')

import subprocess
import numpy as np
import pandas as pd
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

INITIAL_CAPITAL = 1_000_000
START_DATE = "2021-01-01"
END_DATE = "2026-04-25"
COMMISSION = 0.00025  # v4.5.12 fix: 万2.5, 与config/constants.py统一
SLIPPAGE = 0.0005     # v4.5.12 fix: 5bps默认滑点, 与config/constants.py统一
STOP_LOSS = 0.05
SYMBOL = "601899"
NAME = "紫金矿业"

print("=" * 80)
print(f"🔧 {NAME}({SYMBOL}) 参数调优")
print(f"   初始资金: ¥{INITIAL_CAPITAL:,.0f} | 周期: {START_DATE} ~ {END_DATE}")
print("=" * 80)


# ==================== 数据获取 ====================

def fetch_sina_kline(symbol, datalen=1500):
    code = f"sh{symbol}" if symbol.startswith('6') else f"sz{symbol}"
    url = (f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&datalen={datalen}")
    try:
        res = subprocess.run(['curl', '-s', '--connect-timeout', '10', '--max-time', '15', url],
                           capture_output=True, text=True, timeout=20)
        if res.returncode != 0 or 'day' not in res.stdout:
            return None
        data = json.loads(res.stdout)
        if not data:
            return None
        df = pd.DataFrame(data)
        df['day'] = pd.to_datetime(df['day'])
        df = df.set_index('day').sort_index()
        for col in ['open', 'close', 'high', 'low', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except:
        return None


# ==================== 技术指标 ====================

def compute_indicators(df, ma_short=5, ma_long=60, rsi_period=14):
    close = df['close']
    
    # 均线
    df['ma_short'] = close.rolling(ma_short).mean()
    df['ma20'] = close.rolling(20).mean()
    df['ma_long'] = close.rolling(ma_long).mean()
    
    # RSI
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(rsi_period).mean()
    loss = (-delta.clip(upper=0)).rolling(rsi_period).mean()
    rs = gain / loss.replace(0, np.nan)
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # 布林带
    df['bb_mid'] = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    df['bb_upper'] = df['bb_mid'] + 2 * bb_std
    df['bb_lower'] = df['bb_mid'] - 2 * bb_std
    
    return df.dropna()


# ==================== 策略信号 ====================

def generate_resonance_signals(df, ma_long=60, rsi_thresh=50, use_bb=True):
    """
    共振策略信号生成，参数可调
    """
    signals = pd.Series(0, index=df.index)
    
    # 买入条件
    buy_cond = (df['close'] > df['ma_long']) & \
               (df['macd_hist'] > 0) & \
               (df['rsi'] > rsi_thresh)
    if use_bb:
        buy_cond = buy_cond & (df['close'] > df['bb_mid'])
    
    # 卖出条件
    sell_cond = (df['close'] < df['ma20']) | \
                ((df['rsi'] > 80) & (df['close'] < df['ma_short']))
    
    signals.loc[buy_cond] = 1
    signals.loc[sell_cond] = -1
    
    return signals


# ==================== 回测引擎 ====================

def run_backtest(df, signals, initial_capital=INITIAL_CAPITAL):
    capital = initial_capital
    position = 0
    cash = capital
    entry_price = 0
    entry_idx = 0
    
    daily_values = []
    trades = []
    
    closes = df['close'].values
    dates = df.index
    in_position = False
    
    signal_map = dict(zip(signals.index, signals.values))
    
    for i in range(len(df)):
        date = dates[i]
        signal = signal_map.get(date, 0)
        close = closes[i]
        
        # 止损
        if in_position and close < entry_price * (1 - STOP_LOSS):
            sell_price = close * (1 - SLIPPAGE)
            cash = position * sell_price * (1 - COMMISSION)
            profit_pct = (sell_price - entry_price) / entry_price
            trades.append({
                'entry_date': dates[entry_idx], 'exit_date': date,
                'entry_price': entry_price, 'exit_price': sell_price,
                'profit_pct': profit_pct, 'type': 'stop_loss'
            })
            position = 0
            in_position = False
        
        # 买入
        if signal == 1 and not in_position and cash > close * 100:
            buy_price = close * (1 + SLIPPAGE)
            max_shares = int(cash / (buy_price * (1 + COMMISSION)))
            position = max_shares
            cash -= position * buy_price * (1 + COMMISSION)
            entry_price = buy_price
            entry_idx = i
            in_position = True
        
        # 卖出
        elif signal == -1 and in_position:
            sell_price = close * (1 - SLIPPAGE)
            cash = position * sell_price * (1 - COMMISSION)
            profit_pct = (sell_price - entry_price) / entry_price
            trades.append({
                'entry_date': dates[entry_idx], 'exit_date': date,
                'entry_price': entry_price, 'exit_price': sell_price,
                'profit_pct': profit_pct, 'type': 'signal'
            })
            position = 0
            in_position = False
        
        portfolio_value = cash + position * close
        daily_values.append(portfolio_value)
    
    # 期末清仓
    if in_position:
        final_price = closes[-1]
        cash = position * final_price * (1 - COMMISSION)
        position = 0
    
    final_value = cash
    daily_values[-1] = final_value
    
    # 计算指标
    values = np.array(daily_values)
    returns = np.diff(values) / values[:-1]
    total_return = (values[-1] - values[0]) / values[0]
    
    n_days = len(values)
    years = n_days / 252
    annual_return = (1 + total_return) ** (1 / max(years, 0.5)) - 1
    
    rf_daily = 0.02 / 252
    excess = returns - rf_daily
    sharpe = np.sqrt(252) * np.mean(excess) / np.std(excess) if np.std(excess) > 0 else 0
    
    peak = np.maximum.accumulate(values)
    drawdowns = (values - peak) / peak
    max_dd = np.min(drawdowns)
    
    if trades:
        wins = [t for t in trades if t['profit_pct'] > 0]
        win_rate = len(wins) / len(trades)
        avg_win = np.mean([t['profit_pct'] for t in wins]) if wins else 0
        losses = [t for t in trades if t['profit_pct'] <= 0]
        avg_loss = abs(np.mean([t['profit_pct'] for t in losses])) if losses else 0
        pl_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    else:
        win_rate = 0
        pl_ratio = 0
        avg_win = 0
    
    return {
        'total_return': total_return,
        'annual_return': annual_return,
        'sharpe': sharpe,
        'max_drawdown': max_dd,
        'win_rate': win_rate,
        'profit_loss_ratio': pl_ratio,
        'total_trades': len(trades),
        'final_value': final_value,
    }


# ==================== 参数网格搜索 ====================

def grid_search(df):
    """网格搜索最优参数"""
    # 参数范围
    ma_long_range = [40, 50, 60, 80, 100]
    rsi_range = [40, 45, 50, 55, 60]
    use_bb_options = [True, False]
    
    best_result = None
    best_return = -999
    best_params = None
    
    total_combinations = len(ma_long_range) * len(rsi_range) * len(use_bb_options)
    current = 0
    
    print(f"\n🔍 开始网格搜索，共 {total_combinations} 个参数组合...")
    print(f"{'ma_long':<10} {'rsi_thresh':<12} {'use_bb':<8} {'收益':>8} {'夏普':>6} {'胜率':>6} {'交易':>4}")
    print("-" * 70)
    
    for ma_long in ma_long_range:
        for rsi_thresh in rsi_range:
            for use_bb in use_bb_options:
                current += 1
                
                # 计算指标
                df_feat = compute_indicators(df.copy(), ma_long=ma_long, rsi_period=14)
                df_feat = df_feat.dropna()
                
                if len(df_feat) < 200:
                    continue
                
                # 生成信号
                signals = generate_resonance_signals(df_feat, ma_long=ma_long, 
                                                   rsi_thresh=rsi_thresh, use_bb=use_bb)
                
                if signals.sum() == 0:
                    continue
                
                # 回测
                result = run_backtest(df_feat, signals)
                
                # 打印进度
                print(f"{ma_long:<10} {rsi_thresh:<12} {str(use_bb):<8} "
                      f"{result['total_return']:>7.1%} {result['sharpe']:>6.2f} "
                      f"{result['win_rate']:>5.1%} {result['total_trades']:>4d}")
                
                # 更新最优（综合考虑收益和夏普）
                score = result['total_return'] * 0.7 + result['sharpe'] * 0.3
                if score > best_return:
                    best_return = score
                    best_result = result
                    best_params = {
                        'ma_long': ma_long,
                        'rsi_thresh': rsi_thresh,
                        'use_bb': use_bb,
                    }
    
    return best_params, best_result


# ==================== 主流程 ====================

def main():
    # 拉取数据
    print(f"\n📥 拉取 {NAME}({SYMBOL}) 数据...")
    df = fetch_sina_kline(SYMBOL)
    if df is None or len(df) < 300:
        print(f"❌ 数据获取失败")
        return
    
    df = df[(df.index >= START_DATE) & (df.index <= END_DATE)]
    if len(df) < 200:
        print(f"❌ 日期范围内数据不足")
        return
    
    print(f"✅ 数据准备完成，共 {len(df)} 条日线")
    
    # 网格搜索
    best_params, best_result = grid_search(df)
    
    # 打印最优结果
    print("\n" + "=" * 80)
    print(f"🏆 {NAME} 最优参数组合")
    print("=" * 80)
    print(f"参数:")
    for k, v in best_params.items():
        print(f"  {k}: {v}")
    
    print(f"\n回测结果:")
    print(f"  总收益: {best_result['total_return']:.1%}")
    print(f"  年化收益: {best_result['annual_return']:.1%}")
    print(f"  夏普比率: {best_result['sharpe']:.2f}")
    print(f"  最大回撤: {best_result['max_drawdown']:.1%}")
    print(f"  胜率: {best_result['win_rate']:.1%}")
    print(f"  盈亏比: {best_result['profit_loss_ratio']:.2f}")
    print(f"  交易次数: {best_result['total_trades']}")
    print(f"  期末资金: ¥{best_result['final_value']:,.2f}")
    
    # 对比原参数
    print(f"\n📊 对比原参数 (ma_long=60, rsi_thresh=50, use_bb=True):")
    df_feat = compute_indicators(df.copy(), ma_long=60, rsi_period=14)
    df_feat = df_feat.dropna()
    signals = generate_resonance_signals(df_feat, ma_long=60, rsi_thresh=50, use_bb=True)
    orig_result = run_backtest(df_feat, signals)
    print(f"  总收益: {orig_result['total_return']:.1%}")
    print(f"  夏普比率: {orig_result['sharpe']:.2f}")
    print(f"  胜率: {orig_result['win_rate']:.1%}")
    print(f"  交易次数: {orig_result['total_trades']}")
    
    # 保存结果
    report = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'symbol': SYMBOL,
        'name': NAME,
        'best_params': best_params,
        'best_result': {k: float(v) if isinstance(v, (np.float64, float)) else v 
                       for k, v in best_result.items()},
        'original_result': {k: float(v) if isinstance(v, (np.float64, float)) else v 
                           for k, v in orig_result.items()},
    }
    
    report_path = os.path.join(PROJECT_ROOT, 'reports', 'backtest', 
                               f'optimization_{SYMBOL}_{datetime.now().strftime("%Y%m%d_%H%M")}.json')
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    
    print(f"\n💾 完整报告: {report_path}")
    
    return best_params, best_result

if __name__ == "__main__":
    main()
