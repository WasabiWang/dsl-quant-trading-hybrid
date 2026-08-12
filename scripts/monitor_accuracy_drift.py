#!/usr/bin/env python3
"""
scripts/monitor_accuracy_drift.py — 每日精度漂移监控 v4.6.8

用途: 收盘后追踪每只股票的方向预测精度变化
- 精度单日下降>5% → 预警
- 连续3日趋势向下 → 标记degraded
- 精度<45% → 移入黑名单
输出: data/accuracy_drift_alerts.jsonl
"""

import os, sys, json, argparse
from datetime import datetime, timedelta
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)


def load_current_accuracy():
    """从prediction_calibration.json加载当前精度"""
    cal_path = os.path.join(PROJECT_ROOT, 'confidence_data', 'prediction_calibration.json')
    if not os.path.exists(cal_path):
        print("⚠️ prediction_calibration.json not found")
        return {}
    
    with open(cal_path) as f:
        cal = json.load(f)
    
    stock_acc = cal.get('stock_accuracy', {})
    result = {}
    for code, info in stock_acc.items():
        accs = info.get('accuracies', [])
        if accs:
            result[code] = {
                'name': info.get('name', code),
                'mean': round(sum(accs) / len(accs), 4),
                'last': round(accs[-1], 4),
                'accuracies': [round(a, 4) for a in accs[-10:]],  # 最近10次
                'samples': len(accs),
            }
    return result


