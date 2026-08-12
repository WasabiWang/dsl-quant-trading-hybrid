#!/usr/bin/env python3
"""
core/state_snapshot.py — 状态快照与回滚 v1.0 (v4.6.8)

解决cron任务污染后无回滚手段的问题。

架构:
  snapshot/                          # 快照目录
    ├── 20260719_091500/             # 按时间戳组织
    │   ├── _manifest.json          # 快照清单
    │   ├── config:adaptive_params.yaml
    │   └── cache:daily_predict.json
    └── ...

保留策略: 最近48小时的每小时保留, 更早的每天保留1份, 最多50份

使用:
  from core.state_snapshot import snapshot_before, rollback_to

  # 写入前打快照
  snapshot_before("cache/daily_predict.json")

  # 发现问题后回滚
  rollback_to(latest=True)  # 回滚到最近快照
"""

import json, os, shutil, time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
import logging

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
SNAPSHOT_DIR = PROJECT_ROOT / "snapshot"

# 需要保护的关键共享状态文件
CRITICAL_STATE_FILES = [
    "cache/daily_predict.json",
    "config/adaptive_params.yaml",
    "config/master_stock_pool.yaml",
    "data/retrain_queue.json",
    "data/circuit_breaker.json",
    "data/planned/planned_trades_latest.json",
    "data/paper_trading.db",
    "confidence_data/prediction_calibration.json",
    "confidence_data/confidence_calibration.json",
    "confidence_data/degraded_models.json",
]

# 快照保留策略
MAX_SNAPSHOTS = 50
HOURLY_WINDOW_HOURS = 48  # 48小时内保留每小时快照
DAILY_RETENTION_DAYS = 30  # 更远的保留每天1份


