# DSL 量化交易系统 — 集成质量检测协议 v1.1

**设计原则**: 融合 docx 9级检测方案 (广度+运维) × quant-full-audit 7维审查 (深度+领域)  
**架构**: 10大领域 × 4层深度 = 40+个检测模块, ~100个检查项  
**运行模式**: 自动化脚本 (L0-L1) + 人工审查 (L2-L3)

> **v1.1 变更** (2026-05-23):  
> + D3回测保真度补充L0/L1基础检查 (填补结构缺陷)  
> + 新增 D10 安全领域 (注入风险/密钥暴露/公网暴露)  
> + 所有检查项新增「量化阈值」列, 明确PASS/WARN/FAIL判定条件  
> + 评分模型改为加权: PASS=1.0, WARN=0.5, FAIL=0.0  
> + 新增基准对比检查 D3.L2.5  
> + 权重再平衡 (D5恢复至6%, 新增D10=3%)  
> + 新增: 检查项依赖拓扑 / 误报管理机制 / 协议自身治理 / FAIL处理SOP附录

---

## 总览矩阵

```
                    L0 自动化        L1 快速          L2 标准           L3 深度
                    每次git push     每日cron         每周审查          版本发布前
                    ─────────       ────────         ────────          ────────
D1 代码与配置       ████░░░░        ██░░░░░░         ░░░░░░░░          ░░░░░░░░
D2 数据管线         ██░░░░░░        ███░░░░░         ████░░░░          ████████
D3 回测保真度       ██░░░░░░        ███░░░░░         █████░░░          ████████
D4 模型质量         ░░░░░░░░        ██░░░░░░         █████░░░          ████████
D5 DSL设计          █░░░░░░░        ░░░░░░░░         ████░░░░          ████████
D6 风控             ██░░░░░░        ████░░░░         ██████░░          ████████
D7 执行层           ░░░░░░░░        ███░░░░░         █████░░░          ████████
D8 系统工程与运维   ██░░░░░░        ██████░░         ██████░░          ██████░░
D9 跨领域集成       █░░░░░░░        ████░░░░         ██████░░          █████░░░
D10 安全            ███░░░░░        ██░░░░░░         ████░░░░          ███░░░░░
```

---

## 领域 D1: 代码与配置健康

### L0 — 自动化 (每次git push)

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D1.L0.1 | Python语法检查 (排除_archive) | `find . -name "*.py" -not -path "*/_archive/*" \| xargs py_compile` | PASS: 0 syntax error; FAIL: ≥1 error | docx L0.1 |
| D1.L0.2 | 导入完整性验证 | `python3 -c "import core, config, ..."` | PASS: 0 ImportError; FAIL: ≥1 import失败 | docx L0.3 |
| D1.L0.3 | VERSION文件一致性 | 扫描所有文件中的版本号 vs VERSION | PASS: 全部一致; WARN: ≤2处不一致; FAIL: >2处不一致 | docx L0.6 |
| D1.L0.4 | 硬编码Token/密码检测 | `grep -rn 'token\|secret\|password' \| grep -v 'os.getenv' \| grep -v '^#'` | PASS: 0命中; FAIL: ≥1命中(排除注释) | skill P0 |

> 🤖 **Vibe Coding陷阱**: AI生成的代码经常把API key硬编码在注释里（"# Token: sk-xxxx"），grep正则需排除注释行。

### L1 — 快速 (每日cron)

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D1.L1.1 | 配置文件YAML有效性 | 逐文件 `yaml.safe_load()` | PASS: 全部有效; FAIL: ≥1解析失败 | docx L2.5 |
| D1.L1.2 | feature_flags与实际运行一致 | cron provider标注 vs 实际调度器 | PASS: 一致; WARN: provider字段过期; FAIL: 矛盾 | docx L2.3 |
| D1.L1.3 | 死代码增量扫描 | `vulture --min-confidence 80` 仅扫描新增函数 | PASS: 0新增死代码; WARN: 1-3个; FAIL: >3个 | docx L0.2 |

### L2 — 标准 (每周审查)

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D1.L2.1 | 依赖锁定对比 | `pip freeze` vs `requirements.txt` | PASS: ≤3差异; WARN: 4-10差异; FAIL: >10差异 | skill 维度7 |
| D1.L2.2 | 测试覆盖率 | pytest --cov (需pytest安装) | PASS: ≥60%; WARN: 30-59%; FAIL: <30%或无测试 | docx L0.5 |

### L3 — 深度 (版本发布前)

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D1.L3.1 | 全量死代码审计 | vulture + 人工确认后清理 | PASS: 清理完毕; WARN: ≤5个遗留; FAIL: >5个 | skill |
| D1.L3.2 | 依赖漏洞扫描 | `pip-audit` 或 `safety check` | PASS: 0高危; WARN: 仅低危; FAIL: ≥1高危CVE | skill |

---

## 领域 D2: 数据管线完整性

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D2.L0.1 | 数据源Token环境变量检测 | `os.getenv('TUSHARE_TOKEN')`, `MAIRUI_LICENCE` | PASS: 主源Token存在; WARN: 仅备用源可用; FAIL: 无任何Token | skill P0 |
| D2.L0.2 | 节假日数据覆盖检查 | 当前年份±1 是否有节假日数据 | PASS: ±1年均有数据; WARN: 仅当前年; FAIL: 当前年也无 | docx + skill |

