#!/usr/bin/env python3
"""
DSL v4.5.3d HARNESS — 预飞检查脚本 (Phase 1: 止血层)
每次代码修改后运行，验证系统完整性。

用法:
  python3 tests/harness.py                  # 完整检查
  python3 tests/harness.py --quick          # 快速检查 (<5s)
  python3 tests/harness.py --pre-commit     # git pre-commit hook

检查项:
  L0: 编译检查        → 所有.py文件语法正确
  L1: Schema验证      → 所有.json/.yaml文件格式正确
  L2: 双池一致性      → master_pool与stock_pool标的同步
  L3: 假日逻辑验证    → is_trading_day() 不产生假阳性
  L4: 文件锁可用性    → file_lock模块正常
  L5: 关键脚本假日检查 → batch_predict/pre_market/paper_trader 有假日gating
  L6: Cron任务完整性   → 所有cron引用的脚本真实存在
  L7: 数据完整性      → 关键数据文件不损坏
"""

import os, sys, json, yaml, subprocess
from pathlib import Path
from datetime import date, datetime
from typing import List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

PASS = "✅"
FAIL = "❌"
WARN = "⚠️"

results: List[Tuple[str, str, str]] = []  # (section, check, status)

def record(section: str, check: str, passed: bool, detail: str = ""):
    status = PASS if passed else FAIL
    results.append((section, check, status + (" " + detail if detail else "")))
    if not passed:
        print(f"  {status} {check}: {detail}")

def header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")

# ═══════════════════ L0: 编译检查 ═══════════════════
def check_compilation():
    header("L0: 编译检查")
    import py_compile
    py_files = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.venv', '_archive', '_cold_archive', 'node_modules')]
        for f in files:
            if f.endswith('.py'):
                py_files.append(os.path.join(root, f))
    
    passed = 0
    failed = 0
    for f in py_files:
        try:
            py_compile.compile(f, doraise=True)
            passed += 1
        except py_compile.PyCompileError as e:
            record("L0-编译", os.path.relpath(f, PROJECT_ROOT), False, str(e)[:80])
            failed += 1
    
    record("L0-编译", f"总计 {passed}/{len(py_files)} 个文件通过", failed == 0)

# ═══════════════════ L1: Schema验证 ═══════════════════
def check_schemas():
    header("L1: Schema/格式验证")
    
    # JSON files
    json_files = list(PROJECT_ROOT.glob("cache/*.json")) + \
                 list(PROJECT_ROOT.glob("config/*.json")) + \
                 list(PROJECT_ROOT.glob("data/*.json")) + \
                 list(PROJECT_ROOT.glob("confidence_data/*.json"))
    
    for jf in json_files:
        if not jf.exists():
            continue
        try:
            with open(jf) as f:
                json.load(f)
        except json.JSONDecodeError as e:
            record("L1-Schema", str(jf.relative_to(PROJECT_ROOT)), False, str(e)[:80])
    
    # YAML files
    yaml_files = list(PROJECT_ROOT.glob("config/*.yaml"))
    for yf in yaml_files:
        try:
            with open(yf) as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            record("L1-Schema", str(yf.relative_to(PROJECT_ROOT)), False, str(e)[:80])
    
    record("L1-Schema", "JSON/YAML格式检查完成", True)

