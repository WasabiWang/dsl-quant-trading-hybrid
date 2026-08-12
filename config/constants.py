#!/usr/bin/env python3
"""
DSL量化交易系统 — 统一常量定义
消除代码中的魔法数字，统一管理所有硬编码常量
创建日期: 2026-04-26 (P0审计改进)
"""

# ==================== 交易参数 ====================
# v4.5.12 fix: 统一佣金为万2.5（个人投资者真实水平），消除paper_trader/backtest/execute之间的不一致
COMMISSION_RATE = 0.00025          # 佣金万2.5 (个人投资者真实水平)
STAMP_TAX_RATE = 0.001             # 印花税千分之一 (仅卖出)
MIN_COMMISSION = 5                  # 最低佣金5元
MIN_TRADE_UNIT = 100                # A股最小交易单位100股 (主板/创业板，科创板见 get_min_trade_unit)
MIN_TRADE_UNIT_STAR = 200           # 科创板200股


def get_min_trade_unit(symbol: str) -> int:
    """按市场返回最小交易单位"""
    return MIN_TRADE_UNIT_STAR if symbol and symbol.startswith("688") else MIN_TRADE_UNIT

# ==================== 滑点与冲击成本 ====================
# v4.5.12 fix: 分级滑点（按市值），消除3bps/10bps/20bps多套标准的不一致
SLIPPAGE_BPS_LARGE_CAP = 3         # 大市值(>=500亿) 3bp = 0.03%
SLIPPAGE_BPS_MID_CAP = 5           # 中市值(100-500亿) 5bp = 0.05%
SLIPPAGE_BPS_SMALL_CAP = 10        # 小市值(<100亿) 10bp = 0.10%
SLIPPAGE_BPS_DEFAULT = 5           # 默认滑点 5bp (市值未知时)
SLIPPAGE_BPS = SLIPPAGE_BPS_DEFAULT  # 向后兼容别名
SLIPPAGE_VOLUME_FACTOR = 0.1        # 成交量冲击系数: 交易量/日均成交量每10%增加1bp
MIN_VOLUME_RATIO_FOR_FILL = 3.0     # 订单量需≤日均成交量的1/3才能全部成交

def get_slippage_bps(market_cap: float = None) -> int:
    """按市值返回滑点(bp)，未知市值用默认5bp"""
    if market_cap is None:
        return SLIPPAGE_BPS_DEFAULT
    cap_yi = market_cap / 1e8  # 转换为亿
    if cap_yi >= 500:
        return SLIPPAGE_BPS_LARGE_CAP
    elif cap_yi >= 100:
        return SLIPPAGE_BPS_MID_CAP
    else:
        return SLIPPAGE_BPS_SMALL_CAP

# ==================== 风控参数 ====================
MAX_SINGLE_POSITION_PCT = 0.30      # 单票最大仓位30%
MAX_TOTAL_POSITION_PCT = 0.80       # 总仓位上限80%
STOP_LOSS_PCT = -0.08               # 止损线 -8% (主板默认，与risk_manager统一)
DAILY_LOSS_LIMIT_PCT = 0.02         # 日亏损限额 2% (P1新增)
CIRCUIT_BREAKER_DRAWDOWN = 0.05     # 单日回撤熔断阈值 5%
CIRCUIT_BREAKER_CONSECUTIVE_FAILS = 3  # 连续失败交易熔断次数
MAX_DAILY_TRADE_COUNT = 10          # 单日最大交易次数
CIRCUIT_BREAKER_24H = 24            # 回撤熔断暂停小时数
CIRCUIT_BREAKER_1H = 1              # 连续失败熔断暂停小时数
CIRCUIT_BREAKER_2H = 2              # 频率熔断暂停小时数

# ==================== 涨跌停幅度(按市场) ====================
LIMIT_RATES = {
    "A": 0.10,       # 主板 ±10%
    "GEM": 0.20,     # 创业板 ±20%
    "STAR": 0.20,    # 科创板 ±20%
    "ST": 0.05,      # ST股 ±5%
    "BJ": 0.30,      # 北交所 ±30%
}

# P1-6: ST股票缓存 — 避免每个get_market_type调用都做网络请求
_st_cache = {}  # {symbol: bool}


