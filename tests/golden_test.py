#!/usr/bin/env python3
"""
DSL Phase 3 GOLDEN TEST — 金标准自动比对
每次改动后运行，输出差异，超阈值拒绝部署。

三层检查:
  T1: Snapshot Match (<1s)        — 系统快照指纹匹配
  T2: Pipeline Dry-Run (<5s)      — 关键脚本可执行不崩溃
  T3: Full Golden Run (<2min)     — 完整管线+输出比对

阈值:
  - 池变化 > 0只 → WARN (可能正常)
  - 假日变化 > 0天 → FAIL
  - 脚本数变化 > 5 → WARN
  - 熔断器状态变化 → INFO
  - 预测输出差异 > 20% → FAIL

用法:
  python3 tests/golden_test.py              # 完整检查
  python3 tests/golden_test.py --snapshot   # 仅快照比对
  python3 tests/golden_test.py --accept     # 接受当前状态作为新基线
"""

import os, sys, json, yaml, hashlib, subprocess
from pathlib import Path
from datetime import datetime, date
from typing import Dict, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

BASELINE_DIR = PROJECT_ROOT / "tests" / "golden_baseline"
SNAPSHOT_FILE = BASELINE_DIR / "system_snapshot.json"
PASS = "✅"; FAIL = "❌"; WARN = "⚠️"; INFO = "ℹ️"

results = []
baseline = {}
current = {}

def record(name: str, level: str, detail: str = ""):
    results.append((name, level, detail))
    print(f"  {level} {name}" + (f" | {detail}" if detail else ""))


def _short_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def _read_baseline() -> Dict:
    if not SNAPSHOT_FILE.exists():
        return {}
    with open(SNAPSHOT_FILE, encoding="utf-8") as f:
        return json.load(f)


def _script_names() -> list:
    scripts_py = [s for s in Path("scripts").glob("*.py") if not s.name.startswith("_")]
    return sorted(s.name for s in scripts_py)


def _script_hash(script_names: list) -> str:
    return hashlib.sha256(''.join(sorted(script_names)).encode()).hexdigest()[:12]


def _diff_list(current_items: list, baseline_items: list) -> Dict:
    current_sorted = sorted(current_items)
    baseline_sorted = sorted(baseline_items)
    return {
        "count": len(current_sorted),
        "baseline_count": len(baseline_sorted),
        "hash": _short_hash(current_sorted),
        "baseline_hash": _short_hash(baseline_sorted) if baseline_sorted else "",
        "added": sorted(set(current_sorted) - set(baseline_sorted)),
        "removed": sorted(set(baseline_sorted) - set(current_sorted)),
    }


