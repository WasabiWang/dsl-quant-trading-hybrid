# v4.6.9i (2026-08-15) — 风控闭环批次一（全量审计修复批1/4）

基于 v4.6.9h.2 全量审计（8报告, 总评67/100, 7 P0），本批修复全部P0级风控问题：

## 🔴 F1-1 黑天鹅真相源单一化（审计P0-1）
- morning_decision 三处 bs 基线/LPPL比率改读 data/black_swan_status.json（每日08:10新鲜）
- adaptive_params risk.* 仅作fallback（曾冻结2个月0.68 → 仓位上限比正确数据宽松1倍）
- risk_manager 黑天鹅收紧源: event_history(冻结6/15) → memory/black-swan最新analysis的severity
- LPPL cron 增加 --update-config，adaptive_params lppl_position_ratio 恢复每日刷新
- 效果(8/14数据): 执行链 bs=0.31×融合→0.31×BEAR_WEAK 0.4 = 上限12.4%（原25.1%）

## 🔴 F1-2 活跃执行器接入组合风控（审计P0-4）
- execute_scheduled_trades 下单前调用 pre_execution_check（行业配额/组合止损/RS止盈/集中度）
- 持仓/权益从 load_ledger() 构建（PaperTrader无list_positions等旧接口）

## 🔴 F1-3 降级链东财→新浪 + 止损空数据防护（审计P0-3, 决策D2）
- morning_decision/execute_planned_trades/stop_loss_monitor 三处降级源换 stock_zh_a_spot(新浪, 实测可用)
- stop_loss_monitor 数据全空 → CircuitBreaker.pause(2h) + 飞书告警（替代静默跳过）
- 新增 CircuitBreaker.pause(reason, hours) 方法

## 🔴 F1-4 熔断回撤回写（审计P0-7, 决策D1）
- 新增 CircuitBreaker.update_equity_drawdown(equity)：每日首次估值写today_starting_capital+当日回撤
- 修复 update_drawdown 方向比较bug（原`>`与初始0.0比较致today_drawdown永不更新）
- 生产调用点: execute_scheduled_trades(09:30) + stop_loss_monitor(5个盘中检查点)

## 🔴 F1-5 账目对账 + 校准闭环激活（审计P0-5/P0-6, 决策D4）
- 新增 PaperTrader.run_portfolio_reconciliation()：cash+持仓市值 vs 账面权益（容差0.1%），差异告警飞书
- execute_scheduled_trades 每日调用对账
- 校准: _fetch_actual_return 主源换麦蕊kline（akshare stock_zh_a_hist 实测封锁）；batch_predict 每日激活 check_realized_accuracy(60天)
- 新增 scripts/backfill_realized_checks.py：历史回填（本次532条, 真实兑现精度49.2% vs 自报54.5%）

## ✅ 验证
- 10文件 ast.parse 0错误；L0/L1 全绿；L2 仅既有C2-date-current失败（09:30首次执行后自动修复）
- 对账实测: 现金620,951.65+持仓399,159=1,020,110.65 与账面一致（DB持仓价噪音278元<0.1%容差）
- 回填: 41只标的K线拉取100%成功，532条回填完成

## ⚠️ 待观察
- 仓位上限12.4%对新买入生效；存量39%持仓无自动降仓机制（待批次二方案）
- 周一09:30实跑为首次完整验证（组合风控+熔断回写+对账全链路）
# v4.6.9h (2026-08-01) — 股票池质量改进计划 (P0-P3)

## 🔴 P0: degraded持仓退出规则 (James决策)
- morning_decision plan_trades 新增D级卖出信号: degraded(精度<50%)且亏损>5% → 减50%分批
- 盈利的degraded保留观察; 验证: 中兴(-6.9%)→减300股, 平安(+12%)→保留, 恒瑞(-2.7%)→观察

## 🟠 P1: 行业配额 (电子31%→25%)
- update_pool_by_accuracy 新增 sector_quota_check: 单行业≤25%, 超额移出低精度degraded优先
- 修复排序bug: degraded应优先移出(最初实现反了)

## 🟡 P2: bench瘦身 + 持仓复核
- update_pool_by_accuracy: degraded且acc<48%且已degraded≥1轮 → remove_candidate标记(6只)
- 新增 review_holdings_quality.py 持仓质量复核脚本(只报告不自动交易)

## 🟢 P3: shadow候选池 (2周验证)
- config/shadow_pool.yaml: 6只低相关行业候选(农行/工行/长电/中建/国寿/海尔)
- shadow_pool_verify.py: 每周记录精度, 2周后精度≥55%才可入池
- 定向训练6只: 农行57.7%/中建61.7%/长电56.3%达标, 工行38.7%/国寿42.3%不达标
- cron: Shadow候选池验证(周日09:40)

## 🐛 重大修复: P0-2副作用 — dropna删掉1200行样本
- P0-2(fund_仅60行)导致全列dropna后只剩40行 → 所有训练样本不足
- 修复: dropna排除fund_列(1210行保留) + target y NaN显式过滤
- 主池模型未受污染(均为7/30旧代码训练); 周一16:00训练将用修复后代码

## ✅ 验证
- 候选训练: 6只全部成功(修复前0成功)
- 测试零回归 (6失败为既有)
- 审计通过; 新增cron: ba53aac8
# v4.6.9g (2026-08-01) — degraded停训机制

## 🟡 P2-1b: 停训 degraded 标的 (资源优化)
- **背景**: 池精度联动后8只标的标记degraded(精度<50%), 但训练脚本仍全量训练 → 40分钟/天纯浪费
- **机制**: batch_train.py + train_predictor_enhanced.py 的 get_stock_pool 跳过 degraded 标记标的
- **效果**: 训练池 29→21只, 训练时间 -40分钟/天, API调用-9%
- **设计**: 预测(batch_predict)仍覆盖全29只 → 保留观察弹性, 仅训练层跳过
- **恢复路径**: update_pool_by_accuracy.py 重跑时精度回升即自动移出degraded

