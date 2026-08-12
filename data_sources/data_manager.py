"""
数据管理器 (DataManager)
多数据源架构：支持优先级调度 + 自动降级 + 容错机制
优先级顺序：
1. 东方财富API（A股优先，数据质量最高）
2. Curl直连数据源（次优先，无依赖）
3. 市场默认接口（备选）
"""

import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, List
import logging
import os
import json
from datetime import datetime, timedelta
import importlib

# 初始化logger必须放在最前面，避免导入时报NameError
logger = logging.getLogger(__name__)

from markets import get_market
from data_sources.curl_fetcher import fetch_stock_data_via_curl

# 尝试导入东方财富数据源
eastmoney_available = False
EastMoneyFetcher = None
try:
    from data_sources.eastmoney_fetcher import EastMoneyFetcher
    eastmoney_available = True
except Exception as e:
    logger.warning(f"东方财富数据源不可用: {e}")

# 尝试导入AKShare数据源
akshare_available = False
AKShareFetcher = None
try:
    from data_sources.akshare_wrapper import AKShareFetcher
    akshare_available = True
except Exception as e:
    logger.warning(f"AKShare数据源不可用: {e}")

class DataManager:
    def __init__(self, cache_dir: str = "data/cache"):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.markets = {
            "a": get_market("a"),
            "hk": get_market("hk"),
            "us": get_market("us")
        }
        # 数据源优先级配置
        self.data_sources = {
            "a": [
                ("eastmoney", self._fetch_from_eastmoney),
                ("curl", self._fetch_from_curl),
                ("market_api", self._fetch_from_market_api)
            ],
            "hk": [
                ("akshare", self._fetch_from_akshare),
                ("market_api", self._fetch_from_market_api)
            ],
            "us": [
                ("akshare", self._fetch_from_akshare),
                ("market_api", self._fetch_from_market_api)
            ]
        }
        logger.info("✅ 多数据源DataManager初始化完成，支持自动降级容错")

    def get_historical_data(self, symbol: str, years: int = 5, interval: str = "1d") -> pd.DataFrame:
        """
        获取长周期历史数据，支持多数据源自动降级
        """
        # 1. 强制清理代理环境变量（解决 ProxyError 的核心）
        for k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY']:
            os.environ.pop(k, None)

        market_type = self._detect_market(symbol)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=years * 365)
        
        cache_file = os.path.join(self.cache_dir, f"{symbol}_{years}y_{interval}.parquet")
        
        # 尝试从缓存加载
        if os.path.exists(cache_file):
            logger.info(f"从缓存加载 {symbol} 数据")
            df = pd.read_parquet(cache_file)
            # 即使有缓存也要预处理，确保列名和指标正确
            if not df.empty:
                return self._preprocess_data(df)

        # 2. 按优先级尝试数据源，自动降级
        logger.info(f"正在拉取 {symbol} 近 {years} 年数据...")
        sources = self.data_sources.get(market_type, self.data_sources["us"])
        
        for source_name, fetch_func in sources:
            try:
                logger.info(f"尝试使用 [{source_name}] 数据源...")
                df = fetch_func(symbol, start_date, end_date, interval)
                if not df.empty:
                    # 保存缓存
                    df.to_parquet(cache_file)
                    logger.info(f"✅ [{source_name}] 拉取成功，数据已缓存至 {cache_file}")
                    return self._preprocess_data(df)
            except Exception as e:
                logger.warning(f"⚠️  [{source_name}] 拉取失败: {e}，尝试下一个数据源")
                continue
        
        logger.error(f"❌ 所有数据源均拉取 {symbol} 失败")
        return pd.DataFrame()

    def _fetch_from_eastmoney(self, symbol: str, start_date: datetime, end_date: datetime, interval: str) -> pd.DataFrame:
        """从东方财富数据源拉取数据"""
        if not eastmoney_available:
            raise Exception("东方财富数据源不可用")
        fetcher = EastMoneyFetcher()
        return fetcher.get_stock_data(
            symbol, 
            start_date.strftime("%Y%m%d"), 
            end_date.strftime("%Y%m%d")
        )

    def _fetch_from_curl(self, symbol: str, start_date: datetime, end_date: datetime, interval: str) -> pd.DataFrame:
        """从Curl直连数据源拉取数据"""
        return fetch_stock_data_via_curl(
            symbol, 
            start_date.strftime("%Y%m%d"), 
            end_date.strftime("%Y%m%d")
        )

    def _fetch_from_akshare(self, symbol: str, start_date: datetime, end_date: datetime, interval: str) -> pd.DataFrame:
        """从AKShare数据源拉取数据"""
        if not akshare_available:
            raise Exception("AKShare数据源不可用")
        fetcher = AKShareFetcher()
        return fetcher.get_history(
            symbol, 
            start_date.strftime("%Y-%m-%d"), 
            end_date.strftime("%Y-%m-%d")
        )

    def _fetch_from_market_api(self, symbol: str, start_date: datetime, end_date: datetime, interval: str) -> pd.DataFrame:
        """从市场默认接口拉取数据"""
        market_type = self._detect_market(symbol)
        market = self.markets[market_type]
        data = market.get_history(
            symbol, 
            start_date.strftime("%Y-%m-%d"), 
            end_date.strftime("%Y-%m-%d")
        )
        if isinstance(data, dict) and 'data' in data:
            df = pd.DataFrame(data['data'])
        else:
            df = pd.DataFrame(data)
        return df

    def _preprocess_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """预处理数据：计算指标，处理缺失值"""
        if df.empty:
            return df
        
        # 统一列名 (处理 akshare 的中文列名)
        column_mapping = {
            '日期': 'Date', '时间': 'Date',
            '开盘': 'Open', '最高': 'High', '最低': 'Low',
            '收盘': 'Close', '成交量': 'Volume', '成交额': 'Amount',
            '涨跌幅': 'Change'
        }
        df.rename(columns=column_mapping, inplace=True)
        
        # 标准化英文列名
        df.columns = [c.capitalize() if c.lower() in ['date', 'open', 'high', 'low', 'close', 'volume'] else c for c in df.columns]

        if 'Date' in df.columns:
            df['Date'] = pd.to_datetime(df['Date'])
            df.set_index('Date', inplace=True)
        
        # 计算技术指标
        self._add_indicators(df)
        
        # 填充缺失值
        df.ffill(inplace=True)
        df.bfill(inplace=True)
        
        return df

    def _add_indicators(self, df: pd.DataFrame):
        """添加 RL 和回测所需的指标"""
        if 'Close' not in df.columns:
            return

        # SMA 20
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        
        # RSI 14
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # MACD
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()

    def _detect_market(self, symbol: str) -> str:
        if '.' in symbol:
            suffix = symbol.split('.')[1]
            if suffix in ['SH', 'SZ', 'BJ']: return 'a'
            if suffix == 'HK': return 'hk'
        return 'us'