> 🤖 **Vibe Coding陷阱**: AI喜欢写 `return None` 或 `# TODO: 后续实现` 作为数据源fallback。检查每个 `_load_from_*` 方法是否真的有HTTP调用代码。

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D2.L1.1 | 数据源可用性探测 | 每个数据源发1次真实请求 (带超时5s) | PASS: 主源响应<5s; WARN: 仅备用源可用; FAIL: 全部超时 | docx L2.4 |
| D2.L1.2 | ST股票池与模型一致性 | ST标记的股票是否从候选池排除 | PASS: 0只ST混入; FAIL: ≥1只ST在候选池 | skill 维度1 |
| D2.L1.3 | 数据新鲜度检查 | 最近K线日期 vs 最近交易日 | PASS: 差距≤1交易日; WARN: 2-3交易日; FAIL: >3交易日 | docx L1.2 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D2.L2.1 | 复权一致性验证 | 训练特征用的复权方式 vs 预测时用的复权方式 | PASS: 一致; FAIL: 不一致 (任何差异) | skill 维度1 |
| D2.L2.2 | 数据源优先级实际生效 | 模拟主源故障 → 验证fallback是否触发 | PASS: fallback触发+数据完整; FAIL: fallback未触发或数据缺失 | skill 维度1 |
| D2.L2.3 | 停牌/退市股数据质量 | 抽查停牌期间OHLCV是否为前值填充 | PASS: 填充策略一致; WARN: 部分不一致; FAIL: 无填充(NaN) | skill 维度1 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D2.L3.1 | 前视偏差全量扫描 | 搜索所有 `shift(-1)`, `rolling(center=True)`, `pct_change()` 无 `.shift(1)` | PASS: 0泄漏; WARN: 仅已标注豁免; FAIL: ≥1未豁免泄漏 | skill 维度1 |
| D2.L3.2 | 涨跌停日数据失真系数 | 涨停日OHLCV特殊标记是否生效 | PASS: 涨停/跌停日有标记列; WARN: 有标记但未降权; FAIL: 无标记 | skill 维度1 |
| D2.L3.3 | 幸存者偏差检测 | 当前股票池 vs 历史全量 → 是否有退市股缺失 | PASS: 退市股在历史数据中保留; FAIL: 退市股被删除 | skill 维度1 |

> 🤖 **Vibe Coding陷阱 — 前视偏差**: `df['ret_5d'] = df['close'].pct_change(5)` — 当天收盘价包含了当天涨幅，用来预测当天信号 = 未来信息泄露。正确做法: `.pct_change(5).shift(1)`。

---

## 领域 D3: 回测保真度

> ⚠️ 此领域主要来自 quant-full-audit skill。docx 方案基本未覆盖回测质量。  
> **v1.1**: 补充L0/L1基础检查, 填补原协议最大的结构缺陷。

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D3.L0.1 | 回测代码可运行性 | 给定10只股票×100天样本数据, 运行回测不崩溃 | PASS: 正常结束+输出非空; FAIL: 崩溃或返回空DataFrame | 新增 v1.1 |
| D3.L0.2 | 回测输出完整性 | 回测结果包含必需要素: timestamp/参数快照/交易记录/绩效指标 | PASS: 4项齐全; WARN: 缺1项; FAIL: 缺≥2项 | 新增 v1.1 |

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D3.L1.1 | 回测基本指标合理性 | 年化收益率/夏普比率/最大回撤是否在sane范围 | PASS: -99%≤年化≤500%, -5≤夏普≤10, 回撤≤99%; FAIL: 超出范围 | 新增 v1.1 |
| D3.L1.2 | 回测交易记录非空 | 有信号的日子是否产生了交易记录 | PASS: 交易记录数>0; WARN: 交易记录=0但有信号; FAIL: 数据为空 | 新增 v1.1 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D3.L2.1 | T+1买卖约束验证 | 回测记录中买入日与卖出日是否同一天 | PASS: 0同日买卖; FAIL: ≥1同日买卖 | skill 维度2 |
| D3.L2.2 | 涨跌停不可交易验证 | 回测日志中涨停日是否有买入记录 | PASS: 0涨停买入; FAIL: ≥1涨停买入 | skill 维度2 |
| D3.L2.3 | 最小交易单位验证 | 回测买入数量是否为100的整数倍(科创板200) | PASS: 0非整手交易; FAIL: ≥1非整手交易 | skill 维度2 |
| D3.L2.4 | 交易成本核算 | 佣金+印花税+最低佣金+滑点 四者是否全部计入 | PASS: 4项全有; WARN: 缺滑点; FAIL: 缺佣金或印花税 | skill 维度2 |
| D3.L2.5 | 基准对比 | 策略收益 vs 沪深300/中证500同期买入持有收益 | PASS: 信息比率>0; WARN: 超额<0但波动更低; FAIL: 收益<无风险利率 | 新增 v1.1 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D3.L3.1 | 回测引擎非空壳验证 | `run_strategy()` 方法体行数 vs 注释行数 | PASS: 代码行>注释行; FAIL: 注释行≥代码行 | skill 维度2 |
| D3.L3.2 | Walk-forward vs K-Fold对比 | 用两种分割方式跑同一策略，收益率差异 | PASS: 差异≤20%; WARN: 20-50%; FAIL: >50% | skill 维度3 |
| D3.L3.3 | 滑点模型合理性 | 小盘股固定3bp → 抽查模拟滑点 vs 实际盘口价差 | PASS: 模型滑点≥实盘价差均值; FAIL: 模型滑点<实盘价差 | skill 维度2 |
| D3.L3.4 | 成交量约束验证 | 模拟买入金额 > 日均成交额×5% → 是否被拦截 | PASS: 超大单被拦截; FAIL: 超大单通过 | skill 维度2 |
| D3.L3.5 | 模拟vs实盘成交一致性 | 选取N日实盘挂单记录, 对比模拟器同条件成交判断 | PASS: 成交判断一致率≥90%; WARN: 80-90%; FAIL: <80% | 新增 v1.1 |

