#!/usr/bin/env python3
"""
DSL v4.6.x — 市场体制识别器 (生产版)
从 backtrader 迁移到独立模块，用于每日生产流水线

功能:
1. 牛市/熊市/震荡市判定 (MA200 + ADX)
2. 波动率评估 (ATR 20日归一化)
3. 指数间一致性确认 (上证+沪深300+创业板)
4. 输出: regime + volatility_state + 建议position_ratio调整

数据源: 新浪K线API (与 black_swan_optimized/data_adapter 复用)
"""
import os, sys, json, time, math
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'black_swan_optimized'))

# 复用已有的新浪K线获取函数
try:
    from data_adapter import get_sina_kline
except ImportError:
    # 独立版fallback
    import requests as _req

    def get_sina_kline(symbol: str, start_date: str, end_date: Optional[str] = None, max_len: int = 1023):
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        headers = {'Referer': 'https://finance.sina.com.cn'}
        url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
               f'CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=0&datalen={max_len}')
        try:
            import json as _json
            r = _req.get(url, headers=headers, timeout=15)
            data = _json.loads(r.text)
            prices, dates = [], []
            for d in data:
                ds = d['day']
                if start_date <= ds <= end_date:
                    try:
                        close = float(d['close'])
                        if close > 0:
                            prices.append(close)
                            dates.append(ds)
                    except (ValueError, KeyError):
                        continue
            return prices, dates
        except Exception:
            return [], []


# 指数映射
REGIME_INDICES = {
    "上证指数": "sh000001",
    "沪深300": "sh000300",
    "创业板指": "sz399006",
}

# 参数
TREND_MA_PERIOD = 200
ADX_PERIOD = 14
ADX_THRESHOLD = 25
ATR_PERIOD = 20

# 波动率阈值
VOLATILITY_THRESHOLDS = {
    "low": 0.15,       # ATR/价格 < 15% → 低波
    "normal": 0.30,    # 15%~30% → 正常
    # > 30% → 高波
}

# 体制→建议仓位调整因子
REGIME_POSITION_ADJUST = {
    "BULL_RUN":  1.0,    # 牛市中满仓
    "BULL_WEAK": 0.8,    # 弱牛市减仓20%
    "SIDEWAYS":  0.6,    # 震荡市半仓
    "BEAR_WEAK": 0.4,    # 弱熊市保守
    "BEAR_CRASH": 0.2,   # 熊市清仓
}


def compute_adx(prices: list, period: int = ADX_PERIOD) -> float:
    """计算ADX (平均趋向指数) — 简化版，用价格变化代替TR"""
    if len(prices) < period * 2:
        return 0.0

    # 简化ADX: 用连续N日涨跌幅标准差反映趋势强度
    changes = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
    if not changes:
        return 0.0

    # 取最近 period 个变化的标准差作为趋势强度代理
    recent = changes[-period:]
    import statistics
    std = statistics.stdev(recent) if len(recent) > 1 else 0
    # 归一化到0-100 (与ADX范围对齐)
    adx = min(100, std * 1000)
    return adx


def compute_sma(prices: list, period: int) -> float:
    """计算简单移动平均"""
    if len(prices) < period:
        return prices[-1] if prices else 0
    return sum(prices[-period:]) / period


def compute_atr(prices: list, period: int = ATR_PERIOD) -> float:
    """计算ATR (平均真实波幅) — 简化版用收盘价变化"""
    if len(prices) < period + 1:
        return 0.0
    ranges = []
    for i in range(1, min(period + 1, len(prices))):
        ranges.append(abs(prices[-i] - prices[-i - 1]))
    return sum(ranges) / len(ranges) if ranges else 0.0


def detect_regime(prices: list) -> Dict:
    """
    单一指数的体制识别

    Returns:
        {"regime": str, "adx": float, "trend_strength": str,
         "is_bull": bool, "volatility": str, "atr_ratio": float}
    """
    if not prices or len(prices) < TREND_MA_PERIOD + ADX_PERIOD:
        return {"regime": "UNKNOWN", "reason": "数据不足",
                "adx": 0, "is_bull": None, "volatility": "unknown", "atr_ratio": 0}

    close = prices[-1]
    ma200 = compute_sma(prices, TREND_MA_PERIOD)
    adx = compute_adx(prices, ADX_PERIOD)

    is_bull = close > ma200
    trend_strength = "STRONG" if adx > ADX_THRESHOLD else "WEAK"

    # 波动率
    atr = compute_atr(prices, ATR_PERIOD)
    atr_ratio = atr / close if close > 0 else 0
    if atr_ratio < VOLATILITY_THRESHOLDS["low"]:
        volatility = "low"
    elif atr_ratio < VOLATILITY_THRESHOLDS["normal"]:
        volatility = "normal"
    else:
        volatility = "high"

    # 综合判定
    if is_bull and trend_strength == "STRONG":
        regime = "BULL_RUN"
    elif is_bull and trend_strength == "WEAK":
        regime = "BULL_WEAK"
    elif not is_bull and trend_strength == "STRONG":
        regime = "BEAR_CRASH"
    elif not is_bull and trend_strength == "WEAK":
        regime = "BEAR_WEAK"
    else:
        regime = "SIDEWAYS"

    return {
        "regime": regime,
        "adx": round(adx, 1),
        "trend_strength": trend_strength,
        "is_bull": is_bull,
        "volatility": volatility,
        "atr_ratio": round(atr_ratio, 4),
        "close": round(close, 2),
        "ma200": round(ma200, 2),
        "pct_from_ma200": round((close / ma200 - 1) * 100, 2),
    }


