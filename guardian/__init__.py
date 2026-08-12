"""
guardian — DSL量化交易系统 数据完整性防护子系统 v1.0

四层防线架构:
  L1: detector  — 写入前Schema/范围/突变验证    (pollution_detector)
  L2: atomic    — 原子写入(temp→fsync→rename)+备份 (atomic_writer)
  L3: detector  — 写入后解析/大小/时间戳验证      (pollution_detector)
  L4: snapshot  — 写前快照 + 一键回滚              (state_snapshot)

统一入口:
  from guardian import (
      # 原子写入
      atomic_write_json, atomic_write_yaml, safe_read_json,
      # 快照回滚
      snapshot_before, rollback_to, list_snapshots,
      # 污染检测
      validate_before_write, validate_after_write,
      # 健康检查
      run_health_check,
  )

CLI工具:
  python3 -m guardian.cli check     # 运行全部守卫检查
  python3 -m guardian.cli snapshot  # 手动打快照
  python3 -m guardian.cli rollback  # 回滚到最近快照
  python3 -m guardian.cli verify FILE  # 验证单文件完整性
"""

# ═══════════════════════════════════════════
# 子系统版本
# ═══════════════════════════════════════════
__version__ = "1.0.0"
__author__ = "DSL Quant Team"
__build__ = "v4.6.8-guardian"

# ═══════════════════════════════════════════
# L1+L3: 污染检测
# ═══════════════════════════════════════════
from guardian.detector import (
    validate_before_write,
    validate_after_write,
    quick_integrity_check,
    EXPECTED_SCHEMAS,
)

# ═══════════════════════════════════════════
# L2: 原子写入
# ═══════════════════════════════════════════
from guardian.atomic import (
    atomic_write_json,
    atomic_write_yaml,
    atomic_write_bytes,
    safe_write_json,
    safe_write_yaml,
    safe_read_json,
)

# ═══════════════════════════════════════════
# L4: 快照回滚
# ═══════════════════════════════════════════
from guardian.snapshot import (
    snapshot_before,
    rollback_to,
    list_snapshots,
    cleanup_old_snapshots,
    snapshot_on_write,
)

# ═══════════════════════════════════════════
# 导出清单
# ═══════════════════════════════════════════
__all__ = [
    # atomic
    "atomic_write_json", "atomic_write_yaml", "atomic_write_bytes",
    "safe_write_json", "safe_write_yaml", "safe_read_json",
    # snapshot
    "snapshot_before", "rollback_to", "list_snapshots",
    "cleanup_old_snapshots", "snapshot_on_write",
    # detector
    "validate_before_write", "validate_after_write",
    "quick_integrity_check", "EXPECTED_SCHEMAS",
    # health
    "run_health_check",
]


def run_health_check(verbose: bool = False):
    """运行Guardian子系统健康检查
    
    委派到 robustness_guard.run_all_guards (12 Guards)
    """
    try:
        from core.robustness_guard import run_all_guards as _run_guards
        report = _run_guards()
        if verbose:
            return report
        return {
            "overall": report.get("overall", "unknown"),
            "critical": len(report.get("critical_issues", [])),
            "warnings": len(report.get("warnings", [])),
            "guards": len(report.get("guards", {})),
        }
    except ImportError:
        return {"overall": "unknown", "error": "robustness_guard not available"}
