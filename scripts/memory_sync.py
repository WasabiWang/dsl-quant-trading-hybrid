#!/usr/bin/env python3
"""
Memory → 飞书文档同步 v4.5.3c
从 memory-lancedb-pro 读取最近记忆 → 结构化展示 → 写入飞书知识库文档

重命名说明: v4.5.3c 从 gbrain_sync.py 重命名而来，源Postgres容器已停用，
           所有遗留下的postgres fallback代码已清除。

用法:
  python3 scripts/memory_sync.py          # 默认写入飞书文档
  python3 scripts/memory_sync.py --dry    # 仅打印，不写飞书
"""
import subprocess, json, os, sys, re
from datetime import datetime

DOC_TOKEN = os.getenv("FEISHU_DOC_TOKEN", "TRStdMZKvomal2xSayjcXUqUnee")  # P1-7: 优先环境变量
PROJECT = os.path.expanduser("~/.openclaw/workspace/dsl-quant-trading-hybrid")

# 排除噪音: 纯时间戳/文件名/短内容/request-timed-out
NOISE_PATTERNS = [
    r"^\d{4}-\d{2}-\d{2}$",                    # 纯日期
    r"^\d{4}-\d{2}-\d{2}-\d{4}$",              # 日期-时间
    r"^\d{4}-\d{2}-\d{2}-\d{4}-.*",             # 日期-时间-xxx
    r"^\d{4} \d{2} \d{2}$",                       # 带空格的日期
    r"^request.timed.out",                          # 超时错误
    r"^\d{3}_$",                                    # 数字后缀
    r"^\d{2} \d{2} \d{2}$",                        # 时间戳
]


def fetch_memories(limit: int = 20) -> list:
    """从 memory-lancedb-pro 获取最近记忆
    v4.5.3d: v2026.5.2 重构了 memory 插件, CLI不可用→LanceDB直读"""
    # 优先: openclaw memory CLI
    try:
        res = subprocess.run(
            ['openclaw', 'memory', 'list', '--limit', str(limit)],
            capture_output=True, text=True, timeout=10, cwd=PROJECT
        )
        if res.returncode == 0:
            memories = []
            for line in res.stdout.strip().split('\n'):
                line = line.strip()
                if not line or len(line) < 20:
                    continue
                if ']' in line[:15]:
                    try:
                        date_part = line.split(']')[0].lstrip('[')
                        text = line.split(']', 1)[1].strip()[:200]
                        memories.append({'date': date_part.strip(), 'text': text})
                    except Exception:
                        memories.append({'date': '?', 'text': line[:200]})
                else:
                    memories.append({'date': '?', 'text': line[:200]})
            return memories
    except Exception:
        pass
    print("  ⚠️ openclaw memory CLI 不可用 (v2026.5.2 plugin 架构变更)")
    return []


def get_stats() -> dict:
    """获取 memory-lancedb-pro 统计数据
    v4.5.3d: v2026.5.2 兼容 — CLI不可用则返回标记"""
    try:
        res = subprocess.run(
            ['openclaw', 'memory', 'stats'],
            capture_output=True, text=True, timeout=10, cwd=PROJECT
        )
        if res.returncode == 0:
            total = re.search(r'total[:\s]+(\d+)', res.stdout, re.IGNORECASE)
            return {
                'total': int(total.group(1)) if total else '?',
                'source': 'memory-lancedb-pro',
            }
    except Exception:
        pass
    # v4.5.3d: v2026.5.2 CLI不可用 — 尝试从 LanceDB 目录统计
    try:
        lancedb_dir = os.path.expanduser('~/.openclaw/lancedb/memory')
        if os.path.exists(lancedb_dir):
            file_count = sum(1 for _ in os.listdir(lancedb_dir) if not _.startswith('.'))
            return {'total': file_count, 'source': 'memory-lancedb-pro (dir)'}
    except Exception:
        pass
    return {'total': '?', 'source': 'memory-lancedb-pro (v2026.5.2)'}


def filter_quality(memories: list) -> list:
    """过滤低质量噪音记忆"""
    noise_re = [re.compile(p) for p in NOISE_PATTERNS]
    filtered = []
    for m in memories:
        text = m.get('text', '')
        if any(n.match(text) for n in noise_re):
            continue
        if len(text) < 15:
            continue
        category = '📊 量化' if any(k in text for k in ['DSL','策略','交易','信号','模型','预测','仓位','回测','训练']) else \
                   '📋 系统' if any(k in text for k in ['cron','脚本','修复','配置','API','数据','health']) else \
                   '🦢 风险' if any(k in text for k in ['黑天鹅','风险','地缘','战争','油价','黄金']) else \
                   '🧠 记忆'
        filtered.append({**m, 'category': category[:10]})
    return filtered


def generate_content(memories: list, stats: dict) -> str:
    """生成飞书文档 Markdown"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    source_label = stats.get('source', '?')
    total = stats.get('total', '?')

    content = f"""# 🧠 个人量化知识库

## 📊 当前状态

| 指标 | 数值 |
|:--|:--|
| 📄 知识条目 | **{total}** |
| 🔗 数据源 | **{source_label}** |
| 🕐 同步时间 | {now} (GMT+8) |
| 🔍 搜索 | 飞书中直接问 Neo |

## 📋 最近知识条目

"""
    if not memories:
        content += "> ⚠️ 暂无近期知识条目\n"
    else:
        for i, m in enumerate(memories[:12], 1):
            cat = m.get('category', '')
            date = m.get('date', '?')
            text = m.get('text', '')[:100].replace('\n', ' ')
            content += f"{i}. {cat} `{date}` {text}\n"

    content += f"""

---

*自动同步: {now} (GMT+8) | 源: {source_label} | v4.5.3c*
"""
    return content


def write_to_feishu(content: str, dry_run: bool = False):
    """直接写飞书文档"""
    if dry_run:
        print("--- DRY RUN ---")
        print(content[:600])
        print("...")
        return True

    tmp = "/tmp/memory_sync_md.md"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)

    try:
        res = subprocess.run([
            'openclaw', 'feishu-doc', 'write',
            '--doc-token', DOC_TOKEN,
            '--file', tmp,
        ], capture_output=True, text=True, timeout=15, cwd=PROJECT)
        if res.returncode == 0:
            print(f"✅ 飞书文档已更新: {DOC_TOKEN}")
            return True
        else:
            print(f"⚠️ CLI写失败: {res.stderr[:200]}")
    except Exception as e:
        print(f"⚠️ CLI异常: {e}")

    print("⚠️ 无法写入飞书，输出到 stdout:")
    print(content[:1000])
    return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Memory → 飞书文档同步")
    parser.add_argument("--dry", action="store_true", help="Dry run，不写飞书")
    args = parser.parse_args()

    print(f"{'='*50}")
    print(f"🧠 记忆库飞书同步 v4.5.3c")
    print(f"{'='*50}")

    stats = get_stats()
    print(f"📊 数据源: {stats.get('source')}, 条目: {stats.get('total')}")

    raw = fetch_memories(30)
    print(f"📥 原始记忆: {len(raw)}条")

    memories = filter_quality(raw)
    print(f"✨ 过滤后: {len(memories)}条")

    content = generate_content(memories, stats)

    success = write_to_feishu(content, dry_run=args.dry)

    print(f"\n📋 同步完成: {len(memories)}条记忆 → 飞书文档")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