> 🤖 **Vibe Coding陷阱 — 回测空壳**: DSLExecutor定义了完整的方法签名和docstring，但 `run_strategy()` 返回 `pd.DataFrame()` 或全零数据。验证方法：看核心方法体是否比注释短。

---

## 领域 D4: 模型质量

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D4.L1.1 | 模型文件完整性 | 每只股票是否有 .pkl/.npy 文件 | PASS: 覆盖率≥90%; WARN: 70-89%; FAIL: <70% | docx L2.2 |
| D4.L1.2 | 训练时效性 | 最近模型文件修改时间 vs 当前日期 | PASS: ≤7天; WARN: 8-14天; FAIL: >14天 | skill 维度3 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D4.L2.1 | 逐股方向准确率统计 | 从calibration/feedback计算每只股票的accuracy | PASS: 均值≥0.55; WARN: 0.50-0.54; FAIL: <0.50 | docx L3.1 |
| D4.L2.2 | 低精度模型识别 | accuracy < 0.45 的股票列表 | PASS: 0只; WARN: 1-3只; FAIL: ≥4只或占总量>10% | skill 维度3 |
| D4.L2.3 | 过拟合差距 | 训练集accuracy vs 验证集accuracy | PASS: 差距≤10%; WARN: 10-20%; FAIL: >20% | skill 维度3 |
| D4.L2.4 | PSI特征漂移检测 | 从psi_monitor获取最新PSI值 | PASS: PSI<0.1; WARN: 0.1-0.25; FAIL: >0.25 | skill 维度3 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D4.L3.1 | 标签构造前视泄漏 | 检查 `shift(-1)` / 用t+1数据算t日标签 | PASS: 0泄漏; FAIL: ≥1泄漏 | skill 维度3 |
| D4.L3.2 | 止损与预测期望值一致性 | 止损-8% vs 预测涨3%胜率55% → 期望值 < 0? | PASS: 期望值>0; WARN: -0.5%~0%; FAIL: 期望值<-0.5% | skill 维度3 |
| D4.L3.3 | 模型衰减曲线 | 训练后N天的预测精度变化趋势 | PASS: 30天精度衰减<10%; WARN: 10-20%; FAIL: >20% | skill 维度3 |
| D4.L3.4 | 特征重要性稳定性 | 最近3次训练的特征重要性排名相关系数 | PASS: 相关系数≥0.7; WARN: 0.5-0.69; FAIL: <0.5 | skill 维度3 |

> 🤖 **Vibe Coding陷阱 — 标签泄漏**: "用当天收盘价算当天标签"——收盘价在盘后才确定，盘中特征用的也是当天数据，等于在预测时用了未来信息。

---

## 领域 D5: DSL设计质量

> ⚠️ 此领域主要来自 quant-full-audit skill。docx 方案未专门覆盖 DSL。  
> **v1.1**: 权重从4%恢复至6% — 对DSL驱动的系统, DSL是核心抽象层。

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D5.L0.1 | DSL Schema验证 | JSON Schema校验是否能捕获常见错误(period=-1, signal=空) | PASS: 捕获≥5类错误; WARN: 捕获3-4类; FAIL: <3类 | skill 维度4 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D5.L2.1 | DSL原语完备性 | 策略模式覆盖率: 均线交叉/布林带/MACD/RSI/量价背离/连板 | PASS: 覆盖≥5种; WARN: 3-4种; FAIL: <3种 | skill 维度4 |
| D5.L2.2 | DSL→执行可翻译性 | 随机抽取3个策略JSON → 验证引擎真实计算(非占位) | PASS: 3/3真实计算; FAIL: ≥1返回占位数据 | skill 维度4 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D5.L3.1 | DSL执行引擎非空壳 | `run_strategy()` 是否真的运行回测循环 | PASS: 产生≥1笔交易记录; FAIL: 0笔 | skill 维度4 |
| D5.L3.2 | 错误反馈质量 | 传入非法参数(period=-1) → 错误信息是否包含: 字段名+错误原因+建议值 | PASS: 3项齐全; WARN: 缺1项; FAIL: 仅报"error" | skill 维度4 |
| D5.L3.3 | 策略表达力边界 | 能否表达"涨5%回撤3%卖出"等复杂逻辑 | PASS: 可表达trailing_stop类型逻辑; FAIL: 不可表达 | skill 维度4 |

