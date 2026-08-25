#!/usr/bin/env python3
"""
DSL v4.5.12 robustness_guard.py — 鲁棒性守卫模块

今天暴露的脆弱点 → 对应的守卫措施:
  1. 测试删生产数据    → Guard 1: 文件完整性预检
  2. Progress数据源不一致→ Guard 2: 跨数据源一致性校验
  3. daily_predict被删除 → Guard 3: 关键文件兜底恢复
  4. 模型格式双轨制     → Guard 4: 模型池自愈加载
  5. SDK动态加载     → Guard 5: SDK可用性+降级检查
  6. 信号全hold    → Guard 6: 信号活性监测
  7. 精度字段缺失      → Guard 7: 必填字段存在性校验
  8. 配置漂移          → Guard 8: 配置一致性自检
"""

import json, os, sys, glob, yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).parent.parent

# ═══════════════════════════════════════════
# Guard 1: 关键文件完整性预检
# ═══════════════════════════════════════════

CRITICAL_FILES = [
    ("cache/daily_predict.json", "预测数据", True, 24),   # (路径,名称,必须存在,最大年龄小时)
    ("confidence_data/prediction_calibration.json", "校准数据", True, 48),
    ("config/master_stock_pool.yaml", "股票池", True, 168),
    ("config/adaptive_params.yaml", "自适应参数", True, 48),
    ("data/circuit_breaker.json", "熔断器状态", True, 72),
    ("data/retrain_queue.json", "重训队列", False, 168),
    ("data/paper_trading.db", "交易数据库", False, 72),
]

def check_file_integrity() -> List[dict]:
    """检查所有关键文件的完整性和新鲜度

    Returns:
        [{"file": str, "status": "ok|missing|stale|corrupt", "detail": str}, ...]
    """
    results = []
    for relpath, name, required, max_age_h in CRITICAL_FILES:
        fullpath = PROJECT_ROOT / relpath
        check = {"file": relpath, "name": name, "status": "ok", "detail": ""}
        
        if not fullpath.exists():
            check["status"] = "missing" if required else "stale"
            check["detail"] = f"{name}不存在"
            if required:
                check["detail"] += " (关键文件!)"
            results.append(check)
            continue
        
        # 检查文件是否可解析
        if relpath.endswith(".json"):
            try:
                with open(fullpath) as f:
                    json.load(f)
            except (json.JSONDecodeError, Exception) as e:
                check["status"] = "corrupt"
                check["detail"] = f"JSON解析失败: {e}"
                results.append(check)
                continue
        elif relpath.endswith(".yaml"):
            try:
                with open(fullpath) as f:
                    yaml.safe_load(f)
            except Exception as e:
                check["status"] = "corrupt"
                check["detail"] = f"YAML解析失败: {e}"
                results.append(check)
                continue
        
        # 检查新鲜度（周末/非交易日阈值翻倍，避免误报）
        if max_age_h > 0:
            mtime = os.path.getmtime(str(fullpath))
            age = (datetime.now().timestamp() - mtime) / 3600
            effective_max = max_age_h * 2 if datetime.now().weekday() >= 5 else max_age_h
            if age > effective_max:
                check["status"] = "stale"
                check["detail"] = f"数据过期 ({age:.0f}h > {effective_max}h)"
        
        results.append(check)
    
    return results


# ═══════════════════════════════════════════
# Guard 2: 跨数据源一致性校验
# ═══════════════════════════════════════════

