#!/usr/bin/env python3
"""LPPL -> DSL 信号桥接器 v1.0"""
import json, sys, os, re
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PRE_MARKET = PROJECT_ROOT / 'cache' / 'pre_market'
CACHE_REPORTS = PROJECT_ROOT / 'cache' / 'reports'
CONFIG_ADAPTIVE = PROJECT_ROOT / 'config' / 'adaptive_params.yaml'
DATA_BLACKSWAN = PROJECT_ROOT / 'data' / 'black_swan_status.json'
BLACK_SWAN_DIR = PROJECT_ROOT.parent / 'memory' / 'black-swan'

def _current_bs_ratio() -> float:
    """读取 adaptive_params.yaml 当前黑天鹅仓位 (真相源, feedback_controller动态计算)"""
    try:
        import yaml as _yaml
        with open(CONFIG_ADAPTIVE) as f:
            _ap = _yaml.safe_load(f)
        _r = _ap.get('risk', {}).get('black_swan_position_ratio', 1.0)
        return float(_r) if isinstance(_r, (int, float)) else 1.0
    except Exception:
        return 1.0


def compute_lppl_position_ratio(lppl_report):
    cn_targets = ['上证指数', '创业板指', '科创50']
    max_strength, max_crash_prob, min_days = 0, 0, 999
    details = {}
    for t in cn_targets:
        d = lppl_report.get('lppl', {}).get(t, {})
        if not d: continue
        s = d.get('bubble_strength', 0)
        cp = d.get('crash_probability', 0)
        days = d.get('days_to_critical', 999) or 999
        max_strength = max(max_strength, s)
        max_crash_prob = max(max_crash_prob, cp)
        min_days = min(min_days, days)
        details[t] = {'strength': s, 'crash_prob': cp, 'days_to_critical': days,
                      'critical_date': d.get('critical_date'), 'regime': d.get('regime','')}
    risk = max_strength * max_crash_prob * 100
    if risk >= 75: ratio, urgency = 0.20, 'CRITICAL'
    elif risk >= 50: ratio, urgency = max(0.25, 0.50-(risk-50)*0.01), 'HIGH'
    elif risk >= 25: ratio, urgency = 0.55, 'ELEVATED'
    else: ratio, urgency = 0.75, 'LOW'
    if min_days < 5: ratio = max(0.20, ratio * 0.7); urgency = "CRITICAL"
    return {'lppl_risk_score': round(risk,1), 'lppl_position_ratio': round(ratio,2),
            'urgency': urgency, 'min_days_to_critical': min_days if min_days<999 else None,
            'cn_target_details': details}

def write_lppl_risk_json(lppl_result, date_str):
    CACHE_PRE_MARKET.mkdir(parents=True, exist_ok=True)
    risk_file = CACHE_PRE_MARKET / f'{date_str}_risk.json'
    existing = {}
    if risk_file.exists():
        try:
            with open(risk_file) as f: existing = json.load(f)
        except: pass
    lp = compute_lppl_position_ratio(lppl_result)
    opps = lppl_result.get('opportunities', [])
    sector_sigs = [{'sector': o['sector'], 'direction': o['direction'],
                    'confidence': o['confidence'], 'action': o['action'],
                    'trigger': o.get('trigger',''), 'reason': o.get('reason',''),
                    'stocks': o.get('stocks',[])[:4]}
                   for o in opps if o.get('action') in ('BUY','SELL') and o.get('confidence',0)>=0.6]
    summary = []
    for t, d in lp['cn_target_details'].items():
        if d['strength'] > 0.8:
            summary.append(f"{t}: s={d['strength']:.0%} cp={d['crash_prob']:.0%} tc={d.get('critical_date','N/A')}")
    risk_data = {**existing,
        'lppl': {'timestamp': datetime.now().isoformat(), 'risk_score': lp['lppl_risk_score'],
                 'position_ratio': lp['lppl_position_ratio'], 'urgency': lp['urgency'],
                 'min_days_to_critical': lp.get('min_days_to_critical'),
                 'targets': lp['cn_target_details'], 'sector_signals': sector_sigs,
                 'bubble_summary': ' | '.join(summary)},
        # v4.6.9: 修复min()单向压缩 — 与当前adaptive真相源取min, 允许回升
        # 原 min(旧值, lppl) 只降不升, LPPL缓解后 position_ratio 永久卡历史最低
        'position_ratio': min(_current_bs_ratio(), lp['lppl_position_ratio']),
        'recommended_position_ratio': min(_current_bs_ratio(), lp['lppl_position_ratio']),
        'black_swan_active': existing.get('black_swan_active', True)}
    with open(risk_file, 'w') as f: json.dump(risk_data, f, ensure_ascii=False, indent=2)
    print(f"  LPPL->DSL: {risk_file}")
    print(f"  risk={lp['lppl_risk_score']:.1f} ratio={lp['lppl_position_ratio']:.2f} urgency={lp['urgency']}")
    if lp.get('min_days_to_critical'): print(f"  min_tc={lp['min_days_to_critical']}d")
    return risk_file