> 🤖 **Vibe Coding陷阱**: DSL是AI的"舒适区"——文档、JSON Schema、接口定义都很漂亮，但核心执行引擎可能是空的。验证: `run_strategy()` 有多少行真正的业务逻辑。

---

## 领域 D6: 风控

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D6.L0.1 | 风控模块可导入 | `from core.risk_manager import RiskManager` 不报错 | PASS: 导入成功; FAIL: ImportError | 新增 v1.1 |
| D6.L0.2 | 风控配置文件有效性 | `circuit_breaker.json`, `black_swan_status.json` 可解析 | PASS: 2文件均可解析; FAIL: ≥1解析失败 | 新增 v1.1 |

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D6.L1.1 | 熔断器状态检查 | `circuit_breaker.json` → `trading_paused` | PASS: not paused; FAIL: paused (阻断交易) | docx L4.1 |
| D6.L1.2 | 黑天鹅仓位比例 | `black_swan_status.json` → `position_ratio` | PASS: ratio≥0.5; WARN: 0.25-0.49; FAIL: <0.25 | docx L4.2 |
| D6.L1.3 | 持仓集中度 | 单行业/板块占比是否 > 阈值(默认40%) | PASS: ≤40%; WARN: 40-60%; FAIL: >60% | docx L4.4 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D6.L2.1 | 板块差异化止损验证 | 主板-8%/创业板-15%/科创板-15%/ST-5% 是否生效 | PASS: 4板块均正确; WARN: 1板块偏差; FAIL: ≥2板块偏差 | skill 维度5 |
| D6.L2.2 | 涨跌停逻辑一致性 | circuit_breaker.py vs risk_manager.py vs paper_trader.py 的LIMIT_RULES | PASS: 三处一致; FAIL: 不一致 | skill 维度5 |
| D6.L2.3 | 止盈连接验证 | 止盈触发 → 实际卖出 的代码路径是否完整 | PASS: 路径完整(有调用链); FAIL: 断开 | skill 维度5 |
| D6.L2.4 | LPPL风险评分合理性 | risk_score=95 → 仓位是否自动降至20% | PASS: 仓位≤20%; WARN: 20-30%; FAIL: >30% | skill 维度5 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D6.L3.1 | 止损调用链完整性 | check_single_position_stop 是否在 pre_execution_check 中被调用 | PASS: 有调用链; FAIL: 函数孤立 | skill 维度5 |
| D6.L3.2 | 回撤计算分母验证 | 是否用 peak_equity 而非 initial_capital | PASS: 用peak; FAIL: 用initial_capital | skill 维度5 |
| D6.L3.3 | 熔断恢复机制 | 熔断触发后是否有手动恢复的UI或命令 | PASS: 有恢复机制; FAIL: 无恢复途径 | skill 维度5 |
| D6.L3.4 | 流动性风险 | 持仓市值 vs 日均成交额 → 平仓需要多少天 | PASS: ≤3天可平完; WARN: 3-7天; FAIL: >7天 | skill 维度5 |

> 🤖 **Vibe Coding陷阱**: 风控是AI的盲区——AI倾向写"进攻代码"(策略/回测)而忽略"防守代码"。写了 `check_single_position_stop()` 但没有在 `pre_execution_check()` 中调用 = 形同虚设。

---

## 领域 D7: 执行层

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D7.L1.1 | 订单幂等性检查 | `idempotency.db` 中是否有重复订单ID | PASS: 0重复; FAIL: ≥1重复 | skill 维度6 |
| D7.L1.2 | 信号时效性 | 预测生成时间 vs 开盘时间(9:30) | PASS: 预测在9:20前完成; WARN: 9:20-9:29; FAIL: ≥9:30 | skill 维度6 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D7.L2.1 | 决策链路完整性 | morning_decision → risk_manager → execute_trade 每步成功/失败处理 | PASS: 3步均有错误处理; WARN: 缺1步; FAIL: 缺≥2步 | skill 维度6 |
| D7.L2.2 | 部分成交处理 | `partial_fill.py` / fill_ratio 逻辑是否在paper_trader中调用 | PASS: 有调用+逻辑完整; FAIL: 无调用或空函数 | skill 维度6 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D7.L3.1 | 崩溃恢复一致性 | SQLite vs JSON持仓在模拟崩溃后是否一致 | PASS: 完全一致; WARN: ≤2字段差异; FAIL: 数量不一致 | skill 维度6 |
| D7.L3.2 | 午盘休市处理 | 11:30-13:00是否有订单执行 → 应被拦截 | PASS: 0休市订单; FAIL: ≥1休市订单 | skill 维度6 |
| D7.L3.3 | API限流熔断 | 数据源连续3次失败后是否有退避(指数退避≥1s) | PASS: 有退避+恢复重试; FAIL: 无限重试或无退避 | skill 维度6 |

> 🤖 **Vibe Coding陷阱**: AI倾向于完美假设——网络不会断、API不会挂、不会crash。真实世界需要处理重试、幂等、崩溃恢复。

