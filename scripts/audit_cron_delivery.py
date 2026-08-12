#!/usr/bin/env python3
"""
audit_cron_delivery.py — Cron 完整审计脚本 (v4.6.2)

用法:
  python3 scripts/audit_cron_delivery.py          # 全量审计并输出报告
  python3 scripts/audit_cron_delivery.py --fix     # 审计 + 自动修复偏离的任务

检查项 (v4.6.2扩展):
  1. [原有] delivery配置是否符合 config/cron_delivery_defaults.json
  2. [新增P1] cron类型检查: 禁止systemEvent payload (教训54)
  3. [新增P1] cron完整性检查: 对照Daily Rhythm检查每个时间点覆盖 (教训53)
  4. [新增P1] 关键文件名称列检查: LLM消费数据是否含名称字段 (教训61)
"""
import sys, json, subprocess
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS_FILE = PROJECT_ROOT / "config" / "cron_delivery_defaults.json"

USER_TARGET = os.getenv("FEISHU_USER_TARGET", "user:ou_xxx")

def load_defaults() -> dict:
    with open(DEFAULTS_FILE) as f:
        return json.load(f)

def get_cron_list() -> list:
    r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                       capture_output=True, text=True, timeout=15)
    data = json.loads(r.stdout)
    return data.get('jobs', [])

def audit() -> dict:
    defaults = load_defaults()
    expected = defaults.get("expected", {})
    overrides = defaults.get("overrides", {})
    default_delivery = defaults.get("default", {})
    cron_list = get_cron_list()

    results = {
        "total_active": 0,
        "compliant": 0,
        "violations": [],
        "unknown_tasks": [],
        "fixed": [],
    }

    for job in cron_list:
        if not job.get('enabled'):
            continue
        results["total_active"] += 1
        name = job.get('name', '?')
        actual = job.get('delivery', {})

        # 确定期望配置
        if name in overrides:
            expected_delivery = dict(default_delivery)
            expected_delivery.update(overrides[name])
        elif name in expected:
            expected_delivery = dict(default_delivery)
        else:
            results["unknown_tasks"].append(name)
            continue

        # 检查是否有偏差
        issues = []
        for key, expected_val in expected_delivery.items():
            actual_val = actual.get(key)
            if actual_val != expected_val:
                issues.append(f"{key}={actual_val} (期望{expected_val})")

        if issues:
            results["violations"].append({
                "name": name,
                "actual": dict(actual),
                "expected": expected_delivery,
                "issues": issues,
            })
        else:
            results["compliant"] += 1

    return results

