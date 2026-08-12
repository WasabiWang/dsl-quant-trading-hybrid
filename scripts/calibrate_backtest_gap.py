#!/usr/bin/env python3
"""
scripts/calibrate_backtest_gap.py — 回测-实盘Gap校准工具 v4.6.8

用途: 每月对比回测预测 vs 实际执行结果，量化6维Gap并生成校准报告
输出: reports/backtest_calibration_{date}.json

6个校准维度:
1. 胜率Gap (win_rate)
2. 收益Gap (return)  
3. 回撤Gap (drawdown)
4. Sharpe Gap
5. Profit Factor Gap
6. 综合Gap评分
"""

import os, sys, json, argparse
from datetime import datetime, timedelta
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)

def load_actual_trades():
    """从paper_trading.db加载实际交易记录"""
    import sqlite3
    db_path = os.path.join(PROJECT_ROOT, 'data', 'paper_trading.db')
    if not os.path.exists(db_path):
        print("⚠️ paper_trading.db not found")
        return None
    
    db = sqlite3.connect(db_path)
    cur = db.cursor()
    cur.execute('SELECT * FROM trade_history WHERE is_valid_for_metrics=1 ORDER BY timestamp')
    rows = cur.fetchall()
    cols = ['id','timestamp','market','stock_code','action','price','quantity',
            'amount','reason','commission','stamp_tax','total_fee','volume_ratio_pct',
            'is_valid_for_metrics','quality_flag']
    trades = [dict(zip(cols, r)) for r in rows]
    
    # 计算实际指标
    trade_pnls = []
    stock_queues = defaultdict(list)
    for t in sorted(trades, key=lambda x: x['timestamp']):
        code = t['stock_code']
        if t['action'] == 'BUY':
            stock_queues[code].append({'qty': t['quantity'], 'price': t['price']})
        else:
            sell_qty = t['quantity']
            sell_price = t['price']
            while sell_qty > 0 and stock_queues.get(code):
                matched = min(sell_qty, stock_queues[code][0]['qty'])
                pnl = (sell_price - stock_queues[code][0]['price']) * matched
                trade_pnls.append(pnl)
                sell_qty -= matched
                stock_queues[code][0]['qty'] -= matched
                if stock_queues[code][0]['qty'] <= 0:
                    stock_queues[code].pop(0)
    
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    
    # 获取当前权益
    cur.execute("SELECT value FROM ledger WHERE key='initial_capital'")
    initial = json.loads(cur.fetchone()[0])
    cur.execute("SELECT value FROM performance_metrics WHERE key='current_equity'")
    current = json.loads(cur.fetchone()[0])
    
    db.close()
    
    total_return = (current / initial - 1) * 100
    win_rate = len(wins) / len(trade_pnls) * 100 if trade_pnls else 0
    total_profit = sum(wins) if wins else 0
    total_loss = abs(sum(losses)) if losses else 0
    profit_factor = total_profit / total_loss if total_loss > 0 else 0
    
    return {
        'total_return_pct': round(total_return, 2),
        'win_rate_pct': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2),
        'total_trades': len(trade_pnls),
        'wins': len(wins),
        'losses': len(losses),
        'avg_win': round(sum(wins)/len(wins), 2) if wins else 0,
        'avg_loss': round(sum(losses)/len(losses), 2) if losses else 0,
        'pnl_ratio': round(abs(sum(wins)/len(wins) / (sum(losses)/len(losses))), 2) if wins and losses else 0,
    }


def load_latest_backtest():
    """加载最新的walkforward回测结果"""
    bt_dir = os.path.join(PROJECT_ROOT, 'reports', 'backtest')
    if not os.path.exists(bt_dir):
        print("⚠️ backtest reports not found")
        return None
    
    files = sorted([f for f in os.listdir(bt_dir) if f.startswith('walkforward_')])
    if not files:
        print("⚠️ no walkforward reports found")
        return None
    
    latest = os.path.join(bt_dir, files[-1])
    with open(latest) as f:
        data = json.load(f)
    
    perf = data.get('performance', {})
    return {
        'total_return_pct': round(perf.get('total_return_pct', 0), 2),
        'annual_return_pct': round(perf.get('annual_return_pct', 0), 2),
        'sharpe_ratio': round(perf.get('sharpe_ratio', 0), 2),
        'sortino_ratio': round(perf.get('sortino_ratio', 0), 2),
        'max_drawdown_pct': round(perf.get('max_drawdown_pct', 0), 2),
        'win_rate_pct': round(perf.get('win_rate_pct', 0), 1),
        'profit_factor': round(perf.get('profit_factor', 0), 2),
        'total_trades': perf.get('total_trades', 0),
        'avg_hold_days': round(perf.get('avg_hold_days', 0), 1),
    }