def _snapshot_id() -> str:
    """生成快照ID: YYYYMMDD_HHMMSS"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _file_key(path: str) -> str:
    """文件路径 → 快照存储key (用:替换/避免目录嵌套)"""
    rel = str(_resolve_path(path).relative_to(PROJECT_ROOT))
    return rel.replace("/", ":")


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def snapshot_before(paths: List[str] = None, label: str = "") -> Optional[str]:
    """对指定文件打快照 (写操作前调用)
    
    Args:
        paths: 文件路径列表, None=保护所有关键文件
        label: 快照标签 (如 "pre_batch_predict")
    
    Returns:
        快照ID, 失败返回None
    """
    if paths is None:
        paths = CRITICAL_STATE_FILES
    
    sid = _snapshot_id()
    snap_dir = SNAPSHOT_DIR / sid
    files_copied = []
    
    try:
        snap_dir.mkdir(parents=True, exist_ok=True)
        
        for path in paths:
            src = _resolve_path(path)
            if not src.exists():
                continue
            
            key = _file_key(path)
            dst = snap_dir / key
            
            # 确保目标目录存在
            dst.parent.mkdir(parents=True, exist_ok=True)
            
            # 复制文件 (数据库文件可能较大, 对大文件降级为仅记录metadata)
            size_mb = src.stat().st_size / (1024 * 1024)
            if size_mb > 50:  # >50MB的文件只记录元数据
                files_copied.append({"path": path, "size_mb": round(size_mb, 1), "copied": False})
                logger.debug(f"快照跳过(过大 {size_mb:.0f}MB): {path}")
                continue
            
            shutil.copy2(src, dst)
            files_copied.append({"path": path, "size_mb": round(size_mb, 2), "copied": True})
        
        # 写manifest
        manifest = {
            "id": sid,
            "timestamp": datetime.now().isoformat(),
            "label": label,
            "files": files_copied,
            "total_copied": sum(1 for f in files_copied if f["copied"]),
            "total_skipped": sum(1 for f in files_copied if not f["copied"]),
        }
        with open(snap_dir / "_manifest.json", "w") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        
        logger.info(f"快照 {sid} ({label}): {manifest['total_copied']}文件, "
                    f"{manifest['total_skipped']}跳过")
        return sid
    
    except Exception as e:
        logger.error(f"快照失败 {sid}: {e}")
        # 清理不完整快照
        if snap_dir.exists():
            shutil.rmtree(snap_dir, ignore_errors=True)
        return None


def rollback_to(snapshot_id: str = None, latest: bool = False, 
                paths: List[str] = None, dry_run: bool = False) -> dict:
    """回滚到指定快照
    
    Args:
        snapshot_id: 指定快照ID
        latest: True=回滚到最近快照
        paths: 只回滚指定文件, None=回滚快照中所有文件
        dry_run: True=只报告不回滚
    
    Returns:
        {"rolled_back": [文件], "skipped": [文件], "errors": [错误]}
    """
    if latest:
        snapshots = list_snapshots()
        if not snapshots:
            return {"rolled_back": [], "skipped": [], "errors": ["无可用快照"]}
        snapshot_id = snapshots[0]["id"]
    
    if not snapshot_id:
        return {"rolled_back": [], "skipped": [], "errors": ["需要指定snapshot_id或latest=True"]}
    
    snap_dir = SNAPSHOT_DIR / snapshot_id
    if not snap_dir.exists():
        return {"rolled_back": [], "skipped": [], "errors": [f"快照不存在: {snapshot_id}"]}
    
    # 读取manifest
    manifest_path = snap_dir / "_manifest.json"
    if not manifest_path.exists():
        return {"rolled_back": [], "skipped": [], "errors": ["快照manifest丢失"]}
    
    with open(manifest_path) as f:
        manifest = json.load(f)
    
    result = {"rolled_back": [], "skipped": [], "errors": []}
    
    for file_info in manifest["files"]:
        path = file_info["path"]
        copied = file_info.get("copied", True)
        
        # 过滤指定路径
        if paths is not None and path not in paths:
            continue
        
        if not copied:
            result["skipped"].append(f"{path} (快照时未复制)")
            continue
        
        key = _file_key(path)
        src = snap_dir / key
        dst = _resolve_path(path)
        
        if not src.exists():
            result["errors"].append(f"{path}: 快照文件缺失")
            continue
        
        if dry_run:
            result["rolled_back"].append(f"{path} [dry-run]")
            continue
        
        try:
            # 回滚前先备份当前文件
            if dst.exists():
                shutil.copy2(dst, dst.with_suffix(dst.suffix + '.pre_rollback'))
            
            shutil.copy2(src, dst)
            result["rolled_back"].append(path)
            logger.warning(f"已回滚 {path} ← 快照{snapshot_id}")
        except OSError as e:
            result["errors"].append(f"{path}: {e}")
    
    return result


def list_snapshots(limit: int = 10) -> List[dict]:
    """列出最近的快照"""
    if not SNAPSHOT_DIR.exists():
        return []
    
    snapshots = []
    for d in sorted(SNAPSHOT_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        manifest_path = d / "_manifest.json"
        if not manifest_path.exists():
            continue
        try:
            with open(manifest_path) as f:
                m = json.load(f)
            snapshots.append(m)
            if len(snapshots) >= limit:
                break
        except Exception:
            pass
    
    return snapshots


def cleanup_old_snapshots():
    """清理过期快照, 遵守保留策略"""
    if not SNAPSHOT_DIR.exists():
        return
    
    now = datetime.now()
    snapshots = []
    
    for d in SNAPSHOT_DIR.iterdir():
        if not d.is_dir():
            continue
        manifest_path = d / "_manifest.json"
        if not manifest_path.exists():
            # 无manifest的孤立目录, 删除
            shutil.rmtree(d, ignore_errors=True)
            continue
        try:
            with open(manifest_path) as f:
                m = json.load(f)
            ts = datetime.fromisoformat(m["timestamp"])
            snapshots.append((d, ts))
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            continue
    
    snapshots.sort(key=lambda x: x[1], reverse=True)
    
    # 按窗口分组
    hourly_cutoff = now - timedelta(hours=HOURLY_WINDOW_HOURS)
    daily_cutoff = now - timedelta(days=DAILY_RETENTION_DAYS)
    
    kept = set()
    daily_kept_dates = set()
    
    for i, (d, ts) in enumerate(snapshots):
        # 在48小时内: 全部保留
        if ts >= hourly_cutoff:
            kept.add(d)
            continue
        
        # 在30天内: 每天保留1份(最早的)
        if ts >= daily_cutoff:
            date_key = ts.strftime("%Y%m%d")
            if date_key not in daily_kept_dates:
                daily_kept_dates.add(date_key)
                kept.add(d)
            continue
        
        # 超过30天: 删除
    
    # 数量限制
    if len(kept) > MAX_SNAPSHOTS:
        remove_count = len(kept) - MAX_SNAPSHOTS
        kept = set(sorted(kept, key=lambda d: min(
            datetime.fromisoformat(json.load(open(d / "_manifest.json"))["timestamp"])
            for d in [d]
        ))[-MAX_SNAPSHOTS:])
    
    # 执行删除
    removed = 0
    for d, ts in snapshots:
        if d not in kept:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    
    if removed > 0:
        logger.info(f"快照清理: 删除{removed}份, 保留{len(kept)}份")


def snapshot_on_write(func):
    """装饰器: 在函数执行前自动对关键文件打快照
    
    用法:
      @snapshot_on_write
      def batch_predict(): ...
    """
    import functools
    
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        label = f"auto_{func.__name__}"
        sid = snapshot_before(label=label)
        try:
            result = func(*args, **kwargs)
            return result
        except Exception as e:
            if sid:
                logger.warning(f"{func.__name__} 失败, 快照{sid}可用于回滚")
            raise
    return wrapper
