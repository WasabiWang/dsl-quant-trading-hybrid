#!/usr/bin/env python3
"""
统一HTTP请求客户端

已集成 macOS 系统代理绕过。
macOS SystemConfiguration 框架导致 Python 自动使用 127.0.0.1:1082 (Shadowrocket) 代理。
本模块通过 trust_env=False 禁止 requests 读取系统代理。
"""
import time
import os
import requests
from typing import Dict, Any, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── 模块导入时清理代理环境变量（防御第一层） ────────────────────────────────
for _k in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
            'ALL_PROXY', 'all_proxy']:
    os.environ.pop(_k, None)
os.environ['no_proxy'] = '*'
os.environ['NO_PROXY'] = '*'
# ─────────────────────────────────────────────────────────────────────────────

class HttpClient:
    """统一HTTP请求客户端，自动处理重试、超时、限流
    
    核心修复：trust_env=False 禁止 requests 读取 macOS 系统代理配置。
    """
    
    def __init__(self, retry: int = 3, timeout: int = 10, 
                 max_retries: int = 3, backoff_factor: float = 0.5):
        self.timeout = timeout
        self.session = requests.Session()
        
        # 🎯 核心修复：禁止读取 macOS 系统代理
        self.session.trust_env = False
        
        # 配置重试策略
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def get(self, url: str, params: Optional[Dict] = None, 
            headers: Optional[Dict] = None, **kwargs) -> requests.Response:
        """GET请求"""
        kwargs.setdefault('timeout', self.timeout)
        return self.session.get(url, params=params, headers=headers, **kwargs)
    
    def post(self, url: str, data: Optional[Dict] = None, 
             json: Optional[Dict] = None, headers: Optional[Dict] = None, **kwargs) -> requests.Response:
        """POST请求"""
        kwargs.setdefault('timeout', self.timeout)
        return self.session.post(url, data=data, json=json, headers=headers, **kwargs)
    
    def put(self, url: str, data: Optional[Dict] = None, 
            json: Optional[Dict] = None, headers: Optional[Dict] = None, **kwargs) -> requests.Response:
        """PUT请求"""
        kwargs.setdefault('timeout', self.timeout)
        return self.session.put(url, data=data, json=json, headers=headers, **kwargs)
    
    def delete(self, url: str, headers: Optional[Dict] = None, **kwargs) -> requests.Response:
        """DELETE请求"""
        kwargs.setdefault('timeout', self.timeout)
        return self.session.delete(url, headers=headers, **kwargs)

# 全局实例
default_client = HttpClient()

if __name__ == "__main__":
    # 测试
    client = HttpClient()
    try:
        resp = client.get("https://httpbin.org/get")
        print(f"Status: {resp.status_code}")
    except Exception as e:
        print(f"Error: {e}")