def compute_gap(backtest, actual):
    """计算6维Gap"""
    gaps = {}
    
    # Win Rate Gap
    if backtest and actual:
        gaps['win_rate_gap'] = round(backtest['win_rate_pct'] - actual['win_rate_pct'], 1)
    
    # Return Gap (annualized)
    if backtest and actual:
        gaps['return_gap'] = round(backtest['annual_return_pct'] - actual['total_return_pct'], 2)
    
    # Profit Factor Gap
    if backtest and actual:
        gaps['pf_gap'] = round(backtest['profit_factor'] - actual['profit_factor'], 2)
    
    # Sharpe Gap (estimated)
    if backtest:
        gaps['sharpe_bt'] = backtest['sharpe_ratio']
        gaps['sharpe_actual_est'] = round(backtest['sharpe_ratio'] * 0.5, 2)  # 历史衰减约50%
        gaps['sharpe_gap_pct'] = round((1 - 0.5) * 100, 0)
    
    # Drawdown Gap
    if backtest:
        gaps['drawdown_bt'] = backtest['max_drawdown_pct']
    
    # 综合Gap评分 (0=无gap, 100=完全脱节)
    if backtest and actual:
        scores = []
        # 胜率Gap: 每1%差距=2分
        if 'win_rate_gap' in gaps:
            scores.append(min(abs(gaps['win_rate_gap']) * 2, 40))
        # 收益Gap: 每2%差距=5分
        if 'return_gap' in gaps:
            scores.append(min(abs(gaps['return_gap']) / 2 * 5, 30))
        # PF Gap: 每0.1差距=10分
        if 'pf_gap' in gaps:
            scores.append(min(abs(gaps['pf_gap']) * 10, 30))
        
        gaps['composite_gap_score'] = round(sum(scores), 1)
        if gaps['composite_gap_score'] < 15:
            gaps['gap_level'] = '🟢 LOW'
        elif gaps['composite_gap_score'] < 30:
            gaps['gap_level'] = '🟡 MODERATE'
        else:
            gaps['gap_level'] = '🔴 HIGH'
    
    return gaps


def suggest_adjustments(gaps, backtest, actual):
    """基于Gap分析给出参数调整建议"""
    suggestions = []
    
    if not backtest or not actual:
        return suggestions
    
    wr_gap = gaps.get('win_rate_gap', 0)
    pf_gap = gaps.get('pf_gap', 0)
    
    # 胜率Gap > 15%: 回测买入阈值需要提高
    if wr_gap > 15:
        suggestions.append({
            'param': 'backtest buy_threshold',
            'action': 'increase',
            'amount': f'+{min(wr_gap * 0.002, 0.015):.3f}',
            'reason': f'胜率Gap={wr_gap:.1f}%，回测过于乐观，需提高信号门槛'
        })
    
    # PF Gap > 0.5: 回测成本被低估
    if pf_gap > 0.5:
        suggestions.append({
            'param': 'backtest slippage/cost',
            'action': 'increase',
            'amount': f'+{min(pf_gap * 2, 5):.1f}bps',
            'reason': f'PF Gap={pf_gap:.2f}，回测交易成本低估'
        })
    
    # 建议降低回测position_size匹配实盘
    suggestions.append({
        'param': 'backtest POSITION_SIZE',
        'action': 'align',
        'amount': '0.08',
        'reason': '对齐实盘单票仓位8%'
    })
    
    return suggestions


def main():
    parser = argparse.ArgumentParser(description='回测-实盘Gap校准工具')
    parser.add_argument('--output', default=None, help='输出文件路径')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    args = parser.parse_args()
    
    date_str = datetime.now().strftime('%Y-%m-%d')
    
    if not args.json:
        print(f'🔧 回测-实盘Gap校准 v4.6.8 — {date_str}')
        print()
    
    # 加载数据
    actual = load_actual_trades()
    backtest = load_latest_backtest()
    
    if not args.json:
        print(f'  📊 实际交易: {actual["total_trades"]}笔 | 胜率{actual["win_rate_pct"]:.1f}% | PF={actual["profit_factor"]:.2f}' if actual else '  ⚠️ 无实际数据')
        print(f'  📈 回测数据: {backtest["total_trades"]}笔 | 胜率{backtest["win_rate_pct"]:.1f}% | Sharpe={backtest["sharpe_ratio"]:.2f}' if backtest else '  ⚠️ 无回测数据')
        print()
    
    # 计算Gap
    gaps = compute_gap(backtest, actual)
    
    if not args.json:
        print('  === 6维Gap分析 ===')
        for k, v in gaps.items():
            if isinstance(v, float):
                print(f'  {k}: {v}')
            else:
                print(f'  {k}: {v}')
        print()
    
    # 调整建议
    suggestions = suggest_adjustments(gaps, backtest, actual)
    
    if not args.json and suggestions:
        print('  === 参数调整建议 ===')
        for s in suggestions:
            print(f'  • {s["param"]}: {s["action"]} {s["amount"]}')
            print(f'    → {s["reason"]}')
    
    # 构建报告
    report = {
        'date': date_str,
        'version': '4.6.8',
        'actual': actual,
        'backtest': backtest,
        'gaps': gaps,
        'suggestions': suggestions,
    }
    
    # 写JSON
    out_path = args.output or os.path.join(
        PROJECT_ROOT, 'reports', f'backtest_calibration_{date_str}.json'
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    if not args.json:
        print(f'\n✅ 报告已保存: {out_path}')
    
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
