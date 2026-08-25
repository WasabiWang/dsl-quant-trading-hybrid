#!/usr/bin/env python3
"""历史数据同步重试器 — 先探测数据源是否已发布最新交易日数据, 有则全量同步, 无则跳过
用途: baostock 数据发布滞后(15:50 cron 时常拉不到当日数据), 晚间重试用
"""
import sys, os
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from datetime import datetime

TARGET_DATE = datetime.now().strftime("%Y-%m-%d")


if __name__ == "__main__":
    if datetime.now().weekday() >= 5:
        print("非交易日, 跳过")
        sys.exit(0)
    print(f"🔍 探测 baostock 是否已发布 {TARGET_DATE} 数据...")
    from data_db.sync import probe_data_available
    if not probe_data_available(TARGET_DATE):
        print(f"⏭️  {TARGET_DATE} 数据尚未发布, 跳过本次同步 (本地数据保持至最近可用交易日)")
        sys.exit(0)
    print(f"✅ {TARGET_DATE} 数据已发布, 开始全量增量同步...")
    # 直接调用同步引擎 (绕过 daily_hist_sync 的周末判断, 复用其互斥锁逻辑)
    import fcntl
    LOCK_FILE = project_root / "data_db" / ".daily_hist_sync.lock"
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️ 另一个同步实例正在运行, 跳过")
        sys.exit(0)
    from data_db.sync import sync_daily, verify_integrity, _load_sync_state, _save_sync_state

    # 关键修复(2026-08-25): 15:50 daily_hist_sync 可能在 baostock 未发布当日数据时
    # 仍将 last_incremental_sync 标记为今天 → 此处 sync_daily() 会被"今日已同步"挡住。
    # 探测已确认今日数据可用, 安全地将标记回退至昨日以放行增量同步。
    from datetime import timedelta as _td
    state = _load_sync_state()
    if state.get("last_incremental_sync") == TARGET_DATE:
        state["last_incremental_sync"] = (datetime.now() - _td(days=1)).strftime("%Y-%m-%d")
        _save_sync_state(state)
        print(f"  ♻️  清除今日({TARGET_DATE})已同步标记 → 回退至昨日, 放行增量同步")

    result = sync_daily()
    print(f"同步完成: 新增{result['ok']} / 跳过{result['skip']} / 失败{result['error']}")
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