def check_cross_source_consistency() -> dict:
    """校验多个数据源之间的状态一致性"""
    issues = []
    
    try:
        # 2a. daily_predict vs prediction_calibration 标的数量一致性
        dp_path = PROJECT_ROOT / "cache" / "daily_predict.json"
        cal_path = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"
        
        if dp_path.exists() and cal_path.exists():
            with open(dp_path) as f:
                dp = json.load(f)
            with open(cal_path) as f:
                cal = json.load(f)
            
            dp_stocks = {p["symbol"] for p in dp.get("predictions", [])}
            cal_stocks = set(cal.get("stock_accuracy", {}).keys())
            
            # dp有但cal没有
            missing_in_cal = dp_stocks - cal_stocks
            if missing_in_cal:
                issues.append(f"{len(missing_in_cal)}只标的在预测中但不在校准中: {list(missing_in_cal)[:5]}")
            
            # cal有但dp没有
            missing_in_dp = cal_stocks - dp_stocks
            if missing_in_dp:
                issues.append(f"{len(missing_in_dp)}只标的在校准中但不在预测中: {list(missing_in_dp)[:5]}")
    except Exception as e:
        issues.append(f"校准一致性检查异常: {e}")
    
    try:
        # 2b. position_ratio 字段一致性
        adaptive_path = PROJECT_ROOT / "config" / "adaptive_params.yaml"
        if adaptive_path.exists():
            with open(adaptive_path) as f:
                ap = yaml.safe_load(f)
            risk = ap.get("risk", {})
            bs_active = risk.get("black_swan_active", False)
            bs_ratio = risk.get("black_swan_position_ratio", 1.0)
            if bs_active and bs_ratio >= 0.95:
                issues.append(f"黑天鹅active但position_ratio={bs_ratio:.0%} (可能未正确更新)")
    except Exception as e:
        issues.append(f"自适应参数检查异常: {e}")
    
    try:
        # 2c. ProgressTracker数据源一致性
        progress_dir = PROJECT_ROOT / "cache" / "progress"
        task_progress = PROJECT_ROOT / "data" / "task_progress.json"
        if progress_dir.exists() and task_progress.exists():
            pf = list(progress_dir.glob("*.json"))
            if pf:
                latest_pt = max(pf, key=os.path.getmtime)
                latest_progress = os.path.getmtime(str(latest_pt))
                latest_task = os.path.getmtime(str(task_progress))
                if latest_progress > latest_task + 3600:
                    issues.append("ProgressTracker数据比task_progress.json新>1h (数据源不同步)")
    except Exception as e:
        pass
    
    return {"consistent": len(issues) == 0, "issues": issues}


# ═══════════════════════════════════════════
# Guard 3: 关键文件兜底恢复
# ═══════════════════════════════════════════

def recover_daily_predict() -> dict:
    """从校准数据兜底恢复 daily_predict.json"""
    dp_path = PROJECT_ROOT / "cache" / "daily_predict.json"
    cal_path = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"
    
    if dp_path.exists():
        try:
            with open(dp_path) as f:
                dp = json.load(f)
            if dp.get("predictions") and len(dp["predictions"]) > 10:
                return {"status": "ok", "count": len(dp["predictions"])}
        except Exception:
            pass
    
    # 恢复
    if not cal_path.exists():
        return {"status": "failed", "error": "校准数据也不存在，无法恢复"}
    
    try:
        with open(cal_path) as f:
            cal = json.load(f)
        
        sa = cal.get("stock_accuracy", {})
        predictions = []
        for sym, data in sa.items():
            predictions.append({
                "symbol": sym,
                "name": data.get("name", sym),
                "signal": "hold",
                "confidence": round(data.get("last_accuracy", 0.5), 4),
                "direction_accuracy": round(data.get("last_accuracy", 0.5), 4),
                "data_quality": "auto_recovered",
                "tier": "core",
            })
        
        recovered = {
            "predict_date": datetime.now().strftime("%Y-%m-%d"),
            "timestamp": datetime.now().isoformat(),
            "predictions": predictions,
            "confidence_threshold": 0.58,
            "total_stocks": len(predictions),
            "source": "auto_recovered_from_calibration",
            "recovery_time": datetime.now().isoformat(),
        }
        
        with open(dp_path, "w") as f:
            json.dump(recovered, f, indent=2, ensure_ascii=False)
        
        return {"status": "recovered", "count": len(predictions)}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ═══════════════════════════════════════════
# Guard 4: 模型池自愈加载
# ═══════════════════════════════════════════

def check_model_pool_health() -> dict:
    """检查模型池健康状态"""
    models_dir = PROJECT_ROOT / "models"
    pool_dir = models_dir / "pool"
    
    result = {
        "total_models": 0,
        "v3_models": 0,
        "pool_models": 0,
        "with_classifier": 0,
        "corrupt_models": 0,
        "issues": [],
    }
    
    # 扫描v3子目录格式
    if models_dir.exists():
        for entry in sorted(models_dir.iterdir()):
            if not entry.is_dir() or entry.name in ("pool", "_cold_archive", "compatible_lgb", "ml"):
                continue
            result["total_models"] += 1
            
            lgb_path = entry / "lightgbm.pkl"
            clf_path = entry / "lightgbm_clf.pkl"
            
            if lgb_path.exists():
                result["v3_models"] += 1
                if clf_path.exists():
                    result["with_classifier"] += 1
                else:
                    result["issues"].append(f"{entry.name}: 缺分类器模型(lightgbm_clf.pkl)")
            else:
                # v3格式可能存为 lightgbm_5d.pkl 等形式
                variant_pkls = list(entry.glob("lightgbm_*.pkl"))
                if variant_pkls:
                    result["v3_models"] += 1  # 有variant算v3格式
                elif not list(entry.glob("*.pkl")):
                    result["corrupt_models"] += 1
                    result["issues"].append(f"{entry.name}: 无模型文件")
    
    # 扫描pool格式 (旧格式)
    if pool_dir.exists():
        try:
            import joblib
            _has_joblib = True
        except ImportError:
            _has_joblib = False
            result["issues"].append("pool检查跳过: joblib未安装(需venv)")
        
        if _has_joblib:
            for p in pool_dir.glob("*_lgb_*.pkl"):
                result["pool_models"] += 1
                try:
                    _ = joblib.load(str(p))
                except Exception:
                    result["corrupt_models"] += 1
                    result["issues"].append(f"pool/{p.name}: 加载失败")
    
    return result


