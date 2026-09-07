# Rank IC 状态语义与风控隔离 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` and `dsl-harness-engineering` to implement this plan task-by-task.

**Goal:** 修复 Dashboard 将陈旧的历史 Rank IC 误报为“当前模型失效”的问题，同时保留现有保守开仓约束，待当前模型形成足够兑现样本并完成影子验证后再决定是否切换交易 gate。

**Architecture:** 将 Rank IC 输出拆成三层：`historical_summary` 负责历史连续性，`current_summary` 负责当前模型族的可评价质量，`risk_gate` 明确记录交易约束及其依据。数据新鲜度、样本成熟度和模型族资格先于质量判定；HAC 显著性只进入影子状态，首个发布单元不改变现有开仓数量。

**Tech Stack:** Python 3、NumPy/Pandas、JSON、FastAPI data adapter、原生 JavaScript、pytest、DSL harness。

---

## 发布边界

### Release A — P0 事实语义修复（可自主执行）

- 增加 `as_of_date`、`mature_cutoff_date`、`lag_trading_days`、`model_family`、`n_mature_days`。
- 把当前状态区分为 `stale` / `insufficient_data` / `healthy` / `degraded` / `critical` / `drifted` / `data_issue`。
- Dashboard 展示真实评估区间，不再使用“当前失效”的硬断言。
- `risk_gate` 继续继承当前历史 degraded 约束：**新开仓上限仍为1只**，不放宽风控。

### Release B — P1 统计与版本分群（先影子运行）

- 当前模型族单独计算 Rank IC。
- 增加 Newey-West/HAC 标准误、置信区间和单侧 p 值，处理5日收益窗口重叠。
- 生成 `shadow_quality_status`，但不直接驱动交易。

### Release C — P2 gate 切换（需 James 明确批准）

- 当前模型族至少10个成熟交易日、数据新鲜、覆盖率合格后，比较旧 gate 与新 gate。
- 回测/影子结果达标后，才允许从 `legacy_conservative` 切换到 `current_significant`。

---

## 目标数据契约

`confidence_data/rank_ic_series.json` 保留现有 `series` 和旧字段，并新增：

```json
{
  "schema_version": 2,
  "updated_at": "2026-09-07T15:40:15+08:00",
  "series": [
    {
      "date": "2026-08-18",
      "model_version": "v4.6.9i",
      "model_family": "v4.6",
      "n": 25,
      "coverage": 1.0,
      "rank_ic": 0.1862,
      "ic": -0.0557
    }
  ],
  "historical_summary": {
    "quality_status": "degraded",
    "freshness_status": "stale",
    "as_of_date": "2026-08-18",
    "window_start": "2026-07-20",
    "window_end": "2026-08-18",
    "recent_mean": -0.0841,
    "n_mature_days": 20
  },
  "current_summary": {
    "model_version": "v4.7.4",
    "model_family": "v4.7",
    "evaluation_status": "insufficient_data",
    "quality_status": "unknown",
    "freshness_status": "unknown",
    "actionable": false,
    "as_of_date": null,
    "latest_prediction_date": "2026-09-07",
    "mature_cutoff_date": "<按交易日历计算>",
    "lag_trading_days": null,
    "n_mature_days": 0,
    "min_required_days": 10,
    "hac": null
  },
  "risk_gate": {
    "policy": "legacy_conservative",
    "effective_status": "degraded",
    "max_new_positions": 1,
    "basis": "historical_summary",
    "reason": "当前模型族样本不足，暂继承最后一个有效历史告警"
  },
  "data_gaps": [
    {
      "start": "2026-08-19",
      "end": "2026-09-02",
      "reason": "prediction archive unavailable",
      "recoverable": false
    }
  ]
}
```

状态判定顺序固定为：

```text
数据异常 → data_issue
当前模型族无成熟行 → insufficient_data
有成熟行但落后成熟截止日过多 → stale
成熟行不足最低样本数 → insufficient_data
其余才进入 critical / degraded / drifted / healthy 质量判定
```

`stale` 与 `insufficient_data` 均不得被翻译成“当前模型失效”。

---

### Task 1: 用失败测试锁定状态语义

**Files:**
- Create: `tests/test_rank_ic_monitor.py`
- Modify: `tests/dashboard_contract_audit_test.py`
- Modify: `tests/v4_6_regression_test.py`

**Step 1: 写纯函数测试夹具**