---

## 领域 D8: 系统工程与运维

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D8.L0.1 | Git状态检查 | `git status --short` 未提交文件数 | PASS: ≤5个; WARN: 6-15个; FAIL: >15个 | skill 维度7 |
| D8.L0.2 | Git提交时效性 | 最后一次提交时间 vs 当前时间 | PASS: ≤3天; WARN: 4-7天; FAIL: >7天 | skill 维度7 |
| D8.L0.3 | 前端版本号 vs VERSION文件 | `index.html` 和 `server.py` 中硬编码版本号 vs VERSION文件实际内容 | PASS: 三方一致; WARN: 仅fallback不一致; FAIL: 前端+后端均不一致 | 新增 |

> 🤖 **Vibe Coding陷阱**: AI写Dashboard时在HTML `<title>`、`<h1>`、FastAPI `version=`、`version_data` fallback等多处硬编码版本号。改VERSION文件时这些地方不会自动更新。

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D8.L1.1 | Cron任务健康扫描 | 连续错误数/状态未知任务/超时任务 | PASS: 0错误; WARN: 1-3错误或1-2超时; FAIL: >3错误 | docx L5.1-L5.3 |
| D8.L1.2 | Dashboard运行状态 | 进程存活 + API可用性(HTTP 200 on /api/status) | PASS: 200+响应<2s; FAIL: 无响应或超时 | docx L6.1 |
| D8.L1.3 | 备份完整性 | 最近备份时间 vs 预期时间 | PASS: ≤26h; WARN: 26-50h; FAIL: >50h | docx L8.1 |
| D8.L1.4 | 磁盘空间 | `/tmp/dslcache` 大小, 项目目录大小 | PASS: 使用率<80%; WARN: 80-95%; FAIL: >95% | skill 维度7 |
| D8.L1.5 | API响应字段完整性 | 抽样3个核心API, 对比前端JS引用字段 vs 实际响应 | PASS: 100%字段存在; FAIL: ≥1缺失字段 | 新增 |
| D8.L1.6 | 前后端版本号三方一致 | VERSION文件 vs `/api/version` 响应 vs `index.html` 硬编码初始值 | PASS: 三方一致; FAIL: 不一致 | 新增 |
| D8.L1.7 | 双API数据源一致性 | `/api/full`中portfolio vs `/api/paper-trader`中positions | PASS: 股票+数量一致; FAIL: 不一致 | 新增 |

> 🤖 **Vibe Coding陷阱 — Dashboard双数据源**: AI可能让前端同时调用 `/api/full`（走`get_portfolio()`→先读JSON再fallback SQLite）和 `/api/paper-trader`（直读SQLite），两条路径的字段名(`quantity`/`shares`)、数据来源(JSON/SQLite)、计算口径(含费用/不含费用)都可能不同。

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D8.L2.1 | crontab vs 实际调度器同步 | crontab.conf任务数 vs OpenClaw调度器任务数 | PASS: 调度器覆盖crontab所有任务; WARN: ≤3个仅crontab; FAIL: >3个 | docx L2.1 |
| D8.L2.2 | 告警渠道可用性 | 飞书Webhook是否可以发送测试消息 | PASS: 200响应; FAIL: 无响应或4xx/5xx | skill 维度7 |
| D8.L2.3 | API错误响应前端展示 | 模拟后端返回500/超时/空数组 → 检查前端是否展示错误提示 | PASS: 展示错误提示; FAIL: 静默空白或旧数据 | 新增 |
| D8.L2.4 | WebSocket vs REST状态一致 | `/ws/progress`推送cron_status vs `/api/pipeline`返回timeline | PASS: 同任务状态一致; FAIL: 冲突 | 新增 |
| D8.L2.5 | 重训状态机前后端一致 | 前端轮询 `/api/retrain-status` → 后端 `_retrain_status` 内存 → `retrain_queue.json` | PASS: 三方一致; FAIL: 不一致(尤其重启后) | 新增 |
| D8.L2.6 | 筛选器参数前后端映射 | 前端筛选器(tier/score/signal)参数 vs 后端 `/api/screener` query params | PASS: 一一对应; FAIL: 有断裂 | 新增 |

> 🤖 **Vibe Coding陷阱 — 重训状态丢失**: 后端 `_retrain_status` 是内存字典，服务重启后消失。

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D8.L3.1 | 配置集中管理审计 | 参数散落在几个文件 | PASS: ≤3个配置源; WARN: 4-5个; FAIL: >5个 | skill 维度7 |
| D8.L3.2 | 单点故障分析 | 列出所有"挂了就全停"的依赖 | PASS: 每个SPOF有降级方案; FAIL: ≥1无降级 | skill 维度7 |
| D8.L3.3 | Dashboard字段映射完整矩阵 | 逐API端点列出后端返回字段 × 前端渲染字段 | PASS: 0断裂字段; FAIL: ≥1断裂 | 新增 |
| D8.L3.4 | 前端崩溃恢复完整性 | 后端重启后: WebSocket重连+REST重试+轮询恢复 | PASS: 30s内恢复; FAIL: 需手动刷新 | 新增 |
| D8.L3.5 | Dashboard数值口径一致性 | 同一指标在不同卡片/tab中的数值来源 | PASS: 同指标同源; FAIL: 同指标不同值 | 新增 |
| D8.L3.6 | 环境可复现性 | 干净临时目录: `git clone → pip install → smoke_test.py` | PASS: 全流程通过; FAIL: 任一步失败 | 新增 v1.1 |