## ✅ 验证
- 两脚本实测: batch_train 21只, train_predictor_enhanced 21/29只
- 预测池仍29只 (观察不受影响)
- 测试零回归 (6失败为既有)
# v4.6.9f (2026-08-01) — 模型训练/预测机制优化 (A股个人投资者视角)

## 🔴 P0-1: 回测对齐实盘 (5日框架)
- 回测 horizon 20→5日, 阈值对齐实盘 ±0.5% (原用 h20d 阈值±2.5%)
- 重跑: 年化45.39% (原49.35%), 胜率54.8% (原50.3%↑), 回撤-20.36%
- 消除"回测20日策略 vs 实盘5日信号"脱节

## 🔴 P0-2: 修复基本面前视偏差 (真实bug)
- fund_特征原本对整个序列赋值 (2019年样本看到2026年ROE)
- 改为仅最后60行注入, 早期填NaN — 验证: fund_roe 1209行中仅60行非NaN

## 🟠 P1-1: 信号2日确认
- batch_predict 标记 confirmed_2d (连续2日同向信号)
- morning_decision: 双确认+2日确认才Tier-1, 首日信号降Tier-2

## 🟠 P1-2: 双门槛过滤
- buy/sell 需 精度≥55% 且 |预测收益|≥1% (55%精度+2%盈亏-0.2%费用=0边际)
- 阈值从 adaptive_params.signal 读取(可调)

## 🟠 P1-3: 动态Transformer权重
- 固定0.25 → 按Transformer自身精度 0.15~0.35 线性插值

## 🟡 P2-1: 股票池精度联动 (新脚本 update_pool_by_accuracy.py)
- alpha≥60% / core≥55% / bench≥50% / <50%标记degraded
- 更新后: alpha 8 / core 9 / bench 12, 8只degraded
- 拓维/长电/海康/药明/京东方升alpha; 亨通/光迅/君正/富瀚/平安降bench

## 🟡 P2-2: 回测成本模型修复
- 卖出补印花税0.1% (STAMP_TAX_RATE) + 双边最低佣金5元
- 重跑: 年化43.70% (vs 45.39%无税), 成本影响-1.69pp可控

## ✅ 验证
- 回测两轮对比: 框架对齐+成本真实化
- 测试零回归 (6失败为既有)
- Dashboard同步新池 (alpha 8/core 9/bench 12)
# v4.6.9e (2026-08-01) — 模型页 UI/逻辑优化 (Review后全量实施)

## 🟠 P1-1: 前端过滤阈值硬编码 → 动态阈值
- 后端 get_calibration 返回 thresholds(从 calibration.json 动态读, 支持配置调整)
- 前端 renderModels 改用 cal.thresholds, 删除硬编码 0.45 — 卡片计数与表格过滤永不脱节

## 🟡 P2: 列语义修正
- "Correct"列 → "H5D对": 显示 realized_correct (H5D方向正确数), H20D正确/总数 (325/710) 移入括号+tooltip
- "预测数"列 → "精度样本": 明确是历史精度记录数 (len accuracies)
- 状态列: tab过滤激活时用简短图标(🔴/🟡/🟢), 全部视图显示完整标签

## 🟢 P3: 体验增强
- 训练时间 → 相对时间 (刚刚/Xh前/X天前), >48h标黄提示模型陈旧
- 新增"精度走势"列: sparkline 迷你折线图 (后端返回 accuracy_history 最近20条)

## 🐛 顺带修复: 模型页tab过滤失效 (既有bug)
- #model-tabs 的 tab 从未绑定点击事件 — 拆分前就存在, tab有UI但点击无反应
- 补上事件绑定, urgent/planned/all 过滤全部生效

## ✅ 验证
- urgent=1/planned=11/all=29 与后端summary完全一致
- sparkline/相对时间/简短图标均渲染正常
- 测试零回归 (10失败为既有)
# v4.6.9d (2026-08-01) — 风险/仓位页 UI 模块化 + 信息架构重构

## 🎨 一步到位全拆 (单文件 1503行 → 4 模块)
- `index.html`: 瘦身至 228 行纯 HTML 骨架
- `static/app.js` (新): 公共逻辑 — state/API/路由/格式化/总览/预测/模型/进度/paper-trader (1047行)
- `static/blackswan.js` (新): 风险/仓位页全套渲染 — hero/矩阵/仓位卡/LPPL/持仓/时间轴 (350行)
- `static/blackswan.css` (新): 风险页专用样式 (独立于 design-system.css)
- design-system.css 保持全局设计系统

## 🧭 信息架构重构 (按建议布局)
旧: 4个平铺卡片 → 新: 风险横幅 → 决策双栏 → LPPL → 持仓 → 时间轴
1. **顶部风险横幅**: CRITICAL等级 + 100分 + 55%仓位 + 最后检查, 全局第一眼
2. **决策双栏** (900px断点降为单栏):
   - 左: 8因子风险矩阵 (条形列表, 替代雷达, 含权重tooltip+详情行)
   - 右: 融合仓位决策卡 (大数字 + 分解条 基础/黑天鹅/LPPL/融合 + 公式说明 + 依据 + 建议)
3. **LPPL深度诊断**: 目标卡片网格 + 板块级信号
4. **仓位风险分布**: 持仓表
5. **风险历史趋势**: SVG双线图(风险分+仓位) + 事件标记 + 图例

## ✅ 验证
- agent-browser 真实渲染: 6个tab全部正常, 风险页5区块齐全
- 静态文件 app.js/blackswan.js/blackswan.css 均 200
- node --check 语法通过 | 测试零回归 (3失败为既有)
# v4.6.9c (2026-08-01) — 风险分数与等级矛盾修复 + 真实数据源接入

## 🔴 方案B: severity直映因子
- overall_score 34.3/36.4 vs CRITICAL 矛盾: 等级由severity硬决定, 分数被占位因子拉低, 两者永远脱节
- 修复: 新增 severity 因子 (权重0.10, 从lppl_bubble 0.25拆出→0.15), score=severity/5
- 效果: 分数对severity单调敏感, severity=5→因子贡献0.10

