#!/usr/bin/env python3
"""
DSL v4.5.6 — Cron 任务执行日志记录器
用法:
  python3 scripts/log_task.py <task_id> <status> [message]
  
示例:
  python3 scripts/log_task.py pre_market_plan completed "生成了19笔交易预案"
  python3 scripts/log_task.py blackswan_review completed "扫描发现3个事件, severity=5"

输出: data/task_logs/<task_id>/YYYY-MM-DD_HHMMSS.json
"""
import os, sys, json, argparse
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "data", "task_logs")


def log_task(task_id: str, status: str = "completed", message: str = "", detail: dict = None):
    """记录任务执行日志"""
    ts = datetime.now()
    date_str = ts.strftime("%Y%m%d")
    time_str = ts.strftime("%H%M%S")
    entry = {
        "task_id": task_id,
        "status": status,
        "message": message or "",
        "started_at": ts.isoformat(),
        "completed_at": ts.isoformat(),
        "detail": detail or {},
    }
    
    # 保存
    task_dir = os.path.join(LOG_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    fname = f"{date_str}_{time_str}.json"
    fpath = os.path.join(task_dir, fname)
    
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)
    
    print(f"📝 任务日志已记录: {task_id} → {fpath}")
    return fpath


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cron任务执行日志记录")
    parser.add_argument("task_id", help="任务ID (如 pre_market_plan)")
    parser.add_argument("--status", default="completed", help="状态 (completed/failed/skipped)")
    parser.add_argument("--message", default="", help="执行摘要信息")
    parser.add_argument("--detail", type=json.loads, help="详细数据JSON")
    args = parser.parse_args()
    
    log_task(args.task_id, args.status, args.message, args.detail)
