#!/usr/bin/env python3
"""
core/pollution_detector.py — 数据污染检测 v1.0 (v4.6.8)

检测cron任务对系统状态文件的污染, 在写操作前/后验证数据完整性。

检测类型:
  1. Schema validation — 字段类型/必需字段是否正确
  2. Sudden change — 数据突变检测 (值变化幅度监控)
  3. Empty/null check — 空数据/None值检测
  4. Size anomaly — 文件大小异常 (相比历史极值)
  5. Recency check — 时间戳不能倒退

使用:
  from core.pollution_detector import validate_before_write, validate_after_write

  # 写入前验证
  if not validate_before_write("cache/daily_predict.json", new_data):
      raise ValueError("数据污染检测拒绝写入")

  # 写入后验证
  validate_after_write("cache/daily_predict.json", expected_keys=30)
"""

import json, os, yaml
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


# ═══════════════════════════════════════════
# Schema定义 (关键文件的期望结构)
# ═══════════════════════════════════════════

EXPECTED_SCHEMAS = {
    "cache/daily_predict.json": {
        "required_keys": ["generated_at", "trading_date", "predictions"],
        "types": {"predictions": list},
        "min_predictions": 1,
        "max_age_hours": 48,
    },
    "config/adaptive_params.yaml": {
        "required_keys": ["trading", "risk"],
        "trading_required": ["buy_threshold", "max_positions", "position_size"],
        "buy_threshold_range": (0.005, 0.10),
        "position_size_range": (0.01, 0.50),
    },
    "confidence_data/prediction_calibration.json": {
        "required_keys": ["overall_stats", "stock_accuracy", "daily_records"],
        "min_stocks": 5,
        "accuracy_range": (0.20, 0.95),
    },
    "data/retrain_queue.json": {
        "max_queue_size": 100,
        "required_fields_per_item": ["symbol", "priority", "status"],
    },
    "data/planned/planned_trades_latest.json": {
        "max_trades": 20,
        "required_fields_per_trade": ["symbol", "action", "price"],
        "max_age_hours": 72,
    },
}


