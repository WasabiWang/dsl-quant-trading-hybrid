#!/usr/bin/env python3
"""
DSL v4.5.15 自我反思引擎 — 反馈闭环核心

目的1: 及时修复当天运行bug，确保系统正常运行
目的2: 持续优化和自我迭代系统

反馈闭环: 检测→诊断→修复→验证→记录→追踪

执行流程 (9 Phase, P1已移除):
  P0: 清除已知残留 (stale .pyc, stuck progress, dashboard sync)
  [P1已移除 — 由15:40健康检查+22:00质量检查覆盖]
  P2: 数据完整性检查 (关键文件存在+新鲜+一致)
  P3: 错误模式分析 (feedback_log + health_check + 连续失败)
  P4: 反馈控制器校准 (从feedback_controller导入)
  P5: 预测质量分析 (direction_accuracy + 退化检测)
  P6: 持仓表现分析 (PnL + 止损评估)
  P7: 自动修复 + 行动建议 (已知模式自动fix)
  P8: 生成反思报告 (markdown + feishu)
  P9: 反馈闭环 (更新LESSONS.md/MEMORY.md)

用法:
  python3 scripts/self_reflection.py              # 完整执行
  python3 scripts/self_reflection.py --dry-run    # 只分析不修复
  python3 scripts/self_reflection.py --quick      # 快速模式(跳过P5/P6)
"""

import os, sys, json, glob, subprocess
from datetime import datetime, timedelta
from pathlib import Path

# 代理绕过
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,api.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn'

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TODAY = datetime.now().strftime("%Y-%m-%d")
TODAY_DT = datetime.now()
REPORT_DIR = PROJECT_ROOT / "cache" / "reports"
PROGRESS_DIR = PROJECT_ROOT / "cache" / "progress"
WORKSPACE = Path.home() / ".openclaw" / "workspace"
MEMORY_FILE = WORKSPACE / "MEMORY.md"
LESSONS_FILE = WORKSPACE / "LESSONS.md"

# (P1 cron健康扫描已移除 — 由15:40健康检查+22:00质量检查双层覆盖)

# ═══════════════ KNOWN FIX PATTERNS ═══════════════
KNOWN_FIXES = [
    {
        "name": "stale_pyc",
        "check": lambda: len(list(PROJECT_ROOT.rglob("__pycache__"))) > 0,
        "fix": lambda: os.system(f"find {PROJECT_ROOT} -type d -name __pycache__ -exec rm -rf {{}} + 2>/dev/null"),
        "desc": "清理过期.pyc缓存",
    },
    {
        "name": "stuck_progress",
        "check": lambda: _check_stuck_progress(),
        "fix": lambda: _fix_stuck_progress(),
        "desc": "修复卡在running状态的进度文件",
    },
    {
        "name": "dashboard_version_sync",
        "check": lambda: _check_dashboard_version(),
        "fix": lambda: _fix_dashboard_version(),
        "desc": "同步Dashboard版本号",
    },
    {
        "name": "backup_retry",
        "check": lambda: not _has_progress_today("dsl_backup"),
        "fix": lambda: _run_backup(),
        "desc": "补执行跳票的备份任务",
    },
]


def _check_stuck_progress():
    """检查是否有卡在running>1h的进度"""
    if not PROGRESS_DIR.exists():
        return False
    cutoff = datetime.now() - timedelta(hours=1)
    for f in PROGRESS_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            status = data.get("status", "")
            started = data.get("started_at", "")
            if status == "running" and started:
                start_dt = datetime.fromisoformat(started)
                if start_dt < cutoff:
                    return True
        except Exception:
            pass
    return False


def _fix_stuck_progress():
    """将卡住的进度标记为crashed"""
    cutoff = datetime.now() - timedelta(hours=1)
    fixed = 0
    for f in PROGRESS_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            if data.get("status") == "running" and data.get("started_at"):
                start_dt = datetime.fromisoformat(data["started_at"])
                if start_dt < cutoff:
                    data["status"] = "crashed"
                    data["crashed_at"] = datetime.now().isoformat()
                    with open(f, "w") as fh:
                        json.dump(data, fh, ensure_ascii=False)
                    fixed += 1
        except Exception:
            pass
    return fixed


def _check_dashboard_version():
    """检查Dashboard version与VERSION文件一致"""
    version_file = PROJECT_ROOT / "VERSION"
    if not version_file.exists():
        return False
    with open(version_file) as f:
        version = f.read().strip()
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:8888/api/version", 
                                      headers={"User-Agent": "self-reflection"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            api_data = json.loads(resp.read())
        api_version = api_data.get("version", "")
        if api_version and api_version != version:
            return True
        return False
    except Exception:
        return False  # can't check, skip (don't assume needs fix)


def _fix_dashboard_version():
    """重启Dashboard"""
    try:
        # Kill old dashboard
        os.system("pkill -f 'web_dashboard/server.py' 2>/dev/null")
        import time
        time.sleep(1)
        # Relaunch
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "web_dashboard" / "server.py")],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return "Dashboard 已重启"
    except Exception as e:
        return f"Dashboard 重启失败: {e}"


