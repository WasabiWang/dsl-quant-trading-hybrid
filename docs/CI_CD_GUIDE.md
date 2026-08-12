# CI/CD 配置说明

## 📋 概述

本项目已配置完整的CI/CD流水线，使用GitHub Actions实现自动化测试和代码质量检查。

## 🔄 流水线阶段

### 1. 代码质量检查 (lint)
- **Black**: 代码格式检查
- **Flake8**: 代码风格检查
- **Pylint**: 代码质量评分（要求≥7.0）

### 2. 单元测试 (test)
- **pytest**: 运行所有测试
- **coverage**: 生成代码覆盖率报告
- **Codecov**: 上传覆盖率到Codecov.io

### 3. 构建检查 (build)
- **导入检查**: 验证所有模块可导入
- **结构检查**: 验证项目目录结构完整

### 4. 文档检查 (docs)
- 检查README.md存在
- 检查docs目录存在
- 统计文档数量

### 5. 安全扫描 (security)
- **Bandit**: Python代码安全扫描

## 📊 触发条件

- **Push到main/develop分支**: 触发完整流水线
- **Pull Request到main分支**: 触发完整流水线

## 📁 文件结构

```
.github/workflows/
└── ci.yml                 # CI/CD工作流配置

tests/
├── __init__.py
├── test_llm.py            # LLM模块测试
└── test_markets.py        # 市场模块测试

pyproject.toml             # pytest和coverage配置
```

## 🚀 如何添加测试

### 添加新测试文件

```python
# tests/test_your_module.py
import pytest
from your_module import YourClass

class TestYourClass:
    def test_something(self):
        instance = YourClass()
        assert instance is not None
```

### 运行本地测试

```bash
# 安装测试依赖
pip install pytest pytest-cov

# 运行所有测试
pytest

# 运行特定测试
pytest tests/test_llm.py -v

# 生成覆盖率报告
pytest --cov=agents --cov=llm --cov-report=html
```

## 📈 查看结果

1. **GitHub Actions**: https://github.com/WasabiWang/dsl-quant-trading-hybrid/actions
2. **Codecov**: https://app.codecov.io/gh/WasabiWang/dsl-quant-trading-hybrid

## 🔧 自定义配置

### 修改Pylint阈值

编辑 `.github/workflows/ci.yml`:
```yaml
- name: Run Pylint
  run: |
    pylint agents/ llm/ markets/ data_sources/ --fail-under=8.0
```

### 添加新的检查项

编辑 `.github/workflows/ci.yml`，添加新的job。

## 💡 最佳实践

1. **每次提交前运行测试**: `pytest`
2. **保持高覆盖率**: 目标>80%
3. **修复lint警告**: 保持代码整洁
4. **审查CI结果**: 每次PR检查CI状态
