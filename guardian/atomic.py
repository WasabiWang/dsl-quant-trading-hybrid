#!/usr/bin/env python3
"""
core/atomic_writer.py — 原子写入防护 v1.0 (v4.6.8)

防止cron任务写入中途崩溃导致文件损坏的核心防线。

原理:
  1. 数据写入临时文件 (.tmp)
  2. fsync 强制刷盘
  3. 原子 rename (POSIX保证不会留下半截文件)
  4. 可选: 写入后验证(读回比对)

使用:
  from core.atomic_writer import atomic_write_json, atomic_write_yaml

  # 安全写入JSON
  atomic_write_json("cache/daily_predict.json", data, backup=True)

  # 安全写入YAML
  atomic_write_yaml("config/adaptive_params.yaml", params)
"""

import json, os, shutil, tempfile, time, yaml
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import logging

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


def _resolve_path(path: str) -> Path:
    """解析路径: 相对路径基于项目根目录"""
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _safe_fsync(filepath: Path):
    """确保数据从OS缓冲区刷到磁盘"""
    try:
        fd = os.open(str(filepath), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass  # NFS等文件系统可能不支持


def atomic_write_bytes(
    path: str, 
    content: bytes,
    backup: bool = True,
    verify: bool = True
) -> bool:
    """原子写入二进制数据
    
    Args:
        path: 目标文件路径 (相对或绝对)
        content: 要写入的字节
        backup: 是否写前备份
        verify: 写后是否读回验证
    
    Returns:
        True if success
    """
    target = _resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    
    # 1. 写前备份
    if backup and target.exists():
        backup_path = target.with_suffix(target.suffix + '.last_good')
        try:
            shutil.copy2(target, backup_path)
        except OSError as e:
            logger.warning(f"备份失败 {target} → {backup_path}: {e}")
    
    # 2. 写入临时文件
    tmp_path = target.with_suffix(target.suffix + f'.tmp_{os.getpid()}')
    try:
        with open(tmp_path, 'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        logger.error(f"写入临时文件失败 {tmp_path}: {e}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return False
    
    # 3. 原子 rename
    try:
        os.replace(str(tmp_path), str(target))
    except OSError as e:
        logger.error(f"rename失败 {tmp_path} → {target}: {e}")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return False
    
    # 4. fsync 目标目录 (确保 rename 持久化)
    try:
        fd = os.open(str(target.parent), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass
    
    # 5. 写后验证
    if verify and target.exists():
        try:
            with open(target, 'rb') as f:
                verify_content = f.read()
            if len(verify_content) != len(content):
                logger.error(f"写入验证失败 {target}: 长度不匹配 ({len(verify_content)} vs {len(content)})")
                # 尝试从备份恢复
                if backup:
                    _restore_from_backup(target)
                return False
        except OSError as e:
            logger.error(f"写入验证失败 {target}: {e}")
            return False
    
    return True


def atomic_write_json(
    path: str,
    data: Any,
    backup: bool = True,
    indent: int = 2,
    verify: bool = True,
    max_size_kb: Optional[int] = None,
) -> bool:
    """原子写入JSON文件
    
    Args:
        path: 目标路径
        data: Python对象(必须是JSON可序列化的)
        backup: 写前备份到 .last_good
        indent: JSON缩进
        verify: 写入后读回验证
        max_size_kb: 最大文件大小(kb), 超过则拒绝写入
    
    Returns:
        True if success
    """
    # 预检查: 数据可序列化
    try:
        serialized = json.dumps(data, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError) as e:
        logger.error(f"JSON序列化失败 {path}: {e}")
        return False
    
    # 大小检查
    if max_size_kb and len(serialized.encode('utf-8')) > max_size_kb * 1024:
        logger.error(f"JSON数据过大 {path}: {len(serialized):,} bytes > {max_size_kb}KB")
        return False
    
    content = serialized.encode('utf-8')
    
    # 写入前完整性日志
    target = _resolve_path(path)
    prev_size = target.stat().st_size if target.exists() else 0
    logger.debug(f"原子写入JSON {path}: {prev_size:,}B → {len(content):,}B"
                 f" (Δ{len(content)-prev_size:+,}B)")
    
    success = atomic_write_bytes(path, content, backup=backup, verify=verify)
    
    if not success:
        logger.error(f"原子写入JSON失败 {path}")
    else:
        logger.debug(f"原子写入JSON成功 {path}")
    
    return success


def atomic_write_yaml(
    path: str,
    data: Any,
    backup: bool = True,
    verify: bool = True,
) -> bool:
    """原子写入YAML文件"""
    # 预检查
    try:
        serialized = yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logger.error(f"YAML序列化失败 {path}: {e}")
        return False
    
    content = serialized.encode('utf-8')
    
    target = _resolve_path(path)
    logger.debug(f"原子写入YAML {path}: {len(content):,}B")
    
    success = atomic_write_bytes(path, content, backup=backup, verify=verify)
    
    if success:
        # YAML写后额外验证: 确认能重新解析
        try:
            with open(target) as f:
                yaml.safe_load(f)
        except yaml.YAMLError as e:
            logger.error(f"YAML写后验证失败 {target}: {e}")
            if backup:
                _restore_from_backup(target)
            return False
    
    return success


def _restore_from_backup(target: Path):
    """从 .last_good 备份恢复文件"""
    backup_path = target.with_suffix(target.suffix + '.last_good')
    if backup_path.exists():
        try:
            shutil.copy2(backup_path, target)
            logger.warning(f"已从备份恢复 {target} ← {backup_path}")
        except OSError as e:
            logger.error(f"备份恢复失败: {e}")
    else:
        logger.error(f"无法恢复 {target}: 备份文件不存在")


def safe_read_json(path: str) -> Optional[Any]:
    """安全读取JSON — 带损坏恢复
    
    如果主文件损坏, 自动从 .last_good 恢复
    """
    target = _resolve_path(path)
    
    if not target.exists():
        return None
    
    try:
        with open(target) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"JSON读取失败 {path}: {e}, 尝试从备份恢复")
        return _load_from_backup(target)


def _load_from_backup(target: Path) -> Optional[Any]:
    """从备份加载并恢复主文件"""
    backup_path = target.with_suffix(target.suffix + '.last_good')
    if not backup_path.exists():
        logger.error(f"备份不存在 {backup_path}, 无法恢复")
        return None
    
    try:
        with open(backup_path) as f:
            data = json.load(f)
        # 恢复主文件
        shutil.copy2(backup_path, target)
        logger.warning(f"主文件已从备份恢复 {target}")
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"备份也损坏 {backup_path}: {e}")
        return None


# 便捷函数: 带备份的JSON/YAML安全写入
def safe_write_json(path: str, data: Any, **kwargs) -> bool:
    """安全写入JSON = 原子写入 + 自动备份"""
    return atomic_write_json(path, data, backup=True, verify=True, **kwargs)


def safe_write_yaml(path: str, data: Any) -> bool:
    """安全写入YAML = 原子写入 + 自动备份"""
    return atomic_write_yaml(path, data, backup=True, verify=True)