构造三个模型族：旧 `v4.6` 有20个成熟日、当前 `v4.7` 无成熟日、当前 `v4.7` 有足够成熟日。

```python
def _row(day, ic, version="v4.7.4", coverage=1.0):
    return {
        "date": day,
        "model_version": version,
        "model_family": ".".join(version.split(".")[:2]),
        "rank_ic": ic,
        "coverage": coverage,
        "n": 24,
    }


def test_current_model_without_mature_rows_is_insufficient_not_degraded():
    result = build_status_payload(
        series=[_row("2026-08-18", -0.08, "v4.6.9i")],
        daily_records=[{"date": "2026-09-07", "version": "v4.7.4"}],
        current_version="v4.7.4",
        mature_cutoff_date="2026-08-31",
        thresholds={"min_status_days": 10, "max_lag_sessions": 2},
    )
    assert result["current_summary"]["evaluation_status"] == "insufficient_data"
    assert result["current_summary"]["actionable"] is False
    assert result["historical_summary"]["recent_mean"] == -0.08
```

**Step 2: 写混合版本不串池测试**

```python
def test_old_negative_ic_does_not_label_current_family_degraded():
    old = [_row(f"2026-08-{d:02d}", -0.20, "v4.6.9i") for d in range(1, 11)]
    current = [_row(f"2026-09-{d:02d}", 0.10, "v4.7.4") for d in range(1, 11)]
    result = build_status_payload(old + current, [], "v4.7.4", "2026-09-10", THRESHOLDS)
    assert result["current_summary"]["recent_mean"] == 0.10
    assert result["current_summary"]["model_family"] == "v4.7"
```

**Step 3: 写 stale 优先级测试**

```python
def test_stale_precedes_negative_quality_status():
    result = classify_current_status(
        recent_mean=-0.20,
        n_days=20,
        lag_trading_days=5,
        coverage=1.0,
        max_lag_sessions=2,
    )
    assert result["evaluation_status"] == "stale"
    assert result["actionable"] is False
```

**Step 4: 写 Dashboard 契约测试**

- `/api/rank-ic` 必须包含 `current_summary`、`historical_summary`、`risk_gate`。
- 静态前端不得再包含字符串 `模型预测力当前失效`。
- `stale` 和 `insufficient_data` 必须有独立状态映射。

**Step 5: 写 gate 等价回归测试**

```python
def test_legacy_conservative_gate_keeps_one_new_position():
    gate = {"policy": "legacy_conservative", "max_new_positions": 1}
    assert resolve_rank_ic_new_position_cap(gate, available=5) == 1
```

**Step 6: 运行并确认测试先失败**

```bash
.venv/bin/python -m pytest tests/test_rank_ic_monitor.py -q
.venv/bin/python -m pytest tests/dashboard_contract_audit_test.py -q
.venv/bin/python -m pytest tests/v4_6_regression_test.py -q
```

Expected: 因新函数/字段尚不存在而失败。

**Step 7: Commit**

```bash
git add tests/test_rank_ic_monitor.py tests/dashboard_contract_audit_test.py tests/v4_6_regression_test.py
git commit -m "test(rank-ic): define freshness version and gate contracts"
```

---

### Task 2: 增加交易日成熟度与模型族纯函数

**Files:**
- Modify: `scripts/rank_ic_monitor.py`
- Test: `tests/test_rank_ic_monitor.py`

**Step 1: 增加稳定的模型族归一化**

```python
import re


def normalize_model_family(version: str) -> str:
    match = re.match(r"^v?(\d+)\.(\d+)", str(version or ""))
    return f"v{match.group(1)}.{match.group(2)}" if match else "unknown"
```

说明：当前历史记录已有 daily-record 级 `version`，v4.7.3.1 与 v4.7.4 均归入 `v4.7`；不得按每日 `training_hash` 分群，否则每天重训都会清空样本。

**Step 2: 计算可兑现截止日**

```python
def mature_cutoff(trading_days: list[str], horizon: int = 5) -> str | None:
    ordered = sorted(d for d in trading_days if d <= datetime.now().strftime("%Y-%m-%d"))
    return ordered[-(horizon + 1)] if len(ordered) > horizon else None
```

实现时给函数增加 `as_of_date` 参数，测试中禁止依赖真实当前时间。

**Step 3: 用交易日而非自然日计算滞后**