def get_market_type(symbol: str, is_st: bool = None) -> str:
    """按代码前缀返回市场类型，支持ST检测覆盖

    P1-6 fix: 新增is_st参数。当已知ST状态时传入is_st=True，
    否则通过_st_cache查找。ST检测需外部填充缓存。
    """
    code = symbol.lstrip("'\"")
    # ST优先判断
    if is_st is None:
        is_st = _st_cache.get(code, _st_cache.get(symbol, False))
    if is_st:
        return "ST"
    if code.startswith("688"):
        return "STAR"
    elif code.startswith("300") or code.startswith("301"):
        return "GEM"
    elif code.startswith("60") or code.startswith("00"):
        return "A"
    elif code.startswith("8"):
        return "BJ"
    return "A"


def set_st_status(symbol: str, is_st: bool):
    """P1-6: 填充ST缓存，供所有get_market_type调用使用"""
    code = symbol.lstrip("'\"")
    _st_cache[code] = is_st
    _st_cache[symbol] = is_st


def is_st_stock(symbol: str) -> bool:
    """P1-6: 查询ST缓存"""
    code = symbol.lstrip("'\"")
    return _st_cache.get(code, _st_cache.get(symbol, False))

# ==================== 模型训练 ====================
MIN_TRAIN_SAMPLES = 300             # 最小训练样本数
MIN_ACCURACY_THRESHOLD = 0.53       # 最低方向准确率阈值
MIN_R2_THRESHOLD = 0.0              # 最低R²阈值 (R²<0表示不如瞎猜)
TEST_SPLIT_RATIO = 0.2              # 测试集比例
LOOKBACK_DAYS = 60                  # 模型回看天数
MAX_FEATURE_TO_SAMPLE_RATIO = 0.1   # 特征数不超过样本数的10%

# ==================== 缓存 ====================
PRICE_CACHE_MAX_AGE = 300           # 价格缓存有效期 (秒)
KLINE_CACHE_MAX_AGE = 3600          # K线缓存有效期 (秒)

# ==================== 股票池层级分配 (v4.5.3c) ====================
# 与 master_stock_pool.yaml 的 tier 保持一致:
#   bluechip(蓝筹)/core(核心)/growth(成长)/cyclical(周期观察)/flex(灵活观察)

# 层级 → 仓位分配因子 (按层次决定单票最大权重)
TIER_POSITION_FACTORS = {
    "bluechip": 0.20,   # 蓝筹: 单票最多占总仓位20%
    "core":     0.15,   # 核心: 15%
    "growth":   0.12,   # 成长: 12%
    "cyclical": 0.05,   # 周期观察: 5% (仅监控)
    "flex":     0.05,   # 灵活观察: 5% (仅监控)
}

# 观察层级 (不发信号)
OBSERVATION_TIERS = {"cyclical", "flex"}

# ==================== 回测 ====================
BACKTEST_RISK_FREE_RATE = 0.02      # 无风险利率 2%
BACKTEST_TRADING_DAYS = 252         # 年化交易日数

# ==================== A股交易时段 ====================
# 集合竞价: 09:15-09:25 | 连续竞价(上午): 09:30-11:30 | 午休: 11:30-13:00 | 连续竞价(下午): 13:00-15:00
MARKET_OPEN_MORNING = 9 * 60 + 30   # 09:30 = 570 min
MARKET_CLOSE_MORNING = 11 * 60 + 30  # 11:30 = 690 min
MARKET_OPEN_AFTERNOON = 13 * 60      # 13:00 = 780 min
MARKET_CLOSE_AFTERNOON = 15 * 60     # 15:00 = 900 min


def is_lunch_break(now=None) -> bool:
    """判断当前是否为A股午休时段 (11:30-13:00)

    Args:
        now: datetime对象，默认当前时间
    Returns:
        True 如果在午休时段
    """
    if now is None:
        from datetime import datetime as _dt
        now = _dt.now()
    t = now.hour * 60 + now.minute
    return MARKET_CLOSE_MORNING <= t < MARKET_OPEN_AFTERNOON


def is_trading_session(now=None) -> bool:
    """判断当前是否在A股交易时段内（排除午休）

    包含集合竞价时段 (09:15-09:25)。
    Returns:
        True 如果在交易时段内
    """
    if now is None:
        from datetime import datetime as _dt
        now = _dt.now()
    t = now.hour * 60 + now.minute
    # 集合竞价开始 09:15=555min, 收盘 15:00=900min
    COLLECTION_START = 9 * 60 + 15   # 09:15
    return COLLECTION_START <= t < MARKET_CLOSE_AFTERNOON and not (MARKET_CLOSE_MORNING <= t < MARKET_OPEN_AFTERNOON)
