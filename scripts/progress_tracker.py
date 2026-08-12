#!/usr/bin/env python3
"""
DSL量化交易系统 - 进度跟踪模块
文件级进度跟踪（类比TradingAgents AsyncProgressTracker，无Redis依赖）

用法:
    from scripts.progress_tracker import update_task_progress
    update_task_progress("batch_predict", step=3, total=10, message="预测中...", status="running")
"""
import os, json, time
from datetime import datetime

# ── 路径配置 ──
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
PROGRESS_FILE = os.path.join(DATA_DIR, "task_progress.json")
CRON_STATUS_FILE = os.path.join(DATA_DIR, "cron_status.json")
LOCK_FILE = os.path.join(DATA_DIR, ".progress_lock")

os.makedirs(DATA_DIR, exist_ok=True)


def _read_progress() -> dict:
    """线程安全读取进度文件"""
    for _ in range(10):  # 最多重试10次
        try:
            if os.path.exists(PROGRESS_FILE):
                with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f) or {"tasks": [], "last_updated": ""}
            return {"tasks": [], "last_updated": ""}
        except json.JSONDecodeError:
            time.sleep(0.05)
    return {"tasks": [], "last_updated": ""}


def _write_progress(data: dict):
    """原子写入进度文件"""
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_FILE)


def update_task_progress(
    task_id: str,
    step: int = 0,
    total: int = 10,
    message: str = "",
    status: str = "running",
    details: dict = None
):
    """更新任务进度
    
    Args:
        task_id: 任务标识 (如 batch_predict, batch_train, pre_market_refresh)
        step: 当前步骤 (0-based)
        total: 总步骤数
        message: 进度消息
        status: running | completed | failed | skipped
        details: 附加数据 (如预测数量, 信号分布等)
    """
    data = _read_progress()
    tasks = data.get("tasks", [])

    # 查找或新建
    task = None
    for t in tasks:
        if t.get("task_id") == task_id:
            task = t
            break
    if task is None:
        task = {"task_id": task_id}
        tasks.append(task)

    task["step"] = step
    task["total"] = total
    task["message"] = message
    task["status"] = status
    task["progress_pct"] = round((step / max(total, 1)) * 100, 1)
    task["last_update"] = datetime.now().isoformat()
    task["timestamp"] = time.time()
    if details:
        task["details"] = details

    data["tasks"] = tasks
    data["last_updated"] = datetime.now().isoformat()
    _write_progress(data)
    return task


def mark_task_complete(task_id: str, message: str = "完成", details: dict = None):
    """标记任务完成"""
    return update_task_progress(task_id, step=10, total=10, message=message, status="completed", details=details)


def mark_task_failed(task_id: str, error: str = ""):
    """标记任务失败"""
    return update_task_progress(task_id, message=f"失败: {error}", status="failed")


def mark_task_skipped(task_id: str, reason: str = "节假日跳过"):
    """标记任务跳过"""
    return update_task_progress(task_id, message=reason, status="skipped")


def get_task_progress(task_id: str) -> dict:
    """获取单个任务进度"""
    data = _read_progress()
    for t in data.get("tasks", []):
        if t.get("task_id") == task_id:
            return t
    return {"task_id": task_id, "status": "unknown"}


def get_all_tasks() -> list:
    """获取所有任务进度"""
    data = _read_progress()
    return data.get("tasks", [])


# ── Cron状态管理 ──

def update_cron_status(cron_name: str, status: str, details: dict = None):
    """更新Cron任务状态（CRON_STATUS_FILE）"""
    data = {"cron_jobs": [], "last_update": ""}
    if os.path.exists(CRON_STATUS_FILE):
        try:
            with open(CRON_STATUS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {"cron_jobs": [], "last_update": ""}
        except:
            pass

    jobs = data.get("cron_jobs", [])
    found = False
    for j in jobs:
        if j.get("name") == cron_name:
            j["status"] = status
            j["last_run"] = datetime.now().isoformat()
            if details:
                j["details"] = details
            found = True
            break
    if not found:
        job = {"name": cron_name, "status": status, "last_run": datetime.now().isoformat()}
        if details:
            job["details"] = details
        jobs.append(job)

    data["cron_jobs"] = jobs
    data["last_update"] = datetime.now().isoformat()

    tmp = CRON_STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CRON_STATUS_FILE)


if __name__ == "__main__":
    # 测试
    update_task_progress("test_task", step=5, total=10, message="测试中...", status="running")
    print(json.dumps(get_task_progress("test_task"), ensure_ascii=False, indent=2))
    mark_task_complete("test_task", "测试完成")
    print(json.dumps(get_all_tasks(), ensure_ascii=False, indent=2))