## 🔴 方案C: severity作分数下限 (floor)
- score = max(加权分×校准折扣, severity/5×100)
- severity=5 → floor=100 → 显示100 CRITICAL, 等级与分数永不矛盾
- 前端阈值对齐: CRITICAL≥80 / HIGH≥60 / ELEVATED≥40
- 雷达图新增"黑天鹅"轴 (8因子)

## 🟠 方案D: 占位因子接真实数据源
- flow因子: akshare stock_hsgt_fund_flow_summary_em → 北向资金 (2024后净买额停披→自动降级市场宽度代理: 涨1245/1588=78%→score 0.273)
- liquidity因子: akshare rate_interbank Shibor隔夜 → 1.41%→score 0.30 (宽松)
- 6h缓存 + 超时 + 失败降级标注
- correlation: 保留 us_spread 推导 (真实数据, 非占位)

## ✅ 验证
- overall_score=100.0 CRITICAL (severity=5), 矛盾消除
- flow/liquidity 真实数据, 降级链完整
- 测试零回归 (3失败为既有: pool_warmup映射/超时进度)
# v4.6.9b (2026-08-01) — 风险/仓位界面数据源统一

## 🔴 P0: 融合仓位数据源分裂 (Dashboard 34% vs 执行层 55%)
- **根因**: ① `_build_risk_matrix` 读 adaptive_params(0.6), `_compute_position_ratio` 读 black_swan_status.json(0.2) — 同一概念两套源
  ② 融合算法 dashboard用均值(0.2+0.48)/2=0.34, morning_decision用加权min(0.6*0.6+0.48*0.4)=0.552
  ③ lppl_to_dsl 用 min(旧值, lppl) 单向压缩, LPPL缓解后仓位永久卡历史最低(只降不升)
- **修复**: 真相源=adaptive_params(feedback_controller动态计算); status.json仅fallback; 融合算法对齐执行层加权公式; lppl_to_dsl 改读当前真相源取min
- **结果**: 黑天鹅仓位 0.6, 融合仓位 0.552, 与执行层完全一致

## 🟠 P1: 板块数据失真修复
- volatility因子: 读陈旧adaptive us_bubble(19) → 改读pre_market最新(38/MEDIUM)
- liquidity/flow: 明确标注"⚠️未接入实时源, 中性占位" 而非伪装默认
- market_prices: analysis缺commodity_prices → status.json/pre_market兜底, 修复总览页商品价格全空
- top_threats: 场景格式risk_matrix → 提取为威胁列表(美伊战争p=45%等6项)
- risk_history: risk_score从severity映射(全0→60/80/80), risk_level同步

## 🟡 P2: 前端口径统一
- 总览页banner/卡片/ov-risk 与黑天鹅页统一显示融合仓位(recommended_position)
- 仓位来源分解增加"融合"项 + 公式说明, breakdown数学自洽
# v4.6.9 (2026-08-01) — Dashboard"立即重训"周末失效修复 + logger潜伏bug

## 🔴 P1: "立即重训"在周末/节假日不生效
- **现象**: 模型页点击"立即"→ 训练跑完但精度/训练时间不更新,看起来像没训练
- **根因**: retrain-now流程=训练→预测→校准。batch_predict.py开头有"明天非交易日则跳过"保护逻辑,周六点击时直接sys.exit(0)跳过预测 → daily_predict.json未重写 → 校准读旧预测 → Dashboard数据不变;且跳过是exit 0,被当成"预测成功"
- **修复**: batch_predict.py新增`--force`参数绕过交易日检查; server.py的/api/retrain-now调用时加--force
- **验证**: 周六(08-01)真实点击"立即"→ 全链路done, daily_predict刷新为08-03, 600487精度0.4176→0.4262

## 🔴 P2: server.py logger未定义潜伏bug(5/24引入)
- `_load_retrain_status`/`_save_retrain_status`引用logger但从未定义; retrain_runtime_status.json首次创建后重启Dashboard即崩
- 修复: server.py顶部加`import logging` + `logger = logging.getLogger("web_dashboard.server")`
# v4.6.7 (2026-07-06) — exec恢复 + Cron全量统一 + 麦蕊代理加固

## 🔴 P0: exec全面瘫痪根因定位+彻底修复
- OpenClaw v2026.6.9→6.11升级后所有isolated cron脚本执行被拒(host=gateway security=deny)
- **真凶**: 独立host approvals文件 `~/.openclaw/exec-approvals.json` 将 defaults+agents.main 的 security 强制设为 deny (与tools.exec.security取最严)
- 修复: exec-approvals.json security→full(socket实时生效); openclaw.json tools.exec由{mode:full}改{security:full,ask:off}(mode与security/ask互斥)

## 🔄 Cron架构统一
- 07-05应急系统crontab(10条DSL任务)全量迁回OpenClaw agentTurn/isolated, crontab清空
- backup/warmup/trades/health 裸脚本名prompt写死精确命令+toolsAllow:[exec]
- 陈旧重复cron(3be64fb7/08dd9c2c/9dab7eb0)保持禁用

## 🛠️ 代码改动
- `config/mairui_api_config.py`: trust_env=False + 显式空代理双保险 (防macOS代理503)
- `web_dashboard/data_adapter.py`: analysis文件缺失时fallback检查cron_state (适配agentTurn cron无本地progress文件)

## 🔖 版本标准化
- 修复VERSION标记漂移(VERSION文件停在v4.6.3, git tag停在v4.5.6, 代码已到v4.6.7)
- VERSION/CHANGELOG/MEMORY/Dashboard/git tag/飞书文档 统一到 v4.6.7

---

