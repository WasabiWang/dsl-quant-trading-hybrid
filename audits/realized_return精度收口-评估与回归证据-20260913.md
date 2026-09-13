# realized_return 精度收口 — 评估与回归证据（2026-09-13）

> 关联：`scripts/rank_ic_monitor.py` P2 修复（commit `3645970`）后的同类写入点收口。
> 状态：**代码已改，未提交**；`core/calibration_feedback.py` 属置信度校准闭环（🟡 需 owner 确认）。

## 1. 问题

`realized_return` 有 **3 个写入点**，此前都写 `round(actual, 4)`：

| 写入点 | 位置 | 角色 |
|---|---|---|
| `rank_ic_monitor.fill_realized` | `scripts/rank_ic_monitor.py` | 15:40 Cron IC 监控（**已修 → 全精度**） |
| `backfill_realized_checks.main` | `scripts/backfill_realized_checks.py:145` | 09:05 一次性/日常回填 |
| `CalibrationFeedback.check_realized_accuracy` | `core/calibration_feedback.py:432` | 置信度校准闭环 |

三者共享同一字段，且各自带幂等短路（`realized_return` / `realized_checked` 为真即跳过）
→ **谁先写谁生效**。只修一个写入点等于没修：4 位小数会把接近的截面收益压成并列，
改变下游 Spearman 排名（Pearson IC 亦有量化误差）。

## 2. 变更

三处统一为 **保留全精度写入**，格式化只留在展示层：

```python
# before
s["realized_return"] = round(actual, 4)
# after
s["realized_return"] = actual          # 全精度；展示层自行 %.2f / %.4f
```

**只改精度**：不触碰方向判定（`realized_correct`）、阈值、`realized_checked` 语义、
`correct_predictions` 聚合、`stock_accuracy` 回写 → 校准闭环语义不变。

不重写历史：`realized_checked=True` 的行完全不动；已写入的 4 位历史值保持原样。

## 3. 回归证据

### 3.1 单元（L0）
- 新增 `tests/test_unit.py::TestRealizedReturnFullPrecision`（2 用例）
  - `test_calibration_feedback_keeps_full_precision`：monkeypatch `_fetch_actual_return`，
    断言 `realized_return == ACTUAL` 且 `round(v,4) != v`
  - `test_backfill_realized_checks_keeps_full_precision`：临时校准文件 + 假 K 线，
    断言写入值未 round
- 运行结果：`pytest -k RealizedReturnFullPrecision` → **2 passed**；`bash harness.sh l0` → **全部通过**

### 3.2 端到端（真实代码路径 + 真实麦蕊 K 线，只写 /tmp 副本）
| 驱动 | 写入/改写 | 4 位残留 | 历史行(checked=True)被改写 |
|---|---|---|---|
| `calibration_feedback`（现状麦蕊源） | 0 | — | 0 |
| `calibration_feedback`（路径不变 + 修正列映射的真实 K 线） | 32 | **0** | **0** |
| `backfill_realized_checks`（09:05 真实路径） | 3 | **0** | **0** |

- 小数位分布：16–18 位（float 原生），无 4 位值
- 典型改写：`-0.031`（4 位） → `-0.030980828007779994`（全精度）—— 正是此前被压缩的证据
- 生产文件 `sha256` 运行前后一致 → 验证过程零副作用

## 4. ⚠️ 验证过程中发现的**新缺陷**（阻断性，非本任务范围）

`core/calibration_feedback.py::_fetch_actual_return` 的**麦蕊主源分支恒返回 `None`**：

