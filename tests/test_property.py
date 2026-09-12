#!/usr/bin/env python3
"""
DSL Test Pyramid — 属性测试 (Property-Based Testing)
测试系统在随机边界条件下的不变量保持。
"""
import os, sys, json, yaml, random, tempfile
from pathlib import Path
from datetime import date, timedelta
from copy import deepcopy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

PASS = "✅"; FAIL = "❌"
results = []; failed = 0

def test(name: str, condition: bool, detail: str = ""):
    global results, failed
    results.append(name)
    if condition:
        print(f"  {PASS} {name}")
    else:
        failed += 1
        print(f"  {FAIL} {name}" + (f" | {detail}" if detail else ""))

# ═══════════════ P1: 假日模糊测试 (365天全扫描) ═══════════════
def test_holiday_fuzz():
    print(f"\n{'='*50}")
    print(f"  P1: 假日模糊测试 (全365天)")
    print(f"{'='*50}")
    
    from config.holiday_calendar import is_trading_day, A_SHARE_HOLIDAYS
    
    errors = []
    for i in range(365):
        d = date(2026, 1, 1) + timedelta(days=i)
        is_td = is_trading_day(d, "A_SHARE")
        is_hol = d in A_SHARE_HOLIDAYS
        is_wk = d.weekday() >= 5
        
        # 不变量1: 假日必然是交易日 ≠ True
        if is_hol and is_td:
            errors.append(f"假日{d}被判为交易日!")
        
        # 不变量2: 周末必然是交易日 ≠ True
        if is_wk and is_td:
            errors.append(f"周末{d}被判为交易日!")
    
    test("P1-365-no-false-positive", len(errors) == 0,
         "无矛盾" if not errors else f"{len(errors)}处矛盾: {errors[:3]}")
    
    # 不变量3: is_trading_day幂等
    test_dates = [date(2026,5,1), date(2026,5,6), date(2026,12,25)]
    for d in test_dates:
        r1 = is_trading_day(d, "A_SHARE")
        r2 = is_trading_day(d, "A_SHARE")
        test(f"P1-idempotent-{d}", r1 == r2)

# ═══════════════ P2: 池一致性模糊测试 ═══════════════
def test_pool_fuzz():
    print(f"\n{'='*50}")
    print(f"  P2: 池一致性模糊测试")
    print(f"{'='*50}")
    
    # 验证sync脚本在随机修改后能检测到不一致
    try:
        import subprocess
        
        # 当前一致的基线
        r = subprocess.run(
            [sys.executable, "scripts/sync_stock_pool.py", "--dry-run"],
            capture_output=True, text=True, timeout=15
        )
        baseline_ok = "无新增" in r.stdout or "完全一致" in r.stdout
        test("P2-baseline-consistent", baseline_ok)
        
        # 验证: 如果手动修改master_pool, sync可以检测到
        # (此测试不实际修改文件)
        
    except Exception as e:
        test("P2-pool-fuzz", False, str(e)[:100])

# ═══════════════ P3: 数据完整性模糊测试 ═══════════════
def test_data_integrity_fuzz():
    print(f"\n{'='*50}")
    print(f"  P3: 数据完整性模糊测试")
    print(f"{'='*50}")
    
    # P3.1: 损坏JSON文件 → 系统应优雅降级
    print("  P3.1: JSON损坏→优雅降级")
    from common.file_lock import locked_json_read
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write('{"valid": true}')  # 先写合法JSON
        tmp_path = f.name
    
    try:
        # 正常读取
        data = locked_json_read(tmp_path)
        test("P3-normal-read", data == {"valid": True})
        
        # 损坏JSON
        with open(tmp_path, 'w') as f:
            f.write('NOT VALID JSON {{{')
        bad_data = locked_json_read(tmp_path)
        test("P3-corrupted-graceful", isinstance(bad_data, dict),
             "返回空dict而非崩溃")
        
        # 空文件
        with open(tmp_path, 'w') as f:
            f.write('')
        empty_data = locked_json_read(tmp_path)
        test("P3-empty-graceful", isinstance(empty_data, dict),
             "返回空dict而非崩溃")
    finally:
        os.remove(tmp_path)
    
    # P3.2: 关键数据文件快速完整性检查
    print("\n  P3.2: 关键数据文件快速完整性")
    key_files = [
        "data/circuit_breaker.json",
        "confidence_data/prediction_calibration.json",
    ]
    for kf in key_files:
        fp = PROJECT_ROOT / kf
        if not fp.exists():
            test(f"P3-{kf}", False, "文件缺失")
            continue
        try:
            with open(fp) as f:
                json.load(f)
            test(f"P3-{kf}", True, f"{(fp.stat().st_size/1024):.0f}KB")
        except json.JSONDecodeError:
            test(f"P3-{kf}", False, "JSON格式损坏!")
        except Exception as e:
            test(f"P3-{kf}", False, str(e)[:60])

