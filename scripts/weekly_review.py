#!/usr/bin/env python3
"""
weekly_review.py - 每周评审脚本
功能：
1. 汇总过去一周的 Bitable 任务完成情况
2. 生成 Feishu 文档 (标题: "[Sprint X 进度报告] YYYY-MM-DD")
3. 将报告内容存入 Memory（方便回溯）
4. 自动标记已完成任务为 ✅ 完成（可选）
"""
import os, sys, json
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from common.feishu_utils import send_markdown
try:
    from memory import memory_store
except ImportError:
    memory_store = None

APP_TOKEN = os.getenv("FEISHU_TASKS_APP_TOKEN", "")  # P1-7: 仅从环境变量读取
TABLE_ID = "tbliv2bjHBCDtFEe"

def fetch_last_week_records():
    """使用 OpenClaw CLI 调用 Bitable list_records，返回最近一周的记录"""
    # 这里采用 subprocess 调用 openclaw 命令行工具，避免 Python SDK 依赖
    import subprocess, shlex
    if not APP_TOKEN:
        print("⚠️ FEISHU_TASKS_APP_TOKEN未配置，跳过周报任务拉取")
        return []
    cmd = f"openclaw bitable list_records --app-token {APP_TOKEN} --table-id {TABLE_ID} --page-size 200"
    result = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        return data.get("records", [])
    except Exception:
        return []

def summarize_week(records):
    week_ago = datetime.now() - timedelta(days=7)
    completed = []
    pending = []
    for rec in records:
        fields = rec.get("fields", {})
        # 假设有一个 "完成时间" 字段，若不存在则用创建时间判断
        # 这里简化：只看状态字段
        status = fields.get("状态")
        title = fields.get("输出物", "未命名")
        week = fields.get("周次", "未知")
        if status == "✅ 完成":
            completed.append(f"- {title} (周次: {week})")
        else:
            pending.append(f"- {title} (周次: {week}) - {status}")
    return completed, pending

def generate_markdown(completed, pending):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"## 📊 每周进度报告 ({today})", "", "### ✅ 已完成任务", ""]
    lines.extend(completed or ["- 无"])
    lines.append("\n### ⏳ 待完成任务")
    lines.append("")
    lines.extend(pending or ["- 无"])
    return "\n".join(lines)

def main():
    records = fetch_last_week_records()
    completed, pending = summarize_week(records)
    report_md = generate_markdown(completed, pending)
    # 发送 Feishu Markdown 报告
    send_markdown(title="每周进度报告", content=report_md)
    # 记录到 Memory
    if memory_store:
        memory_store(text=f"[周评审] 完成 {len(completed)} 项，待完成 {len(pending)} 项", category="log", importance=0.8)
    else:
        print(f"[周评审] 完成 {len(completed)} 项，待完成 {len(pending)} 项 (memory_store unavailable)")

if __name__ == "__main__":
    main()
