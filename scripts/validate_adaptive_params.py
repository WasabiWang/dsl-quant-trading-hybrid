#!/usr/bin/env python3
"""
scripts/validate_adaptive_params.py — adaptive_params.yaml Schema校验 (v4.6.2)

解决教训59: YAML结构变更后无校验 → key路径错位产生虚假安全信号。
在batch_train、morning_decision等关键模块
启动时自动校验，发现必填字段缺失立即告警。

使用:
  python3 scripts/validate_adaptive_params.py            # 手动校验
  python3 scripts/validate_adaptive_params.py --quiet    # 仅返回exit code
"""

import json
import os
import sys
import yaml
from pathlib import Path
from typing import Optional, Dict, Any, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
SCHEMA_PATH = PROJECT_ROOT / "config" / "adaptive_params.schema.json"
YAML_PATH = PROJECT_ROOT / "config" / "adaptive_params.yaml"


def load_schema() -> dict:
    """加载JSON Schema"""
    if not SCHEMA_PATH.exists():
        print(f"❌ Schema文件不存在: {SCHEMA_PATH}")
        sys.exit(2)
    with open(SCHEMA_PATH) as f:
        return json.load(f)


def load_yaml() -> dict:
    """加载adaptive_params.yaml"""
    if not YAML_PATH.exists():
        print(f"❌ YAML文件不存在: {YAML_PATH}")
        sys.exit(2)
    with open(YAML_PATH) as f:
        return yaml.safe_load(f)


def validate_required_fields(
    data: dict, schema: dict, path: str = ""
) -> List[str]:
    """
    逐层验证required字段是否存在 (不依赖jsonschema库).
    
    遍历schema的required数组，验证data中对应key存在且类型正确。
    返回错误列表，空列表表示全部通过。
    """
    errors = []
    prefix = f"{path}." if path else ""

    if not isinstance(data, dict):
        return [f"{prefix.rstrip('.')}: 期望dict, 实际{type(data).__name__}"]

    if "required" in schema:
        for key in schema["required"]:
            full_path = f"{prefix}{key}"
            if key not in data:
                errors.append(f"❌ 必填字段缺失: {full_path}")
            elif data[key] is None:
                errors.append(f"⚠️  必填字段为None: {full_path}")

    # 递归检查嵌套properties
    if "properties" in schema:
        for key, prop_schema in schema["properties"].items():
            if key in data and isinstance(prop_schema, dict):
                # 检查类型
                if "type" in prop_schema:
                    expected = prop_schema["type"]
                    actual = data[key]
                    if expected == "boolean" and not isinstance(actual, bool):
                        errors.append(
                            f"⚠️  类型不匹配: {prefix}{key} 期望boolean, "
                            f"实际{type(actual).__name__}({actual})"
                        )
                    elif expected == "number" and not isinstance(actual, (int, float)):
                        errors.append(
                            f"⚠️  类型不匹配: {prefix}{key} 期望number, "
                            f"实际{type(actual).__name__}({actual})"
                        )
                    elif expected == "integer" and not isinstance(actual, int):
                        errors.append(
                            f"⚠️  类型不匹配: {prefix}{key} 期望integer, "
                            f"实际{type(actual).__name__}({actual})"
                        )
                    elif expected == "string" and not isinstance(actual, str):
                        errors.append(
                            f"⚠️  类型不匹配: {prefix}{key} 期望string, "
                            f"实际{type(actual).__name__}({actual})"
                        )

                # 递归子对象
                if isinstance(data.get(key), dict):
                    sub_errors = validate_required_fields(
                        data[key], prop_schema, f"{prefix}{key}"
                    )
                    errors.extend(sub_errors)

                # 检查数值范围
                if "minimum" in prop_schema and isinstance(data[key], (int, float)):
                    if data[key] < prop_schema["minimum"]:
                        errors.append(
                            f"❌ 数值越界: {prefix}{key}={data[key]} < "
                            f"最小值{prop_schema['minimum']}"
                        )
                if "maximum" in prop_schema and isinstance(data[key], (int, float)):
                    if data[key] > prop_schema["maximum"]:
                        errors.append(
                            f"❌ 数值越界: {prefix}{key}={data[key]} > "
                            f"最大值{prop_schema['maximum']}"
                        )

    return errors


def validate() -> bool:
    """执行校验, 返回True=通过"""
    try:
        schema = load_schema()
        data = load_yaml()
    except Exception as e:
        print(f"❌ 文件加载失败: {e}")
        return False

    errors = validate_required_fields(data, schema)

    if errors:
        print(f"🔴 adaptive_params.yaml Schema校验失败 ({len(errors)}项):")
        for err in errors:
            print(f"   {err}")
        return False

    # 交叉校验: black_swan_active与black_swan_position_ratio逻辑一致性
    from core.resilience import safe_get
    bs_active = safe_get(data, 'risk.black_swan_active')
    bs_ratio = safe_get(data, 'risk.black_swan_position_ratio')

    if bs_active and (bs_ratio is None or bs_ratio >= 1.0):
        print(f"🔴 逻辑矛盾: black_swan_active=true 但 position_ratio={bs_ratio} "
              f"(应为<1.0的受限值)")
        return False

    if not bs_active and bs_ratio is not None and bs_ratio < 0.5:
        print(f"⚠️  逻辑可疑: black_swan_active=false 但 position_ratio={bs_ratio} "
              f"(通常非黑天鹅期应为0.6+)")

    return True


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="adaptive_params.yaml Schema校验"
    )
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()

    ok = validate()

    if ok:
        if not args.quiet:
            print("✅ adaptive_params.yaml Schema校验通过")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
