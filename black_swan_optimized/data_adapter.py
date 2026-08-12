#!/usr/bin/env python3
"""
LPPL 数据适配器 - 获取各类标的的价格数据
支持: A股指数, 美股个股/指数, 行业板块
"""
import numpy as np
from datetime import datetime, timedelta
from typing import Tuple, List, Optional
import requests
import json
import warnings
warnings.filterwarnings('ignore')

# 标的配置
TARGET_CONFIG = {
    # A股主要指数
    "上证指数":   {"code": "sh000001", "type": "sina_index"},
    "科创50":     {"code": "sh000688", "type": "sina_index"},
    "创业板指":   {"code": "sz399006", "type": "sina_index"},
    "沪深300":    {"code": "sh000300", "type": "sina_index"},
    "中证1000":   {"code": "sh000852", "type": "sina_index"},
    "深证成指":   {"code": "sz399001", "type": "sina_index"},

    # 行业板块 (使用指数替代)
    "半导体":     {"code": "semi_composite", "type": "custom_composite"},  # 自定义全市场半导体复合指数
    "人工智能":   {"code": "sh993660", "type": "sina_index"},

    # 美股 (通过 akshare fallback 或 yfinance)
    "NVDA":       {"code": "NVDA", "type": "us_stock"},
    "SOX":        {"code": "^SOX", "type": "us_index"},
    "纳斯达克":   {"code": "^IXIC", "type": "us_index"},
    "S&P500":     {"code": "^GSPC", "type": "us_index"},

    # 港股
    "恒生科技":   {"code": "HSI", "type": "hk_index"},
}

# ============================================================================
# 半导体复合指数成分股
# 覆盖: 深市主板/创业板 + 沪市 + 科创板 (含中芯国际/北方华创等全市场龙头)
# ============================================================================
# 选股标准: A股半导体各细分赛道龙头, 按市值排序
SEMICONDUCTOR_STOCKS = [
    # 科创板 (7只)
    "sh688981",  # 中芯国际 - 晶圆代工龙头
    "sh688012",  # 中微公司 - 刻蚀/MOCVD设备
    "sh688041",  # 海光信息 - CPU/GPU芯片
    "sh688072",  # 拓荆科技 - CVD设备
    "sh688347",  # 华虹公司 - 晶圆代工
    "sh688126",  # 沪硅产业 - 硅片
    "sh688728",  # 格科微 - CMOS图像传感器

    # 深市 (4只)
    "sz002371",  # 北方华创 - 半导体设备平台
    "sz300661",  # 圣邦股份 - 模拟芯片
    "sz300782",  # 卓胜微 - 射频前端
    "sz300724",  # 捷佳伟创 - 光伏+半导体设备

    # 沪市主板 (4只)
    "sh603501",  # 韦尔股份 - CIS传感器
    "sh600584",  # 长电科技 - 封测龙头
    "sh600703",  # 三安光电 - 化合物半导体
    "sh603986",  # 兆易创新 - MCU/NOR Flash
]


