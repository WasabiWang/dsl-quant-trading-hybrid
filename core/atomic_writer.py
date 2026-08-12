"""
core/atomic_writer.py — 后向兼容包装器

⚠️ 此模块已迁移至 guardian/atomic.py
   新代码请使用: from guardian import atomic_write_json, safe_read_json
   此文件保留以保证现有 import 不中断
"""

# 重新导出 guardian 子系统的原子写入功能
from guardian.atomic import (
    atomic_write_json,
    atomic_write_yaml,
    atomic_write_bytes,
    safe_write_json,
    safe_write_yaml,
    safe_read_json,
    _resolve_path,
    _safe_fsync,
)