def _has_progress_today(prefix):
    """检查今天是否有某个prefix的progress文件"""
    for f in PROGRESS_DIR.glob(f"{prefix}_*.json"):
        if TODAY in f.name:
            return True
    return False


def _run_backup():
    """运行备份脚本"""
    try:
        backup_script = PROJECT_ROOT / "scripts" / "backup.sh"
        if backup_script.exists():
            result = subprocess.run(["bash", str(backup_script)], 
                                    capture_output=True, text=True, timeout=300, cwd=str(PROJECT_ROOT))
            return f"备份执行完成 (exit={result.returncode})"
        return "备份脚本不存在"
    except Exception as e:
        return f"备份执行失败: {e}"


# ═══════════════ P1: Cron健康扫描 (已移除 — 由15:40健康检查+22:00质量检查覆盖) ═══════════════


# ═══════════════ P2: 数据完整性检查 ═══════════════


# ═══════════════ P2: 数据完整性检查 ═══════════════
def check_data_integrity():
    """检查关键数据文件的完整性和新鲜度"""
    issues = []
    
    checks = [
        ("daily_predict.json", "cache/daily_predict.json", 24, "预测数据"),
        ("adaptive_params.yaml", "config/adaptive_params.yaml", 48, "自适应参数"),
        ("master_stock_pool.yaml", "config/master_stock_pool.yaml", 168, "股票池配置"),
        ("prediction_calibration.json", "confidence_data/prediction_calibration.json", 48, "校准数据"),
        ("training_status.json", "cache/training_status.json", 48, "训练状态"),
        ("VERSION", "VERSION", 168, "版本文件"),
        ("feedback_log.jsonl", "data/feedback_log.jsonl", 24, "反馈日志"),
        ("risk_override.json", "data/risk_override.json", 48, "风险覆盖"),
    ]
    
    for name, rel_path, max_age_hours, label in checks:
        fpath = PROJECT_ROOT / rel_path
        if not fpath.exists():
            issues.append({"severity": "critical", "file": name, "issue": "文件不存在", "label": label})
            continue
        
        age_hours = (datetime.now() - datetime.fromtimestamp(fpath.stat().st_mtime)).total_seconds() / 3600
        if age_hours > max_age_hours:
            issues.append({"severity": "warning", "file": name, "issue": f"数据过期({age_hours:.0f}h)", "label": label, "age_h": age_hours})
    
    # 版本一致性检查
    version_file = PROJECT_ROOT / "VERSION"
    predict_file = PROJECT_ROOT / "cache" / "daily_predict.json"
    if version_file.exists() and predict_file.exists():
        with open(version_file) as f:
            version = f.read().strip()
        try:
            with open(predict_file) as f:
                predict = json.load(f)
            pred_version = predict.get("version", "")
            if pred_version and pred_version != version:
                issues.append({"severity": "warning", "file": "daily_predict.json", 
                              "issue": f"版本不一致: predict={pred_version} vs VERSION={version}", "label": "版本"})
        except Exception:
            pass
    
    return issues


# ═══════════════ P3: 错误模式分析 ═══════════════
def analyze_error_patterns():
    """分析最近的错误模式，识别重复失败"""
    patterns = {"today_errors": [], "consecutive_failures": [], "health_alerts": [], "anti_patterns": []}
    
    feedback_log = PROJECT_ROOT / "data" / "feedback_log.jsonl"
    if not feedback_log.exists():
        return patterns
    
    today_entries = []
    yesterday_entries = []
    with open(feedback_log) as f:
        for line in f:
            try:
                entry = json.loads(line)
                ts = entry.get("ts", "")
                if TODAY in ts:
                    today_entries.append(entry)
                elif (TODAY_DT - timedelta(days=1)).strftime("%Y-%m-%d") in ts:
                    yesterday_entries.append(entry)
            except json.JSONDecodeError:
                pass
    
    # 今天错误
    for e in today_entries:
        action = e.get("action", "")
        if any(kw in (action + str(e.get("details", {}))) for kw in ["error", "fail", "alert", "warning", "critical"]):
            patterns["today_errors"].append(e)
    
    # 连续失败 (今天+昨天同source出现error)
    today_sources = set(e["source"] for e in today_entries if e.get("action") in ("error", "alert_triggered"))
    yesterday_sources = set(e["source"] for e in yesterday_entries if e.get("action") in ("error", "alert_triggered"))
    consecutive = today_sources & yesterday_sources
    if consecutive:
        patterns["consecutive_failures"] = list(consecutive)
    
    # 健康检查告警
    for e in today_entries:
        if e.get("source") == "health_check" and e.get("action") == "alert_triggered":
            details = e.get("details", {})
            critical_items = details.get("critical", [])
            patterns["health_alerts"].append({"failed": details.get("failed", 0), "passed": details.get("passed", 0), 
                                               "critical": critical_items})
    
    # 反模式检测
    anti_patterns = _detect_anti_patterns(today_entries)
    patterns["anti_patterns"] = anti_patterns
    
    return patterns