> 🤖 **Vibe Coding陷阱 — 口径分裂**: Dashboard中"总收益率"可能在总览卡片来自 `get_system_status()`，在模拟交易tab来自 `/api/paper-trader`，在精度趋势tab从 `prediction_calibration.json` 推算。三个地方三个数。

---

## 领域 D9: 跨领域集成

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D9.L0.1 | E2E模块导入链 | constants → data_loader → signal_generator → risk_manager → dsl_engine | PASS: 全链导入成功; FAIL: ≥1环断裂 | docx L9.1 |

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D9.L1.1 | SQLite↔JSON持仓一致性 | paper_trading.db vs paper_trading_ledger.json | PASS: 完全一致; FAIL: 不一致 | docx L7.1 |
| D9.L1.2 | 股票池↔模型目录一致性 | master_stock_pool.yaml symbols vs models/ 子目录 | PASS: 偏差≤5%; WARN: 5-15%; FAIL: >15% | docx L2.2 |
| D9.L1.3 | Smoke Test通过 | `scripts/smoke_test.py` 全部通过 | PASS: exit=0; FAIL: exit≠0 | docx L9.3 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D9.L2.1 | 信号→持仓闭环验证 | 最近N个交易日的信号生成 vs 实际持仓变化 | PASS: 信号与持仓变化一致率≥90%; WARN: 70-89%; FAIL: <70% | skill |
| D9.L2.2 | 反馈闭环 | calibration_feedback → retrain_queue → batch_train → accuracy | PASS: 4步完整; WARN: 缺1步; FAIL: 缺≥2步 | skill 维度3 |
| D9.L2.3 | Golden Test回归 | 固定输入(10只股票×100天K线) → 固定输出(预期信号) | PASS: 输出完全匹配; FAIL: 不匹配 | docx L9.2 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D9.L3.1 | 全链路追踪 | 一笔交易: 预测→信号→风控→下单→成交→复盘, 每步时间戳 | PASS: 6步均有时间戳; WARN: 缺1-2步; FAIL: 缺≥3步 | skill 维度6 |
| D9.L3.2 | 双系统对比 | paper_trading.db vs paper_trader.db 行为差异 | PASS: 100%一致; WARN: 95-99%; FAIL: <95% | skill |

---

## 领域 D10: 安全 **(v1.1 新增)**

> ⚠️ 安全是个人量化系统最容易被忽视的维度。Dashboard暴露到公网、API密钥管理、输入验证都是实际风险。

### L0 — 自动化

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D10.L0.1 | 敏感文件Git追踪检查 | `git ls-files \| grep -E '\.env$|\.pem$|credentials|web_auth'` | PASS: 0敏感文件被追踪; FAIL: ≥1敏感文件在git中 | 新增 v1.1 |
| D10.L0.2 | 密钥文件权限检查 | `stat -f "%p" config/web_auth.json` 验证权限=600 | PASS: 权限≤600; FAIL: 权限>600(可被其他用户读取) | 新增 v1.1 |
| D10.L0.3 | 依赖已知漏洞扫描 | `pip-audit` 快速模式 (仅检查Critical/High) | PASS: 0高危; FAIL: ≥1高危CVE | 新增 v1.1 |

### L1 — 快速

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D10.L1.1 | 公网暴露面检测 | Dashboard是否通过Cloudflare Tunnel/反代暴露到公网 | PASS: 有HTTPS+认证; WARN: 有认证但无HTTPS; FAIL: 无认证暴露 | 新增 v1.1 |
| D10.L1.2 | 日志中密钥泄漏扫描 | `grep -rn 'sk-\|token=\|eyJ' logs/ \| grep -v 'os.getenv\|REDACTED'` | PASS: 0密钥明文; FAIL: ≥1密钥明文出现在日志 | 新增 v1.1 |

### L2 — 标准

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D10.L2.1 | API端点注入风险审计 | 审查 `/api/retrain-now` 等接收用户输入的端点是否有输入校验 | PASS: 所有输入端点有校验; FAIL: ≥1无校验 | 新增 v1.1 |
| D10.L2.2 | Session安全配置 | 检查cookie: HttpOnly/SameSite/Secure(公网时) 是否设置 | PASS: 三项齐全; WARN: 缺Secure(内网可接受); FAIL: 缺HttpOnly | 新增 v1.1 |
| D10.L2.3 | 飞书Webhook URL安全 | Webhook URL是否从环境变量读取(非硬编码) | PASS: 从env读取; FAIL: 硬编码 | 新增 v1.1 |

### L3 — 深度