def update_adaptive_params_yaml(lppl_result):
    """v4.6: LPPL写入独立lppl_position_ratio字段, 不再覆盖black_swan_position_ratio
    morning_decision.py 通过加权融合(bs*0.6 + lppl*0.4)使用
    """
    if not CONFIG_ADAPTIVE.exists(): return False
    lp = compute_lppl_position_ratio(lppl_result)
    with open(CONFIG_ADAPTIVE) as f: content = f.read()
    # 写入独立LPPL字段（不覆盖主position_ratio）
    if 'lppl_position_ratio' in content:
        content = re.sub(r'lppl_position_ratio: [\d.]+', f'lppl_position_ratio: {lp["lppl_position_ratio"]:.2f}', content)
    else:
        content = content.replace('black_swan_active:', f'lppl_position_ratio: {lp["lppl_position_ratio"]:.2f}\n  black_swan_active:')
    summary = f'LPPL泡沫检测: {lp["cn_target_details"]}'
    content = re.sub(r'black_swan_overall:.*', f'black_swan_overall: {summary}', content)
    import shutil
    shutil.copy(CONFIG_ADAPTIVE, str(CONFIG_ADAPTIVE)+'.bak.lppl')
    with open(CONFIG_ADAPTIVE, 'w') as f: f.write(content)
    print(f"  adaptive_params updated: position_ratio={lp['lppl_position_ratio']:.2f}")
    return True

def update_black_swan_status(lppl_result):
    lp = compute_lppl_position_ratio(lppl_result)
    try:
        with open(DATA_BLACKSWAN) as f: bs = json.load(f)
    except: bs = {}
    bs['lppl'] = {'risk_score': lp['lppl_risk_score'], 'position_ratio': lp['lppl_position_ratio'],
                   'urgency': lp['urgency'], 'updated': datetime.now().isoformat(),
                   'targets': lp['cn_target_details']}
    # v4.6.9: 修复min()单向压缩bug — 原逻辑 min(旧值, lppl) 只降不升,
    # 导致LPPL缓解后 status.json position_ratio 永久卡在历史最低值(如0.2),
    # 而执行层/adaptive_params已恢复(0.6) → Dashboard显示20% vs 执行55% 分裂。
    # 改为: 读取当前adaptive_params真相源, 取 min(当前黑天鹅仓位, 当前LPPL仓位)
    try:
        import yaml as _yaml
        with open(CONFIG_ADAPTIVE) as f:
            _ap = _yaml.safe_load(f)
        _bs_ratio = float(_ap.get('risk', {}).get('black_swan_position_ratio', 1.0))
    except Exception:
        _bs_ratio = 1.0
    bs['position_ratio'] = min(_bs_ratio, lp['lppl_position_ratio'])
    bs['last_updated'] = datetime.now().isoformat()
    with open(DATA_BLACKSWAN, 'w') as f: json.dump(bs, f, ensure_ascii=False, indent=2)
    print(f"  black_swan_status.json updated")
    return True

BLACK_SWAN_DIR = PROJECT_ROOT.parent / 'memory' / 'black-swan'
LPPL_DIR = PROJECT_ROOT.parent / 'memory' / 'black-swan' / 'lppl'  # v4.6: 独立路径，不与黑天鹅复盘 conflict


