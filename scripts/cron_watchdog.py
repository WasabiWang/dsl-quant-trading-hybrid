#!/usr/bin/env python3
"""
DSL v4.5.13 Cron Watchdog — 监测关键cron任务是否跳票，跳票则补执行
用途: Gateway cron引擎在高event loop延迟下会静默丢失tick，此脚本作为兜底
执行方式: 每15分钟运行 cron: */15 * * * *
"""
import os, sys, json, subprocess
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"
PROGRESS_DIR = CACHE_DIR / "progress"

# 关键任务监控表
# cron: cron表达式  grace_minutes: 应执行后多久无记录视为跳票
CRITICAL_JOBS = [
    {"name": "batch_train",     "label": "预测模型分批训练(16:00)",    "cron": "0 16 * * 0-4",  "timeout": 2700,
     "script": "cd ~/.openclaw/workspace/dsl-quant-trading-hybrid && .venv/bin/python3 scripts/batch_train.py"},
    {"name": "batch_predict",   "label": "个股分批预测(17:00)",       "cron": "0 17 * * 0-4",  "timeout": 900,
     "script": "cd ~/.openclaw/workspace/dsl-quant-trading-hybrid && .venv/bin/python3 scripts/batch_predict.py"},
    {"name": "pre_market_plan", "label": "A股盘前交易预案(21:30)",     "cron": "30 21 * * 0-4", "timeout": 30,
     "script": "cd ~/.openclaw/workspace/dsl-quant-trading-hybrid && .venv/bin/python3 scripts/morning_decision.py --mode evening"},
    {"name": "pre_market_decision", "label": "A股盘前决策(09:20)",    "cron": "20 9 * * 1-5",  "timeout": 30,
     "script": "cd ~/.openclaw/workspace/dsl-quant-trading-hybrid && .venv/bin/python3 scripts/morning_decision.py --market a --mode morning"},

    {"name": "dsl_backup",      "label": "DSL系统备份(02:10)",         "cron": "10 2 * * *",   "timeout": 120,
     "script": "bash ~/.openclaw/workspace/dsl-quant-trading-hybrid/scripts/backup.sh"},
]

def _get_hour_min(expr):
    parts = expr.strip().split()
    if len(parts) >= 3:
        return int(parts[1]), int(parts[0])
    return None, None

def _dow_match(expr, today_dow):
    """Check cron day-of-week matches today (0=Sun)"""
    parts = expr.strip().split()
    if len(parts) >= 5:
        dow = parts[4]
        if dow == '*':
            return True
        if '-' in dow:
            lo, hi = dow.split('-')
            return int(lo) <= today_dow <= int(hi)
        if ',' in dow:
            return str(today_dow) in dow.split(',')
        return str(today_dow) == dow
    return True

WATCHDOG_STATE_FILE = CACHE_DIR / "watchdog_state.json"

def _last_backfill_attempt(task_name):
    """Read last backfill attempt timestamp for dedup."""
    if not WATCHDOG_STATE_FILE.exists():
        return None
    try:
        state = json.loads(WATCHDOG_STATE_FILE.read_text())
        ts = state.get(task_name)
        return datetime.fromisoformat(ts) if ts else None
    except Exception:
        return None

def _record_backfill_attempt(task_name):
    """Record backfill attempt timestamp for dedup."""
    state = {}
    if WATCHDOG_STATE_FILE.exists():
        try:
            state = json.loads(WATCHDOG_STATE_FILE.read_text())
        except Exception:
            pass
    state[task_name] = datetime.now().isoformat()
    WATCHDOG_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    WATCHDOG_STATE_FILE.write_text(json.dumps(state))

def _latest_progress(task_name):
    if not PROGRESS_DIR.exists():
        return None, None
    latest, t = None, None
    for f in sorted(PROGRESS_DIR.iterdir(), reverse=True):
        if f.suffix != '.json':
            continue
        if task_name not in f.name:
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if t is None or mtime > t:
                latest, t = f, mtime
        except OSError:
            continue
    return latest, t

