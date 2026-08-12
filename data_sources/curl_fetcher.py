"""
Curl 数据获取工具

通过系统 curl 命令绕过 Python 代理限制，直接从东方财富获取真实数据。
"""

import subprocess
import json
import pandas as pd
import logging
import os

logger = logging.getLogger(__name__)

def fetch_stock_data_via_curl(symbol: str, beg_date: str = "20210101", end_date: str = "20261231") -> pd.DataFrame:
    """
    使用 curl 获取 A 股历史数据
    """
    # 1. 解析代码和市场 (1=上海, 0=深圳)
    code = symbol.split('.')[0] if '.' in symbol else symbol
    market = "1" if symbol.endswith("SH") or (len(code) == 6 and code.startswith('6')) else "0"
    secid = f"{market}.{code}"

    # 2. 构建新浪财经 API URL (超稳定，支持长周期)
    code = symbol.split('.')[0] if '.' in symbol else symbol
    prefix = "sh" if symbol.endswith("SH") or (len(code) == 6 and code.startswith('6')) else "sz"
    # 新浪接口: scale=240(日线), datalen=1200(约5年)
    sina_url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}&scale=240&ma=5&datalen=1200"

    logger.info(f"正在通过 Curl + 新浪财经拉取 {symbol} 真实数据...")
    try:
        result = subprocess.run(
            ["curl", "-s", "--noproxy", "*", sina_url],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0:
            raise Exception(f"Curl 错误: {result.stderr}")
        
        # 新浪返回的是类 JSON 数组
        klines = json.loads(result.stdout)
        
        if not klines:
            logger.warning(f"未获取到 {symbol} 的数据，可能代码有误或 API 限流")
            return pd.DataFrame()

        # 3. 转换为 DataFrame (新浪格式: 字典列表)
        records = []
        for k in klines:
            records.append({
                "Date": k['day'],
                "Open": float(k['open']),
                "Close": float(k['close']),
                "High": float(k['high']),
                "Low": float(k['low']),
                "Volume": int(k['volume']),
            })
        
        df = pd.DataFrame(records)
        if df.empty: return df
        
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        
        logger.info(f"✅ 成功从新浪获取 {len(df)} 条 {symbol} 真实数据")
        return df

    except Exception as e:
        logger.error(f"Curl 获取数据失败: {e}")
        return pd.DataFrame()