def fix_violations(violations: list) -> list:
    """自动修复偏离的任务"""
    fixed = []
    for v in violations:
        name = v["name"]
        expected = v["expected"]
        try:
            # 通过 cron update 修复
            patch = {"delivery": expected}
            r = subprocess.run(
                ['openclaw', 'cron', 'update', '--name', name, '--json'],
                input=json.dumps(patch), capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                fixed.append(f"✅ {name}: 已修复")
            else:
                fixed.append(f"❌ {name}: 修复失败 - {r.stderr[:100]}")
        except Exception as e:
            fixed.append(f"❌ {name}: 异常 - {e}")
    return fixed


# ═══════════════════════════════════════════
# v4.6.2 新增: 3项P1审计
# ═══════════════════════════════════════════

# 检查2: systemEvent类型检查 (教训54)
def audit_cron_payload_type(cron_list: list) -> list:
    """检查是否有systemEvent payload的cron — 禁止使用 (教训54)"""
    violations = []
    for job in cron_list:
        if not job.get('enabled'):
            continue
        name = job.get('name', '?')
        payload = job.get('payload', {})
        kind = payload.get('kind', '')

        if kind == 'systemEvent':
            violations.append({
                'name': name,
                'severity': 'CRITICAL',
                'detail': (
                    'systemEvent payload — 只投递不执行! '
                    '主session休眠时命令永不处理。必须改为 agentTurn + isolated。'
                    '(教训54: 5个systemEvent cron连续5天跳票)'
                ),
            })
    return violations


# 检查3: 时间点覆盖检查 (教训53)
DAILY_RHYTHM = [
    ('09:00', '盘前数据刷新', True),
    ('09:20', '盘前决策(morning)', True),
    ('09:45', '盘中信号监控(09:45)', True),
    ('10:30', '盘中信号监控(10:30)', True),
    ('11:00', '盘中信号监控(11:00)', True),
    ('13:30', '盘中信号监控(13:30)', True),
    ('14:30', '盘中信号监控(14:30)', True),
    ('09:50', '止损监控(09:50)', True),
    ('10:35', '止损监控(10:35)', True),
    ('11:05', '止损监控(11:05)', True),
    ('13:35', '止损监控(13:35)', True),
    ('14:35', '止损监控(14:35)', True),
    ('15:10', '收盘日报', False),  # 2026-08-01 James主动取消, 不再强制
    ('15:40', '健康检查', False),
    ('16:00', '预测模型分批训练', True),
    ('17:00', '个股分批预测', True),
    ('20:00', '自我反思', False),
    ('21:00', '黑天鹅复盘', True),
    ('21:30', '盘前预案(evening)', True),
    ('22:00', '晚间质量检查', False),
    ('02:10', '系统备份', True),
    ('04:30', '备份完整性校验', False),
    ('Sat 09:00', '深圳房价预测', False),
    ('Sat 09:30', 'Alpha-Quant自迭代', False),
]


def audit_time_slot_coverage(cron_list: list) -> list:
    """检查每个Daily Rhythm时间点是否有对应cron (教训53)"""
    violations = []
    cron_times = {}
    for job in cron_list:
        if not job.get('enabled'):
            continue
        schedule = job.get('schedule', {})
        expr = schedule.get('expr', '')
        parts = expr.split()
        if len(parts) >= 2:
            hour = parts[1]
            minute = parts[0]
            time_slot = f'{hour.zfill(2)}:{minute.zfill(2)}'
            cron_times[time_slot] = job.get('name', '')

    for time_slot, task_desc, required in DAILY_RHYTHM:
        if not required or 'Sat' in time_slot:
            continue
        if time_slot not in cron_times:
            violations.append({
                'name': f'{time_slot} {task_desc}',
                'severity': 'MISSING',
                'detail': (
                    f'Daily Rhythm时间点 {time_slot} ({task_desc}) 没有对应的cron任务! '
                    '可能在cron迁移/批量修改时被遗漏。(教训53)'
                ),
            })
    return violations


# 检查4: 关键产出文件名称列检查 (教训61)
LLM_CONSUMABLE_FILES = [
    ('cache/daily_predict.json', '预测数据', ['symbol', 'name']),
    ('cache/planned_trades.json', '预案数据', ['symbol', 'name']),
]


def audit_llm_data_fields() -> list:
    """检查LLM消费的数据文件是否包含名称字段 (教训61)"""
    violations = []
    import glob as _glob

    for pattern, desc, required_fields in LLM_CONSUMABLE_FILES:
        full_pattern = str(PROJECT_ROOT / pattern)
        matches = sorted(_glob.glob(full_pattern))

        if not matches:
            violations.append({
                'name': f'{desc} ({pattern})',
                'severity': 'WARN',
                'detail': '文件不存在，无法检查名称列。可能数据链路断裂。'
            })
            continue

        latest = matches[-1]
        try:
            with open(latest) as f:
                data = json.load(f)
        except Exception:
            violations.append({
                'name': f'{desc} ({Path(latest).name})',
                'severity': 'WARN',
                'detail': 'JSON解析失败'
            })
            continue

        if isinstance(data, list) and data:
            sample = data[0]
        elif isinstance(data, dict):
            preferred = ["predictions", "trades", "planned_trades", "items", "records"]
            list_fields = [
                k for k in preferred
                if isinstance(data.get(k), list) and data[k] and isinstance(data[k][0], dict)
            ]
            if not list_fields:
                list_fields = [
                    k for k, v in data.items()
                    if isinstance(v, list) and v and isinstance(v[0], dict)
                ]
            sample = data[list_fields[0]][0] if list_fields else {}
        else:
            continue

        for field in required_fields:
            if field not in sample:
                violations.append({
                    'name': f'{desc} ({Path(latest).name})',
                    'severity': 'LLM_HALLUCINATION_RISK',
                    'detail': (
                        f'缺少字段 "{field}" — LLM接触此数据时将依赖内置知识'
                        f'猜测名称，可能产生幻觉。(教训61)'
                    ),
                })
                break

    return violations


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Cron 完整审计 (v4.6.2)")
    parser.add_argument("--fix", action="store_true", help="自动修复偏离的任务")
    parser.add_argument("--skip-llm", action="store_true", help="跳过LLM数据文件检查")
    args = parser.parse_args()

    now_str = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')
    print(f"{'='*60}")
    print(f"🔍 Cron 完整审计 (v4.6.2) — {now_str}")
    print(f"{'='*60}")

    cron_list = get_cron_list()
    total_issues = 0

    # ---- Section 1: Delivery配置审计 ----
    print(f"\n📬 1/4 Delivery配置审计")
    print(f"{'-'*40}")
    results = audit()
    print(f"   活跃任务: {results['total_active']}")
    print(f"   合规:     {results['compliant']} ✅")
    print(f"   违规:     {len(results['violations'])} ❌")
    print(f"   未知:     {len(results['unknown_tasks'])}")

    if results["violations"]:
        for v in results["violations"]:
            print(f"   ❌ {v['name']}:")
            for issue in v["issues"]:
                print(f"      - {issue}")
    if results["unknown_tasks"]:
        print(f"   ❓ 未知任务: {', '.join(results['unknown_tasks'][:5])}")
    total_issues += len(results["violations"])

    # ---- Section 2: systemEvent类型检查 ----
    print(f"\n🚫 2/4 systemEvent类型检查 (教训54)")
    print(f"{'-'*40}")
    payload_issues = audit_cron_payload_type(cron_list)
    if payload_issues:
        for p in payload_issues:
            print(f"   ❌ [{p['severity']}] {p['name']}")
            print(f"      {p['detail']}")
        total_issues += len(payload_issues)
    else:
        print(f"   ✅ 无systemEvent payload — 全部使用agentTurn")

    # ---- Section 3: 时间点覆盖检查 ----
    print(f"\n🕐 3/4 Daily Rhythm时间点覆盖 (教训53)")
    print(f"{'-'*40}")
    coverage_issues = audit_time_slot_coverage(cron_list)
    if coverage_issues:
        for c in coverage_issues:
            print(f"   🔴 [{c['severity']}] {c['name']}")
            print(f"      {c['detail']}")
        total_issues += len(coverage_issues)
    else:
        print(f"   ✅ 所有Daily Rhythm时间点均已覆盖")

    # ---- Section 4: LLM数据文件名称列 ----
    if not args.skip_llm:
        print(f"\n🤖 4/4 LLM数据文件名称列检查 (教训61)")
        print(f"{'-'*40}")
        llm_issues = audit_llm_data_fields()
        if llm_issues:
            for l in llm_issues:
                print(f"   🔴 [{l['severity']}] {l['name']}")
                print(f"      {l['detail']}")
            total_issues += len(llm_issues)
        else:
            print(f"   ✅ 关键产出文件均包含名称字段")

    # ---- Summary ----
    print(f"\n{'='*60}")
    if total_issues == 0:
        print(f"✅ 全部检查通过 — 0项问题")
    else:
        print(f"🔴 发现 {total_issues} 项问题 — 请优先修复systemEvent和MISSING项")
    print(f"{'='*60}")

    if args.fix and results["violations"]:
        print(f"\n🔧 自动修复中...")
        fixed = fix_violations(results["violations"])
        for f in fixed:
            print(f"  {f}")

    return 0 if total_issues == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
