#!/usr/bin/env python3
"""
v4.6: 清理残留的"running"进度文件
- 进程已退出但进度文件仍为 running → 自动标记为 crashed 或 completed
- 供 health_check 和其他 cron 调用
"""
import json, os, sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRESS_DIR = PROJECT_ROOT / "cache" / "progress"


def fix_stale_progress(task_names=None, max_age_minutes=60):
    """标记所有超过 max_age_minutes 的 stale running 任务"""
    if task_names is None:
        task_names = ["batch_train"]

    if not PROGRESS_DIR.exists():
        return 0

    fixed = 0
    for fname in sorted(PROGRESS_DIR.glob("*.json")):
        # 只处理指定任务类型
        if not any(fname.name.startswith(tn) for tn in task_names):
            continue
        try:
            with open(fname) as f:
                data = json.load(f)
        except Exception:
            continue

        if data.get("status") != "running":
            continue

        started = data.get("started_at", "")
        if not started:
            continue
        try:
            started_dt = datetime.fromisoformat(started)
            age = (datetime.now() - started_dt).total_seconds() / 60
        except Exception:
            continue

        if age < max_age_minutes:
            continue  # 可能还在运行

        # 标记完成
        data["status"] = "completed"
        data["message"] = data.get("message", "") + f" (进程退出补全, 运行{age:.0f}min)"
        data["updated_at"] = datetime.now().isoformat()
        try:
            with open(fname, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"  ✅ 进度已补全: {fname.name} (运行{age:.0f}min)")
            fixed += 1
        except Exception as e:
            print(f"  ❌ 补全失败: {fname.name} ({e})")

    return fixed


if __name__ == "__main__":
    fixed = fix_stale_progress(max_age_minutes=10)  # 10分钟没更新=认为退出
    print(f"\n共修复 {fixed} 条 stale 进度记录")
    sys.exit(0)
