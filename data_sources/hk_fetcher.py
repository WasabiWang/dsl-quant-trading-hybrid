#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
港股数据获取模块
数据源：腾讯财经Pro接口（requests直连，替代curl subprocess）
备用：AKShare（已预留，网络请求方式相同）
"""

import pandas as pd
import requests
import time
from typing import Optional


def fetch_hk_data(symbol="hk00700", n=1000, max_retries=2):
    """
    获取港股历史数据 (腾讯财经 Pro 接口，requests直连)
    
    Args:
        symbol: 港股代码，如 'hk00700' (腾讯控股)
        n: 数据条数
        max_retries: 网络异常重试次数
    
    Returns:
        pd.DataFrame: 包含OHLCV的历史数据
    """
    url = (
        f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/"
        f"get?param={symbol},day,,,{n},qfq"
    )
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://stockapp.finance.qq.com/",
        "Accept": "application/json, text/plain, */*",
    }

    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(
                url, headers=headers, timeout=15,
                proxies={"http": None, "https": None}  # 绕过代理
            )
            resp.raise_for_status()
            data = resp.json()
            stock_data = data.get("data", {}).get(symbol, {})
            # 优先获取前复权，降级获取未复权
            klines = stock_data.get("qfqday", stock_data.get("day", []))
            if not klines:
                return pd.DataFrame()

            # 腾讯返回列：date, open, close, high, low, volume, unk1, turnover_rate, amount, ma5, ma10
            df = pd.DataFrame(
                klines,
                columns=["date", "open", "close", "high", "low", "volume",
                         "unk1", "turnover_rate", "amount", "ma5", "ma10"],
            )
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)
            for col in ["open", "close", "high", "low", "amount"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").astype(int)
            return df

        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt < max_retries:
                wait = 1 * (2 ** attempt)
                print(f"⚠️ [hk_fetcher 重试 {attempt+1}/{max_retries}] 网络异常: {e}，等待{wait}s")
                time.sleep(wait)
                continue
            print(f"❌ [hk_fetcher] 重试{max_retries+1}次后失败: {e}")
            return pd.DataFrame()
        except Exception as e:
            print(f"❌ [hk_fetcher] 请求异常: {e}")
            return pd.DataFrame()


def fetch_hk_data_bs4(symbol="hk00700", n=1000) -> pd.DataFrame:
    """
    备用数据源：未来可添加AKShare/东方财富港股数据
    现在作为腾讯接口的包装（统一调用方式）
    """
    return fetch_hk_data(symbol, n)


if __name__ == "__main__":
    df = fetch_hk_data("hk00700", 10)
    if not df.empty:
        print(f"✅ 港股接口正常！最新收盘价: {df['close'].iloc[-1]:.2f}")
        print(df.tail(3))
    else:
        print("❌ 港股接口异常")