# ═══════════════════════════════════════════
# Guard 5: SDK可用性检查
# ═══════════════════════════════════════════

def check_sdk_availability() -> dict:
    """检查SDK各数据源可用性"""
    sources = {}
    
    # 测试dashboard API
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:8888/api/version")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        sources["dashboard"] = {"available": True, "version": data.get("version", "?")}
    except Exception as e:
        sources["dashboard"] = {"available": False, "error": str(e)[:100]}
    
    # 测试麦蕊API
    try:
        from config.mairui_api_config import get_stock_real
        result = get_stock_real("000001")
        sources["mairui"] = {"available": result is not None}
    except Exception as e:
        sources["mairui"] = {"available": False, "error": str(e)[:100]}
    
    return sources


# ═══════════════════════════════════════════
# Guard 6: 信号活性监测
# ═══════════════════════════════════════════

def check_signal_activity() -> dict:
    """检查信号是否异常(全hold/全buy/无变化)"""
    dp_path = PROJECT_ROOT / "cache" / "daily_predict.json"
    if not dp_path.exists():
        return {"active": False, "reason": "daily_predict.json不存在"}
    
    try:
        with open(dp_path) as f:
            dp = json.load(f)
        
        predictions = dp.get("predictions", [])
        if not predictions:
            return {"active": False, "reason": "预测数据为空"}
        
        signals = {}
        for p in predictions:
            sig = p.get("signal", "hold")
            signals[sig] = signals.get(sig, 0) + 1
        
        total = len(predictions)
        all_hold = signals.get("hold", 0) == total
        all_buy = signals.get("buy", 0) == total
        all_sell = signals.get("sell", 0) == total
        
        active = not (all_hold or all_buy or all_sell)
        issues = []
        if all_hold:
            issues.append("全部信号为hold (可能预测数据损坏或降级)")
        if all_buy:
            issues.append("全部信号为buy (可能是模型过拟合)")
        if all_sell:
            issues.append("全部信号为sell (可能是市场崩盘)")
        
        return {
            "active": active,
            "issues": issues,
            "signals": signals,
            "total": total,
        }
    except Exception as e:
        return {"active": False, "reason": str(e)[:200]}


# ═══════════════════════════════════════════
# Guard 7: 必填字段存在性校验
# ═══════════════════════════════════════════

REQUIRED_PREDICTION_FIELDS = [
    "symbol", "name", "signal", "confidence", "direction_accuracy",
]

REQUIRED_CALIBRATION_FIELDS = [
    "last_accuracy", "accuracies", "name"
]

def validate_prediction_data() -> dict:
    """校验预测数据的必填字段"""
    dp_path = PROJECT_ROOT / "cache" / "daily_predict.json"
    if not dp_path.exists():
        return {"valid": False, "error": "daily_predict.json不存在"}
    
    try:
        with open(dp_path) as f:
            dp = json.load(f)
        
        predictions = dp.get("predictions", [])
        issues = []
        missing_fields = {}
        
        for p in predictions:
            sym = p.get("symbol", "?")
            for field in REQUIRED_PREDICTION_FIELDS:
                if field not in p or p[field] is None:
                    missing_fields[field] = missing_fields.get(field, 0) + 1
                    if len(issues) < 5:
                        issues.append(f"{sym}: 缺{field}")
        
        return {
            "valid": len(issues) == 0,
            "issues": issues[:10],
            "missing_field_counts": missing_fields,
        }
    except Exception as e:
        return {"valid": False, "error": str(e)[:200]}


