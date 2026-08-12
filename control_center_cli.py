#!/usr/bin/env python3
"""
统一管控中心命令行工具
"""
import sys
import json
import argparse
from control_plane.control_center import control_center

def main():
    parser = argparse.ArgumentParser(description="统一管控中心CLI")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")
    
    # 状态查看命令
    status_parser = subparsers.add_parser("status", help="查看系统状态")
    status_parser.add_argument("--json", action="store_true", help="输出JSON格式")
    
    # 任务管理命令
    task_parser = subparsers.add_parser("task", help="任务管理")
    task_parser.add_argument("action", choices=["list", "enable", "disable", "run", "status"], help="操作类型")
    task_parser.add_argument("job_id", nargs="?", help="任务ID")
    
    # 消息发送命令
    msg_parser = subparsers.add_parser("send", help="发送消息")
    msg_parser.add_argument("content", help="消息内容")
    msg_parser.add_argument("--title", help="消息标题")
    msg_parser.add_argument("--level", choices=["INFO", "WARNING", "ERROR", "CRITICAL"], default="INFO", help="告警级别")
    
    # 优化建议命令
    opt_parser = subparsers.add_parser("optimizations", help="查看系统优化建议")
    opt_parser.add_argument("--json", action="store_true", help="输出JSON格式")
    
    args = parser.parse_args()
    
    if args.command == "status":
        status = control_center.get_system_status()
        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        else:
            print("\n🚀 统一管控中心状态")
            print("=" * 50)
            cc = status["control_center"]
            print(f"状态: {cc['status']} | 版本: {cc['version']} | 运行时间: {cc['uptime_formatted']}")
            
            ts = status["task_scheduler"]
            print(f"\n📊 任务调度状态")
            print(f"总任务数: {ts['total_jobs']} | 启用: {ts['enabled_jobs']} | 禁用: {ts['disabled_jobs']}")
            print(f"最近执行成功: {ts['last_run_success']} | 最近执行失败: {ts['last_run_failed']}")
            
            mg = status["message_gateway"]
            print(f"\n📧 消息网关状态")
            print(f"飞书Token: {mg['feishu_token_status']} | 过期时间: {mg['token_expire_in']}s")
            
            ai = status["auto_iteration"]
            print(f"\n🧠 自动迭代状态")
            print(f"历史交易记录: {ai['recorded_trades']} | 最近优化建议: {ai['recent_suggestions']}")
    
    elif args.command == "task":
        if args.action == "list":
            jobs = control_center.list_all_cron_jobs()
            print("\n📋 定时任务列表")
            print("=" * 80)
            print(f"{'ID':<38} {'名称':<30} {'状态':<8} {'最近执行状态':<10}")
            print("-" * 80)
            for job in jobs:
                status = "✅ 启用" if job.get("enabled") else "❌ 禁用"
                last_status = job.get("state", {}).get("lastRunStatus", "N/A")
                last_status = "✅ 成功" if last_status == "OK" else "❌ 失败" if last_status == "ERROR" else "⚪ 未知"
                print(f"{job.get('id', ''):<38} {job.get('name', ''):<30} {status:<8} {last_status:<10}")
        
        elif args.action == "enable":
            if not args.job_id:
                print("错误: 请提供任务ID")
                sys.exit(1)
            success = control_center.enable_cron_job(args.job_id)
            if success:
                print(f"✅ 任务 {args.job_id} 已启用")
            else:
                print(f"❌ 启用任务 {args.job_id} 失败")
        
        elif args.action == "disable":
            if not args.job_id:
                print("错误: 请提供任务ID")
                sys.exit(1)
            success = control_center.disable_cron_job(args.job_id)
            if success:
                print(f"✅ 任务 {args.job_id} 已禁用")
            else:
                print(f"❌ 禁用任务 {args.job_id} 失败")
        
        elif args.action == "run":
            if not args.job_id:
                print("错误: 请提供任务ID")
                sys.exit(1)
            print(f"🚀 正在执行任务 {args.job_id}...")
            success = control_center.run_cron_job_now(args.job_id)
            if success:
                print(f"✅ 任务 {args.job_id} 执行完成")
            else:
                print(f"❌ 任务 {args.job_id} 执行失败")
        
        elif args.action == "status":
            if not args.job_id:
                print("错误: 请提供任务ID")
                sys.exit(1)
            status = control_center.get_cron_job_status(args.job_id)
            if status:
                print(json.dumps(status, ensure_ascii=False, indent=2))
            else:
                print(f"❌ 未找到任务 {args.job_id}")
    
    elif args.command == "send":
        success = control_center.send_alert(args.level, args.title or "系统通知", args.content)
        if success:
            print(f"✅ 消息发送成功")
        else:
            print(f"❌ 消息发送失败")
    
    elif args.command == "optimizations":
        suggestions = control_center.suggest_optimizations()
        if args.json:
            print(json.dumps(suggestions, ensure_ascii=False, indent=2))
        else:
            print("\n💡 系统优化建议")
            print("=" * 80)
            if not suggestions:
                print("✅ 系统运行良好，无优化建议")
            else:
                for i, sug in enumerate(suggestions, 1):
                    priority_emoji = "🔴" if sug["priority"] == "HIGH" else "🟡" if sug["priority"] == "MEDIUM" else "🟢"
                    print(f"\n{i}. {priority_emoji} [{sug['priority']}] {sug['title']}")
                    print(f"   来源: {sug['source']}")
                    print(f"   描述: {sug['description']}")
    
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
