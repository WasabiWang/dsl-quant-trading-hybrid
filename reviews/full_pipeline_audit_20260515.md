# DSL v4.5.12 全流程数据链审查 — 修复建议清单
# 审查时间: 2026-05-15 22:30 | 审查人: Neo
# 审查范围: 17个核心脚本 + cron任务 + adaptive_params + 数据文件链路

## 🔴 P0 — 必须立即修复（数据链断裂/资金风险）

### P0-1: 3个shell cron仍报 "isolated job requires payload.kind=agentTurn"
- **现状**: DSL系统备份(f01a8d89)、备份状态报告(867b75d9)、A股盘前交易预案(19bd9fea) 连续skipped
- **根因**: 05-15修复时遗漏，这3个job的payload.kind仍不匹配sessionTarget
- **影响**: 备份不执行=数据无保护; 盘前预案缺失=morning_decision无晚间缓存可用
- **修复**: 3个job统一改为 systemEvent + sessionTarget=main

### P0-2: black_swan_position_ratio=0.16 但实际持仓48%
- **现状**: adaptive_params.yaml中 position_ratio=0.16(16%), 但PaperTrader持仓=48.0%
- **根因**: execute_scheduled_trades.py的缩量逻辑只检查单笔买入后是否超限，不检查**存量持仓已超限**的情况。黑天鹅降级是渐进的(1.0→0.6→0.16)，但持仓是在高位建仓的，ratio下降后从未触发被动减仓
- **影响**: 黑天鹅风控形同虚设——ratio降到16%但系统不主动卖出
- **修复**: morning_decision/stop_loss_monitor需新增"持仓超限强制减仓"逻辑：当current_ratio > position_ratio时，生成sell指令将持仓降至ratio上限

### P0-3: 盘前预案cron脚本名错误
- **现状**: cron "A股盘前交易预案" 调用 `pre_market_preparation.py`，但实际脚本名是 `pre_market_refresh.py`（且morning_decision的evening mode才是生成预案的）
- **影响**: 晚间预案永远执行失败，morning mode无evening缓存可用
- **修复**: 改为调用 `morning_decision.py --mode evening`

### P0-4: batch_train模型更新日≠batch_predict消费日，存在stale model风险
- **现状**: batch_train 16:00训练，batch_predict 17:00预测——但models目录下最新模型是05-11(4天前)，说明batch_train近期可能静默跳过部分标的
- **根因**: batch_train遇到数据不足/训练失败时仅print跳过，不写入任何标记；batch_predict不知道模型是否"今日新鲜"
- **影响**: 用4天前的模型预测今天，精度下降但无告警
- **修复**: batch_train写入 `cache/training_status.json`(每只标的的last_train_date+status)；batch_predict读取并标记stale模型预测的signal_weight×0.5

---

## 🟠 P1 — 应尽快修复（效率损失/数据质量）

### P1-1: daily_predict.json无版本校验，跨日消费无阻断
- **现状**: batch_predict写入daily_predict.json含predict_date字段，但intraday_signal_monitor(09:45/11:00/14:00)直接读取不做日期检查
- **影响**: 如果batch_predict当天失败，盘中监控用的仍是昨天预测，且不知道
- **修复**: 所有消费daily_predict.json的模块必须检查predict_date==today，否则打印醒目告警+signal_weight×0.5

### P1-2: feedback_controller写adaptive_params无并发保护
- **现状**: feedback_controller(20:00)和黑天鹅复盘(21:00)都可能写入adaptive_params.yaml，间隔仅1小时
- **根因**: 虽有file_lock.py，但feedback_controller.update_from_black_swan()内部先load再save，两进程可能交叉覆盖
- **修复**: 所有写adaptive_params的路径统一走 locked_yaml_write()（已有，但部分路径未用）

### P1-3: 5只critical重训标的积压4天未处理
- **现状**: calibration.retrain_plan中5只priority=critical(亿纬锂能0.43/招商银行0.43/赣锋锂业0.42/天齐锂业0.39/天赐材料0.39)，但models目录最新模型是05-11
- **根因**: batch_train的retrain_queue消费逻辑可能跳过这些标的(数据不足/训练失败静默跳过)
- **修复**: batch_train必须对retrain_queue中的critical标的强制重训(降低MIN_TRAIN_SAMPLES)，失败则写入failed_retrain告警

### P1-4: PaperTrader.current_price不实时更新
- **现状**: positions表中current_price字段仅在execute_trade时写入，之后不再更新
- **影响**: 止损监控/日报的持仓市值/盈亏计算基于过时价格；Dashboard展示的浮盈浮亏不准
- **修复**: stop_loss_monitor执行时同步更新positions.current_price；或新增独立的价格刷新步骤

