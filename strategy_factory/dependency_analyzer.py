#!/usr/bin/env python3
"""
依赖分析工具 - 梳理模块依赖关系，检测循环依赖和冗余依赖
"""
import os
import ast
import json
from typing import Dict, List, Set, Tuple
from collections import defaultdict
from common.logger import get_logger

logger = get_logger("dependency_analyzer")

class DependencyAnalyzer:
    """依赖分析器"""
    
    def __init__(self, project_root: str):
        self.project_root = project_root
        self.modules = {}  # module_name -> file_path
        self.dependencies = defaultdict(set)  # module -> dependencies
        self.reverse_deps = defaultdict(set)  # module -> dependents
    
    def scan_modules(self, dirs: List[str] = None) -> Dict:
        """扫描项目模块"""
        if dirs is None:
            dirs = ['core', 'agents', 'scripts', 'common', 
                    '0_control_plane', '1_data_platform', '2_strategy_factory',
                    '3_decision_engine', '4_execution_engine', '5_review_engine']
        
        for dirname in dirs:
            dirpath = os.path.join(self.project_root, dirname)
            if not os.path.exists(dirpath):
                continue
            
            for root, _, files in os.walk(dirpath):
                for file in files:
                    if file.endswith('.py') and not file.startswith('_'):
                        filepath = os.path.join(root, file)
                        module_name = self._get_module_name(dirpath, file)
                        self.modules[module_name] = filepath
        
        logger.info(f"扫描到 {len(self.modules)} 个模块")
        return self.modules
    
    def _get_module_name(self, dirpath: str, filename: str) -> str:
        """获取模块名称"""
        rel_path = os.path.relpath(dirpath, self.project_root)
        if rel_path == '.':
            return filename[:-3]
        return f"{rel_path.replace('/', '.')}.{filename[:-3]}"
    
    def analyze_imports(self) -> Dict:
        """分析模块间的导入关系"""
        for module_name, filepath in self.modules.items():
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    tree = ast.parse(f.read())
                
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            dep = alias.name.split('.')[0]
                            if dep in self.modules:
                                self.dependencies[module_name].add(dep)
                                self.reverse_deps[dep].add(module_name)
                    
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            dep = node.module.split('.')[0]
                            if dep in self.modules:
                                self.dependencies[module_name].add(dep)
                                self.reverse_deps[dep].add(module_name)
            except Exception as e:
                logger.warning(f"分析模块失败 {module_name}: {e}")
        
        return dict(self.dependencies)
    
    def find_circular_dependencies(self) -> List[List[str]]:
        """检测循环依赖"""
        circular = []
        visited = set()
        path = []
        
        def dfs(module: str):
            if module in path:
                # 发现循环
                cycle_start = path.index(module)
                cycle = path[cycle_start:] + [module]
                if cycle not in circular:
                    circular.append(cycle)
                return
            
            if module in visited:
                return
            
            visited.add(module)
            path.append(module)
            
            for dep in self.dependencies.get(module, []):
                dfs(dep)
            
            path.pop()
        
        for module in self.modules:
            dfs(module)
        
        logger.info(f"发现 {len(circular)} 处循环依赖")
        return circular
    
    def find_duplicate_modules(self) -> Dict:
        """查找重复/冗余模块"""
        # 简化实现：查找名字相似的模块
        duplicates = {}
        module_names = list(self.modules.keys())
        
        for i, name1 in enumerate(module_names):
            for name2 in module_names[i+1:]:
                # 简单相似度判断
                if name1.split('.')[-1] == name2.split('.')[-1]:
                    if name1 not in duplicates:
                        duplicates[name1] = []
                    duplicates[name1].append(name2)
        
        logger.info(f"发现 {len(duplicates)} 组可能的重复模块")
        return duplicates
    
    def generate_report(self) -> Dict:
        """生成依赖分析报告"""
        return {
            "total_modules": len(self.modules),
            "dependencies": dict(self.dependencies),
            "circular_dependencies": self.find_circular_dependencies(),
            "duplicate_modules": self.find_duplicate_modules(),
            "modules_count": {m: len(self.dependencies.get(m, [])) 
                           for m in self.modules}
        }

# 全局实例
analyzer = None

def get_analyzer(project_root: str = None) -> DependencyAnalyzer:
    global analyzer
    if analyzer is None:
        if project_root is None:
            project_root = os.path.expanduser("~/.openclaw/workspace/dsl-quant-trading-hybrid")
        analyzer = DependencyAnalyzer(project_root)
    return analyzer

if __name__ == "__main__":
    analyzer = get_analyzer()
    analyzer.scan_modules()
    analyzer.analyze_imports()
    
    report = analyzer.generate_report()
    print(f"总模块数: {report['total_modules']}")
    print(f"循环依赖: {len(report['circular_dependencies'])}")
    print(f"重复模块: {len(report['duplicate_modules'])}")