#!/usr/bin/env python3
"""Safe JSON writer — 防止LLM输出无效JSON的最后一关
===============================================

用法: python3 scripts/safe_json_writer.py --data '{...json字符串...}' --output path/to/file.json
  或: python3 scripts/safe_json_writer.py --input raw.json --output clean.json [--repair]

作用:
  1. 验证输入是否合法JSON
  2. 如果 --repair, 自动修复常见LLM JSON错误(未转义引号)
  3. 用 json.dumps(ensure_ascii=False, indent=2) 重新序列化
  4. 写入目标文件

这确保任何"LLM直接拼JSON字符串"的操作都经过序列化层,
不再依赖LLM自觉转义特殊字符。
"""

import json
import os
import re
import sys
import argparse

def _repair_single_line(text: str) -> str:
    """修复单行JSON中的未转义ASCII双引号（逐字符扫描，不依赖换行格式）"""
    result = []
    in_string = False
    escaped = False
    for i, c in enumerate(text):
        if escaped:
            escaped = False
            result.append(c)
            continue
        if c == '\\':
            escaped = True
            result.append(c)
            continue
        if c == '"':
            if not in_string:
                in_string = True
                result.append(c)
            else:
                # 判断是否真正的结束引号：后跟 ,:}]
                next_chars = text[i+1:i+5].strip()
                if next_chars and next_chars[0] in ',:}]\n':
                    in_string = False
                    result.append(c)
                else:
                    result.append('\\"')  # 内部引号，转义
            continue
        result.append(c)
    return ''.join(result)


def repair_llm_json(text: str) -> str:
    """修复LLM生成JSON的常见错误：未转义ASCII双引号
    
    先尝试逐行修复（适用于美化格式的多行JSON），
    失败后回退到逐字符扫描修复。
    """
    lines = text.split('\n')
    fixed_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith('"') or not (stripped.endswith('",') or stripped.endswith('"')):
            fixed_lines.append(line)
            continue

        raw = line.rstrip('\n')
        indices = [i for i, c in enumerate(raw) if c == '"']
        if len(indices) <= 2:
            fixed_lines.append(line)
            continue

        colon_idx = raw.find(': ')
        if colon_idx > 0 and colon_idx < indices[-1] and raw[colon_idx - 1] in ('"', ' '):
            val_start = raw.index('"', colon_idx)
        else:
            val_start = indices[0]
        val_end = indices[-1]
        interior = [i for i in indices if val_start < i < val_end]
        if not interior:
            fixed_lines.append(line)
            continue

        line_list = list(raw)
        for idx in reversed(interior):
            if idx > 0 and line_list[idx - 1] == '\\':
                continue
            line_list.insert(idx, '\\')
        fixed_lines.append(''.join(line_list))

    result = '\n'.join(fixed_lines)
    # 验证：如果逐行修复后仍然无效，回退到逐字符修复
    try:
        json.loads(result)
        return result
    except json.JSONDecodeError:
        return _repair_single_line(text)


def main():
    parser = argparse.ArgumentParser(description='安全写入JSON文件')
    data_group = parser.add_mutually_exclusive_group(required=True)
    data_group.add_argument('--data', help='JSON字符串（直接传入）')
    data_group.add_argument('--input', help='输入文件路径（读取原始JSON）')
    parser.add_argument('--output', required=True, help='输出文件路径')
    parser.add_argument('--repair', action='store_true',
                        help='自动修复常见LLM JSON错误（未转义引号等）')
    parser.add_argument('--pretty', action='store_true', default=True,
                        help='输出美化格式（默认开启）')

    args = parser.parse_args()

    # Step 1: 读取原始数据
    if args.data:
        raw_text = args.data
    else:
        with open(args.input, 'r', encoding='utf-8') as f:
            raw_text = f.read()

    # Step 2: 尝试解析
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        if not args.repair:
            print(f"[safe_json_writer] ❌ JSON无效 (line {e.lineno}): {e.msg}", file=sys.stderr)
            print(f"[safe_json_writer]    行内容: {raw_text.splitlines()[e.lineno-1][:120]}", file=sys.stderr)
            print(f"[safe_json_writer] 使用 --repair 可尝试自动修复", file=sys.stderr)
            sys.exit(1)

        print(f"[safe_json_writer] ⚠️ JSON解析失败 (line {e.lineno}), 尝试自动修复...", file=sys.stderr)
        fixed = repair_llm_json(raw_text)
        try:
            data = json.loads(fixed)
            print(f"[safe_json_writer] ✅ 修复成功!", file=sys.stderr)
        except json.JSONDecodeError as e2:
            print(f"[safe_json_writer] ❌ 修复后依然无效 (line {e2.lineno}): {e2.msg}", file=sys.stderr)
            print(f"[safe_json_writer]    行: {fixed.splitlines()[e2.lineno-1][:120]}", file=sys.stderr)
            sys.exit(1)

    # Step 3: 序列化写入
    indent = 2 if args.pretty else None
    serialized = json.dumps(data, ensure_ascii=False, indent=indent)

    # 确保输出目录存在
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)

    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(serialized)

    file_size = os.path.getsize(args.output)
    print(f"[safe_json_writer] ✅ 已写入 {args.output} ({file_size:,} bytes)", file=sys.stderr)
    print(json.dumps(data, ensure_ascii=False))  # stdout: 完整JSON用于LLM确认


if __name__ == '__main__':
    main()
