#!/usr/bin/env python3
"""
Daily Smoke Test — 每日自动快速健康检查
v4.5.18: 质量检测P2-3修复

检查项目 (L0-L2级别):
1. 核心模块导入
2. 数据文件存在性
3. 数据库可访问性
4. 持仓数据一致性
5. 定时任务状态
6. 配置文件有效性

运行: python3 scripts/smoke_test.py
Cron: 可通过OpenClaw调度器配置每日自动执行
Exit code 0 = all pass, non-0 = failures detected
"""

import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

PROJECT_ROOT = Path(__file__).parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

# 测试结果收集
results = {"passed": [], "failed": [], "warnings": []}


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        results["passed"].append(name)
        print(f"  ✅ {name}")
    else:
        results["failed"].append(name)
        print(f"  ❌ {name}: {detail}")


def warn(name: str, detail: str = ""):
    results["warnings"].append(name)
    print(f"  ⚠️  {name}: {detail}")


# ─────────────────────────────────────────────
# 1. 核心模块导入
# ─────────────────────────────────────────────
print("1. Core imports...")
os.environ.setdefault("MAIRUI_LICENCE", "")

try:
    from config.constants import get_market_type, is_st_stock, get_slippage_bps
    check("config.constants", True)
except Exception as e:
    check("config.constants", False, str(e))

try:
    from config.holiday_calendar import is_trading_day, get_holidays_for_year
    holidays = get_holidays_for_year(2026)
    check(f"holiday_calendar (2026 holidays: {len(holidays)})", len(holidays) > 0, "empty holidays for 2026")
except Exception as e:
    check("holiday_calendar", False, str(e))

try:
    from core.dsl_engine import DSLValidator
    check("core.dsl_engine", True)
except Exception as e:
    check("core.dsl_engine", False, str(e))

try:
    from core.data_loader import DataLoader
    dl = DataLoader()
    check(f"core.data_loader (sources: {len(dl.data_sources)})", len(dl.data_sources) >= 2)
except Exception as e:
    check("core.data_loader", False, str(e))

try:
    from core.risk_manager import RiskManager
    check("core.risk_manager", True)
except Exception as e:
    check("core.risk_manager", False, str(e))

# ─────────────────────────────────────────────
# 2. 数据文件存在性
# ─────────────────────────────────────────────
print("\n2. Data files...")
data_files = [
    ("VERSION", "VERSION"),
    ("master_stock_pool.yaml", "config/master_stock_pool.yaml"),
    ("adaptive_params.yaml", "config/adaptive_params.yaml"),
    ("circuit_breaker.json", "data/circuit_breaker.json"),
    ("black_swan_status.json", "data/black_swan_status.json"),
    ("cron_status.json", "data/cron_status.json"),
    ("retrain_queue.json", "data/retrain_queue.json"),
    ("paper_trading.db", "data/paper_trading.db"),
]
for name, path in data_files:
    check(f"File: {name}", os.path.exists(path), f"missing: {path}")

# ─────────────────────────────────────────────
# 3. 数据库可访问性
# ─────────────────────────────────────────────
print("\n3. Database accessibility...")
try:
    conn = sqlite3.connect("data/paper_trading.db")
    cur = conn.execute("SELECT COUNT(*) FROM positions")
    pos_count = cur.fetchone()[0]
    check(f"paper_trading.db ({pos_count} positions)", True)
    conn.close()
except Exception as e:
    check("paper_trading.db", False, str(e))

try:
    conn = sqlite3.connect("data/idempotency.db")
    cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
    table_count = cur.fetchone()[0]
    check(f"idempotency.db ({table_count} tables)", True)
    conn.close()
except Exception as e:
    check("idempotency.db", False, str(e))

