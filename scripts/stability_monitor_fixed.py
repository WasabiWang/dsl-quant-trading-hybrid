#!/usr/bin/env python3
"""
DSL稳定性监控脚本 - 修复版

使用简化稳定性系统
"""

import sys
import os
import json
import argparse
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from core.stability_simple import stability_system
    STABILITY_AVAILABLE = True
except ImportError:
    STABILITY_AVAILABLE = False
    print("⚠️ 稳定性系统未安装")

def show_status():
    """显示系统状态"""
    if not STABILITY_AVAILABLE:
        print("❌ 稳定性系统不可用")
        return
    
    status = stability_system.get_status()
    
    print("📊 DSL系统稳定性状态")
    print("=" * 60)
    print(f"更新时间: {status['timestamp']}")
    print(f"健康状态: {status['health']}")
    
    # 显示熔断器状态
    print("\n🔧 熔断器状态:")
    for name, cb_status in status['circuit_breakers'].items():
        state = cb_status['state']
        if state == 'CLOSED':
            state_emoji = "🟢"
        elif state == 'OPEN':
            state_emoji = "🔴"
        else:
            state_emoji = "🟡"
        
        failures = cb_status.get('failures', 0)
        print(f"  {state_emoji} {name}: {state} (失败: {failures})")
    
    # 显示错误统计
    error_count = status.get('error_count_last_hour', 0)
    print(f"\n📈 错误统计: {error_count}次/小时")

def show_detailed_status():
    """显示详细状态"""
    if not STABILITY_AVAILABLE:
        print("❌ 稳定性系统不可用")
        return
    
    status = stability_system.get_status()
    print(json.dumps(status, indent=2, ensure_ascii=False))

def reset_circuit(module: str):
    """重置熔断器"""
    if not STABILITY_AVAILABLE:
        print("❌ 稳定性系统不可用")
        return
    
    print(f"🔄 重置熔断器: {module}")
    stability_system.reset_circuit_breaker(module)
    
    # 验证重置
    status = stability_system.get_status()
    cb_status = status['circuit_breakers'].get(module, {})
    print(f"✅ 重置完成，当前状态: {cb_status.get('state', 'unknown')}")

def test_stability():
    """测试稳定性功能"""
    if not STABILITY_AVAILABLE:
        print("❌ 稳定性系统不可用")
        return
    
    print("🧪 测试稳定性功能...")
    
    # 测试数据获取
    try:
        from dsl_data_sdk import get_price
        result = get_price("000001.SZ")
        print(f"✅ 数据获取测试成功: {result.get('name')} - {result.get('price')}")
        print(f"   数据源: {result.get('source')}")
    except Exception as e:
        print(f"❌ 数据获取测试失败: {e}")
    
    # 显示当前状态
    print("\n" + "=" * 60)
    show_status()

def check_system_health():
    """检查系统健康"""
    if not STABILITY_AVAILABLE:
        print("❌ 稳定性系统不可用")
        return
    
    print("🏥 系统健康检查...")
    
    status = stability_system.get_status()
    health_status = status['health']
    
    if health_status == 'green':
        print("✅ 系统健康状态: 🟢 GREEN - 系统运行正常")
    elif health_status == 'yellow':
        print("⚠️ 系统健康状态: 🟡 YELLOW - 系统有警告")
    elif health_status == 'red':
        print("❌ 系统健康状态: 🔴 RED - 系统故障")
    else:
        print(f"❓ 系统健康状态: {health_status}")
    
    # 检查熔断器
    open_circuits = 0
    for name, cb_status in status['circuit_breakers'].items():
        if cb_status['state'] == 'OPEN':
            open_circuits += 1
            print(f"  ❌ 熔断器 {name} 处于OPEN状态")
    
    if open_circuits == 0:
        print("✅ 所有熔断器状态正常")
    
    # 检查错误频率
    error_count = status.get('error_count_last_hour', 0)
    if error_count > 10:
        print(f"⚠️ 错误频率较高: {error_count}次/小时")
    else:
        print(f"✅ 错误频率正常: {error_count}次/小时")
    
    # 建议
    if health_status == 'red' or open_circuits > 0:
        print("\n💡 建议:")
        if open_circuits > 0:
            print("  - 使用 'python3 scripts/stability_monitor_fixed.py reset <模块名>' 重置熔断器")
        print("  - 检查系统日志: tail -f dsl_errors.log")
        print("  - 查看详细状态: python3 scripts/stability_monitor_fixed.py status")

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='DSL稳定性监控工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')
    
    # status命令
    status_parser = subparsers.add_parser('status', help='显示系统状态')
    status_parser.add_argument('--detailed', action='store_true', help='显示详细状态')
    
    # reset命令
    reset_parser = subparsers.add_parser('reset', help='重置熔断器')
    reset_parser.add_argument('module', help='模块名称 (data_fetch, network)')
    
    # test命令
    subparsers.add_parser('test', help='测试稳定性功能')
    
    # health命令
    subparsers.add_parser('health', help='检查系统健康')
    
    # 默认显示状态
    parser.set_defaults(command='status')
    
    args = parser.parse_args()
    
    if args.command == 'status':
        if args.detailed:
            show_detailed_status()
        else:
            show_status()
    elif args.command == 'reset':
        reset_circuit(args.module)
    elif args.command == 'test':
        test_stability()
    elif args.command == 'health':
        check_system_health()
    else:
        show_status()

if __name__ == "__main__":
    main()