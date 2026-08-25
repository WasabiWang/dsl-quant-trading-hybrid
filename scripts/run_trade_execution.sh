#!/bin/bash
# v4.7.0 P1-3: 交易执行包装脚本 — 硬编码venv+绝对路径, 防止cron agent篡改命令
# 背景: 09:30 cron间歇性Exec failed, 根因是agent把命令改成裸python3/相对路径
# 用法: bash /abs/path/run_trade_execution.sh
set -e
PROJECT="/Users/jameswang/.openclaw/workspace/dsl-quant-trading-hybrid"
cd "$PROJECT"
exec "$PROJECT/.venv/bin/python3" "$PROJECT/scripts/execute_scheduled_trades.py"