| ID | 检查项 | 方法 | 量化阈值 | 来源 |
|----|--------|------|----------|------|
| D10.L3.1 | SQL注入风险审计 | 检查所有SQLite查询是否使用参数化查询(非字符串拼接) | PASS: 100%参数化; FAIL: ≥1字符串拼接 | 新增 v1.1 |
| D10.L3.2 | 全量密钥扫描(git历史) | `git log -p \| grep -E 'sk-[a-zA-Z0-9]{20,}'` 检查历史中是否曾提交密钥 | PASS: 0历史泄漏; FAIL: 发现历史泄漏(需轮换密钥) | 新增 v1.1 |
| D10.L3.3 | 依赖供应链安全 | `pip-audit --full` + 检查依赖是否来自可信源 | PASS: 0已知漏洞+全部PyPI官方; FAIL: ≥1高危或非官方源 | 新增 v1.1 |

> 🤖 **Vibe Coding陷阱 — 安全盲区**: AI生成Dashboard时默认CORS `allow_origins=["*"]`、FastAPI无认证中间件、命令行注入无校验。这些在"本地运行"时没问题，一旦暴露到公网就是严重漏洞。

---

## 运行策略

```
┌─────────────────────────────────────────────────────────┐
│                    L0 自动化层                            │
│ 触发: git push / 每次文件修改                             │
│ 工具: smoke_test.py --level L0                           │
│ 输出: ✅/❌ 二进制结果, 失败阻断push                      │
│ 耗时: < 10秒                                             │
├─────────────────────────────────────────────────────────┤
│                    L1 快速层                              │
│ 触发: 每日cron (建议 04:00)                               │
│ 工具: smoke_test.py --level L1                           │
│ 输出: 飞书通知 (仅告警)                                   │
│ 耗时: < 60秒                                             │
├─────────────────────────────────────────────────────────┤
│                    L2 标准层                              │
│ 触发: 每周六 10:00 (cron / OpenClaw调度器)                │
│ 工具: quality_audit.py --level L2 + 人工审查清单          │
│ 输出: 周度质量报告 (飞书Bitable)                          │
│ 耗时: ~10分钟 (自动) + 可选人工审查                        │
├─────────────────────────────────────────────────────────┤
│                    L3 深层                                │
│ 触发: 版本发布前 / 每月第一周                              │
│ 工具: quant_full_audit.py + 人工专家审查                   │
│ 输出: 完整审查报告 (含P0/P1/P2优先级清单)                  │
│ 耗时: 1-3小时 (部分自动化 + 人工判断)                      │
└─────────────────────────────────────────────────────────┘
```

---

## 评分体系

### 单项评分

每个检查项评分：
- **NA**: 不适用（如无DSL的系统跳过D5）
- **PASS**: 通过 (权重 1.0)
- **WARN**: 警告 — 可延后处理 (权重 0.5)
- **FAIL**: 失败 — 需立即处理 (权重 0.0)

### 领域评分

```
领域评分 = (PASS数×1.0 + WARN数×0.5 + FAIL数×0.0) / (PASS+WARN+FAIL数) × 100
```

> **v1.1 改进**: 原公式 `PASS/(PASS+WARN+FAIL)` 将WARN与FAIL等同对待。新公式区分严重程度——1个FAIL比3个WARN扣分更多。

### 系统总评分

```
系统总评分 = Σ(领域评分 × 领域权重)
```

| 领域 | 权重 | 理由 |
|------|------|------|
| D2 数据管线 | 20% | 垃圾进垃圾出 — 数据质量是系统基石 |
| D3 回测保真度 | 20% | 回测不可信则一切无意义 |
| D4 模型质量 | 15% | 模型是信号源头 |
| D6 风控 | 15% | 个人投资者的最后防线 |
| D7 执行层 | 10% | 策略收益能否真正兑现 |
| D5 DSL设计 | 6% | DSL是系统的核心抽象层 (v1.1: 从4%恢复) |
| D8 系统工程与Dashboard | 6% | 运维可靠性+前后端一致性 |
| D10 安全 | 3% | 公网暴露/注入/密钥管理 (v1.1新增) |
| D1 代码配置 | 3% | 基础的代码卫生 |
| D9 跨领域集成 | 2% | 端到端一致性 |

> **权重和**: 20+20+15+15+10+6+6+3+3+2 = 100%

---

## 检查项依赖拓扑

某些领域的检查结果依赖于其他领域的状态。当上游领域严重异常时，下游检查结果自动标记为 **"不可信(UNRELIABLE)"**。

```
D2 (数据管线) ──┬──→ D3 (回测保真度) ──→ D4 (模型质量)
                │        │
                │        └──→ D7 (执行层)
                │
                └──→ D9 (跨领域集成)

D5 (DSL设计) ────→ D3 (回测保真度)

D10 (安全) ─────→ D8 (系统工程)    [公网暴露影响运维安全]
```

### 依赖阻断规则

| 条件 | 影响 |
|------|------|
| D2 评分 < 50 | D3, D4, D7, D9 检查结果标记为 UNRELIABLE |
| D5 评分 < 40 | D3 检查结果标记为 UNRELIABLE |
| D3 评分 < 50 | D4 检查结果标记为 UNRELIABLE |
| D10 发现 ≥1 FAIL (L0/L1级) | 阻断所有公网暴露, L3发布冻结 |

---

## 误报管理机制

持续运行的质量系统必然产生噪音。以下机制防止告警疲劳：