### P1-5: 盘中监控cron时间窗口 vs 收盘日报时间冲突
- **现状**: 14:00盘中监控 → 15:10收盘日报 → 15:40健康检查，三者间隔仅70/30分钟
- **风险**: 如果14:00监控触发批量预测刷新(耗时>5min)，可能占用资源导致15:10日报延迟
- **修复**: 14:00监控改为"仅告警不刷新预测"(已部分实现但refresh_intraday_predictions仍可被触发)

### P1-6: cache目录kline文件无限增长
- **现状**: cache/下有970+个kline JSON文件(每个150-265KB)，总量~240MB，无清理机制
- **影响**: 磁盘占用持续增长；文件系统readdir变慢
- **修复**: backup.sh中增加kline cache清理：保留最近5天，删除>7天的kline文件

---

## 🟡 P2 — 建议优化（架构改进/可维护性）

### P2-1: morning_decision.py 83812行——单文件过大
- **现状**: morning_decision.py是全系统最大文件(84KB)，包含数据加载、信号融合、alpha评分、交易计划生成、飞书报告等全部逻辑
- **风险**: 修改任一部分都有引入regression的风险；测试困难
- **建议**: 拆分为 morning_data_loader.py / signal_fusion.py / trade_planner.py / morning_reporter.py

### P2-2: 数据流无统一schema验证
- **现状**: 各脚本用json.load()直接读取，不验证字段是否存在/类型是否正确
- **风险**: 一个脚本改了输出格式，下游静默读到None/KeyError
- **建议**: 引入Pydantic或dataclass定义核心数据schema(daily_predict/planned_trades/adaptive_params)，在读写时validate

### P2-3: 多脚本重复实现相同功能
- **现状**: get_stock_pool()在batch_train/batch_predict/morning_decision/batch_predict中各实现一份；fetch_overnight_data()在pre_market_refresh和morning_decision中各一份
- **建议**: 抽取到 common/data_utils.py，单一来源

### P2-4: cron任务无端到端依赖管理
- **现状**: batch_train(16:00)→batch_predict(17:00)→pre_market_refresh(09:00)→morning_decision(09:20)→execute(09:30) 靠时间先后保证执行顺序
- **风险**: 如果batch_train延迟到17:30才完成，batch_predict(17:00)用的仍是旧模型
- **建议**: 引入依赖标记文件(batch_train写`cache/training_done.flag`，batch_predict检查flag不存在则等待/跳过)

### P2-5: 无回测驱动的信号阈值自动调优
- **现状**: 信号阈值(H5D_SIGNAL_THRESHOLD=0.005等)硬编码在batch_predict.py中，仅通过手动修改adaptive_params覆盖
- **建议**: feedback_controller根据近N天回测结果自动调整阈值(sharpe下降→收紧阈值，上升→放宽)

### P2-6: 止损监控仅3个时间点，无法捕捉盘中闪崩
- **现状**: stop_loss_monitor仅在09:50/11:05/14:05执行
- **风险**: 10:30闪崩到-8%时，要等到11:05才触发止损
- **建议**: 增加盘中价格订阅(websocket/轮询)，或至少增加10:30/13:30两个检查点

### P2-7: 执行日志无对账机制
- **现状**: execution_log.json记录了执行结果，但不与PaperTrader SQLite的trade_history做对账
- **风险**: 执行日志说"executed"但DB里无对应记录(或反之)，无法发现数据不一致
- **建议**: health_check新增对账检查：execution_log与trade_history按order_id交叉比对

---

## 📊 审查方法建议

| 方法 | 说明 | 优先级 |
|------|------|--------|
| **数据链DAG图** | 画出所有文件级依赖关系(pre_market_refresh→cache→morning_decision→planned_trades→execute)，可视化断链 | 高 |
| **端到端注入测试** | 构造一个"假交易日"，从batch_train到execute全链路跑一遍，验证每步输出文件存在且schema正确 | 高 |
| **时序竞争检测** | 在cron任务密集时段(09:20-09:30)加日志，检测文件读写竞争 | 中 |
| **数据新鲜度Dashboard** | data_adapter已有_staleness检测，扩展到所有关键文件，Dashboard展示"数据健康度" | 中 |
| **Mutation Testing** | 随机修改一个中间文件(如daily_predict.json删一个字段)，检查下游是否graceful degrade | 低 |

---

*审查完成: 4个P0 + 6个P1 + 7个P2 | 最紧急: P0-2(持仓48%>>ratio16%) + P0-1(备份cron失效)*