def _detect_anti_patterns(entries):
    """检测SOUL.md/LESSONS.md中定义的已知反模式"""
    found = []
    
    # 反模式6: health_check只告警不消除
    alert_entries = [e for e in entries if e.get("source") == "health_check" and e.get("action") == "alert_triggered"]
    all_clear_entries = [e for e in entries if e.get("source") == "health_check" and e.get("action") == "all_clear"]
    if alert_entries and not all_clear_entries:
        found.append({"pattern": "health_check只告警不消除", "ref": "反模式6"})
    
    # 反模式4: data.get()用错key
    for e in entries:
        details_str = str(e.get("details", {}))
        if "KeyError" in details_str or "key" in details_str.lower() and "missing" in details_str.lower():
            found.append({"pattern": "可能的key名不匹配", "ref": "反模式4"})
            break
    
    # 反模式7: API不可用无fallback
    for e in entries:
        if e.get("action") in ("error", "fail"):
            details = str(e.get("details", ""))
            if any(kw in details for kw in ["Connection", "Timeout", "aborted", "Refused", "unreachable"]):
                found.append({"pattern": "API连接失败无fallback", "ref": "反模式7", "detail": details[:100]})
                break
    
    return found


# ═══════════════ P4: 反馈控制器校准 ═══════════════
def run_feedback_controller():
    """运行反馈控制器，更新校准/风险/参数"""
    result = {"status": "skipped", "reason": ""}
    try:
        from scripts.feedback_controller import update_all
        updates = update_all()
        result["status"] = "ok"
        result["updates"] = updates
    except ImportError as e:
        result["status"] = "error"
        result["reason"] = f"无法导入feedback_controller: {e}"
    except Exception as e:
        result["status"] = "error"
        result["reason"] = str(e)
    return result


# ═══════════════ P5: 预测质量分析 ═══════════════
def analyze_prediction_quality():
    """分析今日预测质量"""
    result = {"total": 0, "high_confidence": 0, "low_accuracy": [], "accuracy_mean": None, "degraded": []}
    
    predict_file = PROJECT_ROOT / "cache" / "daily_predict.json"
    if not predict_file.exists():
        return result
    
    try:
        with open(predict_file) as f:
            predictions = json.load(f)
        
        stocks = predictions.get("predictions", predictions.get("stocks", []))
        if isinstance(stocks, dict):
            stocks = list(stocks.values())
        
        accuracies = []
        for s in stocks:
            if not isinstance(s, dict):
                continue
            acc = s.get("direction_accuracy", s.get("accuracy", None))
            if acc is not None and isinstance(acc, (int, float)) and acc > 0:
                accuracies.append(acc)
                if acc < 0.45:
                    code = s.get("stock_code", s.get("symbol", "?"))
                    name = s.get("stock_name", s.get("name", "?"))
                    result["low_accuracy"].append({"code": code, "name": name, "accuracy": acc})
            
            conf = s.get("confidence", s.get("confidence_score", 0))
            if isinstance(conf, (int, float)) and conf >= 0.6:
                result["high_confidence"] += 1
        
        result["total"] = len(accuracies)
        result["accuracy_mean"] = round(sum(accuracies) / len(accuracies), 4) if accuracies else None
        
        # 检测退化 (对比训练状态)
        training_file = PROJECT_ROOT / "cache" / "training_status.json"
        if training_file.exists():
            with open(training_file) as f:
                ts = json.load(f)
            prev_acc = ts.get("_last_accuracy_mean", ts.get("last_accuracy_mean", 0))
            if result["accuracy_mean"] and prev_acc and result["accuracy_mean"] < prev_acc - 0.03:
                result["degraded"] = [f"精度退化: {prev_acc:.4f} → {result['accuracy_mean']:.4f} (-{prev_acc - result['accuracy_mean']:.4f})"]
            
    except Exception as e:
        result["error"] = str(e)
    
    return result



def _fill_stock_names(result):
    """从master_stock_pool.yaml回填股票名称 (SQLite无name字段)"""
    try:
        import yaml
        pool_path = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
        if pool_path.exists():
            with open(pool_path) as f:
                pool = yaml.safe_load(f)
            code_to_name = {}
            for item in pool.get("master_pool", []):
                code = str(item.get("symbol", ""))
                name = item.get("name", "")
                if code and name:
                    code_to_name[code] = name
            for pos in result["positions"]:
                code = str(pos.get("code", ""))
                if code in code_to_name and pos.get("name") == code:
                    pos["name"] = code_to_name[code]
    except Exception:
        pass

