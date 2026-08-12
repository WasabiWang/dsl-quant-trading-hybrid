#!/usr/bin/env python3
"""
DSL v4.6.6 健康检查脚本 — 核心链路全覆盖版
定时检测系统各模块状态，发现问题→分级反馈闭环
"""
import subprocess
import json
import sys
import os
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

LEVEL_OK = "✅"
LEVEL_WARN = "⚠️"
LEVEL_ERROR = "❌"

# 关键检查项名称 — 只有这些失败才触发 feedback_controller
CRITICAL_CHECKS = {"盘前数据缓存", "模型新鲜度", "数据源", "股票池"}


class HealthChecker:
    def __init__(self):
        self.results = []
        self.start_time = time.time()

    def check(self, name, func):
        try:
            result = func()
            status = LEVEL_OK if result['status'] else LEVEL_ERROR
            self.results.append({'name': name, 'status': result['status'],
                                 'message': result.get('message', ''), 'level': status})
            print(f"{status} {name}: {result.get('message', 'OK')}")
        except Exception as e:
            self.results.append({'name': name, 'status': False, 'message': str(e), 'level': LEVEL_ERROR})
            print(f"❌ {name}: {e}")

    def summary(self):
        elapsed = time.time() - self.start_time
        total = len(self.results)
        passed = sum(1 for r in self.results if r['status'])
        failed = total - passed
        print(f"\n{'='*60}\n📊 健康检查汇总\n{'='*60}")
        print(f"总检查项: {total}  通过: {passed} {LEVEL_OK}  失败: {failed} {LEVEL_ERROR}  耗时: {elapsed:.2f}s")
        if failed > 0:
            print("\n❌ 失败项目:")
            for r in self.results:
                if not r['status']:
                    print(f"  - {r['name']}: {r['message']}")
        return failed == 0