```python
def trading_session_lag(as_of_date, cutoff_date, trading_days):
    eligible = [d for d in sorted(trading_days) if as_of_date < d <= cutoff_date]
    return len(eligible)
```

**Step 4: 给 IC 行补充版本来源**

在 `compute_ic_series()` 中读取 `dr.get("version", "unknown")`，每行写入：

```python
"model_version": model_version,
"model_family": normalize_model_family(model_version),
```

**Step 5: 运行定向测试**

```bash
.venv/bin/python -m pytest tests/test_rank_ic_monitor.py -q
```

Expected: 模型族、成熟截止日、交易日 lag 测试通过。

**Step 6: Commit**

```bash
git add scripts/rank_ic_monitor.py tests/test_rank_ic_monitor.py
git commit -m "feat(rank-ic): add model-family and maturity metadata"
```

---

### Task 3: 拆分历史、当前与 gate 状态

**Files:**
- Modify: `scripts/rank_ic_monitor.py`
- Test: `tests/test_rank_ic_monitor.py`

**Step 1: 保留历史摘要**

将原 `compute_drift()` 重命名为 `compute_quality_summary()`，显式接收已筛选的 series，不再隐式用全历史代表当前模型。

**Step 2: 构造当前模型摘要**

```python
def build_status_payload(series, daily_records, current_version, mature_cutoff_date,
                         trading_days, thresholds):
    family = normalize_model_family(current_version)
    current_rows = [r for r in series if r.get("model_family") == family]
    historical = compute_quality_summary(series, thresholds)
    current = compute_quality_summary(current_rows, thresholds)
    current.update(assess_evaluation_readiness(
        rows=current_rows,
        daily_records=daily_records,
        family=family,
        mature_cutoff_date=mature_cutoff_date,
        trading_days=trading_days,
        thresholds=thresholds,
    ))
    return {
        "historical_summary": historical,
        "current_summary": current,
        "risk_gate": build_legacy_conservative_gate(historical, current),
    }
```

**Step 3: 固定风险优先级**

- K线/coverage异常：`data_issue`
- 当前族最后成熟观测落后成熟截止日超过2个交易日：`stale`
- 当前族成熟日少于10日：`insufficient_data`
- 仅 readiness 合格后才允许输出质量状态。

`min_status_days=10` 与 `max_lag_sessions=2` 首期写入输出 thresholds；是否作为正式交易阈值留到 Release C 审批。

**Step 4: 保留兼容字段**

顶层 `summary` 暂时指向 `historical_summary`，并加：

```python
summary["deprecated"] = True
summary["replacement"] = "current_summary"
```

这样旧消费者不会在同一发布中崩溃；所有新消费者必须使用显式字段。

**Step 5: 风控保持不变**

`build_legacy_conservative_gate()` 按旧逻辑生成 `max_new_positions`：critical=0、degraded=1、其他不设上限。当前 v4.7 样本不足时明确写 `basis=historical_summary`，不得悄悄返回 healthy。

**Step 6: 测试**

```bash
.venv/bin/python -m pytest tests/test_rank_ic_monitor.py -q
.venv/bin/python -m py_compile scripts/rank_ic_monitor.py
```

Expected: 全部通过。

**Step 7: Commit**

```bash
git add scripts/rank_ic_monitor.py tests/test_rank_ic_monitor.py
git commit -m "fix(rank-ic): separate current historical and gate status"
```

---

### Task 4: 增加 HAC 显著性影子指标

**Files:**
- Modify: `scripts/rank_ic_monitor.py`
- Test: `tests/test_rank_ic_monitor.py`

**Step 1: 实现无新增依赖的 Newey-West 统计**

```python
def hac_mean_test(values: list[float], max_lag: int = 4) -> dict | None:
    from statistics import NormalDist

    x = np.asarray(values, dtype=float)
    n = len(x)
    if n < 10:
        return None
    centered = x - x.mean()
    long_run_var = float(np.dot(centered, centered) / n)
    for lag in range(1, min(max_lag, n - 1) + 1):
        weight = 1.0 - lag / (max_lag + 1.0)
        gamma = float(np.dot(centered[lag:], centered[:-lag]) / n)
        long_run_var += 2.0 * weight * gamma
    se = math.sqrt(max(long_run_var, 0.0) / n)
    if se == 0:
        return None
    z = float(x.mean() / se)
    p_value_negative = NormalDist().cdf(z)
    return {
        "mean": round(float(x.mean()), 6),
        "se": round(se, 6),
        "z": round(z, 4),
        "p_value_negative": round(p_value_negative, 6),
        "ci95_low": round(float(x.mean() - 1.96 * se), 6),
        "ci95_high": round(float(x.mean() + 1.96 * se), 6),
        "max_lag": min(max_lag, n - 1),
        "n": n,
    }
```