# ═══════════════════════════════════════════
# Guard 8: 配置一致性自检
# ═══════════════════════════════════════════

def check_config_consistency() -> dict:
    """检查关键配置文件的一致性"""
    issues = []
    
    try:
        # 8a. VERSION文件 vs dsl_data_sdk.py
        ver_path = PROJECT_ROOT / "VERSION"
        if ver_path.exists():
            with open(ver_path) as f:
                ver = f.read().strip().split("\n")[0].replace("4.5.7", "")
            # 检查是否有版本号引用不一致
            sdk_path = PROJECT_ROOT / "dsl_data_sdk.py"
            if sdk_path.exists():
                with open(sdk_path) as f:
                    sdk_content = f.read()
                import re
                sdk_versions = set(re.findall(r"4\.5\.\d", sdk_content))
                if len(sdk_versions) > 1:
                    issues.append(f"dsl_data_sdk.py含多个版本号引用: {sdk_versions}")
    except Exception as e:
        issues.append(f"版本检查失败: {e}")
    
    try:
        # 8b. 双股票池一致性 (仅检查都已加载)
        master = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
        stock = PROJECT_ROOT / "config" / "stock_pool.yaml"
        if master.exists() and stock.exists():
            with open(master) as f:
                mp = yaml.safe_load(f)
            with open(stock) as f:
                sp = yaml.safe_load(f)
            m_count = len(mp.get("master_pool", mp.get("stocks", [])))
            s_data = sp.get("stock_pool", sp.get("stocks", sp.get("master_pool", [])))
            s_count = len(s_data) if isinstance(s_data, list) else 0
            if s_count > 0 and abs(m_count - s_count) > 10:
                issues.append(f"双股票池标的数据量差异大: master={m_count} stock={s_count}")
    except Exception as e:
        issues.append(f"股票池一致性检查失败: {e}")
    
    return {"consistent": len(issues) == 0, "issues": issues}


# ═══════════════════════════════════════════
# 全量守卫: 一键运行所有检查
# ═══════════════════════════════════════════

def run_all_guards() -> dict:
    """运行所有鲁棒性守卫, 返回综合健康报告"""
    results = {
        "timestamp": datetime.now().isoformat(),
        "guards": {},
        "overall": "healthy",
        "critical_issues": [],
        "warnings": [],
    }
    
    # Guard 1
    integrity = check_file_integrity()
    results["guards"]["file_integrity"] = {
        "total": len(integrity),
        "ok": sum(1 for c in integrity if c["status"] == "ok"),
        "issues": [c for c in integrity if c["status"] != "ok"],
    }
    for c in integrity:
        if c["status"] == "missing":
            results["critical_issues"].append(f"关键文件缺失: {c['file']}")
        elif c["status"] == "corrupt":
            results["critical_issues"].append(f"关键文件损坏: {c['file']}")
        elif c["status"] == "stale":
            results["warnings"].append(f"数据过期: {c['file']} - {c['detail']}")
    
    # Guard 2
    cross = check_cross_source_consistency()
    results["guards"]["cross_source"] = cross
    
    # Guard 3: 只在需要时恢复
    dp_status = "ok"
    dp_path = PROJECT_ROOT / "cache" / "daily_predict.json"
    if not dp_path.exists():
        recovery = recover_daily_predict()
        results["guards"]["auto_recovery"] = recovery
        if recovery["status"] == "recovered":
            dp_status = "recovered"
            results["warnings"].append(f"daily_predict.json已自动恢复 ({recovery['count']}条)")
        else:
            results["critical_issues"].append(f"daily_predict.json恢复失败: {recovery.get('error','?')}")
    
    # Guard 4
    model_health = check_model_pool_health()
    results["guards"]["model_pool"] = model_health
    if model_health["corrupt_models"] > 0:
        results["warnings"].append(f"{model_health['corrupt_models']}个模型文件损坏")
    if model_health["issues"]:
        for issue in model_health["issues"][:5]:
            results["warnings"].append(f"模型问题: {issue}")
    
    # Guard 5
    sdk_status = check_sdk_availability()
    results["guards"]["sdk_availability"] = sdk_status
    for name, info in sdk_status.items():
        if not info.get("available", False):
            results["warnings"].append(f"SDK源不可用: {name}")
    
    # Guard 6
    signal_status = check_signal_activity()
    results["guards"]["signal_activity"] = signal_status
    if not signal_status.get("active", False):
        results["critical_issues"].append(signal_status.get("reason", "信号异常"))
        for issue in signal_status.get("issues", []):
            results["critical_issues"].append(issue)
    
    # Guard 7
    field_check = validate_prediction_data()
    results["guards"]["field_validation"] = field_check
    if not field_check.get("valid", True):
        results["warnings"].append(f"预测数据缺字段: {field_check.get('missing_field_counts',{})}")
    
    # Guard 8
    config_check = check_config_consistency()
    results["guards"]["config_consistency"] = config_check
    for issue in config_check.get("issues", []):
        results["warnings"].append(issue)
    
    # Guard 9 (v4.6.8): 原子写入合规性检查
    atomic_check = check_atomic_write_compliance()
    results["guards"]["atomic_write"] = atomic_check
    if atomic_check.get("files_missing_backup"):
        results["warnings"].append(f"{len(atomic_check['files_missing_backup'])}个关键文件缺.last_good备份")
    
    # Guard 10 (v4.6.8): 跨脚本写冲突检测
    conflict_check = check_write_conflicts()
    results["guards"]["write_conflicts"] = conflict_check
    if conflict_check.get("conflicts"):
        results["warnings"].append(f"检测到{len(conflict_check['conflicts'])}个潜在写冲突")
    
    # Guard 11 (v4.6.8): 快照完整性检查
    snapshot_check = check_snapshot_health()
    results["guards"]["snapshot_health"] = snapshot_check
    if not snapshot_check.get("healthy", True):
        results["warnings"].append("快照系统异常: " + snapshot_check.get("detail", ""))
    
    # Guard 12 (v4.6.8): 数据Schema合规性检查
    schema_check = check_schema_compliance()
    results["guards"]["schema_compliance"] = schema_check
    for issue in schema_check.get("violations", []):
        results["warnings"].append(f"Schema违规: {issue}")
    
    # 综合评级
    if results["critical_issues"]:
        results["overall"] = "critical"
    elif results["warnings"]:
        results["overall"] = "degraded"
    
    return results


