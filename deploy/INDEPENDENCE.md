# DSL v4.5.9 — 独立部署迁移指南

## 目标架构

```
                    ┌──────────────────────────┐
                    │     DSL 核心 (全独立)      │
                    │  batch_train/batch_predict│
                    │  paper_trader/risk_manager│
                    │  signal/pool_predictor    │
                    │  dynamic_threshold        │
                    └──────┬──────────┬────────┘
                           │          │
                    ┌──────▼──┐ ┌─────▼──────┐
                    │ crontab │ │ feishu_api │  ← 独立替代层
                    └─────────┘ └────────────┘
                           │          │
              ┌────────────┼──────────┼────────────┐
              │            │          │            │
         ┌────▼───┐  ┌────▼───┐ ┌───▼────┐ ┌─────▼─────┐
         │Claude  │  │ LLM    │ │OpenClaw│ │ MCP       │
         │Code    │  │Doubao  │ │(仅备份)│ │ Server    │
         │(调试)  │  │(可选)  │ │        │ │ (接口)    │
         └────────┘  └────────┘ └────────┘ └───────────┘
```

## Phase 1: 路径抽象 (已完成 ✅)

### 问题
~15 处硬编码 `~/.openclaw/workspace/dsl-quant-trading-hybrid`

### 解决
`common/config.py` 的 `PROJECT_ROOT` 自动检测项目根目录。  
所有其他模块通过 `PROJECT_ROOT` 派生路径。

```python
# 已有 (正确)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 独立部署时设置 (可选)
export DSL_ROOT=/opt/dsl-quant
```

### 迁移
```bash
# 1. 复制项目到标准路径
cp -r dsl-quant-trading-hybrid /opt/dsl-quant

# 2. 设置环境变量
cp .env.independent /opt/dsl-quant/.env
# 编辑 .env 填入实际的飞书/LLM密钥

# 3. 验证
cd /opt/dsl-quant && python3 -c "from common.config import config; print(config.PROJECT_ROOT)"
```

---

## Phase 2: Cron 迁移 (已完成 ✅ → 需手动激活)

### 问题
28 个定时任务依赖 `openclaw cron` CLI

### 解决
`deploy/crontab.txt` — 17 个核心任务的 Linux crontab 配置

### 迁移
```bash
# 1. 预览
cat deploy/crontab.txt

# 2. 修改 DSL_ROOT 路径
sed -i 's|DSL_ROOT=/opt/dsl-quant|DSL_ROOT=/你的实际路径|' deploy/crontab.txt

# 3. 激活
crontab deploy/crontab.txt

# 4. 验证
crontab -l
```

### 保留 OpenClaw 的任务 (3个，非核心)
| 任务 | 说明 | OpenClaw 原因 |
|------|------|--------------|
| 全球财经早报 | LLM agent 生成 | 需要 LLM |
| DSL系统备份 | agent 执行 tar | 可用 crontab 替代 |
| 备份状态报告 | agent 生成报告 | 可用 Python 替代 |

---

## Phase 3: 飞书独立 (已完成 ✅ → 需手动激活)

### 问题
5 处 `subprocess(['openclaw', 'message', 'send'])` 调用

### 解决
`common/feishu_direct.py` — 独立的飞书 API 客户端

### 迁移
```python
# 旧代码 (依赖 OpenClaw)
subprocess.run(['openclaw', 'message', 'send', ...])

# 新代码 (独立)
from common.feishu_direct import send_feishu_message
send_feishu_message(content="内容")
```

### 验证
```bash
export FEISHU_APP_ID=cli_xxx
export FEISHU_APP_SECRET=xxx
python3 -c "
from common.feishu_direct import send_feishu_message
print(send_feishu_message('独立部署测试'))
"
```

---

## Phase 4: LLM 边界 (已完成 ✅)

### 问题
LLM 模块 (sentiment_analyzer, dual_agent_verification) 耦合在代码中

### 解决
`config/feature_flags.yaml` — 全局 LLM 开关

```yaml
llm:
  enabled: false   # 独立部署: false → 所有LLM功能降级为规则回退
```

### 回退链
| LLM 功能 | 回退 |
|---------|------|
| 情感分析 | 关键词词典匹配 |
| 双Agent验证 | 直接通过 (空壳) |
| 策略生成 | 固定参数 |
| 宏观分析 | 固定评分 5/10 |

### 验证
```bash
# LLM 关闭时，核心链路正常
LLM_ENABLED=false python3 scripts/batch_predict.py
```

---

## Phase 5: Claude Code 接口边界 (已完成 ✅)

### 角色定义
Claude Code 仅用于：
1. **调试**: 诊断系统问题 (`dsl_get_system_status`, `dsl_get_audit_log`)
2. **修复**: 触发重训练 (`dsl_trigger_retrain`)
3. **改进**: 运行回测验证 (`dsl_get_predictions`, `dsl_get_model_quality`)

### 接口
```
MCP Server (mcp_server/dsl_mcp/server.py)
  ├── 10 个只读/安全工具
  ├── 禁止实盘交易
  ├── 禁止修改配置
  └── 禁止删除数据
```

### 配置
```json
// ~/.claude/mcp.json
{
  "dsl-quant": {
    "command": "python",
    "args": ["-m", "dsl_mcp.server"],
    "cwd": "/opt/dsl-quant"
  }
}
```

---

## Phase 6: 完整独立验证

### 验证清单

```bash
# 1. 核心链路独立运行 (无需 OpenClaw/LLM)
python3 scripts/batch_train.py          # ✅ 训练
python3 scripts/batch_predict.py        # ✅ 预测
python3 scripts/execute_planned_trades.py  # ✅ 执行

# 2. Dashboard 独立运行
python3 web_dashboard/server.py

# 3. 飞书通知独立测试
python3 -c "from common.feishu_direct import send_feishu_message; send_feishu_message('test')"

# 4. LLM 关闭验证
LLM_ENABLED=false python3 scripts/batch_predict.py  # 无报错

# 5. OpenClaw 卸载验证
# 停止 OpenClaw, 系统继续正常工作
```

### 独立后保留的外部依赖

| 依赖 | 用途 | 替代难度 |
|------|------|:--:|
| akshare/efinance | 行情数据 | 不可替代 (核心) |
| Mairui API | 基本面数据 | 中 |
| scikit-learn/LightGBM/XGBoost/CatBoost | ML 模型 | 不可替代 (核心) |
| Flask/FastAPI | Dashboard | 低 |
| requests | HTTP 客户端 | 低 |
| 飞书 API | 通知推送 | 可替换为邮件/Slack |
| Claude Code (MCP) | 调试/修复 | 可替换为手动操作 |
| LLM API | 增强分析 | 全可选 |
| OpenClaw | (已移除) | — |
