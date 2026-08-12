"""
统一行情数据获取模块
特性：双数据源冗余、自动校验、异常重试、熔断机制
"""
import requests
import json
import time
import os
from datetime import datetime
import pandas as pd
import numpy as np
from dsl_data_sdk import get_price as sdk_get_price

class DataFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        # 原有的重试、熔断机制保留，以兼容外部调用（但实际获取已交给 SDK）
        self.retry_times = 3
        self.retry_interval = 2
        self.fuse_count = 0
        self.fuse_threshold = 5
        self.fuse_time = 60
        self.last_fuse_time = 0

    def _check_fuse(self):
        """检查是否熔断"""
        if self.fuse_count >= self.fuse_threshold:
            if time.time() - self.last_fuse_time < self.fuse_time:
                raise Exception(f"数据源熔断，剩余冷却时间: {int(self.fuse_time - (time.time() - self.last_fuse_time))}s")
            else:
                self.fuse_count = 0
                self.last_fuse_time = 0

    def _fuse_trigger(self):
        """触发熔断"""
        self.fuse_count += 1
        if self.fuse_count >= self.fuse_threshold:
            self.last_fuse_time = time.time()
            print(f"⚠️ 数据源连续失败{self.fuse_count}次，触发熔断{self.fuse_time}秒")

    def get_realtime_price(self, code):
        """统一获取实时行情，直接使用 DSL 数据 SDK（已内置多源回退、缓存、校验）"""
        self._check_fuse()
        try:
            data = sdk_get_price(code)
            # 若 SDK 返回 None，视为获取失败
            if not data:
                raise Exception(f"SDK 未返回行情数据 for {code}")
            return data
        except Exception as e:
            # 记录错误并触发熔断计数
            print(f"SDK 获取{code}行情异常: {str(e)}")
            self._fuse_trigger()
            raise

    def get_history_kline(self, code, days=60):
        """获取历史K线数据 v4.5.12 — 通过 DataLoader 加载"""
        try:
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime('%Y-%m-%d')
            start_date = (datetime.now() - timedelta(days=days + 10)).strftime('%Y-%m-%d')
            from core.data_loader import DataLoader
            loader = DataLoader()
            df = loader.load_stock_data(code, start_date, end_date)
            if df is not None and not df.empty:
                return df
        except Exception as e:
            print(f"get_history_kline({code}) 失败: {e}")
        return None

# 全局单例
data_fetcher = DataFetcher()