def update_analysis_json(lppl_result: dict, date_str: str) -> bool:
    """将 LPPL 检测结果写入 memory/black-swan/lppl/lppl-{date}.json
    v4.6 fix: 不再与黑天鹅复盘共享 analysis-{date}.json，避免 Dashboard 误判
    """
    if not LPPL_DIR.exists():
        LPPL_DIR.mkdir(parents=True, exist_ok=True)
    
    lp = compute_lppl_position_ratio(lppl_result)
    analysis_date_str = date_str[:4]+'-'+date_str[4:6]+'-'+date_str[6:8]
    analysis_path = LPPL_DIR / f'lppl-{analysis_date_str}.json'
    
    # 如果今天复盘文件已存在，融合进去；否则新建 LPPL-only 文件
    if analysis_path.exists():
        with open(analysis_path) as f:
            analysis = json.load(f)
    else:
        analysis = {}
    
    # 构建 LPPL 段
    lppl_entry = {
        'timestamp': datetime.now().isoformat(),
        'risk_score': lp['lppl_risk_score'],
        'position_ratio': lp['lppl_position_ratio'],
        'urgency': lp['urgency'],
        'min_days_to_critical': lp.get('min_days_to_critical'),
        'targets': {},
        'sector_signals': [],
        'bubble_summary': '',
    }
    
    raw_lppl = lppl_result.get('lppl', {})
    opps = lppl_result.get('opportunities', [])
    
    for name, detail in lp.get('cn_target_details', {}).items():
        raw = raw_lppl.get(name, {})
        lppl_entry['targets'][name] = {
            'strength': detail['strength'],
            'crash_probability': detail['crash_prob'],
            'days_to_critical': detail.get('days_to_critical'),
            'critical_date': detail.get('critical_date'),
            'regime': detail.get('regime'),
            'r_squared': raw.get('r_squared', 0),
            'recommendation': raw.get('recommendation', ''),
            'drawdown_pct': raw.get('drawdown_pct', 0),
            'drawdown_relaxation': raw.get('drawdown_relaxation', 0),
        }
    
    # 美股
    for name in ['S&P500', 'NVDA']:
        raw = raw_lppl.get(name, {})
        if raw:
            lppl_entry['targets'][name] = {
                'strength': raw.get('bubble_strength', 0),
                'crash_probability': raw.get('crash_probability', 0),
                'critical_date': raw.get('critical_date'),
                'days_to_critical': raw.get('days_to_critical'),
                'regime': raw.get('regime', ''),
                'r_squared': raw.get('r_squared', 0),
                'recommendation': raw.get('recommendation', ''),
                'drawdown_pct': raw.get('drawdown_pct', 0),
                'drawdown_relaxation': raw.get('drawdown_relaxation', 0),
            }
    
    sector_sigs = [
        {'sector': o.get('sector',''), 'direction': o.get('direction',''),
         'confidence': o.get('confidence',0), 'action': o.get('action',''),
         'trigger': o.get('trigger',''), 'stocks': (o.get('stocks') or [])[:4]}
        for o in opps if o.get('action') in ('BUY','SELL') and o.get('confidence',0) >= 0.6
    ]
    lppl_entry['sector_signals'] = sector_sigs
    
    summary_parts = []
    for t, d in lp['cn_target_details'].items():
        if d['strength'] > 0.5:
            tc = d.get('critical_date','N/A')
            summary_parts.append(f"{t}: strength={d['strength']:.0%} cp={d['crash_prob']:.0%} tc={tc}")
    lppl_entry['bubble_summary'] = ' | '.join(summary_parts)
    lppl_entry['overall_risk'] = {
        'level': lppl_result.get('risk_score',{}).get('level',''),
        'overall': lppl_result.get('risk_score',{}).get('overall',0),
    }
    
    # 写入 analysis JSON
    analysis['lppl'] = lppl_entry
    if not analysis.get('analysis_date'):
        analysis['analysis_date'] = date_str[:4]+'-'+date_str[4:6]+'-'+date_str[6:8]
    
    # 同时更新 risk_matrix 中的 position_ratio 信息
    rm = analysis.setdefault('risk_matrix', {})
    rm['lppl_position_ratio'] = lp['lppl_position_ratio']
    rm['lppl_risk_score'] = lp['lppl_risk_score']
    rm['lppl_urgency'] = lp['urgency']
    
    with open(analysis_path, 'w') as f:
        json.dump(analysis, f, ensure_ascii=False, indent=2)
    print(f'  analysis JSON updated: {analysis_path}')
    return True


def _load_lppl_report(path) -> dict:
    """v4.7.0 P1-3: 健壮加载LPPL报告 — 兼容纯JSON与混合日志的 .json_raw
    策略: 先试直接json.load; 失败则取首个'{'到末尾'}'之间的子串再parse
    修复: cron命令链中python -c解析步骤依赖"恰好等于{的行"脆弱, 导致8/25链路断裂
    """
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find('{')
    end = raw.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except Exception as e:
            raise ValueError(f"LPPL报告JSON解析失败: {e}")
    raise ValueError("LPPL报告中未找到JSON对象")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--lppl-file', help='LPPL JSON report path')
    p.add_argument('--date', default=datetime.now().strftime('%Y%m%d'))
    p.add_argument('--update-config', action='store_true', help='Update adaptive_params.yaml')
    p.add_argument('--update-bs', action='store_true', help='Update black_swan_status.json')
    p.add_argument('--update-analysis', action='store_true', help='Update memory/black-swan/analysis JSON')
    args = p.parse_args()

    lppl_path = args.lppl_file or str(CACHE_REPORTS / f'lppl_daily_{args.date}.json')
    if not Path(lppl_path).exists():
        print(f'ERROR: LPPL report not found: {lppl_path}')
        sys.exit(1)
    report = _load_lppl_report(lppl_path)
    print(f'LPPL->DSL Bridge v1.1 (robust loader)')
    print(f'  Report: {lppl_path}')
    write_lppl_risk_json(report, args.date)
    if args.update_config: update_adaptive_params_yaml(report)
    if args.update_bs: update_black_swan_status(report)
    if args.update_analysis: update_analysis_json(report, args.date)
    print('DONE')

if __name__ == '__main__': main()
