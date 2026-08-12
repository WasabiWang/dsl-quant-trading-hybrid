#!/usr/bin/env python3
"""
黑天鹅优化系统 v4.0 - 集成运行入口
=====================================
完整链路: LPPL泡沫检测 → 多因子风险评分 → 行业机会映射 → DSL参数更新

用法:
  python3 run_v4.py                     # 完整检测
  python3 run_v4.py --quick             # 快速模式(仅LPPL + 风险评分)
  python3 run_v4.py --json              # JSON输出
  python3 run_v4.py --update-config     # 更新adaptive_params.yaml
"""

import sys, os, json, time
from pathlib import Path
from datetime import datetime

# Add current dir to path
sys.path.insert(0, str(Path(__file__).parent))

from data_adapter import get_price_data
from lppl_model import diagnose_bubble, BubbleDiagnostic
from risk_scoring import compute_risk_score, RiskScore
from sector_mapping import map_opportunities, format_opportunity_report, SectorOpportunity

# v4.5.18: ProgressTracker for Dashboard visibility
try:
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent.parent))
    from common.progress_tracker import ProgressTracker as _ProgressTracker
except Exception:
    _ProgressTracker = None

# ============================================================================
# 配置
# ============================================================================

DSL_CONFIG_PATH = Path.home() / '.openclaw/workspace/dsl-quant-trading-hybrid/config/adaptive_params.yaml'
OUTPUT_DIR = Path.home() / '.openclaw/workspace/dsl-quant-trading-hybrid/cache/reports'

LPPL_TARGETS = {
    'A股': ['上证指数', '创业板指', '科创50', '半导体'],
    '美股': ['S&P500', 'NVDA'],
}

MACRO_EVENTS_DEFAULT = ['ai_bubble_burst', 'tariff_escalation']


def run_full_pipeline(quick: bool = False) -> dict:
    """
    运行完整的黑天鹅优化检测流水线

    Returns:
        包含所有检测结果的字典
    """
    report = {
        'timestamp': datetime.now().isoformat(),
        'version': '4.0',
    }

    # ========================================================================
    # Step 1: LPPL 泡沫检测
    # ========================================================================
    print('\n' + '=' * 65)
    print('  [1/4] LPPL 泡沫检测')
    print('=' * 65)

    lppl_results = {}
    n_windows = 15 if quick else 40

    for market, targets in LPPL_TARGETS.items():
        for sym in targets:
            print(f'  🔍 {sym}...', end=' ', flush=True)
            try:
                prices, dates = get_price_data(sym, '2023-06-01')
                if not prices or len(prices) < 50:
                    print(f'数据不足({len(prices) if prices else 0})')
                    continue

                import numpy as np
                prices_arr = np.array(prices)
                dates_arr = np.array(dates)

                diag = diagnose_bubble(prices_arr, dates_arr, symbol=sym,
                                       n_windows=n_windows, verbose=False)

                status = '🔴' if diag.bubble_detected else '🟢'
                print(f'{status} strength={diag.bubble_strength:.0%} '
                      f'R2={diag.r_squared:.3f} regime={diag.regime}', end='')

                if diag.critical_date:
                    print(f' tc={diag.critical_date}', end='')
                print()

                lppl_results[sym] = {
                    'bubble_detected': diag.bubble_detected,
                    'bubble_strength': diag.bubble_strength,
                    'crash_probability': diag.crash_probability,
                    'critical_date': diag.critical_date,
                    'days_to_critical': diag.days_to_critical,
                    'regime': diag.regime,
                    'r_squared': diag.r_squared,
                    'recommendation': diag.recommendation,
                    'warnings': diag.warnings,
                    'params': diag.params.to_dict() if diag.params else None,
                    'drawdown_pct': diag.drawdown_pct,
                    'drawdown_relaxation': diag.drawdown_relaxation,
                    'current_price': diag.current_price,
                    'peak_90d_price': diag.peak_90d_price,
                }

            except Exception as e:
                print(f'❌ {e}')

    report['lppl'] = lppl_results

    # ========================================================================
    # Step 2: 多因子风险评分
    # ========================================================================
    print('\n' + '=' * 65)
    print('  [2/4] 多因子风险评分')
    print('=' * 65)

    risk_score = compute_risk_score(lppl_results, viX=None)
    print(f'  综合风险: {risk_score.overall_score:.1f}/100 [{risk_score.risk_level}]')

    for name, val in risk_score.factors.items():
        bar = '#' * int(val * 20)
        print(f'    {name:14s} [{bar:20s}] {val:.2f}')

    report['risk_score'] = {
        'overall': risk_score.overall_score,
        'level': risk_score.risk_level,
        'factors': risk_score.factors,
        'details': risk_score.details,
        'recommendations': risk_score.recommendations,
    }

    # ========================================================================
    # Step 3: 行业机会映射
    # ========================================================================
    print('\n' + '=' * 65)
    print('  [3/4] 行业机会映射')
    print('=' * 65)

    # 根据LPPL结果动态调整事件
    active_events = list(MACRO_EVENTS_DEFAULT)

    # 如果检测到A股泡沫 → 激活泡沫破裂事件
    cn_bubble = any(
        lppl_results.get(s, {}).get('bubble_detected', False)
        for s in ['上证指数', '创业板指', '科创50']
    )
    if cn_bubble:
        if 'ai_bubble_burst' not in active_events:
            active_events.append('ai_bubble_burst')

    # 中东冲突: 仅当油价LPPL检测异常或外部事件触发时才激活
    # 不再无条件加入 — 需要油价>100或特定地缘信号
    oil_lppl = lppl_results.get('S&P500', {}).get('bubble_strength', 0)
    if oil_lppl > 0.8:
        active_events.append('iran_war_escalation')

    opportunities = map_opportunities(active_events, lppl_results)
    print(f'  检测到 {len(opportunities)} 个行业机会')

    report['opportunities'] = []
    for o in opportunities:
        print(f'    {o.action:10s} {o.sector:10s} → {o.direction} (置信度{o.confidence:.0%})')
        report['opportunities'].append({
            'sector': o.sector,
            'direction': o.direction,
            'confidence': o.confidence,
            'trigger': o.trigger,
            'action': o.action,
            'stocks': o.stocks[:4],
            'reason': o.reason,
        })

    # ========================================================================
    # Step 4: 生成报告 + 建议的position_ratio
    # ========================================================================

    # 根据综合风险分计算建议仓位比例
    # 统一映射: 与 lppl_to_dsl.py 的 compute_lppl_position_ratio 对齐
    if risk_score.overall_score >= 75:
        suggested_ratio = 0.20
    elif risk_score.overall_score >= 50:
        # 线性插值: 50分→0.50, 74分→0.26
        suggested_ratio = max(0.25, 0.50 - (risk_score.overall_score - 50) * 0.01)
    elif risk_score.overall_score >= 25:
        suggested_ratio = 0.55
    else:
        suggested_ratio = 0.75

    report['suggested_position_ratio'] = suggested_ratio

    print('\n' + '=' * 65)
    print('  [4/4] 决策输出')
    print('=' * 65)
    print(f'  当前风险等级: {risk_score.risk_level}')
    print(f'  建议仓位比例: {suggested_ratio:.2f}')
    print(f'  当前黑天鹅position_ratio: 0.40')

    if suggested_ratio != 0.40:
        delta = suggested_ratio - 0.40
        direction = '降低' if delta < 0 else '提高'
        print(f'  ⚠️  建议{direction}至 {suggested_ratio:.2f} ({delta:+.2f})')

    for rec in risk_score.recommendations:
        print(f'  • {rec}')

    print()
    return report