### 豁免清单 (Known-Issue Suppression)

在 `config/quality_suppressions.yaml` 中维护已知问题豁免:

```yaml
suppressions:
  - check_id: D2.L1.1
    reason: "东方财富数据源周末偶发超时, 不影响交易日使用"
    expires: "2026-06-30"
    suppress_result: WARN  # 将FAIL降级为WARN
```

### 连续失败升级 (Escalation)

- 同一检查项 **连续1次FAIL** → 报告为 WARN (可能是瞬时故障)
- **连续3次FAIL** → 升级为 FAIL (确认是持续问题)
- **连续7次FAIL** → 升级为 CRITICAL (需人工介入)

### Flaky检查隔离

- 某检查项 30天内 PASS/FAIL 交替 ≥5次 → 标记为 "flaky"
- Flaky检查结果在L0/L1中降级为 WARN
- 每季度审查flaky列表, 修复或替换不稳定检查

---

## FAIL处理SOP索引

| 常见FAIL | 处理方式 |
|----------|---------|
| D1.L0.2 导入完整性验证 | 检查最近修改的 `import` 语句, 确认依赖已安装 |
| D2.L0.1 Token环境变量缺失 | 运行 `scripts/setup_env.sh` 或手动 `export` |
| D2.L1.3 数据新鲜度过期 | 手动触发 `scripts/batch_predict.py` 或等待下一个cron周期 |
| D3.L0.1 回测代码不可运行 | 查看最近git diff, 回滚或修复回测相关修改 |
| D4.L2.2 低精度模型过多 | Dashboard一键重训, 或触发 `batch_train.py --retrain_urgent` |
| D6.L1.1 熔断器触发 | 检查 `circuit_breaker.json` 的 `pause_reason`, 确认后可手动解除 |
| D8.L1.2 Dashboard不可用 | 检查进程: `cat web_dashboard/.dashboard.pid`, 重启: `web_dashboard/start.sh` |
| D10.L0.1 敏感文件被追踪 | 立即 `git rm --cached` 并添加到 `.gitignore`, 如已push则轮换密钥 |
| D10.L3.2 Git历史密钥泄漏 | 立即轮换所有相关密钥/Token, 使用 `git filter-branch` 或 `BFG` 清理历史 |

---

## 协议自身治理

### 版本管理

本协议遵循语义化版本:
- **主版本** (v1→v2): 架构变更 (领域增删、权重重组)
- **次版本** (v1.0→v1.1): 检查项增删、阈值调整、新增章节
- **修订号** (v1.0.0→v1.0.1): 措辞修正、来源链接更新

### 修改流程

1. 提出修改建议 (附带理由和影响分析)
2. 在 `quality_audit.py` 中实现新检查项 (如有)
3. 更新本协议文档 + 版本号 + 变更日志
4. 重新导出 Word 文档
5. 通知相关人员 (飞书)

### 审查节奏

- **每季度**: 审查检查项的有效性, 退役无用的检查项
- **每次重大系统变更后**: 审查是否需要新增检查领域
- **每年**: 全面审视权重分配和评分模型

---

## 实现路线图

### Phase 1: 立即可用 (今天)
- [x] `scripts/smoke_test.py` — L0+L1 自动化
- [ ] 将现有 `smoke_test.py` 扩展为 `--level L0|L1` 模式
- [ ] 新增 `D3.L0` 回测可运行性检查集成到smoke_test
- [ ] 新增 `D6.L0` 风控模块导入+配置文件有效性检查
- [ ] 新增 `D10.L0` 敏感文件+密钥权限检查集成到pre-commit hook

### Phase 2: 一周内
- [ ] `scripts/quality_audit.py` — L2 标准检测脚本 (含量化阈值)
- [ ] L1每日cron集成 (OpenClaw调度器新增任务)
- [ ] 飞书通知集成 (L1告警 → 飞书消息)
- [ ] `config/quality_suppressions.yaml` 豁免清单机制
- [ ] 连续失败升级逻辑 (1次→WARN, 3次→FAIL, 7次→CRITICAL)

### Phase 3: 两周内
- [ ] `scripts/quant_full_audit.py` — L3 深度审查脚本
- [ ] Golden Test 数据集准备 (D9.L2.3)
- [ ] 前视偏差扫描工具 (D2.L3.1)
- [ ] 回测保真度验证套件 (D3.*)
- [ ] Dashboard字段映射矩阵脚本 (D8.L3.3)
- [ ] API响应字段完整性自动检测 (D8.L1.5)
- [ ] 安全检查套件 (D10.*) — 密钥扫描/注入检测/公网暴露检测

### Phase 4: 一月内
- [ ] 完整L0-L3 CI/CD流水线 (含依赖阻断规则)
- [ ] 历史趋势Dashboard (每次审计结果的时间序列)
- [ ] 自动修复建议生成 (L2-L3发现 → 自动创建修复任务)
- [ ] Dashboard前端崩溃恢复测试套件 (D8.L3.4)
- [ ] 前后端一致性E2E自动化测试 (D8.L2.3-L2.6)
- [ ] 环境可复现性自动化测试 (D8.L3.6)
- [ ] Flaky检查自动识别和隔离
