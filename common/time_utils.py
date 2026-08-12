#!/usr/bin/env python3
"""
时间处理工具模块
"""
import time
from datetime import datetime, timedelta
from typing import Optional

def get_current_timestamp() -> int:
    """获取当前时间戳（毫秒）"""
    return int(time.time() * 1000)

def timestamp_to_date(timestamp: int) -> str:
    """时间戳转日期字符串 YYYY-MM-DD"""
    return datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d')

def timestamp_to_datetime(timestamp: int) -> str:
    """时间戳转时间字符串 YYYY-MM-DD HH:MM:SS"""
    return datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S')

def date_to_timestamp(date_str: str) -> int:
    """日期字符串转时间戳"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return int(dt.timestamp() * 1000)

def is_trading_day(date_str: str, market: str = "CN") -> bool:
    """判断是否为交易日"""
    # 简单实现：周末不是交易日
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    if dt.weekday() >= 5:  # 周六、周日
        return False
    # 中国节假日简化判断
    holidays = ['2026-01-01', '2026-02-10', '2026-02-11', '2026-02-12', 
                '2026-04-04', '2026-04-05', '2026-04-06']
    if date_str in holidays:
        return False
    return True

def get_next_trading_day(date_str: str, market: str = "CN") -> str:
    """获取下一个交易日"""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    for _ in range(10):
        dt += timedelta(days=1)
        if is_trading_day(dt.strftime('%Y-%m-%d'), market):
            return dt.strftime('%Y-%m-%d')
    return date_str

def get_market_open_time(timestamp: int, market: str = "CN") -> int:
    """获取当日开盘时间戳"""
    dt = datetime.fromtimestamp(timestamp / 1000)
    if market == "CN":
        hour, minute = 9, 30
    else:
        hour, minute = 9, 30
    dt = dt.replace(hour=hour, minute=minute, second=0)
    return int(dt.timestamp() * 1000)

def get_market_close_time(timestamp: int, market: str = "CN") -> int:
    """获取当日收盘时间戳"""
    dt = datetime.fromtimestamp(timestamp / 1000)
    if market == "CN":
        hour, minute = 15, 0
    else:
        hour, minute = 16, 0
    dt = dt.replace(hour=hour, minute=minute, second=0)
    return int(dt.timestamp() * 1000)

if __name__ == "__main__":
    ts = get_current_timestamp()
    print(f"当前时间戳: {ts}")
    print(f"日期: {timestamp_to_date(ts)}")
    print(f"时间: {timestamp_to_datetime(ts)}")
    print(f"2026-04-14是交易日: {is_trading_day('2026-04-14')}")