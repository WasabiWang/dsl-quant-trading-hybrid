"""
core/pollution_detector.py — 后向兼容包装器

⚠️ 此模块已迁移至 guardian/detector.py
   新代码请使用: from guardian import validate_before_write, validate_after_write
   此文件保留以保证现有 import 不中断
"""

from guardian.detector import (
    validate_before_write,
    validate_after_write,
    quick_integrity_check,
    EXPECTED_SCHEMAS,
    _resolve_path,
    _find_matching_schema,
)