def update_dsl_config(suggested_ratio: float, lppl_results: dict) -> bool:
    """更新 DSL adaptive_params.yaml"""
    if not DSL_CONFIG_PATH.exists():
        print(f'  ❌ 配置文件不存在: {DSL_CONFIG_PATH}')
        return False

    with open(DSL_CONFIG_PATH) as f:
        content = f.read()

    # 更新 position_ratio
    import re

    # black_swan_position_ratio
    content = re.sub(
        r'black_swan_position_ratio: [\d.]+',
        f'black_swan_position_ratio: {suggested_ratio:.2f}',
        content
    )

    # 更新黑天鹅状态摘要
    bubble_symbols = [s for s, d in lppl_results.items() if d.get('bubble_strength', 0) > 0.8]
    summary = f'LPPL泡沫检测: {len(bubble_symbols)}个标的检测到泡沫成熟信号: {", ".join(bubble_symbols)}。综合风险等级: CRITICAL'

    content = re.sub(
        r'black_swan_overall:.*',
        f'black_swan_overall: {summary}',
        content
    )

    # 备份原文件
    backup_path = DSL_CONFIG_PATH.with_suffix('.yaml.bak.v4')
    import shutil
    shutil.copy(DSL_CONFIG_PATH, backup_path)

    with open(DSL_CONFIG_PATH, 'w') as f:
        f.write(content)

    print(f'  ✅ 已更新 {DSL_CONFIG_PATH}')
    print(f'  📋 position_ratio: → {suggested_ratio:.2f}')
    print(f'  📋 备份: {backup_path}')
    return True


def main():
    import argparse

    # v4.5.18: Determine which cron invoked us (morning LPPL or mid-week full)
    _now = datetime.now()
    _task_name = "lppl_morning" if _now.hour < 12 else "lppl_full"
    _tracker = None
    if _ProgressTracker:
        try:
            _tracker = _ProgressTracker(_task_name, total_steps=4)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='黑天鹅优化系统 v4.0')
    parser.add_argument('--quick', action='store_true', help='快速模式')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    parser.add_argument('--update-config', action='store_true', help='更新DSL配置')
    parser.add_argument('--output', help='输出文件路径')
    args = parser.parse_args()

    start = time.time()

    if args.json:
        # 静默模式
        import warnings
        warnings.filterwarnings('ignore')

    report = run_full_pipeline(quick=args.quick)

    elapsed = time.time() - start
    report['elapsed_seconds'] = round(elapsed, 1)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f'\n⏱️ 总耗时: {elapsed:.1f}秒')

    # 保存报告
    if args.output or True:  # Always save
        output_path = args.output or str(
            OUTPUT_DIR / f'black_swan_v4_{datetime.now().strftime("%Y%m%d_%H%M")}.json'
        )
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f'📁 报告已保存: {output_path}')

    # 更新DSL配置
    if args.update_config:
        suggested = report.get('suggested_position_ratio', 0.40)
        update_dsl_config(suggested, report.get('lppl', {}))

    # v4.5.18: Mark progress complete for Dashboard
    if _tracker:
        try:
            _tracker.complete()
        except Exception:
            pass


if __name__ == '__main__':
    main()