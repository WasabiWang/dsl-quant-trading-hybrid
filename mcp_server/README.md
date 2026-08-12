# DSL Quant MCP Server

MCP Server providing AI agents (Claude Code, Cursor, etc.) with direct access to the DSL Quantitative Trading System.

## Install

```bash
cd mcp_server
pip install -e .
```

## Configure Claude Code

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "dsl-quant": {
      "command": "python",
      "args": ["-m", "dsl_mcp.server"],
      "cwd": "<project-root>"
    }
  }
}
```

## Available Tools (10)

| Tool | Category | Description |
|------|----------|-------------|
| `dsl_get_positions` | Trading | 当前持仓及盈亏 |
| `dsl_get_predictions` | Data | 最新预测 (可选按symbol过滤) |
| `dsl_get_system_status` | Data | 系统健康、信号、数据新鲜度 |
| `dsl_get_kline` | Data | K线数据 (最近N天) |
| `dsl_execute_trade` | Trading | 执行纸交易 (BUY/SELL) |
| `dsl_check_stop_losses` | Trading | 止损扫描 |
| `dsl_trigger_retrain` | Training | 触发单股重训练 |
| `dsl_get_model_quality` | Training | 模型精度和校准状态 |
| `dsl_get_retrain_queue` | Training | 重训练队列 |
| `dsl_get_audit_log` | System | 审计日志查询 |