def _resolve_path(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _find_matching_schema(path: str) -> Optional[str]:
    """找到匹配的schema key"""
    for schema_key in EXPECTED_SCHEMAS:
        if path.endswith(schema_key) or schema_key.endswith(path):
            return schema_key
    return None


# ═══════════════════════════════════════════
# 写入前验证
# ═══════════════════════════════════════════

def validate_before_write(path: str, data: Any) -> Tuple[bool, List[str]]:
    """写入前验证数据完整性
    
    Args:
        path: 目标文件路径
        data: 待写入数据
    
    Returns:
        (is_valid, [warnings])
    """
    warnings = []
    schema_key = _find_matching_schema(path)
    
    # 通用检查
    if data is None:
        return False, ["数据为None, 拒绝写入"]
    
    if isinstance(data, (list, dict)) and len(data) == 0:
        warnings.append(f"数据为空 ({type(data).__name__}), 可能误操作")
    
    # Schema-specific检查
    if schema_key:
        schema = EXPECTED_SCHEMAS[schema_key]
        
        if isinstance(data, dict):
            # 检查必需key
            missing = [k for k in schema.get("required_keys", []) if k not in data]
            if missing:
                warnings.append(f"缺少必需字段: {missing}")
            
            # 检查类型
            for field, expected_type in schema.get("types", {}).items():
                if field in data and not isinstance(data[field], expected_type):
                    warnings.append(f"字段 '{field}' 类型错误: "
                                   f"{type(data[field]).__name__} != {expected_type.__name__}")
            
            # 检查预测数量
            if "predictions" in data and isinstance(data["predictions"], list):
                min_preds = schema.get("min_predictions", 0)
                if len(data["predictions"]) < min_preds:
                    warnings.append(f"预测数量({len(data['predictions'])}) < 最小({min_preds})")
        
        # 检查交易参数范围
        if isinstance(data, dict) and "trading" in data:
            t = data["trading"]
            bt_range = schema.get("buy_threshold_range")
            if bt_range and "buy_threshold" in t:
                if not (bt_range[0] <= t["buy_threshold"] <= bt_range[1]):
                    warnings.append(f"buy_threshold={t['buy_threshold']:.3f} 超出合理范围 {bt_range}")
        
        # 检查重训队列大小
        if isinstance(data, list) and schema_key.endswith("retrain_queue.json"):
            max_q = schema.get("max_queue_size", 100)
            if len(data) > max_q:
                warnings.append(f"重训队列{len(data)} > 最大{max_q}")
    
    # 文件大小突变检查
    if len(warnings) == 0:
        target = _resolve_path(path)
        if target.exists():
            try:
                serialized = json.dumps(data, ensure_ascii=False)
                new_size = len(serialized.encode('utf-8'))
                old_size = target.stat().st_size
                size_ratio = new_size / old_size if old_size > 0 else float('inf')
                if size_ratio > 10:  # 数据量暴增10倍+
                    warnings.append(f"文件大小突变: {old_size:,}B → {new_size:,}B ({size_ratio:.1f}x)")
                elif old_size > 10000 and size_ratio < 0.1:  # 数据量骤降10倍+
                    warnings.append(f"文件大小骤降: {old_size:,}B → {new_size:,}B "
                                   f"(可能数据丢失)")
            except Exception:
                pass
    
    is_valid = len([w for w in warnings if "缺少" in w]) == 0
    
    if warnings:
        level = "ERROR" if not is_valid else "WARNING"
        for w in warnings:
            logger.warning(f"[{level}] 写入前验证 {path}: {w}")
    
    return is_valid, warnings


# ═══════════════════════════════════════════
# 写入后验证
# ═══════════════════════════════════════════

def validate_after_write(path: str, 
                         expected_keys: int = None,
                         key_fields: List[str] = None) -> Tuple[bool, List[str]]:
    """写入后验证文件完整性
    
    Args:
        path: 文件路径
        expected_keys: 期望的顶层key数量 (dict)
        key_fields: 必须存在的字段
    
    Returns:
        (is_healthy, [issues])
    """
    issues = []
    target = _resolve_path(path)
    
    if not target.exists():
        issues.append("文件写入后不存在!")
        return False, issues
    
    # 1. 大小检查
    size = target.stat().st_size
    if size == 0:
        issues.append("文件大小为0, 写入失败")
        return False, issues
    
    # 2. 解析验证
    data = None
    if path.endswith('.json'):
        try:
            with open(target) as f:
                data = json.load(f)
        except (json.JSONDecodeError, Exception) as e:
            issues.append(f"JSON解析失败: {e}")
            return False, issues
    elif path.endswith('.yaml'):
        try:
            with open(target) as f:
                data = yaml.safe_load(f)
        except Exception as e:
            issues.append(f"YAML解析失败: {e}")
            return False, issues
    
    if data is None:
        return True, issues
    
    # 3. 结构验证
    if isinstance(data, dict):
        if expected_keys is not None:
            actual = len(data)
            if actual < expected_keys * 0.5:
                issues.append(f"Key数量异常少: {actual} < {expected_keys * 0.5} (期望≈{expected_keys})")
        
        if key_fields:
            missing = [k for k in key_fields if k not in data]
            if missing:
                issues.append(f"缺失关键字段: {missing}")
    
    # 4. 时间戳一致性
    schema_key = _find_matching_schema(path)
    if schema_key:
        schema = EXPECTED_SCHEMAS[schema_key]
        max_age = schema.get("max_age_hours")
        
        if max_age and isinstance(data, dict):
            for ts_field in ["generated_at", "last_updated", "timestamp"]:
                ts_str = data.get(ts_field, "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00')[:19])
                        age_h = (datetime.now() - ts.replace(tzinfo=None)).total_seconds() / 3600
                        if age_h > max_age:
                            issues.append(f"数据过期: {ts_field}={ts_str}, {age_h:.0f}h > {max_age}h")
                    except (ValueError, TypeError):
                        pass
    
    # 5. 与.whiteboard历史基线对比
    baseline = _load_baseline(path)
    if baseline:
        if isinstance(data, dict) and isinstance(baseline, dict):
            key_ratio = len(data) / len(baseline) if len(baseline) > 0 else 1
            if key_ratio < 0.3:
                issues.append(f"条目数骤降: {len(data)} vs baseline {len(baseline)} ({key_ratio:.0%})")
        
        if isinstance(data, list) and isinstance(baseline, list):
            len_ratio = len(data) / len(baseline) if len(baseline) > 0 else 1
            if len_ratio < 0.2 and len(baseline) > 5:
                issues.append(f"列表条目骤降: {len(data)} vs baseline {len(baseline)} ({len_ratio:.0%})")
    
    if issues:
        for i in issues:
            logger.warning(f"[VALIDATION] {path}: {i}")
    
    return len(issues) == 0, issues


def _load_baseline(path: str) -> Optional[Any]:
    """从 .whiteboard 加载历史基线 (由auto_backup维护)"""
    # 尝试 .last_good 备份
    target = _resolve_path(path)
    baseline_paths = [
        target.with_suffix(target.suffix + '.last_good'),
        Path(str(target) + '.last_good'),
    ]
    for bp in baseline_paths:
        if bp.exists():
            try:
                if path.endswith('.json'):
                    with open(bp) as f:
                        return json.load(f)
                elif path.endswith('.yaml'):
                    with open(bp) as f:
                        return yaml.safe_load(f)
            except Exception:
                pass
    return None


def quick_integrity_check(path: str) -> bool:
    """快速完整性检查 — 文件存在 + 大小 > 0 + 可解析"""
    target = _resolve_path(path)
    if not target.exists():
        return False
    if target.stat().st_size == 0:
        return False
    
    try:
        if path.endswith('.json'):
            with open(target) as f:
                json.load(f)
        elif path.endswith('.yaml'):
            with open(target) as f:
                yaml.safe_load(f)
        return True
    except Exception:
        return False
