#!/usr/bin/env python3
"""
黑天鹅预测自动验证引擎 v1.0
============================
功能:
  1. 从 prediction_calibration.json 加载待验证预测
  2. 自动拉取实际市场价格 (Sina/akshare)
  3. 对比预测 vs 实际 → 标记 correct/incorrect/partial
  4. 更新校准数据 → 调整置信度
  5. 输出验证报告

定时: 每日 22:00 (cron)
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import requests
import re
import numpy as np

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

SKILL_DIR = Path.home() / '.agents/skills/black-swan-monitor'
PREDICTION_FILE = SKILL_DIR / 'data/prediction_calibration.json'
CONFIDENCE_FILE = SKILL_DIR / 'data/confidence_calibration.json'

OUTPUT_DIR = Path.home() / '.openclaw/workspace/dsl-quant-trading-hybrid/cache/reports'
DSL_BLACKSWAN = Path.home() / '.openclaw/workspace/dsl-quant-trading-hybrid/data/black_swan_status.json'

# ============================================================================
# 价格数据获取
# ============================================================================

def get_sina_price(symbol: str) -> Optional[float]:
    """从新浪获取实时价格"""
    try:
        headers = {'Referer': 'https://finance.sina.com.cn'}
        sina_map = {
            'sh000001': 's_sh000001',  # 上证
            'sz399001': 's_sz399001',  # 深证
            'sz399006': 's_sz399006',  # 创业板
            'sh000688': 's_sh000688',  # 科创50
            'sh000300': 's_sh000300',  # 沪深300
            'gb_vix': 'gb_vix',        # VIX
        }
        code = sina_map.get(symbol, symbol)
        r = requests.get(f'https://hq.sinajs.cn/list={code}', headers=headers, timeout=10)
        parts = r.text.split('"')[1].split(',')
        if len(parts) >= 2:
            # Try current price first, then previous close
            for idx in [3, 1, 2]:
                try:
                    val = float(parts[idx])
                    if val > 0:
                        return val
                except (ValueError, IndexError):
                    continue
        return None
    except Exception as e:
        print(f'  ⚠️ {symbol}: {e}')
        return None


def get_commodity_prices() -> Dict[str, float]:
    """获取大宗商品价格"""
    prices = {}
    try:
        headers = {'Referer': 'https://finance.sina.com.cn'}
        # 黄金
        r = requests.get('https://hq.sinajs.cn/list=hf_XAU', headers=headers, timeout=10)
        parts = r.text.split('"')[1].split(',')
        if len(parts) > 1 and parts[1]:
            prices['gold'] = float(parts[1])

        # 原油
        r = requests.get('https://hq.sinajs.cn/list=hf_CL', headers=headers, timeout=10)
        parts = r.text.split('"')[1].split(',')
        if len(parts) > 1 and parts[1]:
            prices['oil'] = float(parts[1])

        # 白银
        r = requests.get('https://hq.sinajs.cn/list=hf_XAG', headers=headers, timeout=10)
        parts = r.text.split('"')[1].split(',')
        if len(parts) > 1 and parts[1]:
            prices['silver'] = float(parts[1])
    except Exception as e:
        print(f'  ⚠️ 商品价格: {e}')

    return prices


def get_index_price(index_name: str) -> Optional[float]:
    """统一指数价格获取"""
    mapping = {
        '上证指数': 'sh000001',
        '深证成指': 'sz399001',
        '创业板指': 'sz399006',
        '科创50': 'sh000688',
        '沪深300': 'sh000300',
        'VIX': 'gb_vix',
    }
    code = mapping.get(index_name, index_name)
    return get_sina_price(code)


# ============================================================================
# 预测验证逻辑
# ============================================================================

def parse_prediction_target(prediction_text: str) -> List[Dict]:
    """
    从预测文本中提取量化目标

    格式: "WTI上行至$105(1w)/$110(1m)" 或 "沪深300下行至4780(1w)/4700(1m)"
    """
    targets = []

    # 模式1: TARGET方向至$PRICE(1w)/$PRICE(1m)
    # 模式2: TARGET方向至PRICE(1w)/PRICE(1m)
    patterns = [
        # 价格目标: WTI上行至$105(1w)/$110(1m)
        r'([A-Za-z\u4e00-\u9fff0-9]+)\s*(上行|下行|上涨|下跌|sideways|up|down)\S*?\$?(\d+\.?\d*)\s*\(1w\).*?\$?(\d+\.?\d*)\s*\(1m\)',
        # 指数目标: 沪深300下行至4780(1w)/4700(1m)
        r'(沪深\d+|上证|深证|创业板|科创|S&P|纳斯达克)\s*(上行|下行|上涨|下跌)\S*?(\d+\.?\d*)\s*\(1w\).*?(\d+\.?\d*)\s*\(1m\)',
        # 简化: COMEX黄金上行至$4580(1w)
        r'([A-Za-z\u4e00-\u9fff]+)\s*(上行|下行)\S*?\$?(\d+\.?\d*)\s*\(1w\)',
    ]

    for pattern in patterns:
        matches = re.findall(pattern, prediction_text)
        for m in matches:
            target = {
                'asset': m[0].strip(),
                'direction': m[1],
                'predicted_1w': float(m[2]),
                'predicted_1m': float(m[3]) if len(m) > 3 and m[3] else None,
            }
            targets.append(target)

    return targets


def verify_prediction(pred: Dict) -> Dict:
    """
    验证单个预测

    Returns:
        {outcome, current_price, deviation, verified_targets, ...}
    """
    prediction_text = pred.get('prediction', '')
    targets = parse_prediction_target(prediction_text)

    verified_targets = []
    if not targets:
        # 如果没有可量化的目标, 标记为 insufficient_data
        return {
            'outcome': 'insufficient_data',
            'note': '无法从预测文本中提取量化目标',
            'verified_targets': []
        }

    for target in targets:
        asset = target['asset']
        predicted = target.get('predicted_1w')

        # 获取实际价格
        actual = None
        if asset in ('上证指数', '沪深300', '创业板指', '科创50', '深证成指'):
            actual = get_index_price(asset)
        elif 'WTI' in asset or 'oil' in asset.lower():
            commod = get_commodity_prices()
            actual = commod.get('oil')
        elif '黄金' in asset or 'gold' in asset.lower() or 'COMEX' in asset:
            commod = get_commodity_prices()
            actual = commod.get('gold')
        elif '白银' in asset or 'silver' in asset.lower():
            commod = get_commodity_prices()
            actual = commod.get('silver')
        elif 'S&P' in asset or '标普' in asset:
            actual = get_sina_price('gb_$inx')

        if actual is None or predicted is None:
            verified_targets.append({
                'asset': asset,
                'direction': target['direction'],
                'predicted': predicted,
                'actual': None,
                'deviation_pct': None,
                'status': 'no_data'
            })
            continue

        # 计算偏差
        deviation_pct = abs(actual - predicted) / predicted * 100 if predicted > 0 else 0

        # 判断方向
        # 简化处理: 比较预测方向和实际价格相对于预测值的关系
        if deviation_pct <= 2:
            status = 'correct'
        elif deviation_pct <= 5:
            status = 'partial'
        else:
            status = 'incorrect'

        verified_targets.append({
            'asset': asset,
            'direction': target['direction'],
            'predicted': predicted,
            'actual': actual,
            'deviation_pct': round(deviation_pct, 1),
            'status': status
        })

    # 综合评分
    statuses = [t['status'] for t in verified_targets if t['status'] != 'no_data']
    if not statuses:
        outcome = 'insufficient_data'
    elif all(s == 'correct' for s in statuses):
        outcome = 'correct'
    elif all(s in ('correct', 'partial') for s in statuses):
        outcome = 'partially_correct'
    else:
        outcome = 'incorrect'

    return {
        'outcome': outcome,
        'verified_targets': verified_targets,
        'note': f'{len(statuses)} targets verified: {", ".join(statuses)}'
    }


# ============================================================================
# 主流程
# ============================================================================

def run_auto_verification(dry_run: bool = False) -> Dict:
    """
    自动验证所有待验证预测

    Returns:
        验证报告
    """
    # 加载预测数据
    if not PREDICTION_FILE.exists():
        return {'error': 'prediction_calibration.json not found'}

    with open(PREDICTION_FILE) as f:
        pred_data = json.load(f)

    predictions = pred_data.get('predictions', [])
    pending = [p for p in predictions if p.get('outcome') in ('pending', None)
               or p.get('status') == 'pending']

    if not pending:
        return {'status': 'ok', 'message': '无待验证预测', 'verified': 0}

    print(f'📋 待验证预测: {len(pending)}')

    # 验证每个预测
    verified = []
    for pred in pending:
        print(f'  🔍 验证: {pred.get("event_id", "?")[:20]}...', end=' ')
        result = verify_prediction(pred)

        if not dry_run:
            pred['outcome'] = result['outcome']
            pred['verified_at'] = datetime.now().isoformat()
            pred['verified_targets'] = result['verified_targets']
            if 'note' in result:
                pred['verification_note'] = result['note']

        status_emoji = {'correct': '✅', 'incorrect': '❌', 'partially_correct': '⚠️',
                        'insufficient_data': '❓'}.get(result['outcome'], '?')
        print(f'{status_emoji} {result["outcome"]}')

        verified.append({
            'event_id': pred.get('event_id', ''),
            'outcome': result['outcome'],
            'targets': result['verified_targets']
        })

    # 更新统计
    if not dry_run:
        # 重新计算统计
        all_outcomes = [p.get('outcome', 'pending') for p in predictions]
        hits = sum(1 for o in all_outcomes if o == 'correct')
        partial = sum(1 for o in all_outcomes if o == 'partially_correct')
        misses = sum(1 for o in all_outcomes if o == 'incorrect')
        total_verified = hits + partial + misses

        pred_data['overall_stats'] = {
            'total_predictions': len(predictions),
            'hits': hits + partial * 0.5,
            'misses': misses,
            'accuracy_rate': round((hits + partial * 0.5) / total_verified * 100, 1) if total_verified > 0 else 0,
            'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        }

        pred_data['updated_at'] = datetime.now().strftime('%Y-%m-%d')

        # 保存
        with open(PREDICTION_FILE, 'w') as f:
            json.dump(pred_data, f, ensure_ascii=False, indent=2)
        print(f'  💾 已保存校准数据')

    # ═══════════════════════════════════════════════════════
    # 同步更新 DSL black_swan_status.json (合并写入, 不覆盖lppl字段)
    # ═══════════════════════════════════════════════════════
    try:
        commod = get_commodity_prices()
        # 读取现有状态, 保留其他模块写入的字段
        existing_bs = {}
        if DSL_BLACKSWAN.exists():
            try:
                with open(DSL_BLACKSWAN) as f:
                    existing_bs = json.load(f)
            except Exception:
                pass
        # 只更新auto_verify负责的字段, 保留lppl等字段
        existing_bs['active'] = True
        existing_bs['position_ratio'] = existing_bs.get('position_ratio', 0.35)
        existing_bs['weighted_impact'] = 0.5
        existing_bs['commodity'] = {
            'gold_price': commod.get('gold', 0),
            'gold_change_pct': 0.0,
            'oil_price': commod.get('oil', 0),
            'oil_change_pct': 0.0,
            'silver_price': commod.get('silver', 0),
            'silver_change_pct': 0.0,
        }
        existing_bs['calibration_notes'] = f'auto-verified {len(verified)} predictions on {datetime.now().strftime("%Y-%m-%d")}'
        existing_bs['last_updated'] = datetime.now().isoformat()
        existing_bs['accuracy_rate'] = pred_data.get('overall_stats', {}).get('accuracy_rate', 0)
        with open(DSL_BLACKSWAN, 'w') as f:
            json.dump(existing_bs, f, ensure_ascii=False, indent=2)
        print(f'  💾 已同步 black_swan_status.json (合并模式)')
    except Exception as e:
        print(f'  ⚠️ 同步DSL状态失败: {e}')

    # 生成报告
    report = {
        'timestamp': datetime.now().isoformat(),
        'verified_count': len(verified),
        'results': verified,
        'overall_stats': pred_data.get('overall_stats', {}),
    }

    return report


def main():
    import argparse

    # v4.5.18: ProgressTracker for Dashboard
    _tracker = None
    if _ProgressTracker:
        try:
            _tracker = _ProgressTracker("auto_verify", total_steps=3)
        except Exception:
            pass

    parser = argparse.ArgumentParser(description='黑天鹅预测自动验证')
    parser.add_argument('--dry-run', action='store_true', help='仅预览不更新')
    parser.add_argument('--json', action='store_true', help='JSON输出')
    args = parser.parse_args()

    if args.json:
        report = run_auto_verification(dry_run=args.dry_run)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print('=' * 55)
        print('  🦢 黑天鹅预测自动验证 v1.0')
        print('=' * 55)
        report = run_auto_verification(dry_run=args.dry_run)

        if 'error' in report:
            print(f'  ❌ {report["error"]}')
        else:
            print(f'\n  验证: {report["verified_count"]} 条预测')
            stats = report.get('overall_stats', {})
            print(f'  历史准确率: {stats.get("accuracy_rate", "N/A")}%')

        # 保存报告
        output_path = OUTPUT_DIR / f'auto_verify_{datetime.now().strftime("%Y%m%d_%H%M")}.json'
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f'  📁 报告: {output_path}')

    # v4.5.18: Mark complete for Dashboard
    if _tracker:
        try:
            _tracker.complete()
        except Exception:
            pass


if __name__ == '__main__':
    main()