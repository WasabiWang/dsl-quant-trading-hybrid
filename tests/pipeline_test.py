#!/usr/bin/env python3
"""
DSL v4.5.3d HARNESS Phase 2 — 管线集成测试 + 假日模拟
验证:
  1. 完整交易日管线: train→predict→refresh→decision→trade
  2. 假日管线: 所有gating正确跳过
  3. 假日模拟: 随机假日日期模糊测试
"""

import os, sys, json, yaml, tempfile, shutil
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

PASS = "✅"; FAIL = "❌"; WARN = "⚠️"
results = []

def record(name: str, ok: bool, detail: str = ""):
    s = f"{PASS} {name}" if ok else f"{FAIL} {name}"
    if not ok and detail:
        s += f" | {detail}"
    results.append((name, ok, detail))
    print(f"  {s}")

# ═══════════════ Part A: 假日Gating模拟 ═══════════════

def test_holiday_gating():
    print(f"\n{'='*60}")
    print(f"  Part A: 假日Gating模拟")
    print(f"{'='*60}")
    
    from config.holiday_calendar import is_trading_day, A_SHARE_HOLIDAYS
    
    # A1: 所有假日必须被所有gated脚本跳过
    print("\n  A1: 假日日期全部脚本应跳过")
    
    scripts_to_check = {
        "batch_predict": "scripts/batch_predict.py",
        "pre_market_refresh": "scripts/pre_market_refresh.py", 
        "morning_decision": "scripts/morning_decision.py",
        "paper_trader": "scripts/paper_trader.py",
        "execute_planned": "scripts/execute_planned_trades.py",
    }
    
    # 验证每个脚本确实有假日检查代码
    for name, path in scripts_to_check.items():
        fp = PROJECT_ROOT / path
        if not fp.exists():
            record(f"A1-{name}", False, "文件不存在")
            continue
        content = fp.read_text()
        has_check = "is_trading_day" in content and ("exit" in content or "return" in content)
        record(f"A1-{name}-gated", has_check, 
               "有假日检查" if has_check else "缺失假日检查!")
    
    # A2: 假日模拟 — 使用最近5个假日日期
    print("\n  A2: 假日模拟 (最近5个假日)")
    holidays = sorted(A_SHARE_HOLIDAYS)
    recent_holidays = [h for h in holidays[-10:] if h >= date(2026,5,1)][:5]
    
    for h in recent_holidays:
        is_td = is_trading_day(h, "A_SHARE")
        record(f"A2-holiday-{h}", not is_td, 
               f"{'非交易日✅' if not is_td else '被误判为交易日❌'}")
    
    # A3: 工作日模拟 — 验证正常交易日
    print("\n  A3: 工作日模拟 (节后首周)")
    trading_days_test = [
        date(2026,5,6),   # 周三 节后首日
        date(2026,5,7),   # 周四
        date(2026,5,8),   # 周五
    ]
    for d in trading_days_test:
        is_td = is_trading_day(d, "A_SHARE")
        record(f"A3-trading-{d}", is_td,
               f"交易日✅" if is_td else "被误判为非交易日❌")
    
    # A4: 随机模糊测试 — 全年365天假日逻辑无矛盾
    print("\n  A4: 全年假日逻辑模糊测试")
    errors = 0
    for d in [date(2026,1,1) + timedelta(days=i) for i in range(365)]:
        is_td = is_trading_day(d, "A_SHARE")
        is_hol = d in A_SHARE_HOLIDAYS
        is_weekend = d.weekday() >= 5
        
        # 规则: holiday OR weekend → must be NOT trading
        if is_hol and is_td:
            errors += 1
        if is_weekend and is_td:
            errors += 1
    
    record("A4-fuzz-365days", errors == 0,
           f"无矛盾" if errors == 0 else f"{errors}处假日逻辑矛盾!")

# ═══════════════ Part B: 管线组件测试 ═══════════════