`p_value_negative` 对应单侧检验 `H1: mean < 0`；负向 z 越小，p 值越小。

**Step 2: 生成 shadow 状态**

```text
watch      = mean < 0 但 p_value_negative >= 0.05
degraded   = mean < 0 且 p_value_negative < 0.05
critical   = mean < critical_mean 且 p_value_negative < 0.05
```

这些规则只写入 `current_summary.shadow_quality_status`，Release B 不进入 `risk_gate`。

**Step 3: 测试自相关样本**

- 负均值但高方差序列必须为 `watch`。
- 持续负值且置信区间上界小于0必须为 `degraded`。
- `n<10` 时 HAC 返回 `None`，状态仍为 `insufficient_data`。

**Step 4: 运行测试**

```bash
.venv/bin/python -m pytest tests/test_rank_ic_monitor.py -q
```

Expected: HAC 数值和状态测试通过。

**Step 5: Commit**

```bash
git add scripts/rank_ic_monitor.py tests/test_rank_ic_monitor.py
git commit -m "feat(rank-ic): add HAC significance shadow evaluation"
```

---

### Task 5: 补齐预测记录可追溯字段并显式记录不可恢复缺口

**Files:**
- Modify: `scripts/feedback_controller.py`
- Modify: `scripts/rank_ic_monitor.py`
- Test: `tests/test_rank_ic_monitor.py`

**Step 1: 保留股票级来源字段**

在 `feedback_controller.py` 创建 daily record 时增加：

```python
rec["training_hash"] = s.get("training_hash", "")
rec["source"] = s.get("source", "")
```

`daily_record["version"]` 已存在，继续作为模型族分群依据；`training_hash` 仅审计，不用于日级分群。

**Step 2: 添加不可恢复缺口元数据**

在 Rank IC 输出中写入 2026-08-19～2026-09-02 的已知缺口；不要生成伪造的零 IC，也不要使用插值。

**Step 3: 校验未来归档链**

确认 `batch_predict.py` 每次17:00预测均写日归档；若已有逻辑只补测试，不改路径。验证 daily record 的版本、预测日期、股票数与归档一致。

**Step 4: 运行测试**

```bash
.venv/bin/python -m pytest tests/test_rank_ic_monitor.py -q
.venv/bin/python -m py_compile scripts/feedback_controller.py scripts/rank_ic_monitor.py
```

Expected: provenance 字段存在，缺口不参与 IC 计算。

**Step 5: Commit**

```bash
git add scripts/feedback_controller.py scripts/rank_ic_monitor.py tests/test_rank_ic_monitor.py
git commit -m "fix(calibration): preserve rank-ic provenance and data gaps"
```

---

### Task 6: 修复 Dashboard 适配层和文案

**Files:**
- Modify: `web_dashboard/data_adapter.py`
- Modify: `web_dashboard/static/app.js`
- Modify: `tests/dashboard_contract_audit_test.py`

**Step 1: 适配层显式透传三层状态**

`get_rank_ic()` 返回：

```python
return {
    "schema_version": data.get("schema_version", 1),
    "current_summary": data.get("current_summary", {}),
    "historical_summary": data.get("historical_summary", data.get("summary", {})),
    "risk_gate": data.get("risk_gate", {}),
    "data_gaps": data.get("data_gaps", []),
    "series": data.get("series", [])[-120:],
    "updated_at": data.get("updated_at", ""),
}
```

总览 `get_full_dashboard()` 的 `rank_ic` 改用 `current_summary`，另加 `rank_ic_gate`，禁止一个字段同时代表模型质量和交易限制。

**Step 2: 增加前端状态映射**

```javascript
const stMap = {
  healthy: ['#0dc9a2', '✅ 当前模型有效'],
  watch: ['#ffb020', '🟡 观察中'],
  degraded: ['#ffb020', '⚠️ 当前模型退化'],
  critical: ['#ff4747', '🚨 当前模型严重退化'],
  stale: ['#ffb020', '⏳ 兑现数据陈旧'],
  insufficient_data: ['#8aa4c8', '🧪 当前模型样本不足'],
  data_issue: ['#ff4747', '🔴 数据异常']
};
```