# ═══════════════════════════════════════════
# Guard 9 (v4.6.8): 原子写入合规性检查
# ═══════════════════════════════════════════

CRITICAL_WRITABLE_FILES = [
    "cache/daily_predict.json",
    "data/retrain_queue.json",
    "data/circuit_breaker.json",
    "confidence_data/prediction_calibration.json",
    "confidence_data/confidence_calibration.json",
    "confidence_data/degraded_models.json",
]


def check_atomic_write_compliance() -> dict:
    """检查关键文件是否有 .last_good 备份 (原子写入副产物)"""
    files_missing_backup = []
    for relpath in CRITICAL_WRITABLE_FILES:
        fullpath = PROJECT_ROOT / relpath
        backup = fullpath.with_suffix(fullpath.suffix + '.last_good')
        if fullpath.exists() and not backup.exists():
            files_missing_backup.append(relpath)
    return {
        "checked": len(CRITICAL_WRITABLE_FILES),
        "files_missing_backup": files_missing_backup,
        "compliance_pct": round(100 * (1 - len(files_missing_backup) / max(1, len(CRITICAL_WRITABLE_FILES)))),
    }


# ═══════════════════════════════════════════
# Guard 10 (v4.6.8): 跨脚本写冲突检测
# ═══════════════════════════════════════════

def check_write_conflicts() -> dict:
    """检测不同cron任务同时写入同一文件的风险"""
    conflicts = []
    try:
        cron_status_path = PROJECT_ROOT / "data" / "cron_status.json"
        if not cron_status_path.exists():
            return {"conflicts": [], "note": "cron_status.json不存在"}
        with open(cron_status_path) as f:
            cron_data = json.load(f)
        jobs = cron_data.get("jobs", [])
        schedules = []
        for j in jobs:
            if not j.get("enabled", True):
                continue
            expr = j.get("schedule", {}).get("expr", "")
            if not expr:
                continue
            parts = expr.split()
            if len(parts) >= 2:
                try:
                    schedules.append({
                        "name": j.get("name", "")[:40],
                        "minute": int(parts[0]),
                        "hour": int(parts[1]),
                    })
                except ValueError:
                    continue
        for i in range(len(schedules)):
            for j2 in range(i + 1, len(schedules)):
                a, b = schedules[i], schedules[j2]
                if a["hour"] == b["hour"] and abs(a["minute"] - b["minute"]) <= 5:
                    conflicts.append(
                        f"{a['name']} ({a['hour']:02d}:{a['minute']:02d}) \u2194 "
                        f"{b['name']} ({b['hour']:02d}:{b['minute']:02d})"
                    )
    except Exception as e:
        return {"conflicts": [], "error": str(e)}
    return {"conflicts": conflicts, "risk_level": "HIGH" if len(conflicts) > 2 else "LOW"}