```python
df = pd.DataFrame(rows)                 # 麦蕊列: t,o,h,l,c,v,a,pc,sf
df["date"] = pd.to_datetime(df["t"]).dt.date
return self._calc_return_from_df(df, pred_date, horizon_days)
```
而 `_calc_return_from_df` 只认 `close` / `收盘`：
```python
pred_close = float(df.loc[pred_idx, "close"] if "close" in df.columns else df.loc[pred_idx, "收盘"])
```
→ 麦蕊行未映射 `c → close` ⇒ `KeyError` ⇒ 内层 `except: return None`
⇒ **`check_realized_accuracy` 实际空跑（写入 0 条）**，且因该分支是 `return`（非抛错），
降级用的 akshare 分支永远不会被触发（akshare 实测封锁）。

**影响**：声称「v4.6.9i 换麦蕊保住校准闭环」，实际该闭环的兑现阶段从未真正运行；
pending 行只能靠 `rank_ic_monitor` / `backfill_realized_checks` 消化。
本任务对 `calibration_feedback` 的精度修复因此**目前是防御性的**——修好列映射后才会生效。

## 5. 遗留（已开 follow-up 卡片）

1. `_fetch_actual_return` 列映射修复（见 §4）——修复后 `calibration_feedback` 才真正参与兑现。
   → **已于 2026-09-13 修复**，见 §6。
2. `scripts/backfill_realized_checks.py` 缺少 `target_day` 守卫：`future_close = km[target_day]`
   在全票交易日并集上取值、却从单标的 K 线索引 —— 与 `rank_ic_monitor` 的 P1-5 同类，
   停牌/缓存不全时 `KeyError` 中断整批回填。**仍未修**。

## 6. §4 修复记录（2026-09-13，分支 `openclaw/calibration-feedback`，commit `12a25b7`）

**改动**（`core/calibration_feedback.py`，只改数据获取，不动方向/阈值）：
- 新增模块常量 `MAIRUI_KLINE_COLUMNS`（`t→date_raw, c→close, ...`）；
  麦蕊主源 `df = pd.DataFrame(rows).rename(columns=MAIRUI_KLINE_COLUMNS)` 后由 `date_raw` 派生 `date`。
- `_calc_return_from_df` 按 `CLOSE_COLUMN_CANDIDATES=(close,收盘,c)` 解析收盘列，缺失即 `None`（不外溢）。
- 主源改为「仅当收益非 `None` 才 `return`」；异常/空数据/窗口外一律继续走 akshare 降级链。

**回归**：`tests/test_unit.py::TestMairuiActualReturnFallback`（7 用例：列映射取值、空/异常/窗口外真降级、
`c` 别名、缺失收盘列、`check_realized_accuracy` 全精度写入）。`harness.sh l0`/`l1` 通过；
L2/L3/Property 失败项经 `git stash` 基线比对逐项一致（worktree 缺本地数据/配置，非本改动引入）。

**端到端**（真实麦蕊 K 线，只写 `/tmp/pc_calibration_verify.json` 副本，`max_days=60`）：
| 指标 | 结果 |
|---|---|
| pending 兑现 | **0 → 12 条**（修复前恒 0） |
| 写入精度 | 12/12 为 17–18 位全精度，**0 条 4 位残留** |
| 幂等复跑 | `checked=0`、新增 0 |
| 生产 JSON | sha256 前后一致（零副作用） |

典型值 `000858 @2026-09-04 = -0.030980828007779994`，与 §3.2 记录的被压缩值 `-0.031` 完全对应
→ 交叉验证麦蕊路径已真正生效。

**观察（未改，另议）**：`check_realized_accuracy` 的资格门槛用**日历日**（`days_elapsed >= horizon_days`），
而 `_calc_return_from_df` 的目标位按 **K 线行序**取且 `min(...)` 静默截断 —— 数据源尚未推进到
满 horizon 个交易日时，会以「不足 horizon 的实际窗口」成交（如 5d 实际只覆盖 3 个交易日）。
与本任务领域相近，但属语义变更，未动。

**环境备注**：akshare 降级源在本机仍不可用（`push2his.eastmoney.com` ProxyError），
故本次 12 条均出自修复后的麦蕊主源 —— 降级链已正确接线，但降级源自身可用性另需治理。
