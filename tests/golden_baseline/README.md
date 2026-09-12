# golden_baseline — 金标准基线 (Golden Test)

本目录存放 **Golden Test 层**（测试金字塔最高层，pre-deploy 回归门）的基线。
实现见 `tests/golden_test.py`，入口 `bash harness.sh golden`。

## 文件说明

| 文件 | 是否入库 | 说明 |
|------|---------|------|
| `system_snapshot.json` | ❌ 不入库（`.gitignore` 的 `*.json`） | **真实基线**，运行时产物，由 `--accept` 在私有环境生成 |
| `system_snapshot.example.json` | ✅ 入库 | 格式模板/占位值，仅供理解字段，**不可用于比对** |
| `README.md` | ✅ 入库 | 本文件 |

真实基线不入库的原因有二：
1. 它是运行时产物（随股票池/假日/脚本变化）；
2. 它由私有配置派生，公开仓库不携带私有配置（`.gitignore` 排除 `config/master_stock_pool.yaml` 等）。

## 基线格式 (snapshot_format=2, public-repo-safe)

只保存「仓库中已公开」或「弱标识」的内容：

```json
{
  "snapshot_format": 2,
  "generated_at": "…",
  "version": "v4.7.5",
  "privacy": { "policy": "public-repo-safe", "excluded": ["标的代码明细", "脚本文件名清单"] },
  "master_pool": { "count": 24, "hash": "4f70cdcead45" },
  "holidays":    { "count": 75, "hash": "2cd7219db47a", "list": ["2026-01-01", "…"] },
  "scripts":     { "count": 118, "hash": "67fd28f2ad72" },
  "circuit_breaker_paused": false
}
```

隐私边界：

- **股票池**：只存 计数 + 12 位截断 sha256 指纹，**永不存标的代码明细**。
  明细的唯一来源是私有 `config/master_stock_pool.yaml`。
- **脚本**：只存 计数 + 指纹（部分内部脚本被 `.gitignore` 排除，不公开文件名清单）。
- **假日**：完整列表可存 —— 属公开信息，已在 `config/holiday_calendar.py`。
  （保留列表是为了区分「新增假日(可能正常)」与「移除假日(危险)」。）
- **漂移明细**（含标的代码）：由 `--explain` 写入 `cache/`（已被 `.gitignore` 排除），仅供本地人工评审。
- `t0_privacy_guard()` 会在每次运行时扫描基线，出现成串 6 位标的代码即 FAIL（防旧逻辑回潮）。

> 注意：12 位截断 sha256 在池子很小时存在被枚举的理论风险。`--accept` 在池 < 8 只时会打印告警，
> 此时该基线更应只留在本地。

## 行为契约（公开克隆 + 私有配置注入）

| 环境 | 判定 | 行为 |
|------|------|------|
| 全新克隆/换机（无私有配置、无运行数据） | `PUBLIC_CLONE` | T0–T3 全 **SKIP**（非 FAIL），打印注入指引，exit 0 |
| 已注入私有配置，基线缺失 | `PRIVATE_NO_BASELINE` | T1 **SKIP** + 提示 `--accept`；T2/T3 照常执行 |
| 私有配置 + 基线齐备 | `PRIVATE_READY` | 完整比对（允许 WARN） |

`SKIP ≠ FAIL`：缺输入时本层**无法判定**，不应伪装成「回归失败」。
CI/pre-deploy 要求基线必须存在时使用 `--strict`：任何 SKIP 升级为 FAIL。

## 常用命令

```bash
bash harness.sh golden                     # 日常：环境缺失→SKIP, exit 0
python3 tests/golden_test.py --snapshot    # 仅 T0+T1（<1s）
python3 tests/golden_test.py --strict      # CI/pre-deploy：SKIP 即失败
python3 tests/golden_test.py --explain     # 写漂移证据到 cache/（含标的明细，本地）
python3 tests/golden_test.py --accept      # 在私有环境生成/更新基线
```

在私有环境首次启用：

```bash
cp config/master_stock_pool.example.yaml config/master_stock_pool.yaml   # 填入你的股票池
python3 tests/golden_test.py --accept
bash harness.sh golden
```