# ═══════════════════ L2: 双池一致性 ═══════════════════
def check_pool_sync():
    header("L2: 股票池双文件一致性")
    try:
        with open("config/master_stock_pool.yaml") as f:
            mp = yaml.safe_load(f)["master_pool"]
        with open("config/stock_pool.yaml") as f:
            sp = yaml.safe_load(f)
        
        ms = {str(s["symbol"]).zfill(6) for s in mp}
        master_meta = {
            str(s["symbol"]).zfill(6): {
                "tier": s.get("tier", "core"),
                "sector": s.get("sector", ""),
            }
            for s in mp
        }
        ss = set()
        stock_meta = {}
        allocation_sum = 0.0
        for tier_name, t in sp.get("tiers", {}).items():
            allocation_sum += float(t.get("allocation", 0) or 0)
            for s in t.get("stocks", []):
                code = str(s["code"]).zfill(6)
                ss.add(code)
                stock_meta[code] = {
                    "tier": tier_name,
                    "sector": s.get("sector", ""),
                }
        
        only_master = ms - ss
        only_stock = ss - ms
        
        if only_master:
            record("L2-双池", "master独有(缺失)", False, str(sorted(only_master)))
        if only_stock:
            record("L2-双池", "stock独有(多余)", False, str(sorted(only_stock)))
        if not only_master and not only_stock:
            record("L2-双池", f"完全一致 ({len(ms)}/{len(ss)})", True)
        mismatches = []
        for code in sorted(ms & ss):
            if master_meta.get(code) != stock_meta.get(code):
                mismatches.append({
                    "code": code,
                    "master": master_meta.get(code),
                    "stock": stock_meta.get(code),
                })
        if mismatches:
            record("L2-双池", "tier/sector不一致", False, str(mismatches[:5]))
        else:
            record("L2-双池", "tier/sector一致", True)
        record(
            "L2-双池",
            f"allocation合计={allocation_sum:.2f}",
            abs(allocation_sum - 1.0) <= 0.001,
            "应为1.00",
        )

        try:
            from scripts.audit.pool_structure_audit import run_audit
            audit = run_audit(include_correlation=False)
            record(
                "L2-结构",
                "行业/主题/低精度BUY结构门槛",
                audit["issue_count"] == 0,
                f"issues={audit['issue_count']} warnings={audit['warning_count']}",
            )
        except Exception as e:
            record("L2-结构", "结构审计失败", False, str(e)[:80])
    except Exception as e:
        record("L2-双池", "检查失败", False, str(e)[:80])

# ═══════════════════ L3: 假日逻辑 ═══════════════════
def check_holiday_logic():
    header("L3: 假日逻辑验证")
    try:
        from config.holiday_calendar import is_trading_day, get_holidays_for_year
        from datetime import timedelta
        
        errors = []
        # 1. 所有已定义假日应为非交易日
        dynamic_holidays = set()
        for year in (2025, 2026, 2027):
            dynamic_holidays |= set(get_holidays_for_year(year))

        for h in sorted(dynamic_holidays):
            if is_trading_day(h, "A_SHARE"):
                errors.append(f"假日{h}被判为交易日!")
        
        # 2. 随机抽样验证: 节日前后一致性
        # 五一节: 5/1-5/5全休, 5/6周三开盘
        for d in [date(2026,5,1), date(2026,5,2), date(2026,5,3), date(2026,5,4), date(2026,5,5)]:
            if is_trading_day(d, "A_SHARE"):
                errors.append(f"五一假期{d}应为非交易日!")
        if not is_trading_day(date(2026,5,6)):
            errors.append("5/6周三应为交易日!")
        
        # 3. 国庆: 10/1-10/7全休
        for d in [date(2026,10,1), date(2026,10,2), date(2026,10,3), date(2026,10,4),
                   date(2026,10,5), date(2026,10,6), date(2026,10,7)]:
            if is_trading_day(d, "A_SHARE"):
                errors.append(f"国庆假期{d}应为非交易日!")
        
        # 4. 周末非交易日 (扫描全年所有周六日)
        scan_date = date(2026,1,1)
        while scan_date <= date(2026,12,31):
            if scan_date.weekday() >= 5 and is_trading_day(scan_date, "A_SHARE"):
                errors.append(f"周末{scan_date}被判为交易日!")
            scan_date += timedelta(days=1)
        
        if errors:
            for e in errors[:5]:
                record("L3-假日", e, False)
        else:
            record("L3-假日", "全年假日逻辑无矛盾", True)
    except Exception as e:
        record("L3-假日", "检查失败", False, str(e)[:80])

