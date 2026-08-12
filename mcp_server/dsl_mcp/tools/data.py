"""Data tools: predictions, kline, quotes, market data."""

import json, os, sys

_DSL_ROOT = os.path.expanduser("~/.openclaw/workspace/dsl-quant-trading-hybrid")


def _ensure_dsl():
    if _DSL_ROOT not in sys.path:
        sys.path.insert(0, _DSL_ROOT)
    if os.getcwd() != _DSL_ROOT:
        os.chdir(_DSL_ROOT)


async def get_predictions(symbol: str = "") -> str:
    """获取最新预测数据。可选按股票代码过滤。"""
    _ensure_dsl()
    from web_dashboard.data_adapter import get_predictions
    data = get_predictions()
    preds = data["predictions"]
    if symbol:
        preds = [p for p in preds if p.get("symbol") == symbol]
    return json.dumps({
        "total": len(preds),
        "predictions": [{k: v for k, v in p.items()
                         if k in ("symbol", "name", "signal", "confidence",
                                  "predicted_return", "direction_accuracy",
                                  "tier", "source", "score")}
                        for p in preds]
    }, ensure_ascii=False, indent=2)


async def get_kline_data(symbol: str, days: int = 60) -> str:
    """获取股票K线数据。"""
    _ensure_dsl()
    from dsl_data_sdk_original import get_kline, normalize_symbol
    from datetime import datetime, timedelta
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    kline = get_kline(normalize_symbol(symbol), start, end)
    if not kline:
        return json.dumps({"error": f"No data for {symbol}"})
    # Return last 10 candles plus summary
    last10 = []
    for k in kline[-10:]:
        last10.append({
            "date": k.get("date", k.get("day", "")),
            "open": k.get("open"), "close": k.get("close"),
            "high": k.get("high"), "low": k.get("low"),
            "volume": k.get("volume"),
        })
    latest = kline[-1]
    return json.dumps({
        "symbol": symbol, "total_days": len(kline),
        "latest": {"close": latest.get("close"), "volume": latest.get("volume")},
        "recent": last10,
    }, ensure_ascii=False, indent=2)


async def get_system_status() -> str:
    """获取系统总体状态：健康、信号统计、数据新鲜度。"""
    _ensure_dsl()
    from web_dashboard.data_adapter import get_system_status
    s = get_system_status()
    return json.dumps({
        "health": s["health"],
        "trading_paused": s["health_trading_paused"],
        "signals": {"buy": s["signal_count_buy"], "sell": s["signal_count_sell"],
                     "hold": s["signal_count_hold"]},
        "accuracy": s["accuracy_rate"],
        "is_trading_day": s["is_trading_day"],
        "staleness": {k: {"stale": v["stale"], "age_h": v["age_hours"]}
                      for k, v in s.get("data_staleness", {}).items()},
        "version": s.get("version", "?"),
    }, ensure_ascii=False, indent=2)