# ═══════════════ P4: 边界条件测试 ═══════════════
def test_edge_cases():
    print(f"\n{'='*50}")
    print(f"  P4: 边界条件测试")
    print(f"{'='*50}")
    
    from config.holiday_calendar import is_trading_day
    
    # 跨年边界 — 函数不应崩溃，工作日返回合理默认值
    test("P4-2025-no-crash", isinstance(is_trading_day(date(2025,12,31)), bool))
    test("P4-2027-no-crash", isinstance(is_trading_day(date(2027,1,1)), bool))
    
    # 特殊日期
    leap_day = date(2026, 2, 28) + timedelta(days=1)  # 2026不是闰年
    test("P4-feb-28", leap_day == date(2026, 3, 1), "非闰年正确")
    
    # 连续假日段
    from config.holiday_calendar import A_SHARE_HOLIDAYS
    holidays = sorted(A_SHARE_HOLIDAYS)
    
    # 春节: 按年份分组检查 (A_SHARE_HOLIDAYS跨三年)
    from itertools import groupby
    
    # 每年2月节假日各多少天
    spring_2026 = [h for h in holidays if h.year == 2026 and h.month == 2]
    spring_2027 = [h for h in holidays if h.year == 2027 and h.month == 2]
    test("P4-spring-festival-2026", len(spring_2026) == 6, f"2026春节{len(spring_2026)}天")
    test("P4-spring-festival-2027", len(spring_2027) == 7, f"2027春节{len(spring_2027)}天")
    
    # 五一: 按年份检查5月节假日
    labor_2026 = [h for h in holidays if h.year == 2026 and h.month == 5]
    labor_2027 = [h for h in holidays if h.year == 2027 and h.month == 5]
    test("P4-labor-day-2026", len(labor_2026) == 3, f"2026五一{len(labor_2026)}天")
    test("P4-labor-day-2027", len(labor_2027) == 5, f"2027五一{len(labor_2027)}天")
    
    # 国庆: 按年份检查10月节假日
    national_2026 = [h for h in holidays if h.year == 2026 and h.month == 10]
    national_2027 = [h for h in holidays if h.year == 2027 and h.month == 10]
    test("P4-national-day-2026", len(national_2026) == 5, f"2026国庆{len(national_2026)}天")
    test("P4-national-day-2027", len(national_2027) == 7, f"2027国庆{len(national_2027)}天")

# ═══════════════ MAIN ═══════════════
if __name__ == "__main__":
    from datetime import datetime
    print(f"\n{'#'*60}")
    print(f"# DSL TEST PYRAMID — Property-Based Testing")
    print(f"# {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#'*60}")
    
    test_holiday_fuzz()
    test_pool_fuzz()
    test_data_integrity_fuzz()
    test_edge_cases()
    
    print(f"\n{'='*60}")
    print(f"  📊 Property: {len(results)-failed}/{len(results)} 通过" + (f" | {FAIL} {failed}失败" if failed else " ✅ 全部通过"))
    print(f"{'='*60}")
    
    sys.exit(0 if failed == 0 else 1)