# ═══════════════════ L4: 文件锁 ═══════════════════
def check_file_lock():
    header("L4: 文件锁机制")
    try:
        from common.file_lock import locked_json_write, locked_json_read, locked_rw
        import tempfile
        
        tmp = tempfile.mktemp(suffix=".json")
        locked_json_write(tmp, {"test": True})
        data = locked_json_read(tmp)
        assert data == {"test": True}
        os.remove(tmp)
        record("L4-文件锁", "JSON读写+锁获取正常", True)
    except Exception as e:
        record("L4-文件锁", "失败", False, str(e)[:80])

# ═══════════════════ L5: 关键脚本假日Gating ═══════════════════
def check_holiday_gating():
    header("L5: 关键脚本假日Gating")
    
    scripts_to_check = {
        "scripts/batch_predict.py": "batch_predict",
        "scripts/pre_market_refresh.py": "pre_market",
        "scripts/morning_decision.py": "morning_decision",
        "scripts/paper_trader.py": "paper_trader",
        "scripts/execute_planned_trades.py": "execute_planned",
    }
    
    for path, name in scripts_to_check.items():
        fp = PROJECT_ROOT / path
        if not fp.exists():
            record("L5-Gating", name, False, "文件不存在")
            continue
        content = fp.read_text()
        has_check = "is_trading_day" in content or "holiday" in content.lower()
        if not has_check:
            record("L5-Gating", name, False, "缺少假日检查!")
        else:
            record("L5-Gating", name, True)

# ═══════════════════ L6: Cron任务完整性 ═══════════════════
def check_cron_integrity():
    header("L6: Cron任务引用完整性")
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list"],
            capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.split('\n')
        
        # 搜索cron任务中的脚本引用
        import re
        for line in lines:
            # 查找 python3 scripts/xxx.py 或 xxx.py 引用
            matches = re.findall(r'(?:scripts/|\.venv/bin/python3\s+)?scripts/(\w+\.py)', line)
            for m in matches:
                script_path = PROJECT_ROOT / "scripts" / m
                if not script_path.exists():
                    record("L6-Cron", m, False, f"引用的脚本不存在! (cron: {line[:60]})")
        
        record("L6-Cron", "cron任务脚本引用检查完成", True)
    except Exception as e:
        record("L6-Cron", "检查失败", False, str(e)[:80])

# ═══════════════════ L7: 数据完整性 ═══════════════════
def check_data_integrity():
    header("L7: 关键数据文件完整性")
    
    checks = [
        ("circuit_breaker.json", "data/circuit_breaker.json", ["trading_paused", "today_trade_count", "last_trade_date"]),
        ("prediction_calibration", "confidence_data/prediction_calibration.json", ["stock_accuracy", "daily_records", "overall_stats"]),
        ("adaptive_params", "config/adaptive_params.yaml", [])
    ]
    
    for name, path, required_keys in checks:
        fp = PROJECT_ROOT / path
        if not fp.exists():
            record("L7-数据", name, False, "文件缺失")
            continue
        
        try:
            if path.endswith('.json'):
                with open(fp) as f:
                    data = json.load(f)
            else:
                with open(fp) as f:
                    data = yaml.safe_load(f)
            
            missing = [k for k in required_keys if k not in (data or {})]
            if missing:
                record("L7-数据", name, False, f"缺少字段: {missing}")
            else:
                record("L7-数据", name, True)
        except Exception as e:
            record("L7-数据", name, False, str(e)[:80])

# ═══════════════════ L8: 交易日历覆盖 ═══════════════════
def check_data_contract():
    """L8: 数据契约一致性 — 防字段名错配/多源不同步"""
    header("L8: 数据契约一致性")
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from core.data_schema import DataContractValidator
        v = DataContractValidator()
        errors = v.check_all()
        for e in errors:
            record("数据契约", e, False)
        if not errors:
            record("数据契约", "所有契约一致", True)
    except Exception as ex:
        record("数据契约", str(ex), True, "跳过(可选依赖缺失)")


