#!/usr/bin/env python3
"""
飞书通知工具 — 被 backup.sh 等脚本调用发送系统状态通知
用法: python3 feishu_notify.py --title "标题" --content "内容" --level info
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from monitoring.feishu_alert import AlertManager, AlertLevel

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", required=True)
    parser.add_argument("--content", required=True)
    parser.add_argument("--level", default="info", choices=["info", "warning", "error", "critical"])
    args = parser.parse_args()

    level_map = {"info": AlertLevel.INFO, "warning": AlertLevel.WARNING,
                 "error": AlertLevel.ERROR, "critical": AlertLevel.CRITICAL}
    mgr = AlertManager()
    mgr.create_alert(
        level=level_map.get(args.level, AlertLevel.INFO),
        alert_type="info",
        title=args.title, message=args.content,
        module="backup", send_to_feishu=True
    )
    print(f"✅ 飞书通知已发送: [{args.level}] {args.title}")

if __name__ == "__main__":
    main()