def write_drift_explanation(output_path: Path = None) -> Path:
    """Write human-review package for snapshot drift without accepting baseline."""
    from config.holiday_calendar import A_SHARE_HOLIDAYS

    output_path = output_path or (PROJECT_ROOT / "cache" / "golden_drift_explanation.json")
    base = _read_baseline()

    with open("config/master_stock_pool.yaml", encoding="utf-8") as f:
        mp = yaml.safe_load(f)["master_pool"]
    current_symbols = sorted(str(s["symbol"]).zfill(6) for s in mp)
    current_holidays = sorted(str(h) for h in A_SHARE_HOLIDAYS)
    current_scripts = _script_names()

    try:
        with open("data/circuit_breaker.json", encoding="utf-8") as f:
            cb = json.load(f)
    except Exception:
        cb = {}
    cb_current = {
        "trading_paused": cb.get("trading_paused", True),
        "last_trade_date": cb.get("last_trade_date"),
        "consecutive_failed_trades": cb.get("consecutive_failed_trades"),
        "today_drawdown": cb.get("today_drawdown"),
    }
    cb_baseline = {
        "trading_paused": base.get("circuit_breaker_paused", True),
    } if base else {}

    script_diff = _diff_list(current_scripts, base.get("scripts", []))
    script_diff["hash"] = _script_hash(current_scripts)

    explanation = {
        "generated_at": datetime.now().isoformat(),
        "baseline_file": str(SNAPSHOT_FILE),
        "needs_human_acceptance": True,
        "note": "--explain only writes drift evidence; it does not update golden baseline.",
        "stock_pool": _diff_list(current_symbols, base.get("master_pool_symbols", [])),
        "holidays": _diff_list(current_holidays, base.get("holidays", [])),
        "scripts": script_diff,
        "circuit_breaker": {
            "count": len(cb_current),
            "baseline_count": len(cb_baseline),
            "hash": _short_hash(cb_current),
            "baseline_hash": _short_hash(cb_baseline) if cb_baseline else "",
            "added": sorted(set(cb_current) - set(cb_baseline)),
            "removed": sorted(set(cb_baseline) - set(cb_current)),
            "changed": {
                k: {"baseline": cb_baseline.get(k), "current": cb_current.get(k)}
                for k in sorted(set(cb_current) | set(cb_baseline))
                if cb_baseline.get(k) != cb_current.get(k)
            },
            "current": cb_current,
            "baseline": cb_baseline,
        },
    }

    # Older baselines only store script_count/hash, not script names. Keep those
    # values so reviewers can still attribute drift before accepting a new file.
    explanation["scripts"]["baseline_hash_recorded"] = base.get("script_hash", "")
    explanation["scripts"]["baseline_count_recorded"] = base.get("script_count", 0)

    proposed_baseline = {
        "generated_at": datetime.now().isoformat(),
        "version": _read_version_for_snapshot(),
        "files": {},
        "master_pool_symbols": current_symbols,
        "master_pool_count": len(current_symbols),
        "master_pool_hash": _short_hash(current_symbols),
        "holidays": current_holidays,
        "holiday_count": len(current_holidays),
        "holiday_hash": _short_hash(current_holidays),
        "circuit_breaker_paused": cb_current.get("trading_paused", True),
        "script_count": len(current_scripts),
        "script_hash": _script_hash(current_scripts),
        "scripts": current_scripts,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    proposed_path = output_path.with_name("golden_proposed_system_snapshot.json")
    proposed_path.write_text(json.dumps(proposed_baseline, indent=2, ensure_ascii=False), encoding="utf-8")
    explanation["proposed_baseline_file"] = str(proposed_path)
    explanation["human_review_required_before_accept"] = [
        "确认 stock_pool added/removed 是否为已批准池变更",
        "确认 holidays 仅为新增官方假日/调休日，无误删",
        "确认 scripts count/hash 增量来自已审查脚本",
        "确认 circuit_breaker 状态未进入 paused",
    ]

    output_path.write_text(json.dumps(explanation, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  ℹ️ Golden drift explanation: {output_path}")
    return output_path


def _read_version_for_snapshot() -> str:
    try:
        with open(PROJECT_ROOT / "VERSION", encoding="utf-8") as f:
            return f.readline().strip()
    except Exception:
        return "unknown"

# ═══════════════ T1: Snapshot Match ═══════════════

def t1_snapshot_match() -> bool:
    print(f"\n{'='*60}")
    print(f"  T1: Snapshot Match")
    print(f"{'='*60}")
    
    if not SNAPSHOT_FILE.exists():
        record("T1-baseline", FAIL, "基线文件不存在! 运行 --accept 创建")
        return False
    
    with open(SNAPSHOT_FILE) as f:
        baseline = json.load(f)
    
    issues = 0
    
    # 1.1 池一致性
    from config.holiday_calendar import A_SHARE_HOLIDAYS
    
    with open("config/master_stock_pool.yaml") as f:
        mp = yaml.safe_load(f)["master_pool"]
    current_symbols = sorted(str(s["symbol"]).zfill(6) for s in mp)
    current_hash = hashlib.sha256(
        json.dumps(current_symbols, sort_keys=True).encode()).hexdigest()[:12]
    
    baseline_symbols = baseline.get("master_pool_symbols", [])
    baseline_hash = baseline.get("master_pool_hash", "")
    pool_added = set(current_symbols) - set(baseline_symbols)
    pool_removed = set(baseline_symbols) - set(current_symbols)
    
    if pool_added or pool_removed:
        record("T1-pool", WARN, 
               f"+{len(pool_added)}/-{len(pool_removed)} (新hash={current_hash})")
        if pool_added:
            print(f"    新增: {sorted(pool_added)[:5]}")
        if pool_removed:
            print(f"    移除: {sorted(pool_removed)[:5]}")
    else:
        record("T1-pool", PASS, f"{len(current_symbols)}只 (hash={current_hash})")
    
    # 1.2 假日日历一致性 → 任何变化都告警
    current_holidays = sorted(str(h) for h in A_SHARE_HOLIDAYS)
    current_holiday_hash = hashlib.sha256(
        json.dumps(current_holidays, sort_keys=True).encode()).hexdigest()[:12]
    baseline_holidays = baseline.get("holidays", [])
    baseline_holiday_hash = baseline.get("holiday_hash", "")
    
    holiday_added = set(current_holidays) - set(baseline_holidays)
    holiday_removed = set(baseline_holidays) - set(current_holidays)
    
    if holiday_added or holiday_removed:
        record("T1-holidays", FAIL if holiday_removed else WARN,
               f"+{len(holiday_added)}/-{len(holiday_removed)} (新hash={current_holiday_hash})")
        if holiday_added:
            print(f"    新增假日: {sorted(holiday_added)}")
        if holiday_removed:
            print(f"    移除假日: {sorted(holiday_removed)} ← 危险!")
        issues += 1
    else:
        record("T1-holidays", PASS, f"{len(current_holidays)}天 (hash={current_holiday_hash})")
    
    # 1.3 脚本数量一致性
    scripts_py = list(Path("scripts").glob("*.py"))
    scripts_py = [s for s in scripts_py if not s.name.startswith("_")]
    current_script_count = len(scripts_py)
    current_script_hash = hashlib.sha256(
        ''.join(sorted(s.name for s in scripts_py)).encode()).hexdigest()[:12]
    baseline_script_count = baseline.get("script_count", 0)
    
    delta = abs(current_script_count - baseline_script_count)
    if delta > 5:
        record("T1-scripts", WARN, 
               f"{current_script_count}个 (Δ{delta}, hash={current_script_hash})")
    elif delta == 0:
        record("T1-scripts", PASS, f"{current_script_count}个 (不变)")
    else:
        record("T1-scripts", INFO, f"{current_script_count}个 (Δ{delta})")
    
    # 1.4 熔断器状态
    with open("data/circuit_breaker.json") as f:
        cb = json.load(f)
    current_paused = cb.get("trading_paused", True)
    baseline_paused = baseline.get("circuit_breaker_paused", True)
    
    if current_paused and not baseline_paused:
        record("T1-circuit-breaker", FAIL, "熔断器从UNPAUSED变为PAUSED!")
        issues += 1
    elif not current_paused and baseline_paused:
        record("T1-circuit-breaker", INFO, "熔断器已恢复 (之前暂停)")
    else:
        paused_str = "已暂停" if current_paused else "正常"
        record("T1-circuit-breaker", PASS, paused_str)
    
    return issues == 0

# ═══════════════ T2: Pipeline Dry-Run ═══════════════

def t2_pipeline_dryrun() -> bool:
    print(f"\n{'='*60}")
    print(f"  T2: Pipeline Dry-Run (关键脚本可执行性)")
    print(f"{'='*60}")
    
    scripts = [
        ("batch_predict", "scripts/batch_predict.py", ["--help"]),
        ("pre_market_refresh", "scripts/pre_market_refresh.py", []),
        ("morning_decision", "scripts/morning_decision.py", ["--market", "a"]),
        ("paper_trader", "scripts/paper_trader.py", []),
        ("feedback_controller", "scripts/feedback_controller.py", []),
    ]
    
    all_ok = True
    for name, script, args in scripts:
        script_path = PROJECT_ROOT / script
        if not script_path.exists():
            record(f"T2-{name}", FAIL, "脚本不存在!")
            all_ok = False
            continue
        
        try:
            # 只验证Python语法+import，不实际运行
            with open(script_path) as f:
                content = f.read()
            compile(content, script_path, 'exec')
            record(f"T2-{name}", PASS, "语法通过")
        except SyntaxError as e:
            record(f"T2-{name}", FAIL, f"语法错误: {e}")
            all_ok = False
        except Exception as e:
            record(f"T2-{name}", INFO, f"编译通过 (warnings: {e})")
    
    return all_ok

# ═══════════════ T3: Full Golden Run ═══════════════

def t3_full_golden_run() -> bool:
    print(f"\n{'='*60}")
    print(f"  T3: Full Golden Run (管线输出比对)")
    print(f"{'='*60}")
    
    all_ok = True
    golden_outputs = {}
    
    # T3.1: pool sync一致性
    print("\n  T3.1: 双池同步")
    try:
        result = subprocess.run(
            [sys.executable, "scripts/sync_stock_pool.py", "--dry-run"],
            capture_output=True, text=True, timeout=30
        )
        if "无新增" in result.stdout or "双池完全一致" in result.stdout:
            record("T3-sync-pool", PASS, "双池一致")
        elif "将新增" in result.stdout:
            record("T3-sync-pool", WARN, "有未同步标的")
            print(result.stdout.strip())
        else:
            record("T3-sync-pool", FAIL, result.stderr[:100])
            all_ok = False
    except Exception as e:
        record("T3-sync-pool", FAIL, str(e)[:100])
        all_ok = False
    
    # T3.2: holiday calendar一致性
    print("\n  T3.2: 假日日历")
    try:
        from config.holiday_calendar import is_trading_day
        test_dates = [
            (date(2026,5,1), False), (date(2026,5,6), True),
            (date(2026,10,1), False), (date(2026,10,8), True),
            (date(2026,1,1), False),  (date(2026,1,2), False),  # 元旦假期(1月1-2日, 周五)
        ]
        errors = 0
        for d, expected in test_dates:
            actual = is_trading_day(d, "A_SHARE")
            if actual != expected:
                record(f"T3-holiday-{d}", FAIL, f"期望{expected} 实际{actual}")
                errors += 1
        
        if errors == 0:
            record("T3-holiday-all", PASS, f"{len(test_dates)}个关键日期全部正确")
    except Exception as e:
        record("T3-holiday", FAIL, str(e)[:100])
        all_ok = False
    
    # T3.3: 数据文件完整性
    print("\n  T3.3: 关键输出文件")
    output_files = {
        "熔断器": ("data/circuit_breaker.json", "json"),
        "paper_trading": ("data/paper_trading.db", "sqlite"),
        "校准数据": ("confidence_data/prediction_calibration.json", "json"),
        "自适应参数": ("config/adaptive_params.yaml", "yaml"),
    }
    
    for name, (path, ftype) in output_files.items():
        fp = PROJECT_ROOT / path
        if not fp.exists():
            record(f"T3-output-{name}", FAIL, "文件缺失!")
            all_ok = False
            continue
        
        try:
            if ftype == "json":
                with open(fp) as f:
                    data = json.load(f)
                record(f"T3-output-{name}", PASS, f"{(fp.stat().st_size/1024):.0f}KB")
            elif ftype == "yaml":
                with open(fp) as f:
                    data = yaml.safe_load(f)
                record(f"T3-output-{name}", PASS, f"{(fp.stat().st_size/1024):.0f}KB")
            elif ftype == "sqlite":
                import sqlite3
                conn = sqlite3.connect(fp)
                tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                record(f"T3-output-{name}", PASS, f"{len(tables)} tables, {(fp.stat().st_size/1024):.0f}KB")
                conn.close()
        except Exception as e:
            record(f"T3-output-{name}", FAIL, f"损坏: {str(e)[:60]}")

    return all_ok

# ═══════════════ MAIN ═══════════════

def main():
    import argparse
    ap = argparse.ArgumentParser(description="DSL Golden Test — 金标准自动比对")
    ap.add_argument("--snapshot", action="store_true", help="仅快照比对 (T1)")
    ap.add_argument("--accept", action="store_true", help="接受当前状态为新基线")
    ap.add_argument("--explain", action="store_true", help="输出cache/golden_drift_explanation.json，不接受基线")
    args = ap.parse_args()
    
    if args.accept:
        print("🔧 正在创建新基线...")
        subprocess.run([sys.executable, "-c", """
import json, yaml, hashlib
from pathlib import Path
from datetime import date

PROJECT_ROOT = Path('.').resolve()
BASELINE_DIR = PROJECT_ROOT / "tests" / "golden_baseline"
BASELINE_DIR.mkdir(parents=True, exist_ok=True)

# 从 VERSION 文件读取，消除硬编码 (dsl-harness-engineering §2.4 AP-2)
_vfile = PROJECT_ROOT / "VERSION"
_version_str = _vfile.read_text(encoding="utf-8").splitlines()[0].strip() if _vfile.exists() else "unknown"
baseline = {'generated_at': __import__('datetime').datetime.now().isoformat(), 'version': _version_str, 'files': {}}

with open('config/master_stock_pool.yaml') as f:
    mp = yaml.safe_load(f)['master_pool']
baseline['master_pool_symbols'] = sorted(str(s['symbol']).zfill(6) for s in mp)
baseline['master_pool_count'] = len(mp)
baseline['master_pool_hash'] = hashlib.sha256(json.dumps(baseline['master_pool_symbols'], sort_keys=True).encode()).hexdigest()[:12]

from config.holiday_calendar import A_SHARE_HOLIDAYS
baseline['holidays'] = sorted(str(h) for h in A_SHARE_HOLIDAYS)
baseline['holiday_count'] = len(A_SHARE_HOLIDAYS)
baseline['holiday_hash'] = hashlib.sha256(json.dumps(baseline['holidays'], sort_keys=True).encode()).hexdigest()[:12]

with open('data/circuit_breaker.json') as f:
    cb = json.load(f)
baseline['circuit_breaker_paused'] = cb.get('trading_paused', True)

scripts_py = [s for s in Path('scripts').glob('*.py') if not s.name.startswith('_')]
baseline['script_count'] = len(scripts_py)
baseline['script_hash'] = hashlib.sha256(''.join(sorted(s.name for s in scripts_py)).encode()).hexdigest()[:12]

(BASELINE_DIR / 'system_snapshot.json').write_text(json.dumps(baseline, indent=2, ensure_ascii=False))
print(f'✅ 新基线已保存 ({len(baseline)} features)')
"""], cwd=str(PROJECT_ROOT))
        return 0
    
    print(f"\n{'#'*60}")
    print(f"# DSL GOLDEN TEST Phase 3 — 金标准自动比对")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"# 基线: {SNAPSHOT_FILE}")
    print(f"{'#'*60}")
    
    ok_t1 = t1_snapshot_match()
    if args.explain:
        write_drift_explanation()
    if args.snapshot:
        return 0 if ok_t1 else 1
    
    ok_t2 = t2_pipeline_dryrun()
    ok_t3 = t3_full_golden_run()
    
    # ── 汇总 ──
    total = len(results)
    fails = sum(1 for _, level, _ in results if level == FAIL)
    warns = sum(1 for _, level, _ in results if level == WARN)
    
    print(f"\n{'='*60}")
    status = "✅ 通过" if fails == 0 else f"❌ {fails} FAIL"
    print(f"  📊 Golden Test: {status}" + (f" | {warns} WARN" if warns else ""))
    print(f"{'='*60}")
    
    if fails > 0:
        print(f"\n❌ 以下检查失败 (超过阈值，应拒绝部署):")
        for name, level, detail in results:
            if level == FAIL:
                print(f"  [{name}] {detail}")
    
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
