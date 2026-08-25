#!/usr/bin/env python3
"""
增量同步入口脚本（供 cron 调用）
跳过周末，默认同步全市场
带互斥锁: 防止并发实例同时写 manifest/parquet 造成数据竞争
"""
import sys, os
from pathlib import Path

# 将项目根目录加入 sys.path（cron 调用可能不设置 PYTHONPATH）
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from datetime import datetime

# 周末跳过
if datetime.now().weekday() >= 5:
    print("非交易日，跳过")
    sys.exit(0)

# ---- 互斥锁: 防止多个实例并发写数据 ----
import fcntl
LOCK_FILE = project_root / "data_db" / ".daily_hist_sync.lock"
lock_fd = open(LOCK_FILE, "w")
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print(f"⚠️ 另一个同步实例正在运行 (锁文件: {LOCK_FILE})，本次跳过，避免并发写冲突")
    sys.exit(0)

from data_db.sync import sync_daily, verify_integrity, probe_data_available

# baostock 数据发布滞后: 15:50 时常尚未发布当日K线 (2026-08-25 实测 16:05 仍缺)。
# 先探测, 未发布则直接退出且不更新 last_incremental_sync, 避免:
#   1) 全市场~5500只无谓查询浪费1小时+
#   2) 误标"今日已同步"导致晚间 retry_hist_sync 补数被跳过
today = datetime.now().strftime("%Y-%m-%d")
if not probe_data_available(today):
    print(f"⏭️  {today} 日线数据尚未发布(baostock 滞后), 本次跳过且不更新同步状态; "
          f"晚间 retry_hist_sync.py 将在数据发布后补数")
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    sys.exit(0)

codes = sys.argv[1:] if len(sys.argv) > 1 else None
result = sync_daily(codes=codes)
print(f"同步完成: 新增{result['ok']} / 跳过{result['skip']} / 失败{result['error']}")

# 每5天做一次完整性校验
state_path = __import__("data_db.config", fromlist=["SYNC_DIR"]).SYNC_DIR / "sync_state.json"
if state_path.exists():
    import json
    with open(state_path) as f:
        state = json.load(f)
    last_full_str = state.get("last_full_sync", "1970-01-01")
    last_full = datetime.strptime(last_full_str[:10], "%Y-%m-%d") if isinstance(last_full_str, str) else datetime(1970, 1, 1)
    if (datetime.now() - last_full).days >= 5:
        print("触发完整性校验...")
        v = verify_integrity()
        print(f"校验: {v['ok']}通过 / {v['failed']}失败")

# 释放锁
fcntl.flock(lock_fd, fcntl.LOCK_UN)
lock_fd.close()
