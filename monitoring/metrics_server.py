#!/usr/bin/env python3
"""
Prometheus metrics exporter for DSL‑Quant.
- DAG 成功/失败计数（已实现）
- 业务关键指标（新增）
    - trades_executed_total：累计执行的交易次数
    - trades_success_total：成功完成的交易次数
    - rs_threshold_hits_total：RS 达到阈值的次数
    - stop_loss_trigger_total：触发止损的次数
    - take_profit_trigger_total：触发止盈的次数
- 通过 start_http_server(8000) 暴露 /metrics
"""

from prometheus_client import start_http_server, Counter, Gauge
import time

# 1️⃣ DAG 统计

dag_success = Counter("dsl_dag_success_total", "Number of successful DAG runs")
ndag_failure = Counter("dsl_dag_failure_total", "Number of failed DAG runs")
dag_last_run = Gauge("dsl_dag_last_run_timestamp", "Unix timestamp of the last DAG execution")

# 2️⃣ 业务关键指标（业务层面）
trades_executed = Counter("dsl_trades_executed_total", "Total number of trade executions (including failures)")
trades_success = Counter("dsl_trades_success_total", "Number of successful trades (filled orders)")
rs_threshold_hits = Counter("dsl_rs_threshold_hits_total", "Count of times RS crossed the adaptive percentile threshold")
stop_loss_trigger = Counter("dsl_stop_loss_trigger_total", "Count of stop‑loss events triggered")
take_profit_trigger = Counter("dsl_take_profit_trigger_total", "Count of dynamic take‑profit events triggered")

# 3️⃣ 常规指标（脚本任务统计）
# 这里保留原有 task_success / task_failure（在 run_dag.py 中使用）


def start():
    """启动 HTTP 端口 8000，Prometheus 抓取。"""
    start_http_server(8000, addr="0.0.0.0")
    print("[Prometheus] Metrics server listening on :8000")
    while True:
        time.sleep(30)  # Keep process alive

if __name__ == "__main__":
    start()
