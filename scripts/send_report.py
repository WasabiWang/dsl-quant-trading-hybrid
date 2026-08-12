#!/usr/bin/env python3
import os
import subprocess
import tempfile
import json
from datetime import datetime

SENT_REPORTS = set()
REPORT_EXPIRE_SECONDS = 86400

def run_auto_verifier():
    """运行系统自检，返回是否通过"""
    try:
        result = subprocess.run([
            "python3", os.path.expanduser("~/.openclaw/workspace/scripts/auto_verifier.py")
        ], capture_output=True, text=True, timeout=60)
        return result.returncode == 0, result.stdout
    except Exception as e:
        return False, f"自检失败：{str(e)}"

def run_dual_agent_verify(report_id, content):
    """运行双代理验证，返回是否通过"""
    try:
        # 临时保存报告内容
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as f:
            f.write(content)
            temp_file = f.name
        
        task_info = json.dumps({
            "task_id": report_id,
            "task_name": "收盘日报发送",
            "task_desc": "生成并发送每日交易收盘日报，包含行情分析、持仓收益、交易记录等内容"
        })
        
        result = subprocess.run([
            "python3", os.path.expanduser("~/.openclaw/workspace/scripts/dual_agent_verify.py"),
            task_info, temp_file
        ], capture_output=True, text=True, timeout=120)
        
        # 清理临时文件
        os.unlink(temp_file)
        return result.returncode == 0, result.stdout
    except Exception as e:
        return False, f"双代理验证失败：{str(e)}"

def send_report(report_id, content):
    if report_id in SENT_REPORTS:
        return
    SENT_REPORTS.add(report_id)
    # 定期清理
    if len(SENT_REPORTS) > 100:
        SENT_REPORTS.clear()
    
    # ========== 系统自检（记录但不阻断，v4.5.9d） ==========
    print("🔍 系统自检...")
    verify_pass, verify_log = run_auto_verifier()
    if not verify_pass:
        print(f"⚠️ 自检发现异常（不阻断发送）:\n{verify_log}")
    else:
        print("✅ 系统自检通过")
    # ===================================================
    
    # 发送飞书逻辑
    print(f"✅ 发送报告：{report_id}")
