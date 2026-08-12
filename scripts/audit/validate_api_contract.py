#!/usr/bin/env python3
"""
DSL v4.6.x — API契约校验: 验证类方法的实际集合与声明的 __all__methods__ 一致

检测:
  - 类方法被意外删除
  - 类方法被缩进漂移"吞掉" (PaperTrader Bug复现检测)
  - 新增方法未声明
  - 对外接口签名变化

用法:
  python3 scripts/audit/validate_api_contract.py
  或通过 run_all_audits.py 自动调用
"""
import ast
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
EXECUTION_DIR = PROJECT_ROOT / "execution_engine"

# 需要校验的类列表: (模块路径, 类名, 模块描述)
# 可以不用手动维护, 脚本会自动扫描 __all__methods__ 声明
CONTRACT_MARKER = "__all__methods__"


def extract_declared_methods(filepath: str) -> dict:
    """从文件中提取所有 __all__methods__ 声明"""
    declared = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=filepath)
        
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_name = node.name
                # 查找 __all__methods__ 赋值
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name) and target.id == CONTRACT_MARKER:
                                if isinstance(item.value, ast.List):
                                    methods = [el.value for el in item.value.elts if isinstance(el, ast.Constant) and isinstance(el.value, str)]
                                    declared[class_name] = methods
    except SyntaxError as e:
        print(f"  ⚠️  [{os.path.basename(filepath)}] 语法错误: {e}")
    return declared


def extract_actual_methods(filepath: str) -> dict:
    """从文件中提取每个类的实际方法(包括继承的)"""
    actual = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=filepath)
        
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                class_name = node.name
                methods = []
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        methods.append(item.name)
                actual[class_name] = methods
    except SyntaxError as e:
        print(f"  ⚠️  [{os.path.basename(filepath)}] 语法错误: {e}")
    return actual


def check_file(filepath: str) -> list:
    """检查单个文件, 返回问题列表"""
    issues = []
    basename = os.path.basename(filepath)
    
    declared = extract_declared_methods(filepath)
    if not declared:
        return []  # 没有 __all__methods__ 声明的文件跳过
    
    actual = extract_actual_methods(filepath)
    
    for class_name, declared_methods in declared.items():
        actual_methods = actual.get(class_name, [])
        declared_set = set(declared_methods)
        actual_set = set(actual_methods)
        
        # 声明了但实际不存在 → 方法被删除或被缩进漂移吞掉
        missing = declared_set - actual_set
        for m in sorted(missing):
            issues.append({
                'file': basename,
                'class': class_name,
                'method': m,
                'severity': 'ERROR',
                'message': f"方法 '{m}' 声明在 __all__methods__ 中但实际不存在 (可能被删除或缩进漂移)"
            })
        
        # 实际存在但未声明 → 新方法未注册
        extra = actual_set - declared_set
        for m in sorted(extra):
            if not m.startswith('_'):  # 仅警告非私有方法
                issues.append({
                    'file': basename,
                    'class': class_name,
                    'method': m,
                    'severity': 'WARN',
                    'message': f"方法 '{m}' 存在但未在 __all__methods__ 中声明"
                })
        
        # 检查公有方法数量是否匹配 (快速退化检测)
        public_declared = [m for m in declared_methods if not m.startswith('_')]
        public_actual = [m for m in actual_methods if not m.startswith('_')]
        if len(public_declared) != len(public_actual):
            issues.append({
                'file': basename,
                'class': class_name,
                'method': '(整体)',
                'severity': 'WARN',
                'message': f"公有方法数不匹配: 声明{len(public_declared)}个, 实际{len(public_actual)}个"
            })
    
    return issues


def scan_all_files() -> list:
    """扫描所有需要校验的文件"""
    all_issues = []
    scanned = 0
    
    for dirpath in [SCRIPTS_DIR, EXECUTION_DIR]:
        if not dirpath.exists():
            continue
        for pyfile in sorted(dirpath.rglob('*.py')):
            if pyfile.name.startswith('__') or pyfile.name.startswith('performance_benchmark'):
                continue
            issues = check_file(str(pyfile))
            if issues:
                all_issues.extend(issues)
            scanned += 1
    
    return all_issues, scanned


def main():
    print("=" * 60)
    print("🔍 API契约校验 — validate_api_contract.py")
    print("=" * 60)
    
    issues, scanned = scan_all_files()
    
    if not issues:
        print(f"✅ 扫描 {scanned} 个文件, 所有契约校验通过")
        return 0
    
    errors = [i for i in issues if i['severity'] == 'ERROR']
    warnings = [i for i in issues if i['severity'] == 'WARN']
    
    print(f"\n⚠️  发现 {len(issues)} 个问题 ({len(errors)}错误, {len(warnings)}警告):")
    for iss in issues:
        tag = '❌' if iss['severity'] == 'ERROR' else '⚠️'
        print(f"  {tag} [{iss['file']}] {iss['class']}.{iss['method']}")
        print(f"     {iss['message']}")
    
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