**Step 3: 替换误导 banner**

- 当前应显示：`历史窗口 07-20～08-18 Rank IC=-0.0841；当前 v4.7 模型尚无足够5日兑现样本。`
- 单独显示：`风控仍按历史 degraded：新开仓上限1只。`
- 只有 `current_summary.actionable=true` 且显著退化时，才允许显示“当前模型退化”。

**Step 4: 展示评估元数据**

卡片增加：当前模型族、评估窗口、成熟样本数、数据截至日、交易日 lag、HAC CI/p 值、已知缺口。

**Step 5: 测试**

```bash
.venv/bin/python -m pytest tests/dashboard_contract_audit_test.py -q
node --check web_dashboard/static/app.js
.venv/bin/python -m py_compile web_dashboard/data_adapter.py
```

Expected: 契约测试、JS语法、Python编译均通过。

**Step 6: Commit**

```bash
git add web_dashboard/data_adapter.py web_dashboard/static/app.js tests/dashboard_contract_audit_test.py
git commit -m "fix(dashboard): distinguish stale historical and current rank ic"
```

---

### Task 7: 让盘前决策读取显式 risk_gate，保持行为等价

**Files:**
- Modify: `scripts/morning_decision.py`
- Modify: `tests/v4_6_regression_test.py`

**Step 1: 拆出纯函数**

```python
def resolve_rank_ic_new_position_cap(gate: dict, available: int) -> int:
    cap = gate.get("max_new_positions")
    return available if cap is None else min(available, max(0, int(cap)))
```

**Step 2: `load_rank_ic_status()` 返回完整契约**

不要再只返回 `summary`；异常或文件缺失时返回：

```python
{
  "current_summary": {"evaluation_status": "data_issue", "actionable": False},
  "risk_gate": {"policy": "fail_safe", "max_new_positions": 1}
}
```

保持风险优先：缺文件不应放开到 healthy。

**Step 3: 使用 gate 而非展示状态控制开仓**

```python
ic_payload = load_rank_ic_status()
ic_gate = ic_payload.get("risk_gate", {})
max_new_buys = resolve_rank_ic_new_position_cap(ic_gate, max_new_buys)
```

当前生产数据的最终结果必须仍为 `max_new_buys <= 1`。

**Step 4: 更新盘前报告文案**

明确分两行：

```text
🧪 当前Rank IC: insufficient_data（v4.7，0/10个成熟日）
⚠️ 风控gate: legacy_conservative（继承历史degraded，新开仓≤1）
```

**Step 5: 测试**

```bash
.venv/bin/python -m pytest tests/v4_6_regression_test.py -q
.venv/bin/python -m py_compile scripts/morning_decision.py
```

Expected: 缺文件、stale、insufficient、degraded、critical 五种输入均 fail-safe；当前行为不放宽。

**Step 6: Commit**

```bash
git add scripts/morning_decision.py tests/v4_6_regression_test.py
git commit -m "fix(decision): consume explicit rank-ic risk gate"
```

---

### Task 8: 生成新产物并完成 DSL 验证闭环

**Files:**
- Modify: `VERSION`
- Runtime output: `confidence_data/rank_ic_series.json`
- Runtime output: `confidence_data/prediction_calibration.json`

**Step 1: 备份运行时产物**

```bash
cp confidence_data/rank_ic_series.json backup/rank_ic_series.pre-status-v2.json
cp confidence_data/prediction_calibration.json backup/prediction_calibration.pre-status-v2.json
```

**Step 2: dry-run 生成并检查**

```bash
.venv/bin/python scripts/rank_ic_monitor.py --dry-run
```

Expected:

```text
current=v4.7
evaluation_status=insufficient_data
historical_window=2026-07-20..2026-08-18
risk_gate=legacy_conservative max_new_positions=1
```

**Step 3: 正式生成**

```bash
.venv/bin/python scripts/rank_ic_monitor.py
```

校验 JSON 非空、schema_version=2、current/historical/gate 三块齐全；09-07 当前模型不能被标成 degraded。

**Step 4: 测试金字塔**

当前仓库 `harness.sh l0/l1` 指向的 `tests/test_unit.py`、`tests/test_component.py` 不存在，因此本变更以定向 pytest 作为 L0/L1，并补跑现存 harness 层级：

