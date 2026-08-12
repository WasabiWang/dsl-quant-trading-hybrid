#!/usr/bin/env python3
"""
check_pending_tasks.py - 每日任务检查脚本
功能：读取 Feishu Bitable 中待完成任务，生成简要列表并发送到 Feishu 群。
防忘闭环：
1. 任务记录在 Bitable（状态为 ⚪ 待开始 或 🟡 进行中）
2. 本脚本每天 08:00 通过 cron 触发
3. 通过 common.feishu_utils.send_markdown 推送提醒
4. 脚本结束后在 Memory 中记录一次检查（memory_store）
"""
import os
import sys, os
from datetime import datetime

# 项目根路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from common.feishu_utils import send_markdown
from feishu_bitable import feishu_bitable_get_meta, feishu_bitable_list_records
from memory import memory_store

APP_TOKEN = os.getenv("FEISHU_TASKS_APP_TOKEN", "")  # P1-7: 仅从环境变量读取
TABLE_ID = "tbliv2bjHBCDtFEe"

def fetch_pending():
    if not APP_TOKEN:
        print("⚠️ FEISHU_TASKS_APP_TOKEN未配置，跳过待办拉取")
        return []
    # 只获取 状态 为 ⚪ 待开始 或 🟡 进行中 的记录
    records = feishu_bitable_list_records(app_token=APP_TOKEN, table_id=TABLE_ID, page_size=100)
    pending = []
    for rec in records.get('records', []):
        fields = rec.get('fields', {})
        status = fields.get('状态')
        if status in ["⚪ 待开始", "🟡 进行中"]:
            pending.append({
                "id": rec.get('record_id'),
                "title": fields.get('输出物', '未命名'),
                "week": fields.get('周次'),
                "priority": fields.get('优先级'),
                "status": status,
                "remark": fields.get('备注', '')
            })
    return pending

def format_message(pending):
    if not pending:
        return "✅ 今天没有待办任务，一切顺利！"
    lines = ["⏰ 今日待办任务列表（截至 %s）:" % datetime.now().strftime('%Y-%m-%d %H:%M')]
    for i, t in enumerate(pending, 1):
        lines.append(f"{i}. [{t['priority']}] {t['title']} (周次: {t['week']}) - {t['status']}")
        if t['remark']:
            lines.append(f"   📌 备注: {t['remark']}")
    return "\n".join(lines)

def main():
    pending = fetch_pending()
    msg = format_message(pending)
    # 发送 Feishu 提醒
    send_markdown(title="每日任务提醒", content=msg)
    # 记录到 Memory（方便回溯）
    memory_store(text=f"[每日提醒] {len(pending)} 条待办任务记录", category="log", importance=0.6)

if __name__ == "__main__":
    main()
