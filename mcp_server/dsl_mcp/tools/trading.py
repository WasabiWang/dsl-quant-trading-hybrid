"""Trading tools: positions, execute trades, stop-loss check."""

import json, os, sys

_DSL_ROOT = os.path.expanduser("~/.openclaw/workspace/dsl-quant-trading-hybrid")


def _ensure_dsl():
    if _DSL_ROOT not in sys.path:
        sys.path.insert(0, _DSL_ROOT)
    if os.getcwd() != _DSL_ROOT:
        os.chdir(_DSL_ROOT)


async def get_positions() -> str:
    """获取当前纸交易持仓列表及盈亏。"""
    _ensure_dsl()
    from scripts.paper_trader import PaperTrader
    t = PaperTrader()
    s = t.get_portfolio_summary()
    return json.dumps({
        "total_value": s["total_value"],
        "cash": s["current_cash"],
        "total_return_pct": s["total_return_pct"],
        "win_rate": s["win_rate"],
        "positions": [{
            "symbol": p["symbol"], "quantity": p["quantity"],
            "avg_cost": p["avg_cost"], "current_price": p["current_price"],
            "market_value": p["market_value"], "pnl": p["pnl"], "pnl_pct": p["pnl_pct"],
        } for p in s["positions"]],
    }, ensure_ascii=False, indent=2)


async def execute_trade(symbol: str, action: str, price: float,
                        quantity: int, reason: str = "MCP指令") -> str:
    """执行纸交易。action='BUY'或'SELL'。返回交易结果。"""
    _ensure_dsl()
    from scripts.paper_trader import PaperTrader
    t = PaperTrader()
    result = t.execute_trade("A", symbol, action.upper(), price, quantity,
                             reason=reason, order_id=f"mcp_{symbol}_{action}")
    return json.dumps(result, ensure_ascii=False, indent=2)


async def check_stop_losses() -> str:
    """扫描所有持仓止损条件。返回触发止损的持仓。"""
    _ensure_dsl()
    from scripts.paper_trader import PaperTrader
    t = PaperTrader()
    result = t.check_stop_losses()
    return json.dumps(result, ensure_ascii=False, indent=2)