# ═══════════════════════════════════════════
# Guard 11 (v4.6.8): 快照完整性检查
# ═══════════════════════════════════════════

def check_snapshot_health() -> dict:
    """检查快照系统是否正常工作"""
    snapshot_dir = PROJECT_ROOT / "snapshot"
    if not snapshot_dir.exists():
        return {"healthy": True, "detail": "快照目录不存在 (首次运行)", "snapshot_count": 0}
    snapshots = [d for d in snapshot_dir.iterdir() if d.is_dir() and (d / "_manifest.json").exists()]
    if len(snapshots) == 0:
        return {"healthy": False, "detail": "快照目录存在但无有效快照", "snapshot_count": 0}
    latest_manifest = None
    for d in sorted(snapshots, reverse=True):
        try:
            with open(d / "_manifest.json") as f:
                latest_manifest = json.load(f)
            break
        except Exception:
            continue
    if not latest_manifest:
        return {"healthy": False, "detail": "所有快照manifest损坏", "snapshot_count": len(snapshots)}
    return {
        "healthy": True,
        "snapshot_count": len(snapshots),
        "latest": latest_manifest.get("timestamp", "unknown"),
        "latest_id": latest_manifest.get("id", "unknown"),
    }


# ═══════════════════════════════════════════
# Guard 12 (v4.6.8): 数据Schema合规性检查
# ═══════════════════════════════════════════

def check_schema_compliance() -> dict:
    """检查关键数据文件的结构是否符合Schema预期"""
    violations = []
    try:
        from core.pollution_detector import EXPECTED_SCHEMAS
        for schema_path, schema in EXPECTED_SCHEMAS.items():
            fullpath = PROJECT_ROOT / schema_path
            if not fullpath.exists():
                continue
            try:
                if schema_path.endswith('.json'):
                    with open(fullpath) as f:
                        data = json.load(f)
                elif schema_path.endswith('.yaml'):
                    with open(fullpath) as f:
                        data = yaml.safe_load(f)
                else:
                    continue
            except Exception as e:
                violations.append(f"{schema_path}: 解析失败 ({e})")
                continue
            if isinstance(data, dict):
                missing = [k for k in schema.get("required_keys", []) if k not in data]
                if missing:
                    violations.append(f"{schema_path}: 缺字段 {missing}")
    except ImportError:
        pass
    return {"violations": violations, "compliant": len(violations) == 0}


# ═══════════════════════════════════════════
# CLI入口
# ═══════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("🛡️ DSL v4.5.12 鲁棒性守卫")
    print("=" * 60)
    
    report = run_all_guards()
    
    print(f"\n📊 综合评级: {report['overall'].upper()}")
    print(f"   时间: {report['timestamp']}")
    
    if report["critical_issues"]:
        print(f"\n🔴 严重问题 ({len(report['critical_issues'])}):")
        for i in report["critical_issues"]:
            print(f"   • {i}")
    
    if report["warnings"]:
        print(f"\n🟡 警告 ({len(report['warnings'])}):")
        for w in report["warnings"][:10]:
            print(f"   • {w}")
    
    print(f"\n📋 检查项:")
    for name, result in report["guards"].items():
        if isinstance(result, dict):
            if "ok" in result:
                print(f"   {'✅' if result.get('ok',0)==result.get('total',0) else '⚠️'} {name}: {result.get('ok',0)}/{result.get('total',0)} ok")
            elif "consistent" in result:
                print(f"   {'✅' if result['consistent'] else '⚠️'} {name}: {'一致' if result['consistent'] else '不一致'}")
            elif "active" in result:
                print(f"   {'✅' if result['active'] else '❌'} {name}: {'正常' if result['active'] else '异常'}")
            elif "valid" in result:
                print(f"   {'✅' if result['valid'] else '⚠️'} {name}: {'通过' if result['valid'] else '不通过'}")
            elif "available" in result:
                print(f"   {'✅' if result.get('available',False) else '❌'} {name}")
            elif "total_models" in result:
                print(f"   ℹ️  {name}: {result['total_models']}个模型 (损坏{result['corrupt_models']})")
    
    if not report["critical_issues"] and not report["warnings"]:
        print("\n✅ 系统鲁棒性检查全部通过")