def load_historical_alerts():
    """加载历史告警记录"""
    alert_path = os.path.join(PROJECT_ROOT, 'data', 'accuracy_drift_alerts.jsonl')
    if not os.path.exists(alert_path):
        return []
    
    alerts = []
    with open(alert_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    alerts.append(json.loads(line))
                except:
                    pass
    return alerts


def detect_drift(current, historical_alerts):
    """检测精度漂移"""
    alerts = []
    date_str = datetime.now().strftime('%Y-%m-%d')
    
    # 找到最近的5个daily_record
    cal_path = os.path.join(PROJECT_ROOT, 'confidence_data', 'prediction_calibration.json')
    with open(cal_path) as f:
        cal = json.load(f)
    daily_records = cal.get('daily_records', [])
    
    for code, info in current.items():
        name = info['name']
        mean_acc = info['mean']
        last_acc = info['last']
        accs = info['accuracies']
        
        # 1. 检查绝对精度过低
        if mean_acc < 0.45:
            alerts.append({
                'date': date_str,
                'symbol': code,
                'name': name,
                'type': 'CRITICAL_LOW_ACCURACY',
                'mean_accuracy': mean_acc,
                'last_accuracy': last_acc,
                'message': f'🔴 {code} {name} 精度{mean_acc:.1%}<45%, 建议移入黑名单',
            })
            continue
        
        if mean_acc < 0.50:
            alerts.append({
                'date': date_str,
                'symbol': code,
                'name': name,
                'type': 'WARNING_LOW_ACCURACY',
                'mean_accuracy': mean_acc,
                'last_accuracy': last_acc,
                'message': f'🟡 {code} {name} 精度{mean_acc:.1%}<50%, 监控中',
            })
        
        # 2. 检测单日大幅下降
        if len(accs) >= 2:
            prev = accs[-2]
            curr = accs[-1]
            drop = prev - curr
            if drop > 0.05:  # 单日下降>5%
                alerts.append({
                    'date': date_str,
                    'symbol': code,
                    'name': name,
                    'type': 'SUDDEN_DROP',
                    'mean_accuracy': mean_acc,
                    'last_accuracy': last_acc,
                    'drop_pct': round(drop * 100, 1),
                    'message': f'⚠️ {code} {name} 精度单日下降{drop*100:.1f}% ({prev:.2%}→{curr:.2%})',
                })
        
        # 3. 检测连续下降趋势
        if len(accs) >= 3:
            recent_3 = accs[-3:]
            if recent_3[0] > recent_3[1] > recent_3[2]:
                # 连续3日下降
                total_drop = recent_3[0] - recent_3[2]
                alerts.append({
                    'date': date_str,
                    'symbol': code,
                    'name': name,
                    'type': 'DOWNWARD_TREND',
                    'mean_accuracy': mean_acc,
                    'last_accuracy': last_acc,
                    'trend': f'{recent_3[0]:.2%}→{recent_3[1]:.2%}→{recent_3[2]:.2%}',
                    'total_drop_pct': round(total_drop * 100, 1),
                    'message': f'📉 {code} {name} 连续3日精度下降 ({total_drop*100:.1f}%)',
                })
    
    return alerts


def update_degraded_stocks(alerts):
    """更新degraded_models.json"""
    deg_path = os.path.join(PROJECT_ROOT, 'confidence_data', 'degraded_models.json')
    deg_models = {}
    if os.path.exists(deg_path):
        with open(deg_path) as f:
            deg_models = json.load(f)
    
    for alert in alerts:
        if alert['type'] in ('CRITICAL_LOW_ACCURACY', 'DOWNWARD_TREND'):
            code = alert['symbol']
            if code not in deg_models:
                deg_models[code] = {
                    'symbol': code,
                    'name': alert['name'],
                    'reason': alert['type'],
                    'accuracy': alert['mean_accuracy'],
                    'first_detected': alert['date'],
                }
    
    with open(deg_path, 'w') as f:
        json.dump(deg_models, f, indent=2, ensure_ascii=False)
    
    return len(deg_models)


def main():
    parser = argparse.ArgumentParser(description='精度漂移监控')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    parser.add_argument('--alert-file', default=None, help='告警输出路径')
    args = parser.parse_args()
    
    date_str = datetime.now().strftime('%Y-%m-%d')
    
    if not args.json:
        print(f'📊 精度漂移监控 v4.6.8 — {date_str}')
        print()
    
    current = load_current_accuracy()
    if not current:
        print("⚠️ 无精度数据")
        return
    
    historical = load_historical_alerts()
    alerts = detect_drift(current, historical)
    
    if not args.json:
        # 打印各层统计
        alpha_codes = ['000858','600809','600487','600036','600519','600887','002281']
        core_codes = ['603986','301308','300223','300059','002156','300014','002594',
                      '300613','688525','601318','603893']
        bench_codes = ['688235','002261','002179','600584','600276','000063','002415',
                       '603259','000725','002466','600726']
        
        alpha_accs = [current[c]['mean'] for c in alpha_codes if c in current]
        core_accs = [current[c]['mean'] for c in core_codes if c in current]
        bench_accs = [current[c]['mean'] for c in bench_codes if c in current]
        
        print(f'  ALPHA层({len(alpha_accs)}只): avg={sum(alpha_accs)/len(alpha_accs)*100:.1f}%' if alpha_accs else '  ALPHA: no data')
        print(f'  CORE层({len(core_accs)}只): avg={sum(core_accs)/len(core_accs)*100:.1f}%' if core_accs else '  CORE: no data')
        print(f'  BENCH层({len(bench_accs)}只): avg={sum(bench_accs)/len(bench_accs)*100:.1f}%' if bench_accs else '  BENCH: no data')
        print()
        
        if alerts:
            print(f'  🚨 检测到{len(alerts)}条告警:')
            for a in alerts:
                icon = {'CRITICAL_LOW_ACCURACY': '🔴', 'WARNING_LOW_ACCURACY': '🟡',
                        'SUDDEN_DROP': '⚠️', 'DOWNWARD_TREND': '📉'}.get(a['type'], '•')
                print(f'  {icon} {a["message"]}')
        else:
            print(f'  ✅ 无精度漂移告警')
        
        # 更新degraded
        ndeg = update_degraded_stocks(alerts)
        if ndeg > 0:
            print(f'\n  📝 已更新degraded_models.json ({ndeg}只)')
    
    # 写告警到JSONL
    if alerts:
        alert_path = args.alert_file or os.path.join(PROJECT_ROOT, 'data', 'accuracy_drift_alerts.jsonl')
        with open(alert_path, 'a') as f:
            for a in alerts:
                f.write(json.dumps(a, ensure_ascii=False) + '\n')
    
    if args.json:
        result = {
            'date': date_str,
            'alerts_count': len(alerts),
            'alerts': alerts,
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