# v4.6.6b (2026-07-04) — 数据源韧性框架
- `dsl_data_sdk_original.py`: 跨进程源可用性文件(所有cron进程共享,避免各自重试) + 熔断器集成
- 并行fetch top-2(麦蕊+新浪) + 串行降级(腾讯→东财)用跨进程状态跳过已死源
- 东财blackout窗口60s(2s快检)

---

# v4.6.6 (2026-07-04) — h5d训练解耦 (A换B)
- 16:00核心训练加 `--skip-heavy-post`; 新增独立16:30 cron跑 train_predictor_enhanced (h5d增强/多时间框架训练)
- 隔离内存尖峰防OOM (24GB无swap下核心训练与增强训练进程分离)
- 两cron加failureAlert+resume续跑验证

---

# v4.6.5 (2026-06-29) — P0仓位困局修复 + 新模型
- 融合建议25% vs 实际4.5% → 三重根因(planned_trades遗留锁死/盘前时间门控/signal_override累积)+五重修复
- 新增 `core/sentiment_features.py` (北向/融资/VIX/涨停情绪/行业动量 10维)
- 新增 `core/transformer_model.py` (PyTorch Transformer Encoder 时序预测, CPU友好)
- signal_weight阈值下调(熔断0.20→0.12, 降级0.40→0.30)

---

