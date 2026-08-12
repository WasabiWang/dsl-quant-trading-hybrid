"""
假期日历配置
支持A股、港股、美股市场
v4.5.12 fix: 使用akshare动态获取节假日，避免硬编码过期
"""
from datetime import date, datetime, timedelta
import logging

logger = logging.getLogger(__name__)

# 硬编码节假日 fallback（仅在akshare无法获取时使用）
# 2025年完整数据
A_SHARE_HOLIDAYS_2025 = {
    date(2025, 1, 1),    # 元旦
    date(2025, 1, 29),   # 春节(1月28日-2月4日)
    date(2025, 1, 30),
    date(2025, 1, 31),
    date(2025, 2, 1),
    date(2025, 2, 2),
    date(2025, 2, 3),
    date(2025, 2, 4),
    date(2025, 4, 4),    # 清明节
    date(2025, 4, 5),
    date(2025, 4, 6),
    date(2025, 5, 1),    # 劳动节(5月1日-5日)
    date(2025, 5, 2),
    date(2025, 5, 3),
    date(2025, 5, 4),
    date(2025, 5, 5),
    date(2025, 5, 31),   # 端午节(5月31日-6月2日)
    date(2025, 6, 1),
    date(2025, 6, 2),
    date(2025, 10, 1),   # 国庆节+中秋节(10月1日-8日)
    date(2025, 10, 2),
    date(2025, 10, 3),
    date(2025, 10, 4),
    date(2025, 10, 5),
    date(2025, 10, 6),
    date(2025, 10, 7),
    date(2025, 10, 8),
}
# 2026年完整数据
A_SHARE_HOLIDAYS_2026 = {
    date(2026, 1, 1),    # 元旦
    date(2026, 1, 2),    # 元旦假期
    date(2026, 2, 16),   # 春节(2月16日-20日, 23日调休)
    date(2026, 2, 17),
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 2, 20),
    date(2026, 2, 23),   # 春节调休
    date(2026, 4, 6),    # 清明节
    date(2026, 5, 1),    # 劳动节
    date(2026, 5, 4),
    date(2026, 5, 5),
    date(2026, 6, 19),   # 端午节(6月19日)
    date(2026, 9, 25),   # 中秋节(9月25日)
    date(2026, 10, 1),   # 国庆节(10月1日-2日, 5日-7日)
    date(2026, 10, 2),
    date(2026, 10, 5),
    date(2026, 10, 6),
    date(2026, 10, 7),
}
# 2027年预计节假日（参考国务院往年模式，akshare获取失败时使用）
A_SHARE_HOLIDAYS_2027 = {
    date(2027, 1, 1),    # 元旦
    date(2027, 2, 6),    # 春节（预计2月6日-12日）
    date(2027, 2, 7),
    date(2027, 2, 8),
    date(2027, 2, 9),
    date(2027, 2, 10),
    date(2027, 2, 11),
    date(2027, 2, 12),
    date(2027, 4, 3),    # 清明节（预计4月3日-5日）
    date(2027, 4, 4),
    date(2027, 4, 5),
    date(2027, 5, 1),    # 劳动节
    date(2027, 5, 2),
    date(2027, 5, 3),
    date(2027, 5, 4),
    date(2027, 5, 5),
    date(2027, 6, 12),   # 端午节（预计6月12日-14日）
    date(2027, 6, 13),
    date(2027, 6, 14),
    date(2027, 9, 15),   # 中秋节（预计9月15日-17日）
    date(2027, 9, 16),
    date(2027, 9, 17),
    date(2027, 10, 1),   # 国庆节
    date(2027, 10, 2),
    date(2027, 10, 3),
    date(2027, 10, 4),
    date(2027, 10, 5),
    date(2027, 10, 6),
    date(2027, 10, 7),
}
# 港股节假日（2026年）
HK_HOLIDAYS = {
    date(2026, 1, 1),    # 元旦
    date(2026, 2, 17),   # 春节
    date(2026, 2, 18),
    date(2026, 2, 19),
    date(2026, 4, 4),    # 清明节
    date(2026, 4, 18),   # 耶稣受难日
    date(2026, 4, 21),   # 复活节星期一
    date(2026, 5, 1),    # 劳动节
    date(2026, 6, 2),    # 端午节
    date(2026, 7, 1),    # 香港特别行政区成立纪念日
    date(2026, 9, 21),   # 中秋节
    date(2026, 10, 1),   # 国庆节
    date(2026, 12, 25),  # 圣诞节
    date(2026, 12, 26),  # 圣诞节后第一个周日
}
# 美股节假日（2026年）
US_HOLIDAYS = {
    date(2026, 1, 1),    # 元旦
    date(2026, 1, 20),   # 马丁路德金纪念日
    date(2026, 2, 17),   # 总统日
    date(2026, 4, 18),   # 耶稣受难日
    date(2026, 5, 26),   # 阵亡将士纪念日
    date(2026, 7, 4),    # 独立日
    date(2026, 9, 1),    # 劳动节
    date(2026, 11, 27),  # 感恩节
    date(2026, 12, 25),  # 圣诞节
}

