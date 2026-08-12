import subprocess
import time
import sys
import os
from datetime import datetime

def run_sector_scan():
    """执行全市场高因子板块挖掘 (v3.0.7 新增)"""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🌐 启动全市场因子挖掘...")
    base_path = os.path.dirname(os.path.abspath(__file__))
    scanner_path = os.path.join(base_path, "sector_scanner.py")
    try:
        subprocess.run([sys.executable, scanner_path], check=True)
    except Exception as e:
        print(f"⚠️ 板块挖掘失败: {e}")

def run_market_scan(market, script_path):
    """执行指定市场的扫描任务"""
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🚀 开始 {market} 市场扫描...")
    try:
        # 使用 subprocess 调用实际的监控脚本
        result = subprocess.run(
            [sys.executable, script_path, "--notify"],
            capture_output=True, text=True, timeout=300
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(f"⚠️ {market} 警告: {result.stderr}")
        print(f"✅ {market} 扫描完成。")
    except Exception as e:
        print(f"❌ {market} 扫描失败: {e}")

def main():
    print("🌐 DSL v3.0.7 全局监控中心启动 | Global & Factor Scanning")
    
    # 1. 盘前/盘中优先执行高因子板块挖掘
    run_sector_scan()

    # 定义监控任务 (路径需根据实际情况调整)
    base_path = os.path.dirname(os.path.abspath(__file__))
    tasks = [
        {"name": "A股", "script": os.path.join(base_path, "realtime_monitor.py")},
        {"name": "港股", "script": os.path.join(base_path, "hk_realtime_monitor.py")}
    ]

    for i, task in enumerate(tasks):
        run_market_scan(task["name"], task["script"])
        
        # 错峰执行：在两个任务之间增加 120 秒的强制休眠
        # 个人投资者接口稳定性优先，留足 2 分钟给接口“回血”
        if i < len(tasks) - 1:
            stagger_time = 120 
            print(f"⏳ 进入接口冷却期 ({stagger_time}s)，确保下一次请求 100% 成功...")
            time.sleep(stagger_time)

    print("\n🏁 全局监控本轮次执行完毕。")

if __name__ == "__main__":
    main()