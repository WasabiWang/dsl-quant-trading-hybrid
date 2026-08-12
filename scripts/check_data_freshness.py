#!/usr/bin/env python3
"""scripts/check_data_freshness.py — 数据新鲜度监控 v4.5.12 P2-15

检查关键数据文件的年龄，如果超过阈值则告警。
在 health_check 或开盘前 cron 中运行。

检查项:
  1. prediction_calibration.json — 预测是否在24h内生成
  2. paper_trading_ledger.json — 持仓是否在48h内更新
  3. master_stock_pool.yaml — 配置是否在7天内修改
  4. models/*/lightgbm.pkl — 模型是否在14天内训练
  5. planned_trades.json — 计划交易是否在20分钟内生成(盘前)
"""
import os, sys, json, time
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 新鲜度阈值(秒)
FRESHNESS_RULES = {
    "prediction_calibration.json": {"max_age": 86400, "label": "预测校准数据", "critical": True},
    "paper_trading_ledger.json": {"max_age": 172800, "label": "持仓数据", "critical": True},
    "master_stock_pool.yaml": {"max_age": 604800, "label": "股票池配置", "critical": False},
    "planned_trades.json": {"max_age": 1200, "label": "盘前交易计划", "critical": False},  # 20min
}

MODEL_MAX_AGE = 14 * 86400  # 14天
MODEL_DIR = os.path.join(PROJECT_ROOT, "models")


def _file_age(path: str) -> float:
    """返回文件龄(秒)。文件不存在返回inf。"""
    if not os.path.exists(path):
        return float("inf")
    return time.time() - os.path.getmtime(path)


def _age_str(age_sec: float) -> str:
    if age_sec >= 86400:
        return f"{age_sec/86400:.1f}d"
    elif age_sec >= 3600:
        return f"{age_sec/3600:.1f}h"
    else:
        return f"{age_sec/60:.0f}m"


def check_prediction_freshness(data_dir: str) -> list:
    """检查预测校准数据的新鲜度"""
    issues = []
    path = os.path.join(data_dir, "prediction_calibration.json")
    age = _file_age(path)
    rule = FRESHNESS_RULES["prediction_calibration.json"]
    if age > rule["max_age"]:
        issues.append({
            "file": "prediction_calibration.json",
            "age_sec": age,
            "age_str": _age_str(age),
            "max_age": rule["max_age"],
            "label": rule["label"],
            "critical": rule["critical"],
            "status": "STALE",
        })
    return issues


def check_portfolio_freshness(data_dir: str) -> list:
    """检查持仓数据新鲜度"""
    issues = []
    path = os.path.join(data_dir, "paper_trading_ledger.json")
    age = _file_age(path)
    rule = FRESHNESS_RULES["paper_trading_ledger.json"]
    if age > rule["max_age"]:
        issues.append({
            "file": "paper_trading_ledger.json",
            "age_sec": age,
            "age_str": _age_str(age),
            "max_age": rule["max_age"],
            "label": rule["label"],
            "critical": rule["critical"],
            "status": "STALE",
        })
    return issues


def check_config_freshness(config_dir: str) -> list:
    """检查配置文件新鲜度"""
    issues = []
    path = os.path.join(config_dir, "master_stock_pool.yaml")
    age = _file_age(path)
    rule = FRESHNESS_RULES["master_stock_pool.yaml"]
    if age > rule["max_age"]:
        issues.append({
            "file": "master_stock_pool.yaml",
            "age_sec": age,
            "age_str": _age_str(age),
            "max_age": rule["max_age"],
            "label": rule["label"],
            "critical": rule["critical"],
            "status": "STALE",
        })
    return issues


def check_model_freshness() -> list:
    """检查模型文件新鲜度"""
    issues = []
    if not os.path.exists(MODEL_DIR):
        return issues

    for code in os.listdir(MODEL_DIR):
        model_path = os.path.join(MODEL_DIR, code, "lightgbm.pkl")
        if not os.path.exists(model_path):
            continue
        age = _file_age(model_path)
        if age > MODEL_MAX_AGE:
            issues.append({
                "file": f"{code}/lightgbm.pkl",
                "age_sec": age,
                "age_str": _age_str(age),
                "max_age": MODEL_MAX_AGE,
                "label": f"模型 {code}",
                "critical": True,
                "status": "STALE",
            })
    return issues


def check_planned_trades_freshness(cache_dir: str) -> list:
    """检查盘前交易计划新鲜度(仅在开盘前15min内检查有实际意义)"""
    issues = []
    now = datetime.now()
    t = now.hour * 60 + now.minute
    # 仅09:00-09:35检查(盘前窗口)
    if not (540 <= t <= 575):
        return issues

    path = os.path.join(cache_dir, "planned_trades.json")
    age = _file_age(path)
    rule = FRESHNESS_RULES["planned_trades.json"]
    if age > rule["max_age"]:
        issues.append({
            "file": "planned_trades.json",
            "age_sec": age,
            "age_str": _age_str(age),
            "max_age": rule["max_age"],
            "label": rule["label"],
            "critical": rule["critical"],
            "status": "STALE",
        })
    return issues


def check_all(data_dir: str = None, config_dir: str = None, cache_dir: str = None) -> dict:
    """运行所有新鲜度检查

    Returns:
        {"ok": bool, "issues": [...], "summary": str}
    """
    if data_dir is None:
        data_dir = os.path.join(PROJECT_ROOT, "data")
    if config_dir is None:
        config_dir = os.path.join(PROJECT_ROOT, "config")
    if cache_dir is None:
        cache_dir = os.path.join(PROJECT_ROOT, "cache")

    all_issues = []
    all_issues.extend(check_prediction_freshness(data_dir))
    all_issues.extend(check_portfolio_freshness(data_dir))
    all_issues.extend(check_config_freshness(config_dir))
    all_issues.extend(check_model_freshness())
    all_issues.extend(check_planned_trades_freshness(cache_dir))

    critical = [i for i in all_issues if i.get("critical") and i["status"] == "STALE"]
    warnings = [i for i in all_issues if not i.get("critical") and i["status"] == "STALE"]

    if not all_issues:
        return {"ok": True, "issues": [], "summary": "🟢 全部数据新鲜"}

    summary_parts = []
    if critical:
        summary_parts.append(f"🔴 {len(critical)}个关键数据过期")
    if warnings:
        summary_parts.append(f"🟡 {len(warnings)}个非关键数据过期")

    return {
        "ok": len(critical) == 0,
        "issues": all_issues,
        "critical_count": len(critical),
        "warning_count": len(warnings),
        "summary": " | ".join(summary_parts) if summary_parts else "🟢 全部数据新鲜",
    }


if __name__ == "__main__":
    result = check_all()
    print(f"\n{'='*50}")
    print(f"  数据新鲜度检查")
    print(f"{'='*50}")
    print(f"  状态: {result['summary']}")
    print()

    for issue in result.get("issues", []):
        icon = "🔴" if issue.get("critical") else "🟡"
        print(f"  {icon} {issue['label']}: {issue['age_str']} / 阈值{_age_str(issue['max_age'])}")

    if not result.get("issues"):
        print("  ✅ 所有数据均在有效期内")

    sys.exit(0 if result.get("ok") else 1)
