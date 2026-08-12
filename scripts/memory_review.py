#!/usr/bin/env python3
"""
memory_review.py - 每月记忆回顾脚本
功能：
1. 读取 recent 30 天的 memory/*.md 日志文件
2. 提取关键决策、教训、风险点（基于简单关键词匹配）
3. 合并写入根目录 MEMORY.md 的 "经验教训" 区块（若不存在则创建）
4. 发送 Feishu 汇报并记录到 Memory
"""

import os, re, sys
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(PROJECT_ROOT, "memory")
MEMORY_MD = os.path.join(PROJECT_ROOT, "MEMORY.md")

# 引入 Feishu 通知工具
sys.path.insert(0, PROJECT_ROOT)
from common.feishu_utils import send_markdown
from memory import memory_store

def collect_recent_logs(days=30):
    cutoff = datetime.now() - timedelta(days=days)
    logs = []
    for fname in os.listdir(MEMORY_DIR):
        if not fname.endswith('.md'):
            continue
        fpath = os.path.join(MEMORY_DIR, fname)
        try:
            ts = datetime.strptime(fname.split('.')[0], '%Y-%m-%d')
        except Exception:
            continue
        if ts >= cutoff:
            with open(fpath, 'r', encoding='utf-8') as f:
                logs.append(f.read())
    return '\n'.join(logs)

def extract_insights(text):
    # 简单关键词匹配，提取含有 "[" 或 "⚠️" 或 "✅" 的行
    lines = []
    for line in text.splitlines():
        if any(kw in line for kw in ['✅', '⚠️', '[', '错误', '失败', '成功']):
            lines.append(line.strip())
    return lines

def update_memory_md(insights):
    if not insights:
        return False
    # 确保 MEMORY.md 存在
    if not os.path.exists(MEMORY_MD):
        with open(MEMORY_MD, 'w', encoding='utf-8') as f:
            f.write('# 🧠 MEMORY.md - 长期记忆\n\n')
    # 读取内容
    with open(MEMORY_MD, 'r', encoding='utf-8') as f:
        content = f.read()
    # 找到 "## 经验教训" 标题位置，没有则新增
    header = '## 经验教训'
    if header not in content:
        content += f"\n{header}\n\n"
    # 将新内容插入标题下方
    parts = content.split(header)
    before = parts[0]
    after = parts[1] if len(parts) > 1 else ''
    new_section = '\n'.join(insights) + '\n'
    updated = f"{before}{header}\n\n{new_section}{after}"
    with open(MEMORY_MD, 'w', encoding='utf-8') as f:
        f.write(updated)
    return True

def main():
    logs = collect_recent_logs(30)
    insights = extract_insights(logs)
    updated = update_memory_md(insights)
    if updated:
        send_markdown(title='🧠 每月记忆回顾', content='已将最近30天的关键日志写入 MEMORY.md')
        memory_store(text='[记忆回顾] 更新 MEMORY.md 经验教训', category='log', importance=0.8)
    else:
        send_markdown(title='🧠 每月记忆回顾', content='未检测到新日志，暂无更新')

if __name__ == '__main__':
    main()
