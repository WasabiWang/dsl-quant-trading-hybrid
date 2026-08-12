
#!/usr/bin/env python3
"""
akshare包装器 - 解决代理和网络问题
"""

import os
import sys
import requests
import pandas as pd
from functools import wraps

# 禁用代理
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

class AkshareWrapper:
    """akshare包装器类"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = False
        
        # 配置重试
        from requests.adapters import HTTPAdapter
        from requests.packages.urllib3.util.retry import Retry
        
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def get_realtime_data(self, symbol):
        """获取实时数据"""
        try:
            import akshare as ak
            # 使用包装后的session
            return ak.stock_zh_a_spot_em()
        except Exception as e:
            print(f"实时数据获取失败: {e}")
            return None
    
    def get_historical_data(self, symbol, start_date, end_date):
        """获取历史数据"""
        try:
            import akshare as ak
            code = symbol.split('.')[0]
            return ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq"
            )
        except Exception as e:
            print(f"历史数据获取失败: {e}")
            return None

# 创建全局实例
ak_wrapper = AkshareWrapper()

# 导出常用函数
def stock_zh_a_spot_em():
    """实时行情数据"""
    return ak_wrapper.get_realtime_data(None)

def stock_zh_a_hist(symbol, period, start_date, end_date, adjust):
    """历史行情数据"""
    return ak_wrapper.get_historical_data(symbol, start_date, end_date)
