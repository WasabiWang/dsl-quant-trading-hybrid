#!/usr/bin/env python3
"""scripts/auto_backup.py — 自动化备份系统 v4.5.12

替代手动 .bak 文件重命名:
- config/*.yaml, *.json → config/archive/
- data/*.json, *.db → data/archive/ (排除大型DB文件)
- 每日自动 + 每次git commit前备份
- 保留最近30天备份，自动清理

用法:
  python3 scripts/auto_backup.py            # 执行备份
  python3 scripts/auto_backup.py --cleanup   # 清理过期备份
"""

import os, sys, json, shutil
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_ROOT = PROJECT_ROOT / "backups"

# 备份配置
BACKUP_TARGETS = {
    "config": {
        "source": PROJECT_ROOT / "config",
        "patterns": ["*.yaml", "*.json", "*.py"],
        "exclude": ["*.bak", "*__pycache__*", "archive"],
    },
    "data_meta": {
        "source": PROJECT_ROOT / "data",
        "patterns": ["*.json", "*.jsonl"],
        "exclude": ["*paper_trading.db*", "*idempotency.db*", "cache", "features",
                     "A", "HK", "history", "attribution", "events", "locks", "monitoring"],
    },
    "confidence": {
        "source": PROJECT_ROOT / "confidence_data",
        "patterns": ["*.json"],
    },
    "core": {
        "source": PROJECT_ROOT / "core",
        "patterns": ["*.py"],
        "exclude": ["*__pycache__*"],
    },
    "scripts": {
        "source": PROJECT_ROOT / "scripts",
        "patterns": ["*.py"],
        "exclude": ["*__pycache__*"],
    },
}

RETENTION_DAYS = 30


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def create_backup() -> str:
    """创建完整备份，返回备份目录路径"""
    ts = _timestamp()
    date_tag = datetime.now().strftime("%Y%m%d")
    backup_dir = BACKUP_ROOT / date_tag
    backup_dir.mkdir(parents=True, exist_ok=True)

    total_files = 0
    for name, cfg in BACKUP_TARGETS.items():
        dest = backup_dir / name
        dest.mkdir(exist_ok=True)

        source = cfg["source"]
        if not source.exists():
            continue

        for pattern in cfg["patterns"]:
            for f in source.glob(pattern):
                rel = f.relative_to(source)
                # 排除规则
                skip = False
                for exc in cfg.get("exclude", []):
                    if f.match(exc) or str(rel).startswith(exc.replace("*", "")):
                        skip = True
                        break
                if skip:
                    continue

                dest_file = dest / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(str(f), str(dest_file))
                    total_files += 1
                except Exception as e:
                    print(f"  ⚠️ 备份失败: {f} → {e}")

    # 写入备份元数据
    meta = {
        "timestamp": ts,
        "date": date_tag,
        "total_files": total_files,
        "project_root": str(PROJECT_ROOT),
    }
    with open(backup_dir / "_backup_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"✅ 备份完成: {total_files} 文件 → {backup_dir}")
    return str(backup_dir)


def cleanup_old_backups(days: int = RETENTION_DAYS):
    """清理过期备份"""
    if not BACKUP_ROOT.exists():
        return

    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for d in sorted(BACKUP_ROOT.iterdir()):
        if not d.is_dir():
            continue
        try:
            date = datetime.strptime(d.name, "%Y%m%d")
            if date < cutoff:
                shutil.rmtree(d)
                removed += 1
                print(f"  🗑️ 清理过期备份: {d.name}")
        except ValueError:
            continue  # 非日期目录，跳过

    if removed:
        print(f"✅ 清理完成: {removed} 个过期备份")
    else:
        print(f"ℹ️ 无过期备份 (保留{RETENTION_DAYS}天)")


def list_backups():
    """列出所有备份"""
    if not BACKUP_ROOT.exists():
        print("📭 无备份记录")
        return

    dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir() and (d / "_backup_meta.json").exists()],
                  reverse=True)
    for d in dirs[:10]:
        try:
            with open(d / "_backup_meta.json") as f:
                meta = json.load(f)
            print(f"  {d.name}: {meta.get('total_files', '?')} 文件 @ {meta.get('timestamp', '?')}")
        except Exception:
            print(f"  {d.name}: 元数据损坏")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DSL自动化备份系统")
    parser.add_argument("--cleanup", action="store_true", help="清理过期备份")
    parser.add_argument("--list", action="store_true", help="列出备份记录")
    args = parser.parse_args()

    if args.list:
        list_backups()
    elif args.cleanup:
        cleanup_old_backups()
    else:
        create_backup()
        cleanup_old_backups()  # 备份后自动清理
