#!/usr/bin/env python3
"""DSL Quant MCP Server — v0.1.0

AI Agent interface to the DSL Quantitative Trading System.
Exposes 10 tools for data, trading, and training operations.

Usage:
    pip install dsl-quant-mcp
    dsl-mcp

Or directly:
    python -m dsl_mcp.server

MCP Client config (claude_desktop_config.json / .claude/mcp.json):
{
  "dsl-quant": {
    "command": "python",
    "args": ["-m", "dsl_mcp.server"],
    "cwd": "/path/to/dsl-quant-trading-hybrid"
  }
}
"""

import os, sys

# Ensure DSL root is on path
_DSL_ROOT = os.path.expanduser("~/.openclaw/workspace/dsl-quant-trading-hybrid")
if _DSL_ROOT not in sys.path:
    sys.path.insert(0, _DSL_ROOT)


def main():
    """MCP Server entry point — uses stdio transport."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import Tool, TextContent
        import asyncio
        HAS_MCP = True
    except ImportError:
        print("❌ mcp package not installed. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("dsl-quant")

    # ── Tool registrations ──

    @server.tool()
    async def dsl_get_positions() -> str:
        """获取当前纸交易持仓及盈亏。"""
        from dsl_mcp.tools.trading import get_positions
        return await get_positions()

    @server.tool()
    async def dsl_get_predictions(symbol: str = "") -> str:
        """获取最新预测数据。可选按symbol过滤(如'688525')。"""
        from dsl_mcp.tools.data import get_predictions
        return await get_predictions(symbol)

    @server.tool()
    async def dsl_get_system_status() -> str:
        """获取系统健康状态、信号分布、数据新鲜度。"""
        from dsl_mcp.tools.data import get_system_status
        return await get_system_status()

    @server.tool()
    async def dsl_execute_trade(symbol: str, action: str, price: float,
                                quantity: int, reason: str = "MCP指令") -> str:
        """执行纸交易。action='BUY'或'SELL'。"""
        from dsl_mcp.tools.trading import execute_trade
        return await execute_trade(symbol, action, price, quantity, reason)

    @server.tool()
    async def dsl_check_stop_losses() -> str:
        """扫描所有持仓止损条件。"""
        from dsl_mcp.tools.trading import check_stop_losses
        return await check_stop_losses()

    @server.tool()
    async def dsl_trigger_retrain(symbol: str) -> str:
        """触发单只股票模型重训练。"""
        from dsl_mcp.tools.training import trigger_retrain
        return await trigger_retrain(symbol)

    @server.tool()
    async def dsl_get_model_quality(symbol: str = "") -> str:
        """获取模型精度、校准状态和重训建议。"""
        from dsl_mcp.tools.training import get_model_quality
        return await get_model_quality(symbol)

    @server.tool()
    async def dsl_get_kline(symbol: str, days: int = 60) -> str:
        """获取股票K线数据（最近N天）。"""
        from dsl_mcp.tools.data import get_kline_data
        return await get_kline_data(symbol, days)

    @server.tool()
    async def dsl_get_retrain_queue() -> str:
        """获取重训练队列状态。"""
        from dsl_mcp.tools.training import get_retrain_queue
        return await get_retrain_queue()

    @server.tool()
    async def dsl_get_audit_log(hours: int = 24) -> str:
        """查询审计日志（最近N小时）。"""
        import json
        from common.audit import query, get_summary
        logs = query(since_hours=hours)
        summary = get_summary(since_hours=hours)
        return json.dumps({"logs": logs[:50], "summary": summary},
                          ensure_ascii=False, indent=2)

    # ── Run ──
    async def run():
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