def get_sina_kline(symbol: str, start_date: str, end_date: Optional[str] = None,
                   max_len: int = 1023) -> Tuple[List[float], List[str]]:
    """
    从新浪获取日K线数据
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    headers = {'Referer': 'https://finance.sina.com.cn'}
    url = (f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/'
           f'CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=0&datalen={max_len}')

    try:
        r = requests.get(url, headers=headers, timeout=15)
        data = json.loads(r.text)

        prices = []
        dates = []
        for d in data:
            date_str = d['day']
            if start_date <= date_str <= end_date:
                try:
                    close = float(d['close'])
                    if close > 0:
                        prices.append(close)
                        dates.append(date_str)
                except (ValueError, KeyError):
                    continue

        return prices, dates

    except Exception as e:
        print(f"  ⚠️ 新浪K线获取失败 ({symbol}): {e}")
        return [], []


def get_akshare_us(symbol: str, start_date: str, end_date: Optional[str] = None) -> Tuple[List[float], List[str]]:
    """通过 akshare 获取美股数据 (暂不可用, 备用)"""
    # 由于代理问题, 用 yfinance 作为替代
    try:
        import yfinance as yf
        ticker = yf.Ticker(symbol)
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")

        df = ticker.history(start=start_date, end=end_date)
        if df.empty:
            return [], []

        prices = df['Close'].tolist()
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
        return prices, dates
    except Exception as e:
        print(f"  ⚠️ 美股数据获取失败 ({symbol}): {e}")
        return [], []


def get_semiconductor_composite(start_date: str, end_date: Optional[str] = None) -> Tuple[List[float], List[str]]:
    """
    获取全市场半导体复合指数 (等权)
    替代原有sz399678(深证半导体, 仅深市), 覆盖沪深+科创板所有半导体龙头
    """
    import numpy as np
    from collections import defaultdict

    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    print(f"  📊 构建半导体复合指数: {len(SEMICONDUCTOR_STOCKS)}只成分股")

    # 存储各股票按日期合并的价格
    date_prices = defaultdict(list)
    success_count = 0

    for code in SEMICONDUCTOR_STOCKS:
        prices, dates = get_sina_kline(code, start_date, end_date, max_len=1023)
        if not prices or not dates:
            continue
        success_count += 1
        for i, d in enumerate(dates):
            date_prices[d].append(prices[i])

    if success_count < 5:
        print(f"  ⚠️ 仅获取到{success_count}只成分股数据(<5), 回退至sz399678")
        return get_sina_kline("sz399678", start_date, end_date)

    print(f"  ✅ 成功获取{success_count}只成分股, 共{len(date_prices)}个交易日")

    # 过滤: 只有至少 成功数*50% 的成分股都有数据的日期才保留
    min_stocks = max(5, int(success_count * 0.5))
    sorted_dates = sorted(date_prices.keys())

    composite_prices = []
    composite_dates = []

    for d in sorted_dates:
        prices = date_prices[d]
        if len(prices) >= min_stocks:
            # 等权: 计算相对基准日的归一化因子
            # 用所有股票价格的对数均值作为复合指数
            composite_prices.append(float(np.mean(prices)))
            composite_dates.append(d)

    if composite_prices:
        print(f"  📈 半导体复合指数: {len(composite_prices)}个数据点, "
              f"{composite_dates[0]}→{composite_dates[-1]}, "
              f"均值={np.mean(composite_prices):.2f}")
    else:
        print(f"  ⚠️ 半导体复合指数无数据, 回退sz399678")
        return get_sina_kline("sz399678", start_date, end_date)

    return composite_prices, composite_dates


def get_price_data(symbol_name: str, start_date: str, end_date: Optional[str] = None) -> Tuple[List[float], List[str]]:
    """
    统一数据获取接口

    Args:
        symbol_name: 标的名称 (如 "科创50", "NVDA")
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期, 默认今天

    Returns:
        (prices, dates)
    """
    if symbol_name not in TARGET_CONFIG:
        # 可能是纯代码, 尝试作为A股K线获取
        if symbol_name.startswith('sh') or symbol_name.startswith('sz'):
            return get_sina_kline(symbol_name, start_date, end_date)
        print(f"  ❌ 未知标的: {symbol_name}")
        return [], []

    cfg = TARGET_CONFIG[symbol_name]
    code = cfg['code']
    dtype = cfg['type']

    if dtype == 'sina_index':
        return get_sina_kline(code, start_date, end_date)
    elif dtype == 'custom_composite':
        if code == 'semi_composite':
            return get_semiconductor_composite(start_date, end_date)
        print(f"  ⚠️ 未知自定义复合指数: {code}")
        return [], []
    elif dtype in ('us_stock', 'us_index'):
        return get_akshare_us(code, start_date, end_date)
    elif dtype == 'hk_index':
        # 港股暂未实现, 返回空
        print(f"  ⚠️ 港股数据暂不支持: {symbol_name}")
        return [], []
    else:
        print(f"  ⚠️ 未知类型: {dtype}")
        return [], []


def get_batch_data(symbols: List[str], start_date: str = "2024-01-01",
                   end_date: Optional[str] = None) -> dict:
    """
    批量获取多个标的的价格数据

    Returns:
        {symbol: (prices, dates)} 字典
    """
    results = {}
    for sym in symbols:
        print(f"📊 获取 {sym} 数据...")
        prices, dates = get_price_data(sym, start_date, end_date)
        if prices:
            results[sym] = (np.array(prices), np.array(dates))
            print(f"  ✅ {len(prices)} 个数据点, {dates[0]} → {dates[-1]}")
        else:
            print(f"  ❌ {sym} 无数据")
    return results


# 便捷别名
def get_index_data(index_name: str, start: str = "2023-01-01") -> Tuple[List[float], List[str]]:
    """获取指数数据的便捷函数"""
    return get_price_data(index_name, start)


if __name__ == "__main__":
    # 测试
    for sym in ["科创50", "上证指数", "创业板指", "沪深300"]:
        prices, dates = get_price_data(sym, "2024-01-01")
        if prices:
            print(f"{sym}: {len(prices)}点, {dates[0]}→{dates[-1]}, "
                  f"¥{prices[0]:.2f}→¥{prices[-1]:.2f} "
                  f"(+{(prices[-1]/prices[0]-1)*100:.1f}%)")