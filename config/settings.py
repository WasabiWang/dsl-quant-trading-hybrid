#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSL量化交易系统统一配置入口 v4.5.1
所有模块从此处读取API Key/URL/参数，避免分散维护
"""

import os
from pathlib import Path

# 项目根路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ==================== 系统版本 ====================

def _read_version():
    """从 VERSION 文件读取版本号（单一真相源）"""
    version_file = PROJECT_ROOT / "VERSION"
    if version_file.exists():
        for line in version_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                return line
    return "v0.0.0"

DSL_VERSION = _read_version()

# ==================== 数据源配置 ====================

# 迈瑞API（沪深行情）— P0: 敏感信息改从环境变量读取
MAIRUI_LICENCE = os.getenv("MAIRUI_LICENCE", "")
MAIRUI_BASE_URL = "https://api.mairuiapi.com"
MAIRUI_RATE_LIMIT_INTERVAL = 0.07  # 秒，适配包月1000次/分钟
MAIRUI_TIMEOUT = 15  # 超时秒数
MAIRUI_MAX_RETRIES = 3

# 东方财富数据源
EASTMONEY_ENABLED = False  # 默认关闭，按需启用

# AKShare 数据源
AKSHARE_ENABLED = False

# DeepSeek API
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

# ==================== 交易参数 ====================
# P0-FIX: 从constants.py统一导入，消除settings.py与constants.py的佣金/风控参数不一致
from config.constants import (
    COMMISSION_RATE, STAMP_TAX_RATE, MIN_COMMISSION, MIN_TRADE_UNIT,
    LIMIT_RATES, MAX_SINGLE_POSITION_PCT, MAX_TOTAL_POSITION_PCT,
    STOP_LOSS_PCT, DAILY_LOSS_LIMIT_PCT,
)

# 风控参数 (从constants统一, 保留dict格式供下游兼容)
RISK_CONFIG = {
    "max_position_pct": MAX_SINGLE_POSITION_PCT,
    "max_total_position": MAX_TOTAL_POSITION_PCT,
    "stop_loss_pct": STOP_LOSS_PCT,
    "take_profit_pct": 0.15,
    "trailing_stop_pct": 0.05,
    "volatility_lookback": 20,
    "max_consecutive_losses": 3,
    "cooldown_minutes": 60,
}

# ==================== 信号融合权重 ====================

DECISION_FUSION_WEIGHTS = {
    "technical": 0.3,
    "sentiment": 0.2,
    "macro": 0.3,
    "bullish": 0.1,
    "bearish": 0.1,  # bear_score为负值→正确拉低总分
}

# ==================== 缓存路径 ====================

CACHE_DIR = os.getenv("CACHE_DIR", str(PROJECT_ROOT / "cache"))
PRE_MARKET_CACHE = os.path.join(CACHE_DIR, "pre_market")
BACKTEST_CACHE = os.path.join(CACHE_DIR, "backtest")

# ==================== 模拟交易 ====================

PAPER_TRADING_DB = str(PROJECT_ROOT / "data" / "paper_trading.db")
PAPER_TRADING_INITIAL_CAPITAL = 1_000_000.0

# ==================== 路径辅助 ====================

def ensure_dirs():
    """确保所有缓存目录存在"""
    for path in [CACHE_DIR, PRE_MARKET_CACHE, BACKTEST_CACHE,
                 str(PROJECT_ROOT / "data")]:
        os.makedirs(path, exist_ok=True)
