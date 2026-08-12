#!/usr/bin/env python3
"""
备份历史数据到 OneDrive
用法: python3 scripts/backup_hist_to_onedrive.py [--compress]
"""
import os, sys, shutil, subprocess, tarfile, time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from data_db.config import DATA_ROOT, ONEDRIVE_BACKUP_DIR

def backup(compress=True):
    today = datetime.now().strftime("%Y%m%d_%H%M")
    src = str(DATA_ROOT)
    dst = str(ONEDRIVE_BACKUP_DIR)
    os.makedirs(dst, exist_ok=True)

    if compress:
        # tar.zst 压缩
        archive_name = f"dsl_hist_data_{today}.tar.zst"
        archive_path = os.path.join(dst, archive_name)
        print(f"📦 压缩中: {src} → {archive_path}")

        # 用 tar + zstd (比 gzip 快 3x, 小 30%)
        cmd = f"cd '{os.path.dirname(src)}' && tar --zstd -cf '{archive_path}' '{os.path.basename(src)}'"
        ret = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if ret.returncode != 0:
            print(f"  ❌ 压缩失败: {ret.stderr}")
            return False

        size_mb = os.path.getsize(archive_path) / 1024 / 1024
        print(f"  ✅ 备份完成: {archive_name} ({size_mb:.1f} MB)")
    else:
        # 直接拷贝目录（OneDrive 自动同步）
        dst_dir = os.path.join(dst, f"dsl_hist_data_{today}")
        print(f"📁 拷贝中: {src} → {dst_dir}")
        shutil.copytree(src, dst_dir, dirs_exist_ok=True)
        print(f"  ✅ 拷贝完成")

    return True

def cleanup(max_backups=30):
    """保留最近 N 个备份"""
    dst = str(ONEDRIVE_BACKUP_DIR)
    files = sorted([f for f in os.listdir(dst)
                    if f.startswith("dsl_hist_data_") and (f.endswith(".tar.zst") or os.path.isdir(os.path.join(dst, f)))],
                   reverse=True)
    if len(files) > max_backups:
        for f in files[max_backups:]:
            path = os.path.join(dst, f)
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            print(f"  🗑️ 清理旧备份: {f}")

if __name__ == "__main__":
    compress = "--no-compress" not in sys.argv
    ok = backup(compress=compress)
    if ok:
        cleanup()
    sys.exit(0 if ok else 1)