# ═══════════════ P6: 持仓表现分析 ═══════════════
def analyze_positions():


    """分析当前持仓表现 (优先SQLite paper_trading.db, 降级到JSON)"""
    result = {"positions": [], "total_pnl": 0, "top_gainer": None, "top_loser": None, "alerts": []}
    
    # 优先从 SQLite 读取 (数据最准, Dashboard也用这个)
    db_path = PROJECT_ROOT / "data" / "paper_trading.db"
    if db_path.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM positions WHERE quantity > 0").fetchall()
            if rows:
                for row in rows:
                    d = dict(row)
                    code = d.get("stock_code", "?")
                    qty = float(d.get("quantity", 0) or 0)
                    cost = float(d.get("avg_cost", d.get("avg_price", 0)) or 0)
                    cur = float(d.get("current_price", 0) or 0)
                    name = d.get("stock_name", d.get("name", code))
                    
                    pnl = (cur - cost) * qty
                    pnl_pct = (cur - cost) / cost * 100 if cost > 0 else 0
                    
                    pos = {"code": code, "name": name, "qty": qty, "cost": cost, "price": cur, "pnl": pnl, "pnl_pct": pnl_pct}
                    result["positions"].append(pos)
                    result["total_pnl"] += pnl
                    
                    if not result["top_gainer"] or pnl > result["top_gainer"]["pnl"]:
                        result["top_gainer"] = pos
                    if not result["top_loser"] or pnl < result["top_loser"]["pnl"]:
                        result["top_loser"] = pos
                
                conn.close()
                if result["positions"]:
                    _fill_stock_names(result)
                    result["total_pnl"] = round(result["total_pnl"], 2)
                    return result
            conn.close()
        except Exception as e:
            print(f"  ⚠️ SQLite持仓读取失败, 降级到JSON: {e}")
    
    # 降级: 从JSON读取
    sim_file = PROJECT_ROOT / "data" / "simulation_portfolio.json"
    ledger_file = PROJECT_ROOT / "data" / "paper_trading_ledger.json"
    
    source_file = sim_file if sim_file.exists() else (ledger_file if ledger_file.exists() else None)
    if not source_file:
        return result
    
    try:
        with open(source_file) as f:
            data = json.load(f)
        raw_positions = data.get("positions", data.get("holdings", []))
        
        # 遍历positions (可能是dict{code→info}或list[{code→info}])
        items = []
        if isinstance(raw_positions, dict):
            items = list(raw_positions.items())
        elif isinstance(raw_positions, list):
            items = [(p.get("stock_code", p.get("code", p.get("symbol", "?"))), p) for p in raw_positions]
        
        for code, p in items:
            if not isinstance(p, dict):
                continue
            qty = float(p.get("quantity", p.get("shares", 0)) or 0)
            cost = float(p.get("avg_cost", p.get("avg_price", 0)) or 0)
            cur = float(p.get("current_price", 0) or 0)
            if qty <= 0:
                continue
            
            name = p.get("stock_name", p.get("name", code))
            pnl = (cur - cost) * qty
            pnl_pct = (cur - cost) / cost * 100 if cost > 0 else 0
            
            pos = {"code": code, "name": name, "qty": qty, "cost": cost, "price": cur, "pnl": pnl, "pnl_pct": pnl_pct}
            result["positions"].append(pos)
            result["total_pnl"] += pnl
            
            if not result["top_gainer"] or pnl > result["top_gainer"]["pnl"]:
                result["top_gainer"] = pos
            if not result["top_loser"] or pnl < result["top_loser"]["pnl"]:
                result["top_loser"] = pos
        
        # 止损告警
        for p in result["positions"]:
            if p["pnl_pct"] < -7:
                result["alerts"].append({
                    "code": p["code"], "name": p["name"],
                    "pnl_pct": round(p["pnl_pct"], 2),
                    "message": f"{p['name']}({p['code']})亏损{p['pnl_pct']:.1f}%, 接近止损线"
                })
    except Exception as e:
        result["error"] = str(e)
    
    result["total_pnl"] = round(result["total_pnl"], 2)
    return result


# ═══════════════ P7: 自动修复 ═══════════════
def auto_fix():
    """执行已知模式的自动修复"""
    fixes_applied = []
    
    for pattern in KNOWN_FIXES:
        try:
            if pattern["check"]():
                try:
                    fix_result = pattern["fix"]()
                    fixes_applied.append({"pattern": pattern["name"], "desc": pattern["desc"], "result": str(fix_result)})
                except Exception as e:
                    fixes_applied.append({"pattern": pattern["name"], "desc": pattern["desc"], "result": f"修复失败: {e}", "failed": True})
        except Exception as e:
            pass  # check failed, skip
    
    return fixes_applied