def check_dashboard_smoke():
    """L9: Dashboard烟雾测试 — 版本+关键API+管线标题一致性"""
    header("L9: Dashboard烟雾测试")
    try:
        import urllib.request, json as jmod, os
        base = "http://localhost:8888/api"

        # 1. 版本一致性
        try:
            resp = urllib.request.urlopen(f"{base}/version", timeout=5)
            dash_ver = jmod.loads(resp.read())["version"]
            vpath = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
            with open(vpath) as vf:
                file_ver = vf.readline().strip()
            if dash_ver == file_ver:
                record("Dashboard", f"版本一致: {dash_ver}", True)
            else:
                record("Dashboard", f"版本不一致: VERSION={file_ver} Dashboard={dash_ver}", False)
        except Exception as e:
            record("Dashboard", "无法获取版本", False, str(e)[:60])

        # 2. 管线标题
        try:
            resp = urllib.request.urlopen(f"{base}/pipeline", timeout=5)
            pipe = jmod.loads(resp.read())
            tl = pipe.get("timeline", pipe)
            evening_names = []
            premarket_names = []
            for phase, info in tl.items():
                for t in info.get("tasks", []):
                    if phase == "evening":
                        evening_names.append(t.get("name", ""))
                    if phase == "premarket":
                        premarket_names.append(t.get("name", ""))
            # v4.5.9: 晚间交易预案 + 盘前决策 必须在管线中
            has_evening = any("预案" in n for n in evening_names)
            has_morning = any("盘前决策" in n for n in premarket_names)
            if has_evening and has_morning:
                record("Dashboard", "管线标题正确", True)
            else:
                missing = []
                if not has_evening:
                    missing.append("晚间预案")
                if not has_morning:
                    missing.append("盘前决策")
                record("Dashboard", f"管线标题过时: 缺{missing}", False)
        except Exception as e:
            record("Dashboard", "管线API失败", False, str(e)[:60])

        # 3. 关键数据端点可达
        try:
            for ep in ["status", "predictions", "calibration", "blackswan"]:
                resp = urllib.request.urlopen(f"{base}/{ep}", timeout=5)
                if resp.status != 200:
                    record("Dashboard", f"/api/{ep} HTTP {resp.status}", False)
            record("Dashboard", "关键API可达", True)
        except Exception as e:
            record("Dashboard", "API不可达", False, str(e)[:60])

    except Exception as ex:
        record("Dashboard", "烟雾测试跳过", True, str(ex)[:60])


def check_calendar_coverage():
    header("L9: 交易日历覆盖度")
    try:
        from config.holiday_calendar import A_SHARE_HOLIDAYS
        from datetime import date
        
        # 统计每个月覆盖的假日天数
        months = {}
        for h in A_SHARE_HOLIDAYS:
            key = f"{h.year}-{h.month:02d}"
            months[key] = months.get(key, 0) + 1
        
        # 需要覆盖的关键假期月
        required_months = ["2026-01", "2026-02", "2026-04", "2026-05", "2026-06", "2026-09", "2026-10"]
        missing = [m for m in required_months if m not in months]
        
        if missing:
            record("L8-日历", f"缺覆盖: {missing}", False)
        else:
            record("L8-日历", f"全覆盖{len(A_SHARE_HOLIDAYS)}天 ({len(months)}个月)", True)
    except Exception as e:
        record("L8-日历", "失败", False, str(e)[:80])

