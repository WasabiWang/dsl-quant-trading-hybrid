#!/usr/bin/env python3
"""
备份状态报告 — 每日备份完成后生成状态报告并发送飞书通知
"""
import os, json, subprocess, sys
from datetime import datetime, timedelta

BACKUP_DIR = os.path.expanduser("~/.openclaw/workspace/backup")
LOGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

def _scan_backups():
    """扫描所有备份格式（目录或tar.gz），返回按时间排序的列表。"""
    if not os.path.exists(BACKUP_DIR):
        return []
    entries = []
    for name in os.listdir(BACKUP_DIR):
        path = os.path.join(BACKUP_DIR, name)
        # 目录备份如 dsl_backup_20260726_0210/
        if os.path.isdir(path) and name.startswith('dsl_backup_'):
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            total_size = sum(
                os.path.getsize(os.path.join(dirpath, f))
                for dirpath, _, filenames in os.walk(path)
                for f in filenames
            )
            entries.append({"file": name, "size_bytes": total_size, "mtime": mtime})
        # tar.gz 备份
        elif name.endswith('.tar.gz') and 'dsl_backup' in name:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            entries.append({"file": name, "size_bytes": os.path.getsize(path), "mtime": mtime})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries

def get_latest_backup_info():
    backups = _scan_backups()
    if not backups:
        return None
    latest = backups[0]
    size_mb = round(latest['size_bytes'] / 1024 / 1024, 1)
    oldest = backups[-1]['mtime']
    return {
        "file": latest['file'], "size_mb": size_mb,
        "time": latest['mtime'].strftime("%Y-%m-%d %H:%M"),
        "total": len(backups),
        "oldest": oldest.strftime("%Y-%m-%d"),
        "newest": latest['mtime'].strftime("%Y-%m-%d")
    }

def generate_report():
    info = get_latest_backup_info()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"📦 DSL备份状态报告 | {now}", "=" * 40]
    if info:
        lines.extend([
            f"最新备份: {info['file']}",
            f"备份时间: {info['time']}",
            f"文件大小: {info['size_mb']} MB",
            f"保留备份: {info['total']} 个 ({info['oldest']} ~ {info['newest']})",
        ])
    else:
        lines.append("⚠️ 未找到备份文件")
    return "\n".join(lines)

if __name__ == "__main__":
    report = generate_report()
    print(report)
    # Save report to logs
    os.makedirs(LOGS_DIR, exist_ok=True)
    report_path = os.path.join(LOGS_DIR, f"backup_report_{datetime.now().strftime('%Y%m%d')}.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存: {report_path}")
