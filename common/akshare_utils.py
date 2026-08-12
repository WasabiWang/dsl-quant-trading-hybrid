#!/usr/bin/env python3
"""
DSL v4.5.3d akshare 统一降级层
为所有脚本提供带重试+超时的 akshare 数据获取函数。

使用示例:
  from common.akshare_utils import safe_stock_zh_a_hist, safe_index_us
  
  df = safe_stock_zh_a_hist(symbol="000001", period="daily", start_date="20260401", end_date="20260501")
  df = safe_index_us(symbol=".INX")
  df = safe_realtime(symbol="000001")

设计原则:
  - 3次重试, 指数退避 (1s, 2s, 4s)
  - 10秒超时兜底
  - 所有异常不抛出, 返回 None
  - 不影响原有调用逻辑
"""
import time, signal, sys
from functools import wraps

MAX_RETRIES = 3
TIMEOUT_SECONDS = 60  # v4.6.4: 从10s增加到60s, 新浪数据源下载需要较长时间


def _timeout_handler(signum, frame):
    raise TimeoutError("akshare API超时")


def _log_retry(message: str):
    print(message, file=sys.stderr)


def with_retry(func):
    """装饰器: 为akshare函数提供重试+超时"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # 设置超时
                signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(TIMEOUT_SECONDS)
                
                result = func(*args, **kwargs)
                
                signal.alarm(0)  # 取消超时
                return result
            
            except TimeoutError as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    wait = 2 ** (attempt - 1)
                    _log_retry(f"  ⏱️ {func.__name__} 超时(第{attempt}次), {wait}s后重试...")
                    time.sleep(wait)
            
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    wait = 2 ** (attempt - 1)
                    _log_retry(f"  ⚠️ {func.__name__} 失败(第{attempt}次): {e}, {wait}s后重试...")
                    time.sleep(wait)
            
            finally:
                signal.alarm(0)  # 确保超时取消
        
        if last_error:
            _log_retry(f"  ❌ {func.__name__} 全部重试失败: {last_error}")
        return None
    
    return wrapper


# ═══════════════ akshare 包装函数 ═══════════════

@with_retry
def safe_stock_zh_a_hist(symbol: str, period: str = "daily", 
                          start_date: str = None, end_date: str = None,
                          adjust: str = "qfq"):
    """获取A股历史行情 — v4.6.4: 东财/新浪优先，BaoStock兜底"""
    import akshare as ak
    # 优先东财(历史数据源, push2his可能仍可用)
    try:
        return ak.stock_zh_a_hist(
            symbol=symbol, period=period,
            start_date=start_date, end_date=end_date,
            adjust=adjust
        )
    except Exception as e:
        _log_retry(f"  ⚠️ stock_zh_a_hist 东财失败: {e}, 尝试BaoStock...")
    
    # 降级: BaoStock
    try:
        return _safe_baostock_kline(symbol, period, start_date, end_date, adjust)
    except Exception as e:
        _log_retry(f"  ⚠️ BaoStock K线失败: {e}")
    
    return None


def _safe_baostock_kline(symbol: str, period: str = "daily",
                          start_date: str = None, end_date: str = None,
                          adjust: str = "qfq"):
    """使用BaoStock获取K线数据"""
    import baostock as bs
    import pandas as pd
    
    # 转换代码格式: 000001 -> sz.000001 (深) 或 sh.000001 (沪)
    code_num = symbol.zfill(6)
    if code_num.startswith(('6', '9')):
        bs_code = f"sh.{code_num}"
    elif code_num.startswith(('0', '3', '2')):
        bs_code = f"sz.{code_num}"
    else:
        bs_code = f"sz.{code_num}"
    
    # 频率转换
    freq_map = {"daily": "d", "weekly": "w", "monthly": "m"}
    freq = freq_map.get(period, "d")
    
    # 复权转换
    adj_map = {"qfq": "2", "hfq": "1", "": "3"}
    adj_flag = adj_map.get(adjust, "2")
    
    lg = bs.login()
    if lg is None or (hasattr(lg, 'error_code') and lg.error_code != '0'):
        _log_retry(f"  ⚠️ BaoStock登录失败: {lg}")
        return None
    try:
        fields = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST"
        rs = bs.query_history_k_data_plus(bs_code, fields,
                                           start_date=start_date, end_date=end_date,
                                           frequency=freq, adjustflag=adj_flag)
        if rs is None:
            return None
        data_list = []
        while (rs.error_code == '0') and rs.next():
            data_list.append(rs.get_row_data())
        
        if not data_list:
            return None
        
        df = pd.DataFrame(data_list, columns=fields.split(','))
        # 转换数值列
        for col in ['open', 'high', 'low', 'close', 'preclose', 'volume', 'amount', 'turn', 'pctChg']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df.rename(columns={
            'date': '日期', 'code': '股票代码', 'open': '开盘', 'high': '最高',
            'low': '最低', 'close': '收盘', 'volume': '成交量', 'amount': '成交额',
            'turn': '换手率', 'pctChg': '涨跌幅'
        }, inplace=True)
        return df
    finally:
        bs.logout()


@with_retry
def safe_stock_zh_a_spot_sina():
    """获取A股实时行情 (新浪数据源) — 东财API封锁后的主数据源"""
    import akshare as ak
    return ak.stock_zh_a_spot()


@with_retry
def safe_stock_zh_a_spot_em():
    """获取A股实时行情 — v4.6.4: 东财push2 API已封锁，统一切新浪"""
    import akshare as ak
    # 新浪数据源 (stock_zh_a_spot) — 当前唯一可用
    try:
        return ak.stock_zh_a_spot()
    except Exception:
        pass
    # 降级: 东方财富 (push2 API 2026-06已封锁，大概率不可用)
    try:
        return ak.stock_zh_a_spot_em()
    except Exception:
        pass
    return None


@with_retry
def safe_index_us_stock_sina(symbol: str = ".INX"):
    """获取美股指数"""
    import akshare as ak
    return ak.index_us_stock_sina(symbol=symbol)


@with_retry
def safe_stock_zh_index_daily_em(symbol: str = "sh000300"):
    """获取A股指数行情"""
    import akshare as ak
    return ak.stock_zh_index_daily_em(symbol=symbol)


@with_retry
def safe_index_global_spot_em():
    """获取全球指数实时行情 (东方财富), 包含N225/KS11/HSI/沪深300/上证指数等56个品种"""
    import akshare as ak
    return ak.index_global_spot_em()


@with_retry
def safe_realtime(symbol: str):
    """获取单只A股实时行情 — v4.6.4: 复用新浪全量数据"""
    import akshare as ak
    # 新浪数据源代码格式: sh600519, sz000001 等
    symbol_clean = symbol.zfill(6)
    
    df = safe_stock_zh_a_spot_em()
    if df is not None and not df.empty:
        code_col = None
        for c in ['代码', 'code']:
            if c in df.columns:
                code_col = c
                break
        if code_col:
            codes = df[code_col].astype(str)
            # 匹配多种代码格式: 600519, sh600519, SH600519
            row = df[(codes == symbol_clean) | 
                     (codes.str.upper() == 'SH' + symbol_clean) |
                     (codes.str.upper() == 'SZ' + symbol_clean) |
                     (codes == 'sh' + symbol_clean) |
                     (codes == 'sz' + symbol_clean)]
            if not row.empty:
                return row.iloc[0].to_dict()
    return None


@with_retry
def safe_fund_etf_hist_em(symbol: str, period: str = "daily",
                           start_date: str = None, end_date: str = None):
    """获取ETF历史行情"""
    import akshare as ak
    return ak.fund_etf_hist_em(
        symbol=symbol, period=period,
        start_date=start_date, end_date=end_date,
        adjust="qfq"
    )


if __name__ == "__main__":
    # 自测
    print("🔍 akshare_utils 自测...")
    
    df = safe_stock_zh_a_hist(symbol="000001", period="daily",
                               start_date="20260425", end_date="20260430")
    if df is not None:
        print(f"  ✅ safe_stock_zh_a_hist: {len(df)} rows")
    else:
        print(f"  ⚠️ safe_stock_zh_a_hist: 无数据")
    
    print("✅ 自测完成")
