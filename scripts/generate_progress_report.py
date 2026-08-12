#!/usr/bin/env python3
"""
DSL进度报告生成器
1. 运行 openclaw cron list --json → data/cron_status.json
2. 合并 cache/progress/*.json → data/task_progress.json
3. 可被cron调度定期执行
"""
import os, json, subprocess, sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
PROGRESS_DIR = os.path.join(CACHE_DIR, "progress")
CRON_STATUS_FILE = os.path.join(DATA_DIR, "cron_status.json")
TASK_PROGRESS_FILE = os.path.join(DATA_DIR, "task_progress.json")


def generate_cron_status():
    """运行 openclaw cron list --json 并保存"""
    try:
        result = subprocess.run(
            ["openclaw", "cron", "list", "--json"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            with open(CRON_STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ cron_status.json 已更新 ({len(data.get('cron_jobs', data.get('jobs', [])))} 个任务)")
            return True
        else:
            print(f"⚠️  openclaw cron list 返回非零: {result.returncode}")
            print(f"   stderr: {result.stderr[:200]}")
    except FileNotFoundError:
        print("⚠️  openclaw 命令未找到，跳过cron状态")
    except subprocess.TimeoutExpired:
        print("⚠️  openclaw cron list 超时")
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON解析失败: {e}")
    except Exception as e:
        print(f"⚠️  无法获取cron状态: {e}")
    return False


def merge_progress_files():
    """合并所有 cache/progress/*.json → data/task_progress.json"""
    tasks = []
    if not os.path.exists(PROGRESS_DIR):
        print("⚠️  cache/progress/ 目录不存在")
        os.makedirs(PROGRESS_DIR, exist_ok=True)
        # 写一个空的task_progress.json
        empty = {"tasks": [], "last_updated": datetime.now().isoformat()}
        with open(TASK_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(empty, f, ensure_ascii=False, indent=2)
        return False

    progress_files = sorted(
        [f for f in os.listdir(PROGRESS_DIR) if f.endswith(".json")],
        reverse=True
    )

    if not progress_files:
        print("⚠️  cache/progress/ 中没有进度文件")
        empty = {"tasks": [], "last_updated": datetime.now().isoformat()}
        with open(TASK_PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(empty, f, ensure_ascii=False, indent=2)
        return False

    # 去重：每个task_id只保留最新的一个
    seen_tasks = {}
    for fname in progress_files:
        fpath = os.path.join(PROGRESS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        task_id = data.get("task_id", fname.replace(".json", "").rsplit("_", 2)[0])
        started_at = data.get("started_at", "")

        # 保留最新的一条记录
        if task_id not in seen_tasks or started_at > seen_tasks[task_id].get("started_at", ""):
            seen_tasks[task_id] = {
                "task_id": task_id,
                "status": data.get("status", "unknown"),
                "message": data.get("message", ""),
                "step": data.get("step", 0),
                "total": data.get("total", 0),
                "progress_pct": data.get("progress_pct", 0),
                "started_at": started_at,
                "completed_at": data.get("completed_at", ""),
                "last_update": data.get("last_update", data.get("started_at", "")),
                "source": fname,
            }

    merged = {
        "tasks": list(seen_tasks.values()),
        "last_updated": datetime.now().isoformat(),
        "total_tasks": len(seen_tasks),
        "source_files": len(progress_files),
    }

    with open(TASK_PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"✅ task_progress.json 已更新 ({len(seen_tasks)} 个唯一任务, 来自 {len(progress_files)} 个文件)")
    return True


def main():
    print(f"📊 DSL进度报告生成器 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   项目目录: {PROJECT_ROOT}")
    print()

    # Step 1: 获取cron状态
    print("🔍 步骤1: 获取cron任务状态...")
    cron_ok = generate_cron_status()
    print()

    # Step 2: 合并进度文件
    print("🔍 步骤2: 合并进度文件...")
    progress_ok = merge_progress_files()
    print()

    # 总结
    if cron_ok or progress_ok:
        print("✅ 进度报告生成完成")
        return 0
    else:
        print("⚠️  进度报告部分完成（有警告）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
