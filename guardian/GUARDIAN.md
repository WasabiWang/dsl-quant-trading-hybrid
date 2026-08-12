# 🛡️ Guardian 防护子系统 v1.0

> DSL量化交易系统的数据完整性防护独立子系统
>
> 防止cron任务写入污染，提供原子写入、快照回滚、污染检测4层防线

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Guardian 子系统                         │
├───────────┬───────────┬───────────┬───────────┬─────────────┤
│  detector │  atomic   │  detector │ snapshot  │     CLI     │
│  (L1)     │  (L2)     │  (L3)     │  (L4)     │  (管理)     │
├───────────┼───────────┼───────────┼───────────┼─────────────┤
│ 写入前    │ temp→     │ 写入后    │ 写前快照  │ check       │
│ Schema    │ fsync→    │ 解析验证  │ 一键回滚  │ snapshot    │
│ 范围      │ rename    │ 基线比对  │ 48h保留   │ rollback    │
│ 大小突变  │ .last_good│ 时间戳    │ 自动清理  │ verify      │
└───────────┴───────────┴───────────┴───────────┴─────────────┘
```

## 快速开始

```python
from guardian import (
    atomic_write_json,      # 原子写入JSON
    validate_before_write,  # 写前污染检测
    snapshot_before,        # 写前快照
    rollback_to,            # 快照回滚
    safe_read_json,         # 损坏自动恢复读取
)

# ===== cron任务中使用 =====

# 1. 快照当前状态
snapshot_before(["cache/daily_predict.json"], label="pre_batch_predict")

# 2. 数据验证
valid, warnings = validate_before_write("cache/daily_predict.json", new_data)
if not valid:
    raise ValueError(f"数据污染: {warnings}")

# 3. 原子写入
atomic_write_json("cache/daily_predict.json", new_data)

# ===== 损坏恢复 =====
data = safe_read_json("cache/daily_predict.json")  # 自动从.last_good恢复
```

## CLI 工具

```bash
# 运行全部12项健康检查
python3 -m guardian.cli check

# 查看子系统状态
python3 -m guardian.cli info

# 手动打快照
python3 -m guardian.cli snapshot --label "before_upgrade"

# 回滚 (先dry-run)
python3 -m guardian.cli rollback
python3 -m guardian.cli rollback --execute   # 确认执行

# 验证单个文件完整性
python3 -m guardian.cli verify cache/daily_predict.json

# 审计cron写冲突
python3 -m guardian.cli audit
```

## 模块说明

| 模块 | 路径 | 职责 | 函数 |
|------|------|------|------|
| `detector` | `guardian/detector.py` | L1+L3 污染检测 | `validate_before_write`, `validate_after_write`, `quick_integrity_check` |
| `atomic` | `guardian/atomic.py` | L2 原子写入 | `atomic_write_json`, `atomic_write_yaml`, `safe_read_json` |
| `snapshot` | `guardian/snapshot.py` | L4 快照回滚 | `snapshot_before`, `rollback_to`, `list_snapshots`, `cleanup_old_snapshots` |
| `cli` | `guardian/cli.py` | 命令行管理 | `check`, `snapshot`, `rollback`, `verify`, `audit`, `info` |

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0.0 | 2026-07-19 | 初始版本: 4层防线 + CLI + 12 Guards健康检查 |

## 维护指南

### 添加新的Schema验证

编辑 `guardian/detector.py` 中的 `EXPECTED_SCHEMAS` 字典:

```python
EXPECTED_SCHEMAS = {
    "新文件路径.json": {
        "required_keys": [...],
        "types": {...},
        "max_size_kb": 500,
    },
}
```

### 添加新的关键文件快照保护

编辑 `guardian/snapshot.py` 中的 `CRITICAL_STATE_FILES` 列表。

### 升级防线

1. 修改对应 `guardian/` 模块
2. 更新 `guardian/__init__.py` 导出
3. 运行 `python3 -m guardian.cli check` 验证
4. 更新 `guardian/__init__.py` 中的 `__version__`

## 后向兼容

为保持现有代码不中断，`core/` 目录保留了thin wrapper:

```python
# 旧导入仍然有效
from core.atomic_writer import atomic_write_json     # ✅ 实际委托到 guardian
from core.state_snapshot import snapshot_before      # ✅ 实际委托到 guardian
from core.pollution_detector import validate_before_write  # ✅ 实际委托到 guardian

# 新代码推荐使用统一入口
from guardian import atomic_write_json, snapshot_before, validate_before_write
```
