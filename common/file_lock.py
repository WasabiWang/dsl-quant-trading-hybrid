#!/usr/bin/env python3
"""
DSL v4.5.3d 文件并发写保护模块
防止 cron 任务同时写入同一文件导致数据损坏。

保护的3个关键文件:
  1. cache/daily_predict.json     — batch_predict.py 写入
  2. confidence_data/prediction_calibration.json — calibration_feedback.py + feedback_controller.py 写入
  3. config/adaptive_params.yaml   — calibration_feedback.py + feedback_controller.py 写入

用法:
  from common.file_lock import locked_json_read, locked_json_write, locked_yaml_write
  
  # 读取（不持有锁 → 适合仅读场景）
  data = locked_json_read(path)
  
  # 写入（持有独占锁 → 安全写）
  locked_json_write(path, data)
  
  # 自定义读写周期（持有独占锁）
  with locked_rw(path) as (lock, data):
      data["key"] = "new_value"
      # 自动保存
"""
import os, json, yaml, time, errno
from pathlib import Path
from filelock import FileLock, Timeout

LOCK_DIR = Path(__file__).resolve().parent.parent / "data" / "locks"
LOCK_DIR.mkdir(parents=True, exist_ok=True)

LOCK_TIMEOUT = 30  # 最大等待锁时间(秒)


def _lock_path(file_path: str) -> Path:
    """生成锁文件路径 (放在 data/locks/ 目录)"""
    safe_name = str(file_path).replace("/", "_").replace("\\", "_")
    return LOCK_DIR / f"{safe_name}.lock"


def locked_json_read(file_path: str, default=None) -> dict:
    """线程安全的JSON读取（不使用锁 — 读操作安全无须锁）"""
    p = Path(file_path)
    if not p.exists():
        return default if default is not None else {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return default if default is not None else {}


def locked_json_write(file_path: str, data, backup: bool = True) -> bool:
    """
    线程安全的JSON写入（独占锁）。
    backup=True: 写前备份原文件到 .bak
    """
    lock = FileLock(str(_lock_path(file_path)), timeout=LOCK_TIMEOUT)
    try:
        with lock:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            
            # 写前备份
            if backup and p.exists():
                bak_path = p.with_suffix(p.suffix + ".bak")
                import shutil
                shutil.copy2(p, bak_path)
            
            # 原子写入：先写到临时文件再rename
            tmp = p.with_suffix(p.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.replace(p)
            return True
    except Timeout:
        print(f"⚠️ [file_lock] 写入超时: {file_path} (被占用 >{LOCK_TIMEOUT}s)")
        return False
    except Exception as e:
        print(f"⚠️ [file_lock] 写入失败: {file_path} ({e})")
        return False


def locked_yaml_read(file_path: str, default=None) -> dict:
    """线程安全的YAML读取（不使用锁 — 读操作安全）"""
    p = Path(file_path)
    if not p.exists():
        return default if default is not None else {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return default if default is not None else {}


def locked_yaml_write(file_path: str, data, backup: bool = True) -> bool:
    """
    线程安全的YAML写入（独占锁）。
    """
    lock = FileLock(str(_lock_path(file_path)), timeout=LOCK_TIMEOUT)
    try:
        with lock:
            p = Path(file_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            
            if backup and p.exists():
                bak_path = p.with_suffix(p.suffix + ".bak")
                import shutil
                shutil.copy2(p, bak_path)
            
            tmp = p.with_suffix(p.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            tmp.replace(p)
            return True
    except Timeout:
        print(f"⚠️ [file_lock] 写入超时: {file_path} (被占用 >{LOCK_TIMEOUT}s)")
        return False
    except Exception as e:
        print(f"⚠️ [file_lock] 写入失败: {file_path} ({e})")
        return False


def locked_rw(file_path: str, default=None, is_yaml: bool = False):
    """
    上下文管理器: 以独占锁读取-修改-自动写入。
    用法:
      with locked_rw(path) as (lock, data):
          data["key"] = value
    
    is_yaml=True 时使用yaml解析/序列化。
    """
    class _LockedRW:
        def __init__(self, path, default, is_yaml):
            self.path = Path(path)
            self.default = default if default is not None else {}
            self.is_yaml = is_yaml
            self.lock = FileLock(str(_lock_path(str(path))), timeout=LOCK_TIMEOUT)
            self.data = None
        
        def __enter__(self):
            self.lock.acquire()
            try:
                if self.path.exists():
                    with open(self.path, "r", encoding="utf-8") as f:
                        self.data = yaml.safe_load(f) if self.is_yaml else json.load(f)
                else:
                    self.data = self.default
            except Exception:
                self.data = self.default
            return self.lock, self.data
        
        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                if exc_type is None:
                    p = self.path
                    p.parent.mkdir(parents=True, exist_ok=True)
                    tmp = p.with_suffix(p.suffix + ".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        if self.is_yaml:
                            yaml.dump(self.data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
                        else:
                            json.dump(self.data, f, indent=2, ensure_ascii=False)
                    tmp.replace(p)
            except Exception as e:
                print(f"⚠️ [file_lock] 写入失败: {self.path} ({e})")
            finally:
                self.lock.release()
    
    return _LockedRW(file_path, default, is_yaml)


if __name__ == "__main__":
    # 自测
    import tempfile
    tmp = tempfile.mktemp(suffix=".json")
    
    with locked_rw(tmp) as (lock, data):
        data["test"] = "hello"
        data["counter"] = 42
    
    assert locked_json_read(tmp).get("test") == "hello"
    
    import shutil
    Path(tmp).unlink(missing_ok=True)
    print("✅ file_lock 自测通过")
