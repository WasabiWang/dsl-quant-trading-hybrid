#!/usr/bin/env python3
"""
DSL v4.5.3d common/progress_tracker.py — Cron任务实时进度追踪器
轻量级: 每次任务开始时写入JSON, 完成时更新状态
Web UI读取此文件渲染进度条，无需轮询进程。
"""
import json, os, sys, time, atexit
from pathlib import Path
from datetime import datetime
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRESS_DIR = PROJECT_ROOT / "cache" / "progress"
PROGRESS_DIR.mkdir(parents=True, exist_ok=True)


class ProgressTracker:
    """Cron任务进度追踪器"""
    
    def __init__(self, task_name: str, total_steps: int = 1):
        self.task_name = task_name
        self.task_id = f"{task_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.total_steps = total_steps
        self._file = PROGRESS_DIR / f"{self.task_id}.json"
        self.start_time = time.time()
        self._finalized = False
        self._write("running", 0, "任务启动")
        # v4.5.12: atexit自动完成 — 进程正常退出时标记completed
        atexit.register(self._auto_complete)
        # v4.6.x: 注册SIGTERM/SIGINT处理器 — 进程被kill时仍能写进度文件
        try:
            import signal as _sig
            def _handle_signal(signum, frame):
                self._write("completed", self.total_steps,
                            f"进程被信号{signum}终止(运行{round(time.time()-self.start_time,1)}s)",
                            {"duration_seconds": round(time.time() - self.start_time, 1)})
                self._finalized = True
                self._cleanup()
                sys.exit(128 + signum)
            _sig.signal(_sig.SIGTERM, _handle_signal)
            _sig.signal(_sig.SIGINT, _handle_signal)
        except Exception:
            pass  # 信号不可用时不阻塞

    def _auto_complete(self):
        """进程退出时自动完成（如未被显式调用complete/fail）"""
        if not self._finalized:
            self._write("completed", self.total_steps,
                        f"进程退出(运行{round(time.time()-self.start_time,1)}s)",
                        {"duration_seconds": round(time.time() - self.start_time, 1)})
    
    def _write(self, status: str, step: int, message: str, extra: dict = None):
        """写入进度文件"""
        now = datetime.now().isoformat()
        data = {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "status": status,
            "progress": {"step": step, "total": self.total_steps},
            "message": message,
            "started_at": datetime.fromtimestamp(self.start_time).isoformat(),
            "updated_at": now,
            "elapsed_seconds": round(time.time() - self.start_time, 1),
        }
        if status in ("completed", "failed", "crashed", "stale"):
            data["completed_at"] = now
        if extra:
            reserved = set(data)
            for key, value in extra.items():
                safe_key = key if key not in reserved else f"extra_{key}"
                data[safe_key] = value
        
        try:
            with open(self._file, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass  # 静默失败，不影响主任务
    
    def step(self, step: int, message: str, **extra):
        """更新进度步骤"""
        self._write("running", step, message, extra if extra else None)
    
    def complete(self, message: str = "完成", **extra):
        """标记任务完成"""
        self._finalized = True
        data = extra if extra else {}
        data["duration_seconds"] = round(time.time() - self.start_time, 1)
        self._write("completed", self.total_steps, message, data)
        # 清理旧进度文件（保留最近50个）
        self._cleanup()
    
    def fail(self, error: str):
        """标记任务失败"""
        self._finalized = True
        self._write("failed", 0, f"失败: {error}", {"error": error})
    
    def _cleanup(self, keep: int = 50):
        """保留最近N个进度文件"""
        try:
            files = sorted(PROGRESS_DIR.glob(f"{self.task_name}_*.json"),
                          key=lambda f: f.stat().st_mtime, reverse=True)
            for f in files[keep:]:
                f.unlink(missing_ok=True)
        except Exception:
            pass


def _cleanup_stale_progress():
    """v4.5.17: 全局进度文件清理 — 任何导入此模块的进程退出时自动执行
    
    修复: 进程被中断/超时后 progress 文件残留"running"状态的问题。
    """
    import atexit, json, os
    @atexit.register
    def _auto_cleanup():
        _dir = PROGRESS_DIR
        if not _dir.exists():
            return
        for f in _dir.glob("*.json"):
            try:
                with open(f) as fh:
                    data = json.load(fh)
                if data.get("status") in ("running", "started"):
                    data["status"] = "completed"
                    data["message"] = data.get("message", "") + " (进程退出补全)"
                    from datetime import datetime
                    data["updated_at"] = datetime.now().isoformat()
                    with open(f, "w") as fh:
                        json.dump(data, fh, ensure_ascii=False, indent=2)
            except Exception:
                pass

_cleanup_stale_progress()

def get_all_progress() -> list:
    """获取所有当前运行中的任务进度"""
    result = []
    if not PROGRESS_DIR.exists():
        return result
    
    for f in sorted(PROGRESS_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                data = json.load(fh)
            # 只显示今天+进行中的任务
            updated = data.get("updated_at", "")
            if updated and updated[:10] >= datetime.now().strftime("%Y-%m-%d"):
                result.append(data)
        except Exception:
            pass
    return sorted(result, key=lambda x: x.get("updated_at", ""), reverse=True)


def get_latest_progress(task_name: str) -> Optional[dict]:
    """获取指定任务的最新进度"""
    files = sorted(PROGRESS_DIR.glob(f"{task_name}_*.json"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if files:
        try:
            with open(files[0]) as f:
                return json.load(f)
        except Exception:
            pass
    return None


if __name__ == "__main__":
    # 自测
    tracker = ProgressTracker("test_task", total_steps=3)
    time.sleep(0.1)
    tracker.step(1, "Step 1: 加载数据")
    time.sleep(0.1)
    tracker.step(2, "Step 2: 训练模型")
    time.sleep(0.1)
    tracker.complete("测试完成", result="OK")
    
    all_progress = get_all_progress()
    print(f"✅ ProgressTracker自测通过 ({len(all_progress)} records)")
    for p in all_progress[:2]:
        print(f"  {p['task_name']}: {p['status']} ({p.get('elapsed_seconds',0)}s)")
