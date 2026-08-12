#!/usr/bin/env python3
"""DSL系统部署校验 - 检查所有脚本和依赖是否就绪"""
import os
import sys
import importlib
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).parent.parent
VENV_PYTHON = BASE_DIR / ".venv/bin/python3"

# 必须存在的脚本
REQUIRED_SCRIPTS = [
    "scripts/health_check.py",
    "scripts/predictor_cron.py",
    "scripts/daily_sim_report_v3.py",
    "scripts/weekly_review.py",
    "scripts/pre_market_preparation.py",
    "scripts/backup.sh",
]

# 必须存在的核心模块
REQUIRED_MODULES = [
    ("dsl_engine", "run_backtest"),
    ("portfolio_backtest", "PortfolioBacktest"),
    ("safety_guard", "SafetyGuard"),
    ("strategy_monitor", "StrategyMonitor"),
    ("market_adapter", "MarketAdapter"),
]

# 必须安装的Python包
REQUIRED_PACKAGES = [
    "lightgbm", "xgboost", "joblib", "sklearn",
    "openai", "aiohttp", "backtrader", "akshare",
    "yfinance", "pandas", "numpy", "feedparser",
]

def check_scripts():
    """检查必需脚本是否存在"""
    results = []
    for script in REQUIRED_SCRIPTS:
        path = BASE_DIR / script
        exists = path.exists()
        results.append((script, exists))
    return results

def check_modules():
    """检查核心模块是否可导入"""
    sys.path.insert(0, str(BASE_DIR / "scripts"))
    sys.path.insert(0, str(Path.home() / ".agents/skills/alphaquant-backtest/dsl/engine"))
    results = []
    for mod_name, symbol in REQUIRED_MODULES:
        try:
            mod = importlib.import_module(mod_name)
            has_symbol = hasattr(mod, symbol)
            results.append((f"{mod_name}.{symbol}", has_symbol))
        except ImportError as e:
            results.append((f"{mod_name}.{symbol}", False))
    return results

def check_packages():
    """检查Python包是否安装"""
    results = []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            results.append((pkg, True))
        except ImportError:
            results.append((pkg, False))
    return results

def check_venv():
    """检查虚拟环境"""
    return VENV_PYTHON.exists()

def main():
    print("=" * 60)
    print(f"🔍 DSL系统部署校验 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    total = 0
    passed = 0
    
    # 1. 虚拟环境
    print("\n📦 虚拟环境")
    venv_ok = check_venv()
    total += 1
    if venv_ok:
        passed += 1
        print(f"  ✅ .venv 存在")
    else:
        print(f"  ❌ .venv 不存在!")
    
    # 2. 脚本
    print("\n📜 脚本文件")
    for name, ok in check_scripts():
        total += 1
        if ok:
            passed += 1
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} 缺失!")
    
    # 3. 核心模块
    print("\n🔧 核心模块")
    for name, ok in check_modules():
        total += 1
        if ok:
            passed += 1
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} 不可用")
    
    # 4. Python包
    print("\n📦 Python依赖")
    for name, ok in check_packages():
        total += 1
        if ok:
            passed += 1
            print(f"  ✅ {name}")
        else:
            print(f"  ❌ {name} 未安装")
    
    # 总结
    print("\n" + "=" * 60)
    pct = passed / total * 100 if total > 0 else 0
    print(f"📊 总计: {passed}/{total} 通过 ({pct:.0f}%)")
    if passed == total:
        print("✅ 系统部署完整，所有组件就绪")
    else:
        print(f"⚠️ {total - passed} 项未通过，需要修复")
    print("=" * 60)
    
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