def audit_delivery():
    """审计所有活跃cron的delivery配置是否符合模板"""
    try:
        import subprocess, json
        r = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(r.stdout)
        jobs = data.get('jobs', [])
        issues = []
        for j in jobs:
            if not j.get('enabled'):
                continue
            # 跳过Watchdog自身——它使用delivery=none是故意设计，避免每15分钟刷屏
            if j['name'] == 'Cron Watchdog':
                continue
            d = j.get('delivery', {})
            mode = d.get('mode', 'none')
            to = d.get('to')
            if mode == 'none':
                issues.append(f"delivery=none: {j['name']}")
            elif mode == 'announce' and not to:
                issues.append(f"announce但to=null: {j['name']}")
        if issues:
            print(f"⚠️ 发现 {len(issues)} 个delivery异常:")
            for i in issues:
                print(f"  {i}")
            return issues
        return []
    except Exception as e:
        print(f"⚠️ delivery审计异常: {e}")
        return []


def check_and_backfill():
    now = datetime.now()
    cron_dow = (now.weekday() + 1) % 7  # Mon=1, Sun=0

    backfilled = []
    for job in CRITICAL_JOBS:
        h, m = _get_hour_min(job["cron"])
        if h is None:
            continue
        if not _dow_match(job["cron"], cron_dow):
            continue

        expected = now.replace(hour=h, minute=m, second=0, microsecond=0)
        # 还没到时间
        if now < expected:
            continue
        # 还在宽限期内
        if now < expected + timedelta(minutes=job.get("grace_minutes", 15)):
            continue

        latest_file, latest_time = _latest_progress(job["name"])
        if latest_time and latest_time >= expected:
            continue  # 已执行 (有progress文件)

        # v4.5.18: Dedup check — skip if we already tried backfill in last 2h
        last_attempt = _last_backfill_attempt(job["name"])
        if last_attempt and (now - last_attempt).total_seconds() < 7200:
            continue  # 2h内已尝试过补执行，跳过

        print(f"⚠️  [{now.strftime('%H:%M')}] {job['label']} 跳票! 最后:{latest_time.strftime('%H:%M') if latest_time else '无'}")
        try:
            _record_backfill_attempt(job["name"])  # v4.5.18: record attempt for dedup
            r = subprocess.run(job["script"], shell=True, capture_output=True,
                             text=True, timeout=job["timeout"])
            ok = r.returncode == 0
            preview = (r.stdout or r.stderr or "")[:100]
            print(f"   → {'✅成功' if ok else '❌失败'} ({len(r.stdout or '')+len(r.stderr or '')}B)")
            backfilled.append({"label": job["label"], "status": "✅" if ok else "❌", "preview": preview})
        except subprocess.TimeoutExpired:
            print(f"   → ⏰超时({job['timeout']}s)")
            backfilled.append({"label": job["label"], "status": "⏰超时"})
        except Exception as e:
            print(f"   → ❌{e}")
            backfilled.append({"label": job["label"], "status": f"❌{str(e)[:60]}"})

    if backfilled:
        print(f"\n📋 补执行 ({len(backfilled)}项):")
        for b in backfilled:
            print(f"  {b['label']}: {b['status']}")
    else:
        print(f"✅ [{now.strftime('%H:%M')}] 全部正常")

def audit_mapping_completeness() -> list:
    """v4.6.x: 审计 _CRON_NAME_TO_LOGICAL 映射是否缺少实际cron任务名"""
    missing = []
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from web_dashboard.data_adapter import audit_cron_name_mappings
        missing = audit_cron_name_mappings()
    except ImportError:
        pass
    except Exception as e:
        print(f"⚠️ 映射审计异常: {e}")
    return missing


if __name__ == "__main__":
    delivery_issues = audit_delivery()
    check_and_backfill()
    
    # v4.6.x: 审计cron名称映射完整性 — 预防Dashboard显示跳票
    missing_mappings = audit_mapping_completeness()
    if missing_mappings:
        print(f"\n⚠️ _CRON_NAME_TO_LOGICAL 缺少 {len(missing_mappings)} 个映射:")
        for m in missing_mappings:
            print(f"  • {m['name']} ({m['schedule']})")
            print(f"    建议: {m['suggestion']}")
        delivery_issues = True
    
    if delivery_issues:
        # 发现delivery异常或映射缺失时以非零退出码退出，agentTurn会看到告警
        sys.exit(1)
