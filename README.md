# DSL 量化交易系统

![Version](https://img.shields.io/badge/version-4.6.9h-blue)
![Python](https://img.shields.io/badge/python-3.10+-green)
![License](https://img.shields.io/badge/license-MIT-orange)

基于 Qlib + LightGBM + Backtrader 的 A 股多层级量化交易系统，包含数据管线、模型训练、信号生成、盘前决策、盘中监控、Dashboard 可视化等完整闭环。

> ⚠️ **免责声明**: 本项目仅供学习与研究使用，不构成任何投资建议。量化交易存在风险，使用本项目进行实盘交易造成的任何损失由使用者自行承担。

## ✨ 特性

- **多层级股票池管理**: alpha / core / growth / bench / flex 五级分层，动态阈值引擎
- **模型管线**: LightGBM + 三模型自适应集成，截面 rank 特征 + 时间加权采样
- **完整闭环**: 晚间预案 → 盘前数据刷新 → 盘前决策 → 盘中信号监控 → 收盘日报
- **风控体系**: 黑天鹅监控、动态仓位管理、止损止盈、行业配额限制
- **Dashboard**: 持仓/绩效/预测/模型/风险全景可视化 (Flask)
- **多数据源降级链**: 麦蕊(MyQuant) → akshare → 新浪/东财/腾讯，自动降级
- **定时任务**: 全自动 cron 调度 (训练/预测/备份/健康检查)

## 🏗️ 架构概览

```
├── core/                  # 核心逻辑 (信号/特征/模型/风控)
├── predictor/             # 训练与预测管线
├── scripts/               # 可执行脚本 (训练/预测/盘前/决策/监控)
├── execution_engine/      # 交易执行 (paper broker / 券商适配)
├── simulation/            # 回测与模拟
├── web_dashboard/         # Flask Dashboard
├── common/                # 通用工具 (飞书/邮件/配置)
├── config/                # 配置 (股票池为私有, 见下方说明)
├── tests/                 # 测试
└── docs/                  # 技术文档
```

## 🚀 快速开始

### 1. 环境准备

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env        # 填入 MAIRUI_LICENCE 等环境变量
cp config/master_stock_pool.example.yaml config/master_stock_pool.yaml   # 填入你的股票池
cp config/adaptive_params.example.yaml config/adaptive_params.yaml       # 可选, 调整策略参数
```

> 🔒 **股票池说明**: 本项目公开代码但不公开真实股票池与策略参数（位于 `config/master_stock_pool.yaml`、`config/adaptive_params.yaml` 等）。请使用上述模板自行配置。

### 3. 数据源

系统主要依赖 [麦蕊智数 API](https://mairui.club)（需自行申请 Licence），降级链为 akshare 等免费源。

### 4. 运行

```bash
# 训练模型
python scripts/batch_train.py

# 生成预测
python scripts/batch_predict.py

# 盘前数据刷新
python scripts/pre_market_refresh.py

# 盘前决策
python scripts/morning_decision.py

# 启动 Dashboard
python web_dashboard/server.py  # 默认 http://localhost:8888

# 运行测试
pytest tests/ -v -m "not slow"
```

## 📊 Dashboard

- 预测/信号/持仓/绩效/模型精度/黑天鹅风险全景
- 支持"立即重训"等运维操作

## 📁 关键文档

- `docs/DSL_v4_2_1_OPTIMIZATION.md` — 优化历程
- `docs/dsl_data_sdk_api.md` — 数据 SDK API 参考
- `docs/mairui_api_reference.md` — 麦蕊 API 参考
- `WHITEPAPER.md` — 系统白皮书

## 🛡️ 质量保障

- L0-L3 分层测试 + Property-Based + Golden Test (`harness.sh`)
- CI: pytest + 代码逻辑校验
- 健康检查: 8 项全链路守卫

## 📄 License

[MIT](LICENSE) © 2026 WasabiWang