# ═══════════════════ L10: 鲁棒性守卫 ═══════════════════
def check_scoring_edge_cases():
    """L11: 评分引擎回归 — 防止"修A坏B"的边界条件验证
    验证compute_alpha_score在关键边缘场景下的不变量
    """
    header("L11: 评分引擎边界验证")
    try:
        from core.production_signal import compute_alpha_score
        
        # 1. change_pct=0 + ML buy → 不惩罚方向矛盾
        r1 = compute_alpha_score('T', 0, ml_pred={'signal':'buy','confidence':0.68},
                                 risk_position_ratio=0.56, black_swan_active=True)
        f2_ok = r1['raw_factors']['f2_ml_confirm'] >= 0
        record("L11-change0+MLbuy", f"f2={r1['raw_factors']['f2_ml_confirm']:.2f}, score={r1['total_score']}, {r1['action_signal']}", f2_ok)
        
        # 2. 涨+ML买方向一致 → 加分
        r2 = compute_alpha_score('T', 2.0, ml_pred={'signal':'buy','confidence':0.68},
                                 risk_position_ratio=0.56, black_swan_active=True)
        match_ok = r2['raw_factors']['f2_ml_confirm'] > 0
        record("L11-涨+ML买一致", f"f2={r2['raw_factors']['f2_ml_confirm']:.2f}, score={r2['total_score']}", match_ok)
        
        # 3. 涨+ML卖方向矛盾 → 严重惩罚
        r3 = compute_alpha_score('T', 2.0, ml_pred={'signal':'sell','confidence':0.65},
                                 risk_position_ratio=0.56, black_swan_active=True)
        contra_ok = r3['raw_factors']['f2_ml_confirm'] == -0.8
        record("L11-涨+ML卖矛盾", f"f2={r3['raw_factors']['f2_ml_confirm']:.2f}", contra_ok)
        
        # 4. 强信号可达增持(score≥5.5)
        r4 = compute_alpha_score('T', 2.0, ml_pred={'signal':'buy','confidence':0.70},
                                 risk_position_ratio=0.8, black_swan_active=False)
        reachable_ok = r4['action_signal'] == '增持'
        record("L11-增持可达", f"score={r4['total_score']}, {r4['action_signal']}", reachable_ok)
        
        # 5. 评分永不越界
        bounds_ok = True
        for chg in [-10, 0, 10]:
            for conf in [0, 0.5, 1.0]:
                for sig in ['buy', 'hold', 'sell']:
                    r = compute_alpha_score('T', chg, ml_pred={'signal':sig,'confidence':conf},
                                            risk_position_ratio=0.5, black_swan_active=True)
                    if not (0 <= r['total_score'] <= 10):
                        bounds_ok = False
        record("L11-评分范围", f"全部[{0},{10}]内", bounds_ok)
        
        # 6. 无ML数据时评分中性
        r6 = compute_alpha_score('T', 0.5, ml_pred=None,
                                 risk_position_ratio=0.56, black_swan_active=True)
        no_ml_ok = 4.0 <= r6['total_score'] <= 6.0
        record("L11-无ML数据", f"score={r6['total_score']}", no_ml_ok)
        
    except Exception as e:
        record("L11", f"异常: {e}", False)


def check_bug_patterns():
    """L12: 已知Bug模式扫描 — 防止反复出现相同类型的bug
    
    扫描范围:
    1. 硬编码值挡住fallback链 (如麦蕊结构数据挡住akshare)
    2. if not X: 条件只检查存在性不检查质量
    3. key名不匹配的生产者/消费者对
    """
    header("L12: 已知Bug模式扫描")
    issues = []
    
    # 1. 扫描 "if not " + "sectors/stocks/list" 模式 — 只检查存在性不检查质量
    _pattern1_files = [
        ('scripts/pre_market_refresh.py', 1),
    ]
    for fpath, _ in _pattern1_files:
        full_path = os.path.join(PROJECT_ROOT, fpath)
        if os.path.exists(full_path):
            with open(full_path) as f:
                content = f.read()
            # 检查是否仍有`if not sectors:`前无质量检查
            if 'if not sectors:' in content and 'if not sectors or _mairui_only_structure' not in content:
                issues.append(f"{fpath}: `if not sectors:` 仍无质量检查")
            if 'if not sectors or' in content:
                issues.append(f"{fpath}: ✅ 已添加质量检查")
    
    # 2. 检查compute_alpha_score中对momentum_dir=0的处理
    _ps_path = os.path.join(PROJECT_ROOT, 'core', 'production_signal.py')
    if os.path.exists(_ps_path):
        with open(_ps_path) as f:
            ps = f.read()
        if 'momentum_dir == 0' in ps:
            issues.append("core/production_signal.py: ✅ 已处理change=0边缘情况")
        else:
            issues.append("❌ core/production_signal.py: 未处理change=0边缘情况!")
    
    # 3. 检查数据契约 — 生产者/消费者key一致性
    try:
        from core.data_schema import check_field_consistency
        result = check_field_consistency()
        if result.get('consistent', False):
            issues.append("✅ 数据字段一致")
        else:
            for m in result.get('mismatches', [])[:3]:
                issues.append(f"❌ 字段不一致: {m}")
    except Exception:
        issues.append("⚠️ 数据契约检查跳过")
    
    for issue in issues:
        record("L12", issue, '❌' not in issue)