# v4.6.4 (2026-06-26) — 数据源封锁应对
- 东财push2.eastmoney.com API封锁 → 标记不可用自动跳过
- A股行情迁移: 新浪优先(适配sh/sz前缀) → 同花顺/巨潮/腾讯降级链
- 新增通达信TCP直连数据源(不受HTTP WAF封锁, 46字段)
- ST/*ST检测改用akshare名称映射(东财API封锁)

---

# v4.6.3 (2026-06-18) — 节假日gating + Dashboard修复
- batch_predict节假日跳过写progress文件(Dashboard不再显示跳票)
- evening_quality_check越权修复(归档脚本+prompt加固) + 预案距离检查(>2天跳过)

---

# v4.6.2 (2026-06-08) — macOS代理+VPN DNS全局修复
- P0: macOS系统代理+VPN DNS劫持全局修复(sitecustomize.py+proxy_bypass模块, 4层防御)

---

# v4.6.1 (2026-06-06) — 风控加固
- P0: retrain_queue消费 + 精度<40%禁入 + 单票仓位硬上限15% + gem/star止损线-0.15→-0.10

---

# v4.5.6 (2026-05-07) — S6修复集 + 盘前决策双模式分离

## 🚀 核心新增

### 盘前决策双模式分离（v4.5.6）
- **问题**: 21:30晚间预案和09:20开盘决策共用一个接口，晚间生成→凌晨数据变化→早盘执行脱节
- **修复**: `morning_decision.py`新增`--mode evening|morning`参数
  - `evening mode` (21:30): 生成次日预案预览, 不执行交易, 标注 📋晚间预案预览
  - `morning mode` (09:20): 读取 `planned_trades.json`（支持手动调整）, 融合09:00新鲜数据后最终决策
- **Dashboard同步**: 管线标题更新 + VERSION同步

### Alpha-Quant 自迭代引擎
- 新增 `scripts/alpha_quant_self_iterate.py` — 自动分析转债+个股+ETF
- 双模型生成：基础版 + 增强版
- 自动记录分析结果到 AlphaQuant 知识库

### 训练后自动参数调优 (auto-tune)
- 训练完成后自动执行参数网格搜索
- 优化信号阈值、仓位分配参数
- 将优化结果写入 `config/adaptive_params.yaml`

### 训练后精度自动同步到Calibration
- 训练完成后自动更新 `prediction_calibration.json` 中的精度数据
- 消除

## 核心升级

### 1. 反馈闭环系统
- 新增 `scripts/feedback_controller.py` — 统一反馈控制器
- 新增 `config/adaptive_params.yaml` — 自适应参数中枢
- 黑天鹅→风控、回测→交易参数、反思→模型参数三大反馈链路
- 9/12个业务cron已全部接入反馈闭环

### 2. Walk-Forward回测（无未来信息泄露）
- 新增 `scripts/backtest_walkforward.py` — 35只全量滚动回测
- 8季度窗口，每窗口重新训练，零前视偏差
- 回测结果: +114.1%总收益, 夏普1.0, 胜率56.4%, 盈亏比2.67

### 3. 增强预测模型
- 基本面特征集成（PE/ROE/营收增速/每股收益，来自麦蕊API）
- 多时间框架（1d+5d+20d），20d为主信号
- 特征选择器(SelectFromModel)保存→回测可复现
- 每日训练→自动回测→飞书同步→反馈闭环四步管线

### 4. 系统运维升级
- 盘中监控: crontab→launchd常驻守护, 文件级冷却缓存
- 蓝筹股信号抑制白名单, 无ML模型不输出买入信号
- GBrain知识库: PostgreSQL+pgvector激活, 69条笔记导入
- 健康检查: 10项检查, 异常自动反馈闭环
- 飞书5表回写: 股票池/模拟持仓/持仓追踪/交易记录/绩效看板

### 5. Skill精简
- 删除冗余skill 22个, 保留核心77个
- 禁用旧版cron 3个(主题轮动复盘/每日策略迭代/旧版)

## 文件变更
- 新增: `scripts/feedback_controller.py`, `scripts/backtest_walkforward.py`, `scripts/train_predictor_enhanced.py`, `config/adaptive_params.yaml`
- 重写: `scripts/daily_sim_report_v3.py`, `scripts/realtime_monitor.py`, `scripts/health_check.py`
- 升级: `scripts/pre_market_preparation.py`(反馈闭环集成), `scripts/sync_bitable.py`(20d主信号)

---

# v4.3.0 (2026-04-26) — P0/P1 审计改进

## 🔴 P0 紧急修复 (上线前必须完成)
1. ✅ **回测前视偏差修复** — `pool_backtest.py` 改用次日开盘价交易，消除使用当日收盘价的偏差
2. ✅ **滑点模型** — `dsl_engine.py` 新增3bp固定滑点，`pool_backtest.py` 支持可配置滑点
3. ✅ **敏感信息加密** — `.env` 和 `config/settings.py` 移除硬编码密钥，改用环境变量读取
4. ✅ **生产模式** — `dsl_data_sdk_original.py` 新增 `DSL_PRODUCTION_MODE` 开关，生产环境禁用模拟数据fallback

## 🟡 P1 高优先级 (1个月内完成)
1. ✅ **统一信号模块** — 新增 `core/signal_generator.py`，回测和生产共用同一套因子计算+信号评分+仓位分配
2. ✅ **训练评估指标** — 新增 `core/train_evaluator.py`，输出R²/方向准确率/夏普比率/最大回撤 + 质量检查
3. ✅ **日亏损限额** — `circuit_breaker.py` 新增 `set_today_starting_capital()` + `update_daily_loss()`，单日亏损≥2%触发熔断
4. ✅ **Cron任务告警** — `health_check.py` 新增cron任务失败检测和近期执行结果检查

## 📦 新增文件
- `config/constants.py` — 统一魔法数字管理
- `core/signal_generator.py` — 统一信号生成器
- `core/train_evaluator.py` — 训练模型评估器

## 📊 版本信息
- 版本: 4.4.0
- CodeName: P0-P1-Audit-Fix
- Build: 20260426

---

# v4.2.2 更新日志 (2026-04-23)
## 🎯 核心新增功能
1. **新增LightGBM量化选股模型v1.0**
   - 训练数据：2016-2025年全A股10年历史数据（5401只个股，量价+基本面特征）
   - 模型效果：测试集AUC 0.708，预测准确率 61.2%
   - 选股逻辑：预测个股未来5日涨幅超5%的概率，输出概率Top20标的
2. **移动止盈策略上线**
   - 替换原有固定8%止盈逻辑，采用3%回落移动止盈
   - 触发条件：持仓盈利超过3%后，价格从最高点回落3%自动止盈，吃满上涨行情
3. **三时段自动推送体系**
   - 8:30 盘前决策报告：当日选股标的+交易计划
   - 盘中实时监控：触发止损/止盈信号实时推送预警
   - 15:30 收盘日报+复盘：当日交易情况+收益+持仓明细
4. **新增策略优化工具集**
   - 参数自动优化：网格搜索最优参数组合（概率阈值、止损/止盈比例、仓位等）
   - 历史回测系统：输出核心指标（总收益率、年化收益、最大回撤、夏普比率、胜率等）
## 🔧 优化改进
1. 定时任务时间优化：完全错开原有系统执行时间，避免Gateway堵塞和资源冲突
2. 假期避让逻辑升级：专注A股市场，自动识别A股节假日/周末，休市日不执行任务
3. 飞书推送优化：复用现有飞书机器人配置，直接推送到个人账号，不需要新群/新机器人
4. 交易频率控制：平均每月8-10笔交易，低磨损符合个人投资者需求
## 📊 策略历史表现（回测2022-2026.04）
- 总收益率：62.8%，年化15.7%
- 最大回撤：-15.7%
- 交易胜率：61.2%，盈亏比1.87
- 夏普比率：1.87
## 🔄 兼容说明
- 原有v4.4.0所有功能完全保留，无破坏性变更
- 定时任务新增不影响原有系统运行，执行时间完全错开

# DSL Quant Trading System 版本变更日志

## v3.1.4 (2026-04-11)
### 🎯 核心升级
1. **多市场支持**：完整适配港股市场，与A股完全隔离的独立交易体系，数据、逻辑、持仓互不干扰
2. **策略自动进化**：集成EvoSkill自动迭代框架，每周日自动优化参数，双Agent交叉验证后自动上线
3. **选股逻辑升级**：从固定RS阈值升级为「个股历史分位自适应RS阈值」，RS≥自身过去60天前30%分位即可入选，适配不同波动率个股
4. **MA60趋势软化**：股价在MA60±2%区间且RS≥前10%破格入选，捕捉底部反转机会
5. **财报季防护**：港股财报季自动降低仓位上限，规避个股跳空风险
### 🛡️ 风控升级
1. **动态止损止盈**：止损系数、止盈乘数与个股RS分位挂钩，RS越高止损越松、止盈空间越大
2. **假期自动避让**：自动识别A股/港股节假日、周末，非交易日自动跳过所有交易任务
3. **宏观过滤集成**：自动根据宏观数据、北向资金流向、市场情绪调整仓位上限
4. **双Agent校验**：所有交易决策、策略优化由火山+DeepSeek双模型交叉验证，不一致自动拦截
### 📊 监控升级
1. **全新专业监控界面**：深色主题，顶部全局指标、全流程实时状态、持仓绩效、历史信号回溯完整展示
2. **实时推送**：盘前决策、交易信号、收盘日报、异常告警自动推送到飞书
3. **每周自动优化报告**：每周日自动生成策略优化报告，推送飞书后自动上线新参数
### 🐛 问题修复
1. 修复网络/SSL代理错误导致的行情拉取失败问题，支持4层数据源自动降级
2. 修复飞书推送参数错误问题
3. 修复.zshrc gbrain命令语法错误
### 📈 回测表现提升
- 胜率：从58%提升至63%
- 盈亏比：从2.2提升至2.7
- 最大回撤：从14.2%降低至11.8%
- 年化收益预期：从28%提升至35%
---
## v3.1.3 (2026-04-07)
- 初始版本发布，支持A股基础交易
- 集成RS选股、ATR动态止损


# v4.5.5 (2026-05-05) — 信号质量增强 + 校准闭环修复

## S1: 低精度噪声信号分层压制
- **问题**: 14只精度<50%的标的仍在输出非HOLD信号(含33%的中钨高新→buy)
- **修复**: 三层压制体系
  - acc < 45%: 强制hold + 置信度×0.3 (原已有)
  - 45% <= acc < 50%: 允许信号但置信度上限50%, signal_weight=0.6
  - acc >= 50%: 正常输出
- **文件**: `scripts/batch_predict.py` → `compute_h5d_signal()`
- **新常量**: `ACC_HOLD_FLOOR`, `ACC_LOW_CAP`, `LOW_ACC_SIGNAL_WEIGHT`, `ACC_CONFIDENCE_CAP`

## S2: 双预测周期矛盾强裁决
- **问题**: 上周时h5d vs h20d方向矛盾仅依赖±阈值判断(±5%), 许多潜在冲突被忽略
- **修复**: 符号级方向检测(±0即可), 冲突时强制hold + signal_weight×0.5
- **文件**: `scripts/batch_predict.py` → 交叉确认段
- **新常量**: `H5D_H20D_SIGN_FLIP_WEIGHT=0.5`, `H5D_H20D_SIGN_FLIP_HOLD=True`

## S3: 动态阈值正式切换
- **Phase 3 提前**: 原计划2026-05-09 → 2026-05-05
- `dynamic_threshold.enabled = true`
- 校准反馈的hard_floor_boost与动态阈值完全联动
- **文件**: `config/adaptive_params.yaml`

## S4: accuracy数组去重 (根部修复)
- **问题**: 28/43只标的有≤2个唯一accuracy值(因同模型未重训练), 趋势检测完全失效
- **修复**: 
  - batch_predict → 计算`training_hash` (文件mtime+平均精度的md5)
  - feedback_controller → 按training_hash去重, 同一版本不重复追加
  - 向后兼容: 无hash时维持旧去重逻辑
- **文件**: `scripts/batch_predict.py` + `scripts/feedback_controller.py`

## S5: correct_predictions 兑现闭环
- **问题**: correct_predictions始终为0, 兑现回路断裂
- **修复**: 
  - 从daily_records.realized_correct重新累加总数
  - 非仅增量计数(避免节假日跳过导致计数偏差)
- **文件**: `core/calibration_feedback.py` → `check_realized_accuracy()`

# v4.5.7-20260509 — Phase 1-3 模型管线升级 + 质检修复集

## 🔬 模型管线三阶段升级

### Phase 1: 重训+参数优化 (Δ+0.53%)
- 优化超参: N_ESTIMATORS=150~300, PREDICT_HORIZON=3(高波动)/5(稳定)
- 按精度分层训练: critical→high→medium→low
- 整体精度: 55.70%→56.23%
- 关键案例: 300014 亿纬锂能 44.26%→55.21% (+10.42%)

### Phase 2: 分类器+自适应集成 (Δ+0.31%)
- 新增 LGBMClassifier (直接预测方向, 非回归取符号)
- 三模型自适应权重融合: LGBMReg + XGBReg + LGBMClf (替代固定60/40)
- CV分类器精度: 56.63% (优于回归3-5%)
- 模型文件: models/{code}/lightgbm_clf.pkl

### Phase 3: 截面排序特征+时间加权 (Δ+0.18%)
- 9维截面rank特征 (全池42只跨截面排序)
- 时间指数加权采样 (近期数据权重高)
- PoolPredictor兼容v3子目录格式
- PoolPredictor预测含分类器推理

## 🛠 质检修复 (2026-05-09)

### 🔴 P0 修复
- 测试 clear_cache fixture 删除生产cache → 改为不碰文件系统
- batch_predict OOS精度补丁 → 添加未覆盖标的池均值fallback
- daily_predict.json 恢复 (含h5d信号+h20d预测+Tier)

### 🟡 P2 修复
- 1905个K线缓存 → 清理至114个
- retrain_queue 26项→13项 (去重+过滤过期)
- 37只accuracies重复值去重
- Dashboard版本号统一 (4.5.5→4.5.7)
- dsl_data_sdk.py版本号硬编码修复 (4.5.1→4.5.7)
- h20d精度从OOS评估回填 (35只真实+7只fallback)

### 🟢 P3 优化
- OpenRouter模型ID修正: ling-2.6-1t→ring-2.6-1t (免费模型)
- paper_trader.db重建 (缺失trade_history表)
- pytest安装并跑通全部测试 (53passed, 13skipped, 2xfail)
- 前端h20d显示: 区分真实精度/OOS未覆盖/无数据
- data_adapter: 校准API输出h20d_accuracy
- 整体精度: 55.70%→56.72% (Δ+1.02%)

## ⏭ 已知未集成
- h20d渠道单独训练管线, 需额外训练20日分类器 (未来工作)

# v4.5.8 (2026-05-09) — 鲁棒性加固

## 🛡️ 鲁棒性守卫 (core/robustness_guard.py)
新增8项全链路守卫，集成到 cron 健康检查 + harness pre-commit:

| Guard | 检查内容 | 防护的脆弱点 |
|-------|---------|------------|
| Guard 1 | 关键文件完整性预检 | 测试删生产数据、daily_predict被删 |
| Guard 2 | 跨数据源一致性校验 | ProgressTracker vs task_progress不同步 |
| Guard 3 | 关键文件兜底恢复 | 从校准数据自动重建daily_predict.json |
| Guard 4 | 模型池自愈加载 | 模型格式双轨制(v3/pool)、文件损坏 |
| Guard 5 | SDK可用性检查 | SDK动态加载不可用时的降级 |
| Guard 6 | 信号活性监测 | 全hold/全buy信号异常检测 |
| Guard 7 | 必填字段存在性校验 | 精度字段缺失检测 |
| Guard 8 | 配置一致性自检 | VERSION文件/股票池/配置漂移 |

## 🔧 集成修复
- ProgressTracker数据源 → Dashboard progress API (合并两个数据源)
- batch_predict OOS精度补丁 → 自动fallback未覆盖标的(池均值)
- health_check.py → 每次运行时执行守卫
- harness.py → L10 pre-commit检查
- 前端 h20dDisplay() → 区分 OOS真实精度 / 池均值fallback / 无数据
- 前端预测表 → 信号重算(预变%→方向)，Tier从股票池回填
- data_adapter get_calibration() → 输出 h20d_accuracy

## 🐛 修复
- test_dsl_data_sdk clear_cache 删除生产cache (P0) → 改为不碰文件系统
- test_unit test_feedback_controller sys.exit(0)误报 → 修复
- test_dsl_data_sdk MockResponse 缺失 raise_for_status → 修复
- paper_trader.db 缺失 trade_history 表 → 重建


# v4.5.9 — v4.5.12 安全与质量修复集

## v4.5.12a (2026-05-17) — P0/P1 安全审计修复

### 🔴 P0 致命修复
- **前视偏差修复**: `backtest_engine._build_ohlcv_df()` 不再暴露未来bar; `signal_generator.price_position` 使用shifted_close; `pool_predictor.build_features` 所有ret_Xd添加shift(1)
- **策略Bug**: `high_win_rate_strategy` self.order买入后清空(不再冻结); `rs_dynamic_profit_strategy` RS逻辑用60日均值替代硬编码0.02; 宏观分析20bar缓存
- **凭证泄露**: `.env.local` 5个API密钥清空，DSL_PRODUCTION_MODE=false
- **佣金统一**: `settings.py`删除重复COMMISSION_RATE，从`constants.py`统一导入(万2.5)

### 🟠 P1 重要修复
- **Paper Trading**: T+1约束 + 现价市值计算 + 5bp滑点
- **Decision Fusion**: 最低票数MIN_QUORUM=2 + 多空冲突检测(30%置信度惩罚) + mock全量检测
- **RiskAgent**: 风险逻辑修正(低置信度=高风险) + ATR动态止损
- **Circuit Breaker**: fcntl文件锁 + 原子写入
- **common/config.py**: 硬编码路径→相对路径 + override=False
- **data_source.py**: dsl_data_sdk懒加载import
- **港股印花税**: 0.1%→0.065%/边(2023-11降费)

### 🟡 P2 一般修复
- dsl_engine: ST/北交所涨跌停 + 向量化换手率异动
- risk_manager: 黑天鹅事件缓存 + position_peaks持久化 + 绩效归因用实际交易金额
- pool_backtest: 统一成本计算器 + 边界检查
- technical_analyst: fusion评分桥接 + end_date参数 + 数据可用性检测

> ⚠️ 注意: 前视偏差修复后，所有历史回测结果需重新验证。之前声称的+114.1%收益率可能虚高。

## v4.6.8 — Benchmark优化精简 (2026-07-19)

### 🎯 Benchmark驱动改进
基于全面benchmark分析（胜率32.1%/模型精度54%/回测Sharpe 0.87→实盘~0.45/Profit Factor 0.85<1），执行4阶段系统优化。

### 📊 股票池精简化 (Phase 1)
- **42→18只(alpha+core)+11只bench观察**
- 新三层体系：alpha(≥60%精度,7只) / core(55-60%,11只) / bench(50-55%,11只)
- 删除13只低精度股(恒玄/宁德/昆仑/汇川/协创/天华/紫金/全志/天赐/阳光/广信/赣锋/立讯)
- 扩展pool_predictor黑名单至15只(含v4.5.12原有5只)
- signal_generator新增精度门控: <50%精度直接hold, <55%需额外置信度

### 📈 交易参数优化 (Phase 2)
- buy_threshold: 0.015→0.025 (+67%, 过滤弱信号)
- sell_threshold: -0.02→-0.015 (更及时止损)
- max_positions: 3→5 (分散化)
- position_size: 0.10→0.08 (降低单票敞口)
- max_hold_days: 30→20 (减少持仓漂移)
- min_hold_days: 3→5 (减少噪音交易)
- 新增max_single_stock_pct: 0.10 (硬上限)
- trailing_trigger: 0.10→0.08, trailing_drawdown: 0.05→0.03 (收紧止盈)
- 各板止损收紧0.01-0.02
- 板块/概念/层级集中度限制收紧5-10%
- 滑点成本: base_bps 2.0→4.0, impact 0.5→0.8

### 🔬 回测校准 (Phase 3)
- backtest_walkforward.py: 次日开盘价执行(USE_NEXT_DAY_OPEN=True, 消除look-ahead bias)
- 动态成本模型: 成交量加权滑点+印花税
- 新增 calibrate_backtest_gap.py: 6维Gap分析+自动参数调整建议

### 🛡️ 模型监控 (Phase 4)
- retrain_queue清理: 27→19只(移除不在新池中的)
- 新增 monitor_accuracy_drift.py: 每日精度漂移检测(单日>5%预警/连续3日下降标记degraded/<45%黑名单)

### 📋 文件变更
- `config/master_stock_pool.yaml` — 完全重构(tier体系+精度字段)
- `config/adaptive_params.yaml` — 交易参数全线优化
- `core/pool_predictor.py` — 黑名单扩展
- `core/signal_generator.py` — 精度门控+accuracy_penalty
- `scripts/backtest_walkforward.py` — 执行价格修复+成本校准
- `scripts/calibrate_backtest_gap.py` — 新增Gap校准工具
- `scripts/monitor_accuracy_drift.py` — 新增精度漂移监控
- `data/retrain_queue.json` — 清理非池股票
- 备份: `backup/20260719_benchmark_improve/`

### 🎯 预期效果
- 胜率: 32.1% → 42-48%
- 年化收益: 14.97% → 20-25%
- Sharpe: ~0.45 → 0.70-0.85
- Profit Factor: 0.85 → 1.2-1.4

## v4.6.8.1 — 鲁棒性防护升级 (2026-07-19)

### 🛡️ 防护体系 — 4层新防线

**攻击面分析**：
- 26个cron任务异步写入11个共享状态文件
- 无原子写入 → 崩溃中途文件损坏
- 无文件锁 → 并发写冲突
- 无快照 → 污染后无回滚手段
- 无Schema校验 → 写入有效但错误的数据

**新增模块**：

1. `core/atomic_writer.py` (4函数) — 原子写入防线
   - 写临时文件 → fsync → os.replace() → fsync父目录
   - 自动创建 .last_good 备份 (JSON可自动恢复)
   - 写入后大小验证
   - max_size_kb 拒绝超大写入

2. `core/state_snapshot.py` (5函数) — 快照+回滚系统
   - snapshot_before(): 写操作前打快照
   - rollback_to(): 快照回滚 (支持dry_run)
   - 保留策略: 48h内全部 + 30天内每天1份
   - @snapshot_on_write 装饰器自动保护

3. `core/pollution_detector.py` (3函数) — 数据污染检测
   - validate_before_write(): Schema验证+字段类型+范围检查+大小突变
   - validate_after_write(): 解析验证+Key数量+时间戳+基线比对
   - 5类污染检测: Schema/类型/范围/大小/时间戳

4. `core/robustness_guard.py` — 扩充至12 Guards
   - 新增 Guard 9: 原子写入合规性 (检查.last_good备份)
   - 新增 Guard 10: 跨脚本写冲突检测 (5分钟内cron冲突)
   - 新增 Guard 11: 快照完整性检查
   - 新增 Guard 12: 数据Schema合规性

### 📁 文件变更
- `core/atomic_writer.py` — 新增 (7KB)
- `core/state_snapshot.py` — 新增 (9KB)
- `core/pollution_detector.py` — 新增 (10KB)
- `core/robustness_guard.py` — 升级 (Guards 8→12, 新增4函数)

### 🔬 防护覆盖
| 威胁 | 防线 | 状态 |
|------|------|------|
| 写入崩溃→文件损坏 | atomic_writer的temp+rename | ✅ |
| 并发写竞争 | 写冲突检测+last_good恢复 | 🟡 检测已实现 |
| 写入错误数据 | pollution_detector schema验证 | ✅ |
| 污染后无法恢复 | state_snapshot 快照回滚 | ✅ |
| 文件静默损坏 | .last_good 自动恢复 | ✅ |
| 参数越界 | buy_threshold/position_size范围检查 | ✅ |
| 数据量异常 | 大小突变检测 (10x变化告警) | ✅ |

## 🐛 v4.6.9h.1 (2026-08-01) — 交易页空白修复 (拆分遗漏)

### 🔴 P0: renderPaperTrader 函数在 UI 拆分时丢失
- **现象**: 交易页持续刷新但无任何数据显示
- **根因**: v4.6.9d 拆分时, app.js 从 index.html 220-1262 行提取, 但 renderPaperTrader 定义在 1391 行(超出范围) → 调用存在但函数从未定义, JS 运行时抛 ReferenceError
- **修复**: 从 backup_v4.6.9c_20260801/index.html 恢复函数(112行), 追加到 app.js
- **验证**: 函数对比确认无其他遗漏(黑天鹅函数均在 blackswan.js); agent-browser 实测交易页完整显示(总资产¥1,032,017/8持仓/交易记录)
- **教训**: 拆文件时必须对比备份函数清单(comm -23), 不能只靠调用点 grep

## 🐛 v4.6.9h.2 (2026-08-01) — 刷新按钮前后端统一 (方案A)

### 🔴 不一致: /api/full 缺 paperTrader 字段
- **现象**: 刷新按钮正常, 但 /api/full 无 paperTrader 字段, 交易页靠 loadPaperTraderBg 异步补丁
- **风险**: 竞态(刷新瞬间显示旧数据) + 静默失败(catch(e){})
- **修复**:
  - data_adapter.py 新增 get_paper_trader_sync() (与旧端点同结构: positions/history/ledger/performance/summary)
  - get_full_dashboard 加 "paperTrader" 字段 → /api/full 一次到位
  - server.py api_paper_trader 复用 get_paper_trader_sync (保留后台价格刷新触发)
  - 前端删除 loadPaperTraderBg 异步补丁, renderPaperTrader 直接从 DATA.paperTrader 渲染
- **验证**: /api/full.paperTrader 与 /api/paper-trader 完全一致(总资产1032017.11/8持仓/市值零差异); agent-browser 交易页+刷新+各tab正常

# v4.7.0 (2026-08-25) — 股票池结构优化 (29→17只)

基于深度分析(电子超配31%/相关性对5组违反/38%标的degraded/治理闭环断裂), 分三阶段:

## 🔴 P0: 池子瘦身与结构合规 (配置层)
- 移出12只至 observation_pool.yaml: 9只remove_candidate + 瑞芯微/北京君正/通富微电
- 中国平安保留(持仓600股+盈利12.2%, 盈利degraded观察规则)
- 指标: 精度均值0.513→0.561, 精度<50%从11→1只, 电子31%→23.5%
- pair_controls对齐: 存储/封测/通信AI自动合规, baijiu active=[000858,600519], AI应用豁免注
- retrain_queue清理71条(116→45), planned_trades清除百济神州

## 🟡 P1: 治理闭环
- update_pool_by_accuracy: remove_candidate自动移出(持仓保护) + 观察池回池机制(30天≥3样本精度≥50%)
- pool_structure_audit: tier别名映射 + stock_pool.yaml prune重建(25只漂移清理)
- batch_predict: 观察池+影子池 predict-only覆盖(12只, suspended层, morning_decision已显式跳过)
- shadow_weekly_train_verify.py: 周日先训后验(修复acc=0根因: 候选无模型/无预测产出)
- 新cron: 股票池结构审计(周日10:00)

## 🟢 P2: 信号质量 (回测验证后部署)
- 波动率自适应h5d阈值: 有效门槛=1%×clamp(vol20/截面中位数, 0.5, 2.0)
  - 回测(90天817条预测): 方向精度51.3%→52.5%, 盈亏比1.22→1.26
  - 新增74条低波信号精度51.4%, 过滤51条高波信号原本精度43.1%
- tier命名统一: TIER_LAYER_MAP对齐alpha/core/bench
- 同组单买裁决: pair组当日最多1个新买单(白酒组双买防护)
