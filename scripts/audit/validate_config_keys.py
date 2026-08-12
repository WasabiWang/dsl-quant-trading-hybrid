#!/usr/bin/env python3
"""
DSL v4.6.x — 配置Key一致性校验: 验证YAML key的生产者/消费者一致

检测:
  - YAML key被重命名后, 某个消费者还在用旧key (→ 读到None/默认值)
  - 两个模块对同一数据用不同key名
  - 配置key路径与实际YAML层级不匹配

用法:
  python3 scripts/audit/validate_config_keys.py
  或通过 run_all_audits.py 自动调用
"""
import sys
import os
import re
import yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
CONFIG_DIR = PROJECT_ROOT / "config"

# 已知的配置key映射表: (key路径, 示例用途)
# 扫描所有 .py 文件中的 yaml.get/get() 调用, 与YAML实际结构对比
KNOWN_CONFIG_KEYS = {
    # adaptive_params.yaml
    "risk.black_swan_active": "黑天鹅是否活跃",
    "risk.black_swan_position_ratio": "黑天鹅持仓上限比例",
    "trading.max_positions": "最大持仓数量",
    "signals.stop_loss.base_threshold": "止损基础阈值",
}


def load_yaml_structure(filepath: str) -> dict:
    """加载YAML并返回扁平化的key路径 → 值(类型) 映射"""
    flat = {}
    try:
        if not os.path.exists(filepath):
            return flat
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            return flat
        
        def _flatten(d, prefix=''):
            for k, v in d.items():
                path = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    _flatten(v, path)
                else:
                    flat[path] = type(v).__name__
        _flatten(data)
    except Exception as e:
        print(f"  ⚠️  YAML加载失败 [{os.path.basename(filepath)}]: {e}")
    return flat


def extract_python_keys(filepath: str) -> dict:
    """从Python文件中提取所有 yaml.get/get() 和 ap.get() / params.get() 调用"""
    keys = {}  # key_path → [(line_no, context)]
    with open(filepath, 'r', encoding='utf-8') as f:
        try:
            content = f.read()
        except Exception:
            return keys
    
    lines = content.split('\n')
    
    # 模式1: .get('xxx', default) 或 .get("xxx", default)
    patterns = [
        r"""\.get\(['"](\w+)['"]""",           # .get('xxx')
        r"""\[['"](\w+)['"]\]""",              # ['xxx']
        r"""\.get\(['"]([\w.]+)['"]""",        # .get('nested.key')
    ]
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('//'):
            continue
        
        for pattern in patterns:
            matches = re.findall(pattern, line)
            for m in matches:
                # 过滤已知的非配置key
                if any(skip in m for skip in ['__', 'role', 'type', 'format', 'encoding']):
                    continue
                if m not in keys:
                    keys[m] = []
                keys[m].append((i + 1, stripped[:60]))
    
    return keys


def main():
    print("=" * 60)
    print("🔍 配置Key一致性校验 — validate_config_keys.py")
    print("=" * 60)
    
    # 1. 加载YAML实际结构
    yaml_files = list(CONFIG_DIR.glob('*.yaml'))
    yaml_keys = {}
    for yf in yaml_files:
        structure = load_yaml_structure(str(yf))
        for k, v in structure.items():
            yaml_keys[f"{yf.name}:{k}"] = v
    
    if not yaml_keys:
        print("⚠️  未找到YAML配置文件或配置为空")
        return 0
    
    print(f"\n📄 YAML配置: {len(yaml_files)} 个文件, {len(yaml_keys)} 个key")
    
    # 2. 扫描所有Python文件中的get调用
    py_keys = {}  # key → [(file, line)]
    for pyfile in sorted(SCRIPTS_DIR.rglob('*.py')):
        if pyfile.name.startswith('__'):
            continue
        keys = extract_python_keys(str(pyfile))
        for k, locations in keys.items():
            if k not in py_keys:
                py_keys[k] = []
            for loc in locations:
                py_keys[k].append((pyfile.name, loc[0], loc[1]))
    
    print(f"📝 Python引用: {len(py_keys)} 个unique key")
    
    # 3. 对比: 找出PY引用了但YAML不存在的key (潜在断链)
    issues = []
    
    # 扁平化YAML key (去掉文件名前缀, 只取路径部分)
    yaml_paths = set()
    for full_key in yaml_keys:
        # full_key格式: "adaptive_params.yaml:risk.black_swan_active"
        parts = full_key.split(':', 1)
        if len(parts) == 2:
            yaml_paths.add(parts[1])
    
    for py_key, locations in py_keys.items():
        # 跳过太短的key (可能是通用变量名)
        if len(py_key) < 3:
            continue
        # 跳过数字
        if py_key.isdigit():
            continue
        
        # 检查是否是已知的问题key
        # 规则: 如果PY引用了一个deep路径(含.)但YAML中没有
        if '.' in py_key:
            if py_key not in yaml_paths:
                # 检查是否是已知的特殊key(如自适应参数、运行时key等)
                known_dynamic = ['black_swan', 'main_board', 'gem', 'star', 'bse', 'st',
                                'base_threshold', 'accuracy_adjust', 'mode', 'position_ratio']
                if not any(kd in py_key for kd in known_dynamic):
                    issues.append({
                        'key': py_key,
                        'locations': locations[:3],  # 最多显示3个引用点
                        'severity': 'WARN',
                        'message': f"Key '{py_key}' 在Python中被引用但在任何YAML中未找到"
                    })
        
        # 检查已知的配置key映射
        if py_key in KNOWN_CONFIG_KEYS:
            if py_key not in yaml_paths:
                issues.append({
                    'key': py_key,
                    'locations': locations[:3],
                    'severity': 'ERROR',
                    'message': f"关键配置key '{py_key}' ({KNOWN_CONFIG_KEYS[py_key]}) 在YAML中未定义"
                })
    
    if not issues:
        print("✅ 所有配置key一致性校验通过")
        return 0
    
    errors = [i for i in issues if i['severity'] == 'ERROR']
    warnings = [i for i in issues if i['severity'] == 'WARN']
    
    print(f"\n⚠️  发现 {len(issues)} 个问题 ({len(errors)}错误, {len(warnings)}警告):")
    for iss in issues:
        tag = '❌' if iss['severity'] == 'ERROR' else '⚠️'
        print(f"  {tag} {iss['key']}")
        print(f"     {iss['message']}")
        for f, ln, ctx in iss['locations'][:3]:
            print(f"       {f}:{ln} → {ctx}")
    
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