```bash
.venv/bin/python -m pytest tests/test_rank_ic_monitor.py tests/dashboard_contract_audit_test.py tests/v4_6_regression_test.py -q
bash harness.sh quick
bash harness.sh l2
bash harness.sh golden
```

Expected: 全绿；如出现既有失败，必须用变更前基线对比证明零新增回归。

**Step 5: 静态验证**

```bash
.venv/bin/python -m py_compile scripts/rank_ic_monitor.py scripts/feedback_controller.py scripts/morning_decision.py web_dashboard/data_adapter.py
node --check web_dashboard/static/app.js
rg -n "模型预测力当前失效" web_dashboard/static/app.js
```

Expected: compile/check 通过；最后一个 `rg` 无结果。

**Step 6: 检查 Dashboard 三张映射表**

本变更不改 cron 名、task_name 或输出路径，但仍确认 `_map_task_id`、`_CRON_NAME_TO_LOGICAL`、`_alias_map`、`DATA_DIR_MAP` 无需调整。

**Step 7: 更新版本并重启 Dashboard**

将 `VERSION` 升为下一补丁版本；按当前服务管理方式重启 Dashboard，然后验证：

```bash
curl -fsS http://127.0.0.1:8888/api/rank-ic
curl -fsS http://127.0.0.1:8888/api/full
curl -fsS http://127.0.0.1:8888/api/pipeline
curl -fsS http://127.0.0.1:8888/api/version
```

Expected: HTTP 200；版本一致；模型页显示“当前样本不足”，同时明确 gate 仍限1只。

**Step 8: Commit**

```bash
git add VERSION scripts/rank_ic_monitor.py scripts/feedback_controller.py scripts/morning_decision.py web_dashboard/data_adapter.py web_dashboard/static/app.js tests
git commit -m "fix(rank-ic): make model quality status freshness-aware"
```

---

### Task 9: 影子验证与 Release C 决策包

**Files:**
- Create: `reports/rank_ic/rank_ic_gate_shadow_<date>.json`
- Create: `reports/rank_ic/rank_ic_gate_decision_<date>.md`

**Step 1: 每日并行记录旧/新状态**

至少记录：日期、current family、成熟日数、recent mean、HAC CI/p、coverage、legacy cap、shadow cap、当日候选数量、模拟成交及后续5日收益。

**Step 2: 满足最小决策样本**

进入 gate 评审必须同时满足：

- 当前模型族 `n_mature_days >= 10`；
- 最新成熟观测距 `mature_cutoff_date <= 2` 个交易日；
- 最近10日最小 coverage >= 60%；
- 无不可解释的数据断层；
- 旧/新策略回放可复现。

**Step 3: 回放 gate 差异**

对旧20日与当前族分别回放：

- 新开仓数量；
- 5日方向命中率；
- 多空分位差；
- 收益、最大回撤、盈亏比；
- 旧 gate 与新 gate 的增量风险。

**Step 4: 提交 James 审批**

只提交三个选项：

1. 保持 `legacy_conservative`；
2. 切换 `current_significant`；
3. 延长观察至20个成熟日。

在 James 明确批准前，不修改 `risk_gate.policy`，不放宽新开仓上限。

---

## 验收标准

1. Dashboard 不再把07-20～08-18窗口称为“当前模型失效”。
2. 当前 v4.7 模型在未满成熟样本时显示 `insufficient_data`，而非 healthy/degraded。
3. 历史 `-0.0841` 仍可见、带窗口和版本范围，不抹掉真实风险信号。
4. 盘前决策继续最多新开1只，且报告明确 gate 依据。
5. 旧消费者在过渡期不因 schema v2 崩溃。
6. HAC 指标只做影子判断，未审批前不改变交易。
7. 定向测试、现有 quick/L2/golden、py_compile、JS check、Dashboard 四个 API 全部通过。
8. 产物可从 `backup/*.pre-status-v2.json` 恢复。

## 回滚方案

1. 回滚代码 commit；
2. 恢复 `rank_ic_series.pre-status-v2.json` 与 `prediction_calibration.pre-status-v2.json`；
3. 恢复 `VERSION`；
4. 重启 Dashboard；
5. 再次检查 `/api/rank-ic`、`/api/full`、`/api/version`；
6. 风控 gate 在任何回滚路径下不得比当前“新开仓≤1”更宽松。
