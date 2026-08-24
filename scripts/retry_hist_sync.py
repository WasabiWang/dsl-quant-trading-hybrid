#!/usr/bin/env python3
"""历史数据同步重试器 — 先探测数据源是否已发布最新交易日数据, 有则全量同步, 无则跳过
用途: baostock 数据发布滞后(15:50 cron 时常拉不到当日数据), 晚间重试用
"""
import sys, os
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import socket
from datetime import datetime

TARGET_DATE = datetime.now().strftime("%Y-%m-%d")
PROBE_CODE = "sh.600000"  # 浦发银行, 大盘蓝筹, 数据最稳定

def data_available() -> bool:
    """探测 baostock 是否已发布 TARGET_DATE 数据"""
    socket.setdefaulttimeout(20)
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        print(f"❌ baostock 登录失败: {lg.error_msg}")
        return False
    try:
        rs = bs.query_history_k_data_plus(
            PROBE_CODE, "date", start_date=TARGET_DATE, end_date=TARGET_DATE,
            frequency="d", adjustflag="2",
        )
        has = False
        while (rs.error_code == "0") & rs.next():
            has = True
            break
        return has
    finally:
        try:
            bs.logout()
        except Exception:
            pass

if __name__ == "__main__":
    if datetime.now().weekday() >= 5:
        print("非交易日, 跳过")
        sys.exit(0)
    print(f"🔍 探测 baostock 是否已发布 {TARGET_DATE} 数据...")
    if not data_available():
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
    from data_db.sync import sync_daily, verify_integrity
    result = sync_daily()
    print(f"同步完成: 新增{result['ok']} / 跳过{result['skip']} / 失败{result['error']}")
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