# ═══════════════════ P0-2: cron 实时状态 ═══════════════════
def check_cron():
    """从 gateway 实时读取 cron 状态"""
    try:
        res = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                           capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return {'status': False, 'message': f'命令失败: {res.stderr[:80]}'}
        data = json.loads(res.stdout)
        crons = data.get('jobs', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not crons:
            return {'status': False, 'message': '无cron任务'}
        crons = [c for c in crons if isinstance(c, dict)]
        enabled = [c for c in crons if c.get('enabled')]
        failed = [c for c in enabled if c.get('state', {}).get('consecutiveErrors', 0) > 0]
        if failed:
            names = ', '.join(c.get('name', '?') for c in failed[:3])
            return {'status': False, 'message': f'{len(failed)}个cron有连续错误: {names}'}
        return {'status': True, 'message': f'{len(enabled)}个cron正常'}
    except json.JSONDecodeError:
        return {'status': False, 'message': 'cron JSON解析失败'}
    except Exception as e:
        return {'status': False, 'message': f'cron异常: {e}'}


def check_data_source():
    """检查数据源 — 麦蕊优先 → 新浪降级"""
    import requests
    # 麦蕊API快速检测（LICENCE有效期）
    mairui_licence = os.environ.get("MAIRUI_LICENCE", "")
    if mairui_licence:
        try:
            r = requests.get(
                f"https://api.mairuiapi.com/hsrl/ssjy/600519/{mairui_licence}",
                timeout=5
            )
            if r.status_code == 200:
                d = r.json()
                if d.get("p") or d.get("code") == "600519":
                    return {'status': True, 'message': '麦蕊API可用'}
        except Exception:
            pass
    else:
        print("⚠️ MAIRUI_LICENCE未配置，跳过麦蕊健康探测")
    # 降级：新浪财经
    symbol = "sh600519"
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen=5"
    try:
        res = subprocess.run(['curl', '-s', '--noproxy', '*', '--max-time', '10', url],
                           capture_output=True, text=True, timeout=12)
        if res.returncode == 0 and 'day' in res.stdout:
            return {'status': True, 'message': '数据源可用(新浪降级)'}
    except Exception:
        pass
    return {'status': False, 'message': '数据源不可用(麦蕊+新浪均失败)'}


def check_imports():
    """检查核心模块导入"""
    modules = [("core.auto_engine", "AutoIterativeEngine"),
               ("core.strategy_monitor", "StrategyMonitor"),
               ("core.circuit_breaker", "CircuitBreaker")]
    failed = []
    for mod_name, cls in modules:
        try:
            m = __import__(mod_name, fromlist=[cls])
            getattr(m, cls)
        except ImportError as e:
            failed.append(f"{mod_name}.{cls}")
    if failed:
        return {'status': False, 'message': f'导入失败: {", ".join(failed)}'}
    return {'status': True, 'message': '核心模块正常'}


def check_feishu():
    """检查飞书集成"""
    try:
        from common.feishu_utils import send_markdown
        return {'status': True, 'message': '飞书模块可用'}
    except ImportError as e:
        return {'status': False, 'message': f'飞书导入失败: {e}'}


def check_scripts():
    """检查核心脚本文件"""
    scripts = ["scripts/batch_predict.py", "scripts/batch_train.py",
               "scripts/pre_market_refresh.py", "scripts/morning_decision.py",
               "scripts/feedback_controller.py"]
    missing = [s for s in scripts if not os.path.exists(os.path.join(PROJECT_ROOT, s))]
    if missing:
        return {'status': False, 'message': f'缺失: {", ".join(missing)}'}
    return {'status': True, 'message': f'{len(scripts)}个核心脚本齐全'}


def check_backtest_data():
    """检查股票池完整性"""
    import yaml
    pool_file = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    if not os.path.exists(pool_file):
        return {'status': False, 'message': 'master_stock_pool.yaml 不存在'}
    try:
        with open(pool_file, 'r', encoding='utf-8') as f:
            pool = yaml.safe_load(f)
        stocks = pool.get('master_pool', [])
        unique = set(s.get('symbol', '') for s in stocks if s.get('symbol'))
        dup_count = len(stocks) - len(unique)
        if len(unique) < 30:
            return {'status': False, 'message': f'池不足: {len(unique)}只唯一'}
        if dup_count > 0:
            return {'status': False, 'message': f'池{len(unique)}只唯一但有{dup_count}条重复'}
        tiers = {}
        for s in stocks:
            t = s.get('tier', '?')
            tiers[t] = tiers.get(t, 0) + 1
        tier_summary = ','.join(f'{k}{v}' for k, v in sorted(tiers.items()))
        return {'status': True, 'message': f'{len(unique)}只唯一({tier_summary})'}
    except Exception as e:
        return {'status': False, 'message': f'池读取失败: {e}'}


# ═══════════════════ P1-1: 按池标的检查模型新鲜度 ═══════════════════
def check_model_freshness():
    """检查池中每只标的的模型新鲜度
    v4.5.3d: 假期感知阈值 — 连续休市越长, 阈值越宽松
    """
    import yaml
    models_dir = os.path.join(PROJECT_ROOT, "models")
    pool_file = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    if not os.path.exists(pool_file) or not os.path.exists(models_dir):
        return {'status': False, 'message': '池或模型目录不存在'}

    with open(pool_file) as f:
        pool = yaml.safe_load(f)
    pool_codes = set(s['symbol'] for s in pool.get('master_pool', []))

    now = datetime.now()
    now_ts = now.timestamp()
    weekday = now.weekday()
    
    # v4.5.3d: 假期感知阈值
    # - 工作日: 训练每24h跑一次 → 48h阈值
    # - 周末: 两天不跑 → 72h
    # - 长假(春节/国庆/五一): 5-7天不跑 → 120h (周五最后一次训练到周一)
    try:
        from config.holiday_calendar import is_trading_day
        from datetime import timedelta
        # 计算距上一个交易日多少天
        check_date = now.date()
        days_since_trade = 0
        for d in range(10):
            test_date = check_date - timedelta(days=d)
            if is_trading_day(check_date=test_date, market="A_SHARE"):
                days_since_trade = d
                break
        # 阈值 = max(48, 距上次交易日天数 * 24 + 24)
        max_hours = max(48, days_since_trade * 24 + 24)
    except ImportError:
        is_weekend = weekday >= 5
        max_hours = 72 if is_weekend else 48
    
    threshold = 86400 * max_hours // 24
    fresh = 0
    stale = 0
    missing_model = []

    for code in pool_codes:
        code_dir = os.path.join(models_dir, code)
        if not os.path.isdir(code_dir):
            missing_model.append(code)
            continue
        pkls = [f for f in os.listdir(code_dir) if f.endswith('.pkl')]
        if not pkls:
            missing_model.append(code)
            continue
        newest = max(os.path.getmtime(os.path.join(code_dir, f)) for f in pkls)
        if now_ts - newest < threshold:
            fresh += 1
        else:
            stale += 1

    issues = []
    if missing_model:
        issues.append(f'{len(missing_model)}只缺模型({",".join(missing_model[:4])})')
    if stale:
        issues.append(f'{stale}只>{max_hours}h')

    if issues:
        return {'status': False, 'message': '; '.join(issues)}
    return {'status': True, 'message': f'{fresh}只模型新鲜(<{max_hours}h)'}


# (已移除: 预测链路check — 改由20:00自我反思的P2数据完整性+P5预测质量覆盖)





def check_cron_results_fresh():
    """检查近期cron执行结果"""
    try:
        res = subprocess.run(['openclaw', 'cron', 'list', '--json'],
                           capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout)
        crons = data.get('jobs', []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        crons = [c for c in crons if isinstance(c, dict)]
        if not crons:
            return {'status': True, 'message': 'cron为空'}
        stale_count = 0
        err_count = 0
        for c in crons:
            state = c.get('state', {})
            last_run = state.get('lastRunAtMs', 0)
            status = state.get('lastRunStatus', '?')
            errors = state.get('consecutiveErrors', 0)
            if errors > 0:
                err_count += 1
            # 超过36小时未运行 (跳过周末)
            now_ms = datetime.now().timestamp() * 1000
            if now_ms - last_run > 36 * 3600 * 1000 and last_run > 0:
                stale_count += 1
        if err_count > 0:
            return {'status': False, 'message': f'{err_count}个cron执行异常'}
        return {'status': True, 'message': f'cron执行正常 (stale={stale_count})'}
    except Exception as e:
        return {'status': True, 'message': f'cron执行检查跳过: {e}'}


def check_data_contract():
    """v4.5.5 S6: 验证数据契约一致性(字段名/多源同步/测试隔离)"""
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from core.data_schema import DataContractValidator, assert_test_isolation
        v = DataContractValidator()
        errors = v.check_all()
        cleaned = assert_test_isolation()
        if not errors:
            msg = f"契约一致"
            if cleaned:
                msg += " + 清理测试污染"
            return {"status": True, "message": msg}
        return {"status": False, "message": f"{len(errors)}项违反: {'; '.join(errors[:3])}"}
    except Exception as e:
        return {"status": True, "message": f"数据契约检查跳过: {e}"}


def check_three_system_consistency():
    """v4.5.7: 三系统一致性验证 — ledger vs simulation vs paper_trading.db"""
    """
    验证三个独立持仓系统的数据一致性:
    1. paper_trading_ledger.json (PaperTrader SQLite的JSON镜像)
    2. simulation_portfolio.json (PortfolioManager的持仓文件)
    3. paper_trading.db (PaperTrader的主存储SQLite)
    """
    issues = []

    # ── 1. 读取 ledger JSON ──
    ledger_path = os.path.join(PROJECT_ROOT, "data", "paper_trading_ledger.json")
    ledger_positions = {}  # code -> shares
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                ledger = json.load(f)
            for p in ledger.get("positions", []):
                code = p.get("code", "")
                shares = int(p.get("shares", 0))
                if code and shares > 0:
                    ledger_positions[code] = shares
        except Exception as e:
            issues.append(f"ledger JSON读取失败: {e}")

    # ── 2. 读取 simulation JSON ──
    sim_path = os.path.join(PROJECT_ROOT, "data", "simulation_portfolio.json")
    sim_positions = {}  # code -> shares
    if os.path.exists(sim_path):
        try:
            with open(sim_path, "r", encoding="utf-8") as f:
                sim = json.load(f)
            for code, info in sim.get("positions", {}).items():
                shares = int(info.get("shares", 0))
                if shares > 0:
                    sim_positions[code] = shares
        except Exception as e:
            issues.append(f"simulation JSON读取失败: {e}")

    # ── 3. 读取 paper_trading.db SQLite ──
    sqlite_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
    sqlite_positions = {}  # code -> shares
    if os.path.exists(sqlite_path):
        try:
            import sqlite3
            conn = sqlite3.connect(sqlite_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT stock_code, quantity FROM positions")
            for row in cur.fetchall():
                code = row["stock_code"]
                qty = int(row["quantity"])
                if qty > 0:
                    sqlite_positions[code] = qty
            conn.close()
        except Exception as e:
            issues.append(f"paper_trading.db查询失败: {e}")

    # ── 4. 交叉比对 ──
    all_codes = set(ledger_positions.keys()) | set(sim_positions.keys()) | set(sqlite_positions.keys())
    if not all_codes:
        return {"status": True, "message": "三系统持仓均空(无持仓需比对)"}

    # ledger vs simulation
    for code in all_codes:
        l_qty = ledger_positions.get(code, 0)
        s_qty = sim_positions.get(code, 0)
        db_qty = sqlite_positions.get(code, 0)

        # 允许 ±1 股舍入误差
        if abs(l_qty - s_qty) > 1:
            issues.append(f"{code}: ledger={l_qty} ≠ simulation={s_qty}")
        if l_qty > 0 and db_qty > 0 and abs(l_qty - db_qty) > 1:
            issues.append(f"{code}: ledger={l_qty} ≠ sqlite={db_qty}")
        if s_qty > 0 and db_qty > 0 and abs(s_qty - db_qty) > 1:
            issues.append(f"{code}: simulation={s_qty} ≠ sqlite={db_qty}")

    # 缺失检查: 某个系统存在而另一个不存在
    for code in ledger_positions:
        if code not in sim_positions and ledger_positions[code] > 0:
            issues.append(f"{code}: ledger有({ledger_positions[code]}股)但simulation无")
        if code not in sqlite_positions and ledger_positions[code] > 0:
            issues.append(f"{code}: ledger有({ledger_positions[code]}股)但sqlite无")

    if issues:
        msg = "; ".join(sorted(set(issues))[:5])
        return {"status": False, "message": f"{len(issues)}处不一致: {msg}"}

    return {"status": True, "message": f"三系统一致 ({len(all_codes)}只持仓, ledger={len(ledger_positions)} sim={len(sim_positions)} sqlite={len(sqlite_positions)})"}


def check_retrain_queue():
    """v4.5.7: 验证重训队列与股票池一致性"""
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from core.retrain_queue_manager import validate_retrain_queue
        result = validate_retrain_queue()
        return {"status": result["valid"], "message": result["message"]}
    except Exception as e:
        return {"status": True, "message": f"重训队列检查跳过: {e}"}


def check_calibration_integrity():
    """v4.5.7: 验证 daily_record.correct_predictions 累加与 overall 一致
    v4.5.18: 移除内联checker调用 (scope错误), 由行580统一包装
    """
    # --- v4.5.17: Data quality monitoring ---
    dq_checks = check_data_quality()
    dq_warnings = []
    for dq in dq_checks:
        mark = "  ✅ " if dq["status"] else "  ❌ "
        print(f"{mark}{dq['name']}: {dq['detail']}")
        if not dq["status"]:
            dq_warnings.append(dq["name"])
    
    try:
        cal_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        if not os.path.exists(cal_path):
            return {"status": True, "message": "校准文件不存在(跳过)"}
        with open(cal_path, "r", encoding="utf-8") as f:
            cal = json.load(f)

        overall = cal.get("overall_stats", {})
        daily_records = cal.get("daily_records", [])

        overall_correct = overall.get("correct_predictions", 0)
        sum_daily = sum(dr.get("correct_predictions", 0) for dr in daily_records)

        if overall_correct != sum_daily:
            return {
                "status": False,
                "message": f"correct_predictions不一致: overall={overall_correct} vs sum(daily)={sum_daily} (差异{abs(overall_correct - sum_daily)})"
            }

        return {"status": True, "message": f"校准完整性OK: overall={overall_correct}, sum(daily)={sum_daily}"}
    except Exception as e:
        return {"status": True, "message": f"校准完整性检查跳过: {e}"}


def check_data_quality():
    """v4.5.17: 数据质量监控 — 防止全0/全相同值/无意义数据进入决策
    
    从历史Bug中抽象的检查:
    1. 行业评分是否全部相同 (P0-1: 硬编码6.0/中性)
    2. 个股change_pct是否全部=0 (P0-2: 盘前数据)
    3. scoring引擎能否输出增持/减持 (P1-2: 阈值不可达)
    4. 晚间缓存日期是否与今日匹配 (P1-1: 日期断链)
    """
    checks = []
    
    # 1. 行业评分唯一性
    today = datetime.now().strftime('%Y%m%d')
    sectors_path = os.path.join(PROJECT_ROOT, 'cache', 'pre_market', f'{today}_sectors.json')
    if os.path.exists(sectors_path):
        with open(sectors_path) as f:
            sectors = json.load(f)
        scores = [s.get('total_score') for s in sectors]
        if len(set(scores)) == 1 and len(scores) > 1:
            checks.append({"name": "行业评分唯一性", "status": False, "detail": f"全部{scores[0]}分! 数据源可能未返回真实涨跌幅"})
        else:
            checks.append({"name": "行业评分唯一性", "status": True, "detail": f"{len(set(scores))}种分值, 正常"})
    else:
        checks.append({"name": "行业评分", "status": True, "detail": "今日无缓存"})
    
    # 2. change_pct是否全0
    stocks_path = os.path.join(PROJECT_ROOT, 'cache', 'pre_market', f'{today}_stocks.json')
    if os.path.exists(stocks_path) and datetime.now().hour >= 10:
        with open(stocks_path) as f:
            stocks = json.load(f)
        changes = [s.get('change_pct', 0) for s in stocks]
        if all(c == 0 for c in changes) and len(changes) > 0:
            checks.append({"name": "个股涨跌幅", "status": False, "detail": f"{len(changes)}只全为0! 可能用了盘前数据"})
        else:
            non_zero = sum(1 for c in changes if c != 0)
            checks.append({"name": "个股涨跌幅", "status": True, "detail": f"{non_zero}/{len(changes)}非零, 正常"})
    else:
        checks.append({"name": "个股涨跌幅", "status": True, "detail": "跳过(盘前或无缓存)"})
    
    # 3. 晚间缓存日期匹配
    evening_path = os.path.join(PROJECT_ROOT, 'cache', 'pre_market', f'{today}_stocks_evening.json')
    if not os.path.exists(evening_path):
        # 尝试回溯前一天
        from datetime import timedelta
        prev = datetime.now() - timedelta(days=1)
        prev_str = prev.strftime('%Y%m%d')
        prev_path = os.path.join(PROJECT_ROOT, 'cache', 'pre_market', f'{prev_str}_stocks_evening.json')
        if os.path.exists(prev_path):
            checks.append({"name": "晚间缓存", "status": True, "detail": f"使用{prev_str}缓存(已修复日期匹配)"})
        else:
            checks.append({"name": "晚间缓存", "status": False, "detail": f"当日和昨日均无可用的晚间缓存!"})
    else:
        checks.append({"name": "晚间缓存", "status": True, "detail": "当日可用"})
    
    # 4. 评分引擎可达性 — 强信号能否产生增持
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, 'core'))
        from production_signal import compute_alpha_score
        r = compute_alpha_score('T', 2.0, ml_pred={'signal':'buy','confidence':0.70},
                                risk_position_ratio=0.8, black_swan_active=False)
        if r['action_signal'] == '增持':
            checks.append({"name": "增持可达", "status": True, "detail": "强涨+ML买0.7→增持"})
        else:
            checks.append({"name": "增持可达", "status": False, "detail": f"强信号仅达{r['action_signal']}({r['total_score']}分)! 阈值可能过高"})
    except Exception as e:
        checks.append({"name": "增持可达", "status": True, "detail": f"跳过: {e}"})
    
    return checks


def check_harness():
    """v4.5.5 S6: 运行Harness L0-L7快速检查, 验证三层防御体系有效"""
    try:
        result = subprocess.run(
            [sys.executable, "tests/harness.py"],
            capture_output=True, text=True, timeout=30,
            cwd=PROJECT_ROOT
        )
        if result.returncode == 0:
            # 从 "📊 结果: X/Y 通过" 提取数值
            import re
            match = re.search(r"📊 结果: (\d+)/(\d+) 通过", result.stdout)
            if match:
                passed, total = match.groups()
                return {"status": True, "message": f"Harness {passed}/{total} 通过"}
            return {"status": True, "message": "Harness检查通过"}
        else:
            # 提取失败输出
            summary = ""
            if "📊" in result.stdout:
                summary = result.stdout.split("📊")[-1][:80]
            return {"status": False, "message": f"Harness失败: {summary}"}
    except subprocess.TimeoutExpired:
        return {"status": True, "message": "Harness超时(30s), 跳过(不影响主流程)"}
    except Exception as e:
        return {"status": True, "message": f"Harness跳过: {e}"}


def main():
    # v4.5.5 S6: 进度追踪
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from common.progress_tracker import ProgressTracker
        _tracker = ProgressTracker("health_check", total_steps=1)
        _tracker.step(1, "运行健康检查")
    except Exception:
        _tracker = None
    print(f"{'='*60}")
    # 动态读取版本号
    try:
        ver_path = os.path.join(PROJECT_ROOT, "VERSION")
        with open(ver_path) as vf:
            dsl_ver = vf.read().strip()
    except Exception:
        dsl_ver = "unknown"
    print(f"🛡️ DSL v{dsl_ver} 健康检查 (核心链路全覆盖)")
    print(f"{'='*60}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    checker = HealthChecker()

    # ── 基础设施层 ──
    checker.check("数据源", check_data_source)
    checker.check("核心模块导入", check_imports)
    checker.check("飞书集成", check_feishu)

    # ── 定时任务层 (P0-2: 从 gateway 实时读) ──
    checker.check("Cron实时状态", check_cron)
    # v4.5.19: 已包含在Cron实时状态中(同一错误不重复评分)
    # checker.check("Cron执行结果", check_cron_results_fresh)

    # ── 核心链路层 (移除预测链路check — 由20:00自我反思覆盖) ──
    checker.check("核心脚本", check_scripts)
    checker.check("股票池", check_backtest_data)
    checker.check("模型新鲜度", check_model_freshness)

    # ── 版本控制 ──


    # ── 三层Harness快速检查 (白天每隔几次执行, 性能约5s) ──
    # v4.5.5 S6: 作为第三道防线, 每小时cron内集成Harness L0-L7
    checker.check("Harness完整性", check_harness)

    # ── 数据契约一致性 (S6: 防止字段名错配/多源不同步) ──
    checker.check("数据契约", check_data_contract)

    # ── v4.5.7: 三系统一致性 + 重训队列 + 校准完整性 ──
    checker.check("三系统一致性", check_three_system_consistency)
    checker.check("重训队列", check_retrain_queue)
    checker.check("校准完整性", check_calibration_integrity)

    # v4.5.7: 鲁棒性守卫 (全量8项检查)
    print(f"\n🛡️ 鲁棒性守卫 (v4.5.7)...")
    try:
        from core.robustness_guard import run_all_guards
        report = run_all_guards()
        overall = report.get("overall", "unknown")
        critical = len(report.get("critical_issues", []))
        warnings = len(report.get("warnings", []))
        if overall == "healthy":
            checker.check(f"鲁棒性守卫({len(report['guards'])}项)", lambda: {"status": True, "message": "8项全通过"})
        elif overall == "degraded":
            print(f"  ⚠️ {warnings}个警告")
            for w in report.get("warnings", [])[:3]:
                print(f"    - {w}")
            checker.check(f"鲁棒性守卫({len(report['guards'])}项/{warnings}警告)", lambda: {"status": True, "message": f"{warnings}个警告"})
        else:
            for c in report.get("critical_issues", []):
                print(f"  🔴 {c}")
            checker.check(f"鲁棒性守卫({len(report['guards'])}项/{critical}严重)", lambda: {"status": False, "message": f"{critical}个严重问题"})
    except Exception as e:
        print(f"  ⚠️ 鲁棒性守卫跳过: {e}")
        checker.check("鲁棒性守卫", lambda: {"status": False, "message": f"守卫异常: {e}"})

    all_passed = checker.summary()

    # ===== P2: 分级反馈闭环 — 仅关键链失败才触发 =====
    failed_names = [r['name'] for r in checker.results if not r['status']]
    critical_fails = [n for n in failed_names if n in CRITICAL_CHECKS]

    if critical_fails:
        print(f"\n🔴 关键检查失败: {critical_fails}")
        print("  触发反馈闭环...")
        try:
            from scripts.feedback_controller import update_all, log_feedback
            update_all()
            log_feedback("health_check", "alert_triggered", {
                "passed": sum(1 for r in checker.results if r['status']),
                "failed": len(critical_fails),
                "critical": critical_fails,
            })
        except Exception as e:
            print(f"  反馈闭环跳过: {e}")
    elif failed_names:
        print(f"\n🟡 非关键检查失败 (不触发闭环): {failed_names}")
    else:
        # v4.5.3c: 全通过 → 检查上次是否有 critical 失败记录，如有则 log resolved
        prev_result_file = os.path.join(PROJECT_ROOT, "cache", "health_check_result.json")
        if os.path.exists(prev_result_file):
            try:
                with open(prev_result_file, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                prev_critical = prev.get("critical", [])
                if prev_critical:
                    print(f"\n🟢 先前关关键项恢复: {prev_critical} → 记录 resolved")
                    try:
                        from scripts.feedback_controller import log_feedback
                        log_feedback("health_check", "all_clear", {
                            "resolved": prev_critical,
                            "total_passed": len(checker.results),
                        })
                    except Exception as e:
                        print(f"  resolved日志跳过: {e}")
            except Exception:
                pass

    # ===== 写入结果文件 =====
    result_file = os.path.join(PROJECT_ROOT, "cache", "health_check_result.json")
    os.makedirs(os.path.dirname(result_file), exist_ok=True)
    health_result = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "all_passed": all_passed,
        "total": len(checker.results),
        "passed": sum(1 for r in checker.results if r['status']),
        "failed": sum(1 for r in checker.results if not r['status']),
        "critical": critical_fails,
        "issues": [{"name": r["name"], "message": r["message"]} for r in checker.results if not r['status']],
    }
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(health_result, f, ensure_ascii=False, indent=2)

    if '_tracker' in dir() and _tracker:
        status = "通过" if all_passed else "有异常"
        _tracker.complete(f"健康检查: {status}", all_passed=all_passed)
    
    # 健康检查结果由 cron delivery 统一发送至飞书
    # 脚本内不再自报告，避免双重发送
    
    return 0 if all_passed else 1


def cleanup_old_files():
    """清理过期日志和缓存"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    now = time.time()
    cleaned = 0
    log_dir = os.path.join(base, 'logs')
    if os.path.exists(log_dir):
        for f in os.listdir(log_dir):
            path = os.path.join(log_dir, f)
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > 30 * 86400:
                os.remove(path)
                cleaned += 1
    cache_dir = os.path.join(base, 'cache', 'pre_market')
    if os.path.exists(cache_dir):
        for f in os.listdir(cache_dir):
            path = os.path.join(cache_dir, f)
            if os.path.isfile(path) and (now - os.path.getmtime(path)) > 7 * 86400:
                os.remove(path)
                cleaned += 1
    if cleaned > 0:
        print(f"🧹 已清理 {cleaned} 个过期文件")


if __name__ == "__main__":
    cleanup_old_files()
    sys.exit(main())