def get_market_regime(start_date: str = "2023-06-01") -> Dict:
    """
    获取综合市场体制判断 (多指数一致确认)

    Returns:
        {
            "regime_summary": str,        # BULL_RUN / BEAR_CRASH / SIDEWAYS
            "consensus": float,            # 0.0~1.0 各指数判定一致性
            "position_adjust": float,      # 建议仓位调整因子 (0.2~1.0)
            "indices": {index_name: regime_result},
            "overall_volatility": str,
            "position_ratio_adjustment": float,  # 叠加到现有position_ratio的乘数
            "timestamp": str,
        }
    """
    results = {}
    for name, code in REGIME_INDICES.items():
        prices, dates = get_sina_kline(code, start_date)
        if prices and len(prices) >= TREND_MA_PERIOD + ADX_PERIOD:
            results[name] = detect_regime(prices)
        else:
            results[name] = {"regime": "UNKNOWN", "reason": f"数据不足({len(prices) if prices else 0})"}

    # 一致性计数 (剔除UNKNOWN)
    valid = [r for r in results.values() if r.get("regime") != "UNKNOWN"]
    regime_counts = {}
    for r in valid:
        reg = r["regime"]
        regime_counts[reg] = regime_counts.get(reg, 0) + 1

    if not valid:
        return {
            "regime_summary": "UNKNOWN",
            "consensus": 0,
            "position_adjust": 0.5,
            "indices": results,
            "overall_volatility": "unknown",
            "position_ratio_adjustment": 0.5,
            "timestamp": datetime.now().isoformat(),
        }

    # 多数表决
    majority_regime = max(regime_counts, key=regime_counts.get)
    consensus = regime_counts[majority_regime] / len(valid)

    # 波动率:取所有指数的最高波动率
    vols = [r.get("volatility", "normal") for r in valid]
    if "high" in vols:
        overall_vol = "high"
    elif "normal" in vols:
        overall_vol = "normal"
    else:
        overall_vol = "low"

    # 获取仓位调整因子
    base_adjust = REGIME_POSITION_ADJUST.get(majority_regime, 0.5)
    # 高波动时额外收紧20%
    if overall_vol == "high":
        base_adjust *= 0.8
    # 一致性低(<0.5)时保守折中
    if consensus < 0.5:
        base_adjust = min(base_adjust, 0.5)

    return {
        "regime_summary": majority_regime,
        "consensus": round(consensus, 2),
        "position_adjust": round(base_adjust, 2),
        "indices": results,
        "overall_volatility": overall_vol,
        "regime_counts": regime_counts,
        "position_ratio_adjustment": round(base_adjust, 2),
        "timestamp": datetime.now().isoformat(),
    }


def run_detection(cache_path: Optional[str] = None) -> Dict:
    """
    运行市场体制检测并缓存结果

    Args:
        cache_path: 缓存文件路径, 默认 None→cache/market_regime.json

    Returns:
        检测结果字典
    """
    result = get_market_regime()

    # 缓存
    if cache_path is None:
        cache_dir = os.path.join(PROJECT_ROOT, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = os.path.join(cache_dir, "market_regime.json")

    with open(cache_path, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def load_regime(cache_path: Optional[str] = None) -> Optional[Dict]:
    """从缓存加载最新市场体制"""
    if cache_path is None:
        cache_path = os.path.join(PROJECT_ROOT, "cache", "market_regime.json")
    try:
        with open(cache_path) as f:
            data = json.load(f)
        # 检查是否过期 (>24h)
        ts = data.get("timestamp", "")
        if ts:
            cached_time = datetime.fromisoformat(ts)
            if (datetime.now() - cached_time).total_seconds() > 86400:
                return None  # 过期
        return data
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return None


def get_adjusted_position_ratio(current_ratio: float) -> Dict:
    """
    根据市场体制调整position_ratio

    Args:
        current_ratio: 当前的position_ratio (来自adaptive_params)

    Returns:
        {"original": float, "adjusted": float, "regime": str, "adjustment": str}
    """
    regime = load_regime()
    if regime is None:
        regime = run_detection()

    adjust = regime.get("position_ratio_adjustment", 1.0)
    adjusted = round(current_ratio * adjust, 2)

    return {
        "original": current_ratio,
        "adjusted": adjusted,
        "regime": regime.get("regime_summary", "UNKNOWN"),
        "adjustment": f"市场{regime.get('regime_summary', '?')} × 因子{adjust:.2f} = {adjusted:.2f}",
        "volatility": regime.get("overall_volatility", "unknown"),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="市场体制识别器")
    parser.add_argument("--run", action="store_true", help="运行检测并缓存")
    parser.add_argument("--load", action="store_true", help="从缓存加载")
    parser.add_argument("--adjust", type=float, default=None, help="输入当前position_ratio并输出调整值")
    args = parser.parse_args()

    if args.run:
        result = run_detection()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.load:
        result = load_regime()
        if result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("❌ 缓存过期或不存在, 请先运行 --run")
    elif args.adjust is not None:
        result = get_adjusted_position_ratio(args.adjust)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        # 默认: 运行+输出
        result = run_detection()
        r = result.get("regime_summary", "?")
        v = result.get("overall_volatility", "?")
        c = result.get("consensus", 0)
        a = result.get("position_adjust", 0)
        print(f"\n📊 市场体制: {r} | 波动: {v} | 一致性: {c:.0%} | 建议仓位调整因子: {a:.2f}")
        for name, info in result.get("indices", {}).items():
            reg = info.get("regime", "?")
            vol = info.get("volatility", "?")
            pct = info.get("pct_from_ma200", "?")
            print(f"  {name}: {reg:12s} | 波动={vol:6s} | MA200偏离={pct}")