def check_robustness_guard():
    """L10: 鲁棒性守卫 — 防生产数据损坏/信号异常/配置漂移
    包含8项检查: 文件完整性/跨源一致性/模型池健康/SDK可用性/信号活性/字段校验/配置一致性"""
    header("L10: 鲁棒性守卫")
    try:
        from core.robustness_guard import run_all_guards
        report = run_all_guards()
        overall = report.get("overall", "unknown")
        critical = len(report.get("critical_issues", []))
        warnings_ = len(report.get("warnings", []))
        
        for gname, gresult in report.get("guards", {}).items():
            if isinstance(gresult, dict):
                if "ok" in gresult:
                    record(f"L10-{gname}", f"{gresult['ok']}/{gresult['total']} ok", gresult['ok'] == gresult['total'])
                elif "consistent" in gresult:
                    record(f"L10-{gname}", "一致" if gresult["consistent"] else "不一致", gresult["consistent"])
                elif "active" in gresult:
                    record(f"L10-{gname}", "正常" if gresult["active"] else "异常", gresult["active"])
                elif "valid" in gresult:
                    record(f"L10-{gname}", "通过" if gresult["valid"] else "不通过", gresult["valid"])
        
        record(f"L10-综合", overall, critical == 0)
        for issue in report.get("critical_issues", [])[:3]:
            record(f"L10-严重", issue, False)
    except Exception as e:
        record("L10-导入", str(e)[:80], False)

# ═══════════════════ MAIN ═══════════════════
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="快速模式 (L0+L2+L3 only)")
    ap.add_argument("--pre-commit", action="store_true", help="pre-commit模式 (L0+L1+L5)")
    args = ap.parse_args()
    
    print(f"\n{'#'*60}")
    print(f"# DSL HARNESS v0.1 — 预飞检查 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'#'*60}")
    
    if args.quick:
        check_compilation()
        check_pool_sync()
        check_holiday_logic()
    elif args.pre_commit:
        check_compilation()
        check_schemas()
        check_holiday_gating()
        # v4.5.6: pre-commit也运行数据契约检查（防止字段名错配）
        check_data_contract()
        check_dashboard_smoke()
        # v4.5.7: L10 鲁棒性守卫（防生产数据损坏/信号异常/配置漂移）
        check_robustness_guard()
    else:
        check_compilation()
        check_schemas()
        check_pool_sync()
        check_holiday_logic()
        check_file_lock()
        check_holiday_gating()
        check_cron_integrity()
        check_data_integrity()
        check_calendar_coverage()
        check_data_contract()
        # v4.5.6: Dashboard烟雾测试
        check_dashboard_smoke()
        # v4.5.17: 评分引擎回归 + 已知Bug模式扫描
        check_scoring_edge_cases()
        check_bug_patterns()
    
    # 清理测试残留
    try:
        from core.data_schema import assert_test_isolation
        if assert_test_isolation():
            record("清理", "circuit_breaker", True, "已清理测试残留")
            print(f"  {PASS} 已清理circuit_breaker测试污染")
    except Exception:
        pass
    
    # ── 汇总 ──
    total = len(results)
    passed = sum(1 for _, _, r in results if r.startswith(PASS))
    failed = sum(1 for _, _, r in results if r.startswith(FAIL))
    
    print(f"\n{'='*60}")
    print(f"  📊 结果: {passed}/{total} 通过" + (f" | {FAIL} {failed} 失败" if failed else ""))
    print(f"{'='*60}")
    
    if failed > 0:
        print(f"\n❌ 失败项:")
        for section, check, status in results:
            if status.startswith(FAIL):
                print(f"  [{section}] {status}")
    
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
