#!/usr/bin/env python3
"""
scripts/create_sprint_tasks.py

一次性向新建的 Bitable 看板（NIYyb9FRUa0pMasMLJqcjqNangd）批量创建
Sprint 2‑9 的所有关键任务记录。

- 脚本会先调用 `common.feishu_utils.get_tenant_access_token()` 取得 Feishu 的租户 access‑token。
- 然后对每个任务使用 Feishu Bitable 的 **Create Record** 接口一次性写入。
- 所有必要的字段（**周次、状态、优先级、工作量(h)、输出物、备注**）都已经在看板中提前创建。
- 运行后会在终端打印每条记录的 `record_id`，并把成功创建的记录写入 `memory/YYYY‑MM‑DD.md` 作为持久化日志（防忘闭环的一环）。
"""

import os
import json, sys, os, datetime, requests

# 项目根目录（确保能 import 我们的 feishu_utils）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from common.feishu_utils import get_tenant_access_token
# from memory import memory_store  # Disabled for script execution context

APP_TOKEN = os.getenv("FEISHU_SPRINT_APP_TOKEN", "NIYyb9FRUa0pMasMLJqcjqNangd")  # P1-7: 优先环境变量
TABLE_ID = "tbljQlCh0f7ZLTzR"
API_URL = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records"

# ------------------- 任务清单 -------------------
# 每条任务是一个 dict，键名必须和 Bitable 中的字段名保持一致
TASKS = [
    # ---- Sprint 2 ----
    {
        "DSL 量化系统任务看板": "完成完整预测模型 (train_predictor.py)",
        "周次": "W2 数据集成",
        "状态": "⚪ 待开始",
        "优先级": "P0 关键",
        "工作量(h)": 8,
        "输出物": "train_predictor.py 完成，模型文件保存",
        "备注": "实现数据抓取、特征工程、模型训练与持久化"
    },
    # ---- Sprint 3 ----
    {
        "DSL 量化系统任务看板": "实现每日模拟交易 (simulated_trade.py)",
        "周次": "W3 反馈基础设施",
        "状态": "⚪ 待开始",
        "优先级": "P0 关键",
        "工作量(h)": 6,
        "输出物": "simulated_trade.py 完成，交易日志写入 Memory",
        "备注": "读取当天信号、生成买卖指令、推送 Feishu"
    },
    {
        "DSL 量化系统任务看板": "实现每日反馈闭环 (feedback_loop.py)",
        "周次": "W3 反馈基础设施",
        "状态": "⚪ 待开始",
        "优先级": "P0 关键",
        "工作量(h)": 5,
        "输出物": "feedback_loop.py 完成，关键改进写入 Memory",
        "备注": "解析每日复盘日志、生成策略优化建议"
    },
    # ---- Sprint 4 ----
    {
        "DSL 量化系统任务看板": "完善风险管理 (StrategyMonitor / CircuitBreaker)",
        "周次": "W4 策略增强",
        "状态": "✅ 完成",
        "优先级": "P0 关键",
        "工作量(h)": 4,
        "输出物": "core/strategy_monitor.py、core/circuit_breaker.py",
        "备注": "已在代码库中实现并通过单元测试"
    },
    # ---- Sprint 5 ----
    {
        "DSL 量化系统任务看板": "实现 LLM 策略生成器 (llm_strategy_generator.py)",
        "周次": "W5 混合架构",
        "状态": "⚪ 待开始",
        "优先级": "P0 关键",
        "工作量(h)": 7,
        "输出物": "llm_strategy_generator.py 完成，生成 DSL 并写入 Bitable",
        "备注": "调用 OpenRouter/Claude 接口，将自然语言转为 DSL JSON"
    },
    # ---- Sprint 6 ----
    {
        "DSL 量化系统任务看板": "CI/CD 完整化 (GitHub Actions)",
        "周次": "W6 生产化",
        "状态": "✅ 完成",
        "优先级": "P0 关键",
        "工作量(h)": 3,
        "输出物": ".github/workflows/ci.yml",
        "备注": "已在仓库中配置，自动跑单元测试、健康检查"
    },
    # ---- Sprint 7 ----
    {
        "DSL 量化系统任务看板": "部署实时仪表盘 (Streamlit Docker)",
        "周次": "W7 监控仪表盘",
        "状态": "⚪ 待开始",
        "优先级": "P1 重要",
        "工作量(h)": 10,
        "输出物": "dashboard/app.py + Dockerfile",
        "备注": "展示资金、盈亏、策略信号等实时指标"
    },
    # ---- Sprint 8 ----
    {
        "DSL 量化系统任务看板": "完善每日/每周提醒脚本",
        "周次": "W8 LLM生成",
        "状态": "✅ 完成",
        "优先级": "P0 关键",
        "工作量(h)": 4,
        "输出物": "scripts/check_pending_tasks.py、scripts/weekly_review.py、scripts/memory_review.py",
        "备注": "已在 crontab 中加入，自动推送 Feishu 并记入 Memory"
    },
    # ---- Sprint 9 ----
    {
        "DSL 量化系统任务看板": "四层防忘闭环全链路验证",
        "周次": "W9 防忘闭环",
        "状态": "🟡 进行中",
        "优先级": "P0 关键",
        "工作量(h)": 6,
        "输出物": "Bitable ↔ Cron ↔ Weekly Doc ↔ Memory",
        "备注": "确认每层信息同步、任务状态自动标记、月度记忆回顾"
    }
]

# ------------------- 创建记录函数 -------------------
def create_record(task: dict, token: str):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"fields": task}
    resp = requests.post(API_URL, headers=headers, data=json.dumps(payload))
    if resp.status_code != 200:
        print(f"❌ 创建任务失败: {task['DSL 量化系统任务看板']}")
        print("Response:", resp.text)
        return None
    data = resp.json()
    record_id = data.get("record", {}).get("record_id")
    print(f"✅ 创建成功: {task['DSL 量化系统任务看板']} → record_id={record_id}")
    return record_id

# ------------------- 主入口 -------------------
def main():
    token = get_tenant_access_token()
    if not token:
        print("❌ 获取 Feishu access_token 失败，终止任务创建。")
        sys.exit(1)

    today = datetime.date.today().isoformat()
    created_ids = []

    for t in TASKS:
        rid = create_record(t, token)
        if rid:
            created_ids.append(rid)

    # 记录到 Memory，方便防忘闭环追踪
    if created_ids:
        memory_store(
            text=f"[Bitable] 批量创建 Sprint 任务，record_ids: {', '.join(created_ids)}",
            category="log",
            importance=0.8
        )
        print("\n📝 已写入 Memory，任务创建日志已持久化。")

if __name__ == "__main__":
    main()
