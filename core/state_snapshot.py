"""
core/state_snapshot.py — 后向兼容包装器

⚠️ 此模块已迁移至 guardian/snapshot.py
   新代码请使用: from guardian import snapshot_before, rollback_to
   此文件保留以保证现有 import 不中断
"""

from guardian.snapshot import (
    snapshot_before,
    rollback_to,
    list_snapshots,
    cleanup_old_snapshots,
    snapshot_on_write,
    CRITICAL_STATE_FILES,
)
