#!/usr/bin/env python3
"""
guardian/cli.py — 防护子系统命令行管理工具

用法:
  python3 -m guardian.cli check          # 运行12 Guards健康检查
  python3 -m guardian.cli snapshot       # 手动打快照
  python3 -m guardian.cli rollback       # 回滚到最近快照 (dry-run)
  python3 -m guardian.cli rollback --execute  # 确认回滚
  python3 -m guardian.cli verify FILE    # 验证单文件完整性
  python3 -m guardian.cli audit          # 审计cron写冲突
  python3 -m guardian.cli info           # 显示子系统状态
"""

import argparse, json, os, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def cmd_check(args):
    """运行健康检查"""
    from guardian import run_health_check
    report = run_health_check(verbose=True)
    print(json.dumps(report, indent=2, ensure_ascii=False))


def cmd_snapshot(args):
    """手动打快照"""
    from guardian import snapshot_before, list_snapshots
    label = args.label or "manual_cli"
    sid = snapshot_before(label=label)
    if sid:
        print(f"✅ 快照已创建: {sid} ({label})")
        snaps = list_snapshots(limit=5)
        print(f"   共{len(snaps)}份快照可用")
    else:
        print("❌ 快照创建失败")
        sys.exit(1)


def cmd_rollback(args):
    """回滚"""
    from guardian import rollback_to, list_snapshots
    
    snaps = list_snapshots(limit=5)
    if not snaps:
        print("❌ 无可用快照")
        sys.exit(1)
    
    print(f"可用快照:")
    for s in snaps[:5]:
        print(f"  {s['id']} | {s.get('label','')} | {s.get('timestamp','')[:19]}")
    
    if not args.execute:
        print("\n🔍 DRY RUN (不会实际回滚):")
        result = rollback_to(latest=True, dry_run=True)
    else:
        print(f"\n⚠️ 确认回滚到最近快照? 输入 'yes' 继续: ", end="")
        if input().strip().lower() != "yes":
            print("已取消")
            sys.exit(0)
        result = rollback_to(latest=True, dry_run=False)
    
    if result["rolled_back"]:
        print(f"✅ 已回滚 {len(result['rolled_back'])} 个文件:")
        for f in result["rolled_back"]:
            print(f"  → {f}")
    if result["errors"]:
        print(f"❌ 错误: {result['errors']}")
    if result["skipped"]:
        print(f"⏭️ 跳过: {result['skipped']}")


def cmd_verify(args):
    """验证文件完整性"""
    from guardian import quick_integrity_check, validate_after_write
    
    path = args.file
    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}")
        sys.exit(1)
    
    # 快速检查
    ok = quick_integrity_check(path)
    if not ok:
        print(f"❌ {path}: 文件损坏或不可解析")
        sys.exit(1)
    
    # 详细验证
    is_ok, issues = validate_after_write(path)
    if is_ok:
        size = os.path.getsize(path)
        print(f"✅ {path}: 完整 ({size:,} bytes)")
    else:
        print(f"⚠️ {path}: 发现{len(issues)}个问题")
        for i in issues:
            print(f"  • {i}")


def cmd_audit(args):
    """审计cron写冲突"""
    try:
        from core.robustness_guard import check_write_conflicts
        result = check_write_conflicts()
        conflicts = result.get("conflicts", [])
        if conflicts:
            print(f"⚠️ 检测到{len(conflicts)}个潜在写冲突:")
            for c in conflicts:
                print(f"  → {c}")
            print(f"\n风险等级: {result.get('risk_level', '?')}")
        else:
            print("✅ 无写冲突")
    except ImportError:
        print("❌ robustness_guard不可用")


def cmd_info(args):
    """显示子系统状态"""
    from guardian import __version__, __build__
    from guardian import list_snapshots
    
    snaps = list_snapshots(limit=3)
    
    print(f"🛡️ Guardian 防护子系统 v{__version__} ({__build__})")
    print(f"   防线: L1(Detector) → L2(Atomic) → L3(Verify) → L4(Snapshot)")
    print(f"   快照: {len(snaps)}份可用")
    
    if snaps:
        latest = snaps[0]
        print(f"   最近: {latest.get('id','?')} | {latest.get('label','')} | {latest.get('timestamp','')[:19]}")


def main():
    parser = argparse.ArgumentParser(description="Guardian 防护子系统CLI")
    sub = parser.add_subparsers(dest="command", help="子命令")
    
    sub.add_parser("check", help="运行12 Guards健康检查")
    
    p = sub.add_parser("snapshot", help="手动打快照")
    p.add_argument("--label", help="快照标签")
    
    p = sub.add_parser("rollback", help="回滚")
    p.add_argument("--execute", action="store_true", help="确认执行(默认dry-run)")
    
    p = sub.add_parser("verify", help="验证文件完整性")
    p.add_argument("file", help="文件路径")
    
    sub.add_parser("audit", help="审计cron写冲突")
    sub.add_parser("info", help="子系统状态")
    
    args = parser.parse_args()
    
    if args.command is None:
        parser.print_help()
        sys.exit(1)
    
    handlers = {
        "check": cmd_check, "snapshot": cmd_snapshot, "rollback": cmd_rollback,
        "verify": cmd_verify, "audit": cmd_audit, "info": cmd_info,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