# 按年份组织A股节假日，方便动态扩展
_A_SHARE_HOLIDAYS_BY_YEAR = {
    2025: A_SHARE_HOLIDAYS_2025,
    2026: A_SHARE_HOLIDAYS_2026,
    2027: A_SHARE_HOLIDAYS_2027,
}

# 向后兼容: 合并所有年份供旧代码调用
A_SHARE_HOLIDAYS = A_SHARE_HOLIDAYS_2025 | A_SHARE_HOLIDAYS_2026 | A_SHARE_HOLIDAYS_2027


def _load_holidays_from_akshare(year: int):
    """从akshare动态获取A股交易日历
    v4.6.x: akshare 1.18+ 的 tool_trade_date_hist_sina() 只返回 trade_date 列(均为交易日)
    holidays = 该年所有工作日 - 交易日
    """
    try:
        import akshare as ak
        import pandas as pd
        cal = ak.tool_trade_date_hist_sina()
        if 'is_open' in cal.columns:
            # 旧版API: 含 is_open 列
            holidays = set()
            for _, row in cal.iterrows():
                trade_date = row['trade_date']
                if hasattr(trade_date, 'year') and trade_date.year == year:
                    if not row['is_open']:
                        dt = trade_date.date() if hasattr(trade_date, 'date') else trade_date
                        holidays.add(dt)
            if holidays:
                return holidays
            return None
        else:
            # 新版API: 只有 trade_date 列(均为交易日)
            # 节假日 = 该年所有工作日 - 交易日
            trade_dates = set()
            for _, row in cal.iterrows():
                td = row['trade_date']
                if hasattr(td, 'year') and td.year == year:
                    dt = td.date() if hasattr(td, 'date') else td
                    trade_dates.add(dt)
            if not trade_dates:
                return None
            # 生成该年所有工作日
            from datetime import date, timedelta
            all_weekdays = set()
            start = date(year, 1, 1)
            end = date(year, 12, 31)
            d = start
            while d <= end:
                if d.weekday() < 5:  # 周一到周五
                    all_weekdays.add(d)
                d += timedelta(days=1)
            holidays = all_weekdays - trade_dates
            logger.info(f"akshare动态获取{year}年节假日: {len(holidays)}天(交易日{len(trade_dates)}天)")
            return holidays
    except Exception as e:
        logger.warning(f"akshare动态获取节假日失败({year}): {e}")
        return None


def get_holidays_for_year(year: int) -> set:
    """获取指定年份的A股节假日
    v4.6.7 fix: 硬编码优先→未知年份才调akshare。已有数据的年份(2025-2027)无需网络请求，
    避免akshare MiniRacer C扩展在macOS上的兼容性问题。
    """
    # 1) 已有硬编码数据 → 直接返回（已过去或已公布的年份，最可靠）
    hardcoded = _A_SHARE_HOLIDAYS_BY_YEAR.get(year)
    if hardcoded is not None:
        return hardcoded
    # 2) 未知年份 → 尝试akshare动态获取（如2028+）
    akshare_data = _load_holidays_from_akshare(year)
    if akshare_data is not None:
        return akshare_data
    # 3) 都不可用 → 仅依赖周末休市
    logger.warning(f"无节假日数据({year})，仅依赖周末休市检查")
    return set()


# 缓存的当前年份节假日
_CACHED_HOLIDAYS = {}


def _get_holidays(check_date: date) -> set:
    """获取指定日期所属年份的节假日（带缓存）"""
    year = check_date.year
    if year not in _CACHED_HOLIDAYS:
        _CACHED_HOLIDAYS[year] = get_holidays_for_year(year)
    return _CACHED_HOLIDAYS[year]


def is_trading_day(check_date: date = None, market: str = "A_SHARE") -> bool:
    """
    检查指定日期是否为交易日
    :param check_date: 要检查的日期，默认今天
    :param market: 市场类型：A_SHARE / HK / US
    :return: 是否为交易日
    """
    if check_date is None:
        check_date = date.today()
    # 周末休市 (A股周六日永远不开盘，即使调休上班)
    if check_date.weekday() >= 5:  # 5=周六，6=周日
        return False
    # 节假日休市
    if market == "A_SHARE":
        return check_date not in _get_holidays(check_date)
    elif market == "HK":
        return check_date not in HK_HOLIDAYS
    elif market == "US":
        return check_date not in US_HOLIDAYS
    else:
        raise ValueError(f"不支持的市场类型：{market}")


def get_next_trading_day(market: str = "A_SHARE", current_date: date = None) -> date:
    """获取下一个交易日"""
    if current_date is None:
        current_date = date.today()
    next_day = current_date + timedelta(days=1)
    while not is_trading_day(check_date=next_day, market=market):
        next_day += timedelta(days=1)
    return next_day