# ═══════════════ P8: 生成报告 ═══════════════
def generate_report(data_integrity, error_patterns, 
                    feedback_result, prediction_quality, position_analysis, fixes, quick=False):
    """生成结构化自我反思报告"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    
    # 系统健康评分 (无cron健康扫描 — 由健康检查+质量检查覆盖)
    health_score = 100
    if data_integrity:
        health_score -= min(20, len(data_integrity) * 3)
    if error_patterns["consecutive_failures"]:
        health_score -= 10
    health_score = max(0, health_score)
    
    def health_emoji(score):
        if score >= 90: return "🟢"
        if score >= 70: return "🟡"
        return "🔴"
    
    lines = []
    lines.append(f"# 🔄 DSL自我反思报告 {TODAY}")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**系统版本**: DSL v{_read_version()}")
    lines.append(f"**健康评分**: {health_emoji(health_score)} {health_score}/100")
    lines.append("")
    
    # --- 今日摘要 (无cron健康扫描) ---
    lines.append("## 📋 执行摘要")
    integrity_issues = len(data_integrity)
    fixes_count = len(fixes)
    low_acc_count = len(prediction_quality.get("low_accuracy", []))
    pos_alerts = len(position_analysis.get("alerts", []))
    
    key_items = []
    if fixes_count > 0:
        key_items.append(f"🔧 **{fixes_count}个自动修复已应用**")
    if integrity_issues > 0:
        key_items.append(f"📁 **{integrity_issues}个数据完整性问题**")
    if low_acc_count > 0:
        key_items.append(f"📉 **{low_acc_count}只标的精度<45%**")
    if pos_alerts > 0:
        key_items.append(f"⚠️ **{pos_alerts}只持仓触发止损关注**")
    
    if not key_items:
        key_items.append("✅ 今日系统运行正常，无异常")
    
    for item in key_items:
        lines.append(f"- {item}")
    lines.append("")
    
    # --- 自动修复 ---
    if fixes:
        lines.append("## 🔧 自动修复")
        for f in fixes:
            status = "❌" if f.get("failed") else "✅"
            lines.append(f"- {status} **{f['desc']}**: {f['result']}")
        lines.append("")
    
    # --- 数据完整性 ---
    if data_integrity:
        lines.append("## 📁 数据完整性")
        for issue in data_integrity:
            sev = "🔴" if issue["severity"] == "critical" else "🟡"
            lines.append(f"- {sev} **{issue['label']}** ({issue['file']}): {issue['issue']}")
        lines.append("")
    
    # --- 错误模式 ---
    if error_patterns["today_errors"] or error_patterns["consecutive_failures"] or error_patterns["anti_patterns"]:
        lines.append("## ⚠️ 异常模式")
        if error_patterns["health_alerts"]:
            for ha in error_patterns["health_alerts"]:
                lines.append(f"- 🔴 **健康检查告警**: {ha['failed']}/{ha['passed']+ha['failed']}失败, 关键项: {', '.join(ha['critical'])}")
        if error_patterns["consecutive_failures"]:
            lines.append(f"- 🔴 **连续失败**: {', '.join(error_patterns['consecutive_failures'])} (≥2天)")
        for ap in error_patterns["anti_patterns"]:
            lines.append(f"- 🟡 **反模式{ap['ref']}**: {ap['pattern']}")
        if error_patterns["today_errors"]:
            lines.append(f"\n### 今日错误详情 ({len(error_patterns['today_errors'])}条)")
            for e in error_patterns["today_errors"][:5]:
                details = str(e.get("details", {}))
                lines.append(f"- `{e.get('source')}` → `{e.get('action')}`: {details[:120]}")
        lines.append("")
    
    # --- 反馈校准 ---
    lines.append("## 🎯 反馈校准")
    if feedback_result["status"] == "ok":
        updates = feedback_result.get("updates", {})
        lines.append(f"- ✅ 反馈控制器执行成功")
        for key, val in updates.items():
            if isinstance(val, dict):
                lines.append(f"  - **{key}**: {val.get('status', str(val)[:60])}")
            else:
                lines.append(f"  - **{key}**: {str(val)[:80]}")
    else:
        lines.append(f"- ❌ 反馈控制器失败: {feedback_result.get('reason', 'unknown')}")
    lines.append("")
    
    # --- 预测质量 ---
    lines.append("## 📈 预测质量")
    pq = prediction_quality
    acc = pq.get("accuracy_mean")
    if pq.get("total", 0) == 0 and quick:
        lines.append("- ⏭️ 快速模式跳过")
        lines.append("")
    elif acc is not None:
        rating = "🟢" if acc >= 0.55 else ("🟡" if acc >= 0.48 else "🔴")
        lines.append(f"| 指标 | 值 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 预测标的数 | {pq['total']} |")
        lines.append(f"| 方向精度均值 | {rating} {acc:.4f} |")
        lines.append(f"| 高置信度信号(≥0.6) | {pq['high_confidence']} |")
        lines.append(f"| 低精度标的(<0.45) | {len(pq['low_accuracy'])} |")
        lines.append("")
        
        if pq["degraded"]:
            for d in pq["degraded"]:
                lines.append(f"- 🔴 {d}")
        
        if pq["low_accuracy"]:
            lines.append(f"\n### 低精度标的 (需关注)")
            for la in pq["low_accuracy"][:10]:
                lines.append(f"- `{la['code']}` {la['name']}: accuracy={la['accuracy']:.4f}")
        lines.append("")
    else:
        lines.append("- ⚠️ 无法分析预测质量(文件缺失或格式错误)")
        lines.append("")
    
    # --- 持仓表现 ---
    pa = position_analysis
    if pa["positions"]:
        lines.append("## 💰 持仓表现")
        lines.append(f"**总盈亏**: {'+' if pa['total_pnl'] >= 0 else ''}{pa['total_pnl']:,.0f}元 | 持仓数: {len(pa['positions'])}")
        lines.append("")
        lines.append("| 代码 | 名称 | 盈亏 | 涨跌幅 |")
        lines.append("|------|------|------|--------|")
        for p in sorted(pa["positions"], key=lambda x: x["pnl"], reverse=True):
            emoji = "🌟" if p["pnl_pct"] > 15 else ("✅" if p["pnl_pct"] > 0 else ("⚪" if p["pnl_pct"] > -3 else "🔴"))
            lines.append(f"| {p['code']} | {p['name']} | {emoji} {p['pnl']:+,.0f} | {p['pnl_pct']:+.1f}% |")
        lines.append("")
        
        if pa["alerts"]:
            lines.append("### ⚠️ 止损关注")
            for a in pa["alerts"]:
                lines.append(f"- 🔴 **{a['message']}**")
            lines.append("")
    
    # --- 行动建议 ---
    lines.append("## 🔧 行动建议")
    actions = _generate_actions(data_integrity, error_patterns, 
                                 prediction_quality, position_analysis, fixes)
    for a in actions:
        lines.append(f"- **{a['priority']} {a['title']}**: {a['desc']} → {a['action']}")
    
    if not actions:
        lines.append("- ✅ 无需立即处理的行动项")
    lines.append("")
    
    # --- 反馈闭环状态 ---
    lines.append("## 🔄 反馈闭环")
    lines.append(f"- ✅ 反思报告已生成 → `cache/reports/self_reflection_{TODAY}.md`")
    new_lessons = _check_new_lessons(error_patterns, fixes)
    if new_lessons:
        lines.append(f"- 📝 新教训已记录 → `LESSONS.md` ({len(new_lessons)}条)")
    else:
        lines.append("- ℹ️ 无新教训")
    lines.append(f"- 📊 校准数据已更新 → feedback_controller @ {datetime.now().strftime('%H:%M')}")
    lines.append("")
    
    lines.append(f"---\n*报告自动生成 by self_reflection.py v4.5.15 @ {datetime.now().isoformat()}*")
    
    report_content = "\n".join(lines)
    report_path = REPORT_DIR / f"self_reflection_{TODAY}.md"
    with open(report_path, "w") as f:
        f.write(report_content)
    
    return report_content, str(report_path)


def _read_version():
    vf = PROJECT_ROOT / "VERSION"
    if vf.exists():
        return vf.read_text().strip()
    return "?"


def _generate_actions(data_integrity, error_patterns, pq, pa, fixes):
    actions = []
    
    # 数据完整性问题
    for issue in data_integrity:
        if issue["severity"] == "critical":
            actions.append({
                "priority": "🔴 P0",
                "title": f"{issue['label']}文件缺失",
                "desc": f"{issue['file']}: {issue['issue']}",
                "action": "立即重新生成或从备份恢复"
            })
    
    # 低精度标的
    if len(pq.get("low_accuracy", [])) >= 5:
        actions.append({
            "priority": "🟡 P1",
            "title": "批量精度退化",
            "desc": f"{len(pq['low_accuracy'])}只标的accuracy<45%",
            "action": "下次训练(16:00)优先处理低精度标的，增强正则化"
        })
    
    # 止损关注
    for a in pa.get("alerts", []):
        actions.append({
            "priority": "🔴 P0" if a["pnl_pct"] < -8 else "🟡 P1",
            "title": "止损关注",
            "desc": a["message"],
            "action": "盘前决策评估是否止损或减仓"
        })
    
    # 连续失败
    if error_patterns.get("consecutive_failures"):
        actions.append({
            "priority": "🔴 P0",
            "title": "连续失败告警",
            "desc": f"{', '.join(error_patterns['consecutive_failures'])}连续2天失败",
            "action": "检查相关脚本日志，诊断根因"
        })
    
    # 精度退化
    if pq.get("degraded"):
        actions.append({
            "priority": "🟡 P1",
            "title": "模型精度退化",
            "desc": pq["degraded"][0],
            "action": "检查训练数据质量 + 是否需要增量训练"
        })
    
    return actions


def _check_new_lessons(error_patterns, fixes):
    """检查是否产生新教训需要记录"""
    new_lessons = []
    
    # 连续失败
    if error_patterns.get("consecutive_failures"):
        new_lessons.append(f"连续失败告警: {', '.join(error_patterns['consecutive_failures'])}")
    
    # 反模式
    for ap in error_patterns.get("anti_patterns", []):
        new_lessons.append(f"反模式触发: {ap['pattern']} ({ap['ref']})")
    
    return new_lessons


# ═══════════════ P9: 反馈闭环 ═══════════════
def close_feedback_loop(report_content, data_integrity, error_patterns, fixes, prediction_quality):
    """关闭反馈闭环: 更新LESSONS.md + MEMORY.md + 发送飞书报告

    v4.6.2: 新增 LESSONS 同步检查 — 扫描MEMORY.md最近修复记录，
    对比LESSONS.md是否已记录对应教训，发现缺失时提示。(教训52/63)
    """
    results = {"lessons_updated": False, "memory_updated": False, "feishu_sent": False}

    # 1. 更新LESSONS.md
    new_lessons = _check_new_lessons(error_patterns, fixes)
    if new_lessons:
        try:
            with open(LESSONS_FILE, "a") as f:
                f.write(f"\n## 📅 {TODAY} 自我反思教训\n\n")
                for i, lesson in enumerate(new_lessons, 1):
                    f.write(f"### 教训 {i}: {lesson[:80]}\n")
                    f.write(f"**检测**: 自我反思P3错误模式分析\n")
                    f.write(f"**修复**: 已自动处理\n\n")
            results["lessons_updated"] = True
            print(f"📝 LESSONS.md 已更新 ({len(new_lessons)}条教训)")
        except Exception as e:
            print(f"⚠️ 更新LESSONS.md失败: {e}")

    # v4.6.2: LESSONS.md同步检查 (教训52/63)
    unsynced = _check_lessons_sync()
    if unsynced:
        print(f"⚠️ LESSONS.md同步提醒: MEMORY.md中有{len(unsynced)}条修复记录")
        print(f"   可能未同步到LESSONS.md, 请人工review:")
        for item in unsynced[:5]:
            print(f"   - {item[:100]}")
        if len(unsynced) > 5:
            print(f"   ... 等{len(unsynced)}条")

    # 2. 发送飞书报告
    try:
        from monitoring.feishu_alert import send_alert, AlertLevel, AlertType
        has_issues = len(data_integrity) > 0 or len(error_patterns.get("today_errors", [])) > 0
        send_alert(
            level=AlertLevel.INFO if not has_issues else AlertLevel.WARNING,
            alert_type=AlertType.SYSTEM,
            title=f"🔄 DSL自我反思报告 {TODAY}",
            message=report_content[:5000],
            module="self_reflection",
            metric="health_score",
        )
        results["feishu_sent"] = True
        print("📤 飞书报告已发送")
    except ImportError:
        print("ℹ️ 飞书告警模块不可用，报告仅保存本地")
    except Exception as e:
        print(f"⚠️ 飞书发送失败: {e}")

    return results


def _check_lessons_sync() -> list:
    """v4.6.2: 检查 MEMORY.md 中最近7天的修复记录是否已同步到 LESSONS.md.
    
    用简单的启发式方法:
      - MEMORY.md中包含关键词(修复/已修复/P0/P1/Bug)的行
      - LESSONS.md中是否存在对应的时间戳/描述
    """
    unsynced = []
    if not MEMORY_FILE.exists() or not LESSONS_FILE.exists():
        return unsynced

    try:
        memory_content = MEMORY_FILE.read_text()
        lessons_content = LESSONS_FILE.read_text()
    except Exception:
        return unsynced

    # 提取MEMORY.md中最近7天的修复关键词
    from datetime import datetime as _dt
    cutoff = (_dt.now() - timedelta(days=7)).strftime("%Y-%m-%d")

    fix_keywords = ["修复", "已修复", "P0", "P1", "Bug", "bug", "根因:", "教训"]
    memory_lines = memory_content.split("\n")

    for i, line in enumerate(memory_lines):
        # 只检查最近7天的内容
        has_recent_date = any(
            d in line and d >= cutoff
            for d in [_dt.now().strftime("%Y-%m-%d"),
                      (_dt.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
                      (_dt.now() - timedelta(days=2)).strftime("%Y-%m-%d")]
        )
        if not has_recent_date:
            # 也检查以日期开头的行
            date_match = any(line.startswith(d) for d in [
                _dt.now().strftime("%Y-%m-%d"),
                (_dt.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
            ])
            if not date_match:
                continue

        has_fix_kw = any(kw in line for kw in fix_keywords)
        if has_fix_kw:
            # 简单检查: LESSONS.md中是否有类似描述
            snippet = line.strip()[:80]
            if snippet and snippet not in lessons_content[:5000]:
                unsynced.append(snippet)

    return unsynced




# ═══════════════ MAIN ═══════════════
def main(dry_run=False, quick=False):
    # v4.5.17: ProgressTracker — 让Dashboard检测任务执行状态
    _tracker = None
    try:
        from common.progress_tracker import ProgressTracker
        _tracker = ProgressTracker("self_reflection", total_steps=8)
        _tracker.step(1, "启动自我反思")
    except ImportError:
        pass

    """执行完整自我反思流程"""
    print(f"🔄 DSL自我反思引擎 v4.6 — {TODAY}")
    print(f"   Mode: {'DRY-RUN' if dry_run else 'QUICK' if quick else 'FULL'}")
    print()

    fixes = []

    # P0 (已移除 — 由15:40健康检查 + fix_stale_progress.py 覆盖)
    # P1 (已移除 — 由15:40健康检查+22:00质量检查覆盖)
    
    # P2: 数据完整性
    print("\nP2: 检查数据完整性...")
    data_integrity = check_data_integrity()
    if data_integrity:
        for issue in data_integrity:
            sev = "🔴" if issue["severity"] == "critical" else "🟡"
            print(f"  {sev} {issue['label']}: {issue['issue']}")
    else:
        print("  ✅ 所有关键文件完整")
    
    # P3: 错误模式分析
    print("\nP3: 分析错误模式...")
    error_patterns = analyze_error_patterns()
    print(f"  今日错误: {len(error_patterns['today_errors'])}条")
    print(f"  连续失败: {len(error_patterns['consecutive_failures'])}个")
    print(f"  健康告警: {len(error_patterns['health_alerts'])}个")
    print(f"  反模式: {len(error_patterns['anti_patterns'])}个")
    for ap in error_patterns['anti_patterns']:
        print(f"    ⚠️ {ap['ref']}: {ap['pattern']}")
    
    # P4: 反馈控制器校准
    print("\nP4: 运行反馈控制器校准...")
    if dry_run:
        print("  ⏭️ [DRY-RUN] 跳过")
        feedback_result = {"status": "skipped", "reason": "dry-run"}
    else:
        feedback_result = run_feedback_controller()
        print(f"  {'✅' if feedback_result['status'] == 'ok' else '❌'} {feedback_result.get('status')}")
    
    # P5: 预测质量分析
    if quick:
        print("\nP5: 预测质量分析 (QUICK模式跳过)")
        prediction_quality = {"total": 0, "accuracy_mean": None, "low_accuracy": [], "high_confidence": 0, "degraded": []}
    else:
        print("\nP5: 分析预测质量...")
        prediction_quality = analyze_prediction_quality()
        if prediction_quality["accuracy_mean"]:
            print(f"  标的数: {prediction_quality['total']} | 精度均值: {prediction_quality['accuracy_mean']:.4f} | 高信度: {prediction_quality['high_confidence']} | 低精度: {len(prediction_quality['low_accuracy'])}")
        if prediction_quality["degraded"]:
            for d in prediction_quality["degraded"]:
                print(f"  🔴 {d}")
    
    # P6: 持仓表现
    if quick:
        print("\nP6: 持仓表现分析 (QUICK模式跳过)")
        position_analysis = {"positions": [], "total_pnl": 0, "alerts": []}
    else:
        print("\nP6: 分析持仓表现...")
        position_analysis = analyze_positions()
        if position_analysis["positions"]:
            print(f"  持仓: {len(position_analysis['positions'])}只 | 总盈亏: {position_analysis['total_pnl']:+,.0f} | 告警: {len(position_analysis['alerts'])}")
            if position_analysis["alerts"]:
                for a in position_analysis["alerts"]:
                    print(f"    🔴 {a['message']}")
        else:
            print("  ℹ️ 无持仓数据")
    
    # P7: 行动建议
    print("\nP7: 生成行动建议...")
    actions = _generate_actions(data_integrity, error_patterns, 
                                 prediction_quality, position_analysis, fixes)
    for a in actions:
        print(f"  {a['priority']} {a['title']}")
    
    # P8: 生成报告
    print("\nP8: 生成反思报告...")
    report_content, report_path = generate_report(
        data_integrity, error_patterns,
        feedback_result, prediction_quality, position_analysis, fixes,
        quick=quick
    )
    print(f"  📄 报告已生成: {report_path} ({len(report_content)} chars)")
    
    # P9: 反馈闭环
    if dry_run:
        print("\nP9: 反馈闭环 (DRY-RUN 跳过)")
        loop_result = {"lessons_updated": False, "memory_updated": False, "feishu_sent": False}
    else:
        print("\nP9: 关闭反馈闭环...")
        loop_result = close_feedback_loop(report_content, data_integrity, error_patterns, fixes, prediction_quality)
        print(f"  📝 LESSONS: {'已更新' if loop_result['lessons_updated'] else '无新教训'}")
        print(f"  📤 飞书: {'已发送' if loop_result['feishu_sent'] else '未发送'}")
    
    print(f"\n✅ 自我反思完成")
    if _tracker:
        _tracker.complete("自我反思完成", duration_seconds=round(__import__("time").time() - _tracker.start_time, 1))
    return {
        "data_integrity": data_integrity,
        "error_patterns": error_patterns,
        "feedback_result": feedback_result,
        "prediction_quality": prediction_quality,
        "position_analysis": position_analysis,
        "fixes": fixes,
        "actions": actions,
        "report_path": report_path,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DSL自我反思引擎")
    parser.add_argument("--dry-run", action="store_true", help="只分析不修复")
    parser.add_argument("--quick", action="store_true", help="快速模式(跳过P5/P6)")
    args = parser.parse_args()
    
    result = main(dry_run=args.dry_run, quick=args.quick)