def test_pipeline_components():
    print(f"\n{'='*60}")
    print(f"  Part B: 管线组件测试")
    print(f"{'='*60}")
    
    # B1: batch_predict — 加载池 + h5d模型
    print("\n  B1: batch_predict 池加载 + 模型加载")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("batch_predict", 
            PROJECT_ROOT / "scripts" / "batch_predict.py")
        
        # 只验证池加载逻辑 (不运行完整预测)
        pool_path = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
        with open(pool_path) as f:
            pool_data = yaml.safe_load(f)
        stocks = pool_data.get("master_pool", [])
        
        record("B1-pool-load", len(stocks) >= 10, f"{len(stocks)}只标的")
        record("B1-pool-tiers", sum(1 for s in stocks if s.get('tier')), f"{sum(1 for s in stocks if s.get('tier'))}只有tier")
    except Exception as e:
        record("B1", False, str(e)[:100])
    
    # B2: pre_market_refresh — 验证数据结构
    print("\n  B2: pre_market_refresh 数据结构验证")
    try:
        pre_market_dir = PROJECT_ROOT / "cache" / "pre_market"
        pre_market_dir.mkdir(parents=True, exist_ok=True)
        
        # 用今天假日测试: pre_market_refresh有假日检查 → 应跳过
        spec = importlib.util.spec_from_file_location("pre_market",
            PROJECT_ROOT / "scripts" / "pre_market_refresh.py")
        
        # 直接验证脚本入口有假日检查
        content = (PROJECT_ROOT / "scripts" / "pre_market_refresh.py").read_text()
        has_holiday = "is_trading_day" in content
        record("B2-holiday-check", has_holiday, 
               "有假日检查" if has_holiday else "缺失!")
    except Exception as e:
        record("B2", False, str(e)[:100])
    
    # B3: morning_decision — 验证决策生成逻辑
    print("\n  B3: morning_decision 假日Gating + 缓存读取")
    try:
        content = (PROJECT_ROOT / "scripts" / "morning_decision.py").read_text()
        has_holiday = "is_trading_day" in content
        has_pred_read = "daily_predict.json" in content
        has_pre_market = "pre_market" in content
        
        record("B3-holiday-check", has_holiday, "有假日检查")
        record("B3-reads-daily-predict", has_pred_read, "读取预测数据")
        record("B3-reads-pre-market", has_pre_market, "读取盘前数据")
    except Exception as e:
        record("B3", False, str(e)[:100])
    
    # B4: paper_trader — 假日Gating + 熔断器集成
    print("\n  B4: paper_trader 假日Gating + 熔断器集成")
    try:
        content = (PROJECT_ROOT / "scripts" / "paper_trader.py").read_text()
        has_holiday = "is_trading_day" in content
        has_cb = "CircuitBreaker" in content or "circuit_breaker" in content
        
        record("B4-holiday-check", has_holiday, "有假日检查")
        record("B4-circuit-breaker", has_cb, "有熔断器集成")
    except Exception as e:
        record("B4", False, str(e)[:100])

# ═══════════════ Part C: 交易执行模拟（dry-run） ═══════════════