# ─────────────────────────────────────────────
# 4. 持仓数据一致性
# ─────────────────────────────────────────────
print("\n4. Position consistency...")
try:
    conn = sqlite3.connect("data/paper_trading.db")
    cur = conn.execute("SELECT stock_code, quantity FROM positions WHERE quantity > 0")
    db_positions = {r[0]: r[1] for r in cur.fetchall()}
    conn.close()

    if os.path.exists("data/paper_trading_ledger.json"):
        with open("data/paper_trading_ledger.json") as f:
            ledger = json.load(f)
        json_positions = ledger if isinstance(ledger, list) else ledger.get("positions", [])
        json_stocks = {p.get("code", ""): p.get("quantity", p.get("shares", 0)) for p in json_positions}

        only_db = set(db_positions) - set(json_stocks)
        only_json = set(json_stocks) - set(db_positions)
        mismatched = {s for s in set(db_positions) & set(json_stocks) if db_positions[s] != json_stocks[s]}

        if not only_db and not only_json and not mismatched:
            check(f"SQLite↔JSON ({len(db_positions)} positions)", True)
        else:
            details = []
            if only_db: details.append(f"only_db={only_db}")
            if only_json: details.append(f"only_json={only_json}")
            if mismatched: details.append(f"mismatch={mismatched}")
            check("SQLite↔JSON", False, "; ".join(details))
    else:
        warn("SQLite↔JSON", "paper_trading_ledger.json not found")
except Exception as e:
    check("SQLite↔JSON", False, str(e))

# ─────────────────────────────────────────────
# 5. 定时任务状态
# ─────────────────────────────────────────────
print("\n5. Cron health...")
try:
    with open("data/cron_status.json") as f:
        cron_data = json.load(f)
    jobs = cron_data.get("jobs", [])
    total = len(jobs)
    errors = sum(1 for j in jobs if j.get("state", {}).get("consecutiveErrors", 0) > 0)
    disabled = sum(1 for j in jobs if not j.get("enabled", True))
    stale_jobs = []
    now_ts = datetime.now().timestamp() * 1000
    for j in jobs:
        last_run = j.get("state", {}).get("lastRunAtMs", 0)
        if last_run and (now_ts - last_run) > 24 * 3600 * 1000:
            stale_jobs.append(j.get("name", "?"))

    check(f"Cron: {total} jobs", total > 10, f"only {total} jobs")
    if errors > 0:
        warn(f"Cron errors: {errors}", f"{errors} jobs have consecutive errors")
    else:
        check(f"Cron errors: 0", True)
    if disabled > 0:
        warn(f"Cron disabled: {disabled}", f"{disabled} disabled jobs")
    if stale_jobs:
        warn(f"Cron stale: {len(stale_jobs)}", f"jobs not run in 24h: {stale_jobs[:3]}")
except Exception as e:
    check("Cron health", False, str(e))

# ─────────────────────────────────────────────
# 6. 配置文件有效性
# ─────────────────────────────────────────────
print("\n6. Config validation...")
try:
    import yaml
    with open("config/feature_flags.yaml") as f:
        flags = yaml.safe_load(f)
    check("feature_flags.yaml valid", isinstance(flags, dict))
except Exception as e:
    check("feature_flags.yaml", False, str(e))

try:
    with open("data/circuit_breaker.json") as f:
        cb = json.load(f)
    paused = cb.get("trading_paused", False)
    check("Circuit breaker OK" if not paused else "Circuit breaker PAUSED", not paused,
          f"trading paused: {cb.get('pause_reason', 'unknown')}")
except Exception as e:
    check("circuit_breaker.json", False, str(e))

try:
    from web_dashboard.data_adapter import get_blackswan
    bs = get_blackswan()
    active = bs.get("active", False)
    ratio = bs.get("position_ratio", 1.0)
    risk = bs.get("lppl", {}).get("risk_score", 0)
    if active:
        warn(f"Black swan ACTIVE", f"position_ratio={ratio}, LPPL risk={risk}")
    else:
        check("Black swan inactive", True)
except Exception as e:
    check("black_swan adapter", False, str(e))

# ─────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────
print("\n" + "=" * 50)
print(f"SMOKE TEST RESULTS: {len(results['passed'])} passed, {len(results['failed'])} failed, {len(results['warnings'])} warnings")
print("=" * 50)

if results["failed"]:
    print(f"\n❌ FAILURES ({len(results['failed'])}):")
    for f in results["failed"]:
        print(f"  - {f}")
    sys.exit(1)
elif results["warnings"]:
    print(f"\n⚠️  WARNINGS ({len(results['warnings'])}):")
    for w in results["warnings"]:
        print(f"  - {w}")
    print("\n✅ Smoke test PASSED with warnings")
    sys.exit(0)
else:
    print("\n✅ All smoke tests PASSED")
    sys.exit(0)
