#!/usr/bin/env python3
"""
v4.6.x: 验证Python文件无缩进漂移——函数/类定义层级一致性检查

检查模式:
  indent=0  → 模块级 def/class
  indent=4  → 类方法 (带 self 或 @staticmethod/@classmethod)
  indent=4+ → 嵌套函数/方法

检查规则:
  1. indent=0 的 def 之后, 如果出现 indent=4 的 def(有self), 视为BUG
  2. 类内部的模块级辅助函数(无self)与被错误嵌套的类方法之间的边界
"""
import ast
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def check_file(filepath: str) -> list:
    """检查单个Python文件, 返回问题列表"""
    issues = []
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 分析每行定义
    # Track: indent=0 defs and what follows
    module_def_lines = []  # (line_no, name, indent_of_next_def)
    last_module_def = None
    
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if not stripped or stripped.startswith('#'):
            continue
        
        # 检测函数/类定义
        if stripped.startswith('def ') or stripped.startswith('class '):
            indent = len(line) - len(line.lstrip())
            name = stripped.split('(')[0].split(' ')[1] if 'def ' in stripped else stripped.split(' ')[1]
            has_self = 'self' in stripped.split('(')[-1].split(')')[0] if 'def ' in stripped else False
            
            # 规则1: indent=0 的 def 之后, 不应该有 indent=4 的 def(有self)
            if indent == 0:
                if last_module_def:
                    # 检查上一个模块级def和当前def之间是否有indent=4的def
                    pass
                last_module_def = {'line': i + 1, 'name': name}
            elif indent == 4 and last_module_def:
                # 看看上一个模块级def之后, 这个indent=4的def是否合法
                # 如果是紧跟在module def之后(中间只有空行/注释), 说明是嵌套/漂移
                gap = lines[last_module_def['line']:i] if last_module_def['line'] < i else []
                gap_content = [l for l in gap if l.strip() and not l.strip().startswith('#')]
                if len(gap_content) <= 1:  # 基本没有有效内容, 像是直接跟在return后面
                    issues.append({
                        'file': filepath,
                        'line': i + 1,
                        'severity': 'ERROR',
                        'message': f"可能的缩进漂移: def {name}(has_self={has_self}) 在模块级函数 '{last_module_def['name']}' ({last_module_def['line']}) 之后, 但保持在 indent=4, 可能被 Python 解析为嵌套函数"
                    })
    
    return issues


def main():
    scripts_dir = PROJECT_ROOT / 'scripts'
    config_dir = PROJECT_ROOT / 'config'
    issues = []
    
    for pyfile in sorted(scripts_dir.glob('*.py')):
        issues.extend(check_file(str(pyfile)))
    for pyfile in sorted(config_dir.glob('*.py')):
        if pyfile.name != '__init__.py':
            issues.extend(check_file(str(pyfile)))
    
    if not issues:
        print("✅ 所有文件缩进检查通过")
        return 0
    
    print(f"⚠️ 发现 {len(issues)} 个缩进问题:")
    for iss in issues:
        print(f"  [{iss['severity']}] {iss['file']}:{iss['line']}")
        print(f"    {iss['message']}")
    return 1


if __name__ == '__main__':
    sys.exit(main())