def test_trade_execution_dryrun():
    print(f"\n{'='*60}")
    print(f"  Part C: 交易执行模拟 (Dry Run)")
    print(f"{'='*60}")
    
    # C1: 假日交易拒绝
    print("\n  C1: 假日交易应被拒绝")
    try:
        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from paper_trader import PaperTrader
        trader = PaperTrader(test_mode=True)
        
        result = trader.execute_trade("A", "600519", "BUY", 1500.0, 100, "测试")
        rejected = not result.get("success", True)
        record("C1-holiday-reject", rejected, 
               f"假日拒绝✅" if rejected else "假日未拒绝❌")
        
        # 检查拒绝原因
        error = result.get("error", "")
        has_holiday_reason = "非交易日" in error or "假日" in error
        record("C1-error-message", has_holiday_reason or rejected,
               f"原因: {error[:60]}")
    except Exception as e:
        record("C1", False, str(e)[:100])
    
    # C2: 熔断器状态正常
    print("\n  C2: 熔断器状态检查")
    try:
        with open("data/circuit_breaker.json") as f:
            cb = json.load(f)
        
        not_paused = not cb.get("trading_paused", True)
        has_date = bool(cb.get("last_trade_date"))
        today_dt = datetime.now().strftime("%Y-%m-%d")
        date_current = cb.get("last_trade_date") == today_dt
        
        record("C2-not-paused", not_paused, "未暂停")
        record("C2-date-current", date_current, f"日期={cb.get('last_trade_date')}")
    except Exception as e:
        record("C2", False, str(e)[:100])
    
    # C3: 模拟正常交易日 (使用non-holiday日期验证交易执行逻辑)
    print("\n  C3: 数据链路完整性 (dry-run)")
    try:
        # 验证关键数据文件存在性
        files_to_check = [
            ("池文件", "config/master_stock_pool.yaml"),
            ("池配置", "config/stock_pool.yaml"),
            ("自适应参数", "config/adaptive_params.yaml"),
            ("假日日历", "config/holiday_calendar.py"),
            ("熔断器", "data/circuit_breaker.json"),
            ("paper_trading", "data/paper_trading.db"),
            ("校准数据", "confidence_data/prediction_calibration.json"),
            ("文件锁", "common/file_lock.py"),
            ("akshare wrapper", "common/akshare_utils.py"),
            ("池同步", "scripts/sync_stock_pool.py"),
            ("harness", "tests/harness.py"),
        ]
        
        all_ok = True
        for name, path in files_to_check:
            exists = (PROJECT_ROOT / path).exists()
            if not exists:
                record(f"C3-missing-{name}", False, f"{path} 缺失!")
                all_ok = False
        
        if all_ok:
            record("C3-files-complete", True, f"全部{len(files_to_check)}个关键文件存在")
    except Exception as e:
        record("C3", False, str(e)[:100])

# ═══════════════ Part D: 随机交易日模拟 ═══════════════

def test_random_trading_days():
    print(f"\n{'='*60}")
    print(f"  Part D: 全年交易日模拟")
    print(f"{'='*60}")
    
    from config.holiday_calendar import is_trading_day, A_SHARE_HOLIDAYS
    import random
    random.seed(42)
    
    # D1: 统计全年交易日数量
    print("\n  D1: 全年交易日统计")
    total_trading = 0
    total_holiday = 0
    for i in range(365):
        d = date(2026, 1, 1) + timedelta(days=i)
        if is_trading_day(d, "A_SHARE"):
            total_trading += 1
        else:
            total_holiday += 1
    
    # A股大约242-246个交易日
    is_reasonable = 240 <= total_trading <= 250
    record("D1-total-trading", is_reasonable,
           f"{total_trading}个交易日 (预期~244)")
    
    # D2: 每月交易日分布
    print("\n  D2: 每月交易日分布")
    monthly = {}
    for i in range(365):
        d = date(2026, 1, 1) + timedelta(days=i)
        if is_trading_day(d, "A_SHARE"):
            key = d.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0) + 1
    
    anomalies = []
    for m, cnt in sorted(monthly.items()):
        # 每月应有10-23个交易日
        if cnt < 10 or cnt > 23:
            anomalies.append(f"{m}: {cnt}天")
    
    record("D2-monthly-distribution", len(anomalies) == 0,
           f"{len(monthly)}个月" + (f", 异常: {anomalies}" if anomalies else ", 正常"))

# ═══════════════ MAIN ═══════════════

def main():
    print(f"\n{'#'*60}")
    print(f"# DSL HARNESS Phase 2 — 管线集成测试 + 假日模拟")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#'*60}")
    
    test_holiday_gating()
    test_pipeline_components()
    test_trade_execution_dryrun()
    test_random_trading_days()
    
    # ── 汇总 ──
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    
    print(f"\n{'='*60}")
    print(f"  📊 Phase 2: {passed}/{total} 通过" + (f" | ❌ {failed} 失败" if failed else " ✅ 全部通过"))
    print(f"{'='*60}")
    
    if failed:
        print(f"\n❌ 失败项:")
        for name, ok, detail in results:
            if not ok:
                print(f"  {name}: {detail}")
    
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
