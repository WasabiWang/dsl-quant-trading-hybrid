#!/usr/bin/env python3
"""
macOS系统代理自动绕过模块

根因：macOS SystemConfiguration 框架导致 Python 的 requests/urllib3 即使无 HTTP_PROXY 环境变量，
也会自动通过 127.0.0.1:1082 (Shadowrocket/MacPacketTunnel) 路由所有HTTP请求。
VPN隧道不稳定时导致 Mairui API 503 和 akshare ProxyError。

本模块提供多层级绕过方案，确保所有 HTTP 请求直连目标服务器。
"""
import os
import sys
import logging
from typing import Optional

_logger = logging.getLogger(__name__)

# ─── 常量 ─────────────────────────────────────────────────────────────────────
# macOS系统代理检测
SYS_PROXY_HOST = "127.0.0.1"
SYS_PROXY_PORT = 1082
SYS_PROXY = f"http://{SYS_PROXY_HOST}:{SYS_PROXY_PORT}"


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 1: 环境变量清理（立即执行，模块导入时生效）
# ═══════════════════════════════════════════════════════════════════════════════

def clean_env_proxies():
    """
    清理所有代理环境变量。
    这是最基础的防御——requests/urllib3 通过 os.environ 检测代理。
    macOS SystemConfiguration 的代理信息会被 requests 写入 os.environ 代理字典，
    清理环境变量可以阻止这一传递链。
    """
    proxy_keys = [
        'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
        'ALL_PROXY', 'all_proxy', 'SOCKS_PROXY', 'socks_proxy',
    ]
    removed = []
    for k in proxy_keys:
        if k in os.environ:
            del os.environ[k]
            removed.append(k)
    
    # 设置 no_proxy = '*' 确保任何残留的代理检测都不生效
    os.environ['no_proxy'] = '*'
    os.environ['NO_PROXY'] = '*'
    
    if removed:
        _logger.info(f"🔧 已清理代理环境变量: {', '.join(removed)}")
    return removed


# Layer 1 在模块导入时自动执行
_cleaned = clean_env_proxies()


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 2: 统一的代理绕过 Session 工厂
# ═══════════════════════════════════════════════════════════════════════════════

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def get_session(
    retries: int = 3,
    timeout: int = 15,
    backoff_factor: float = 0.5,
    verify: bool = True,
    status_forcelist: Optional[list] = None,
) -> requests.Session:
    """
    创建代理绕过的 requests Session。
    
    关键特性:
    - trust_env=False: 阻止 requests 读取任何代理配置（包括 macOS 系统代理）
    - 自动重试: 默认对 429, 500-504 重试3次
    - 连接池: 复用连接，减少 TLS 握手开销
    
    Args:
        retries: 最大重试次数
        timeout: 请求超时秒数
        backoff_factor: 退避因子
        verify: 是否验证 SSL 证书
        status_forcelist: 触发重试的 HTTP 状态码列表
    
    Returns:
        配置好的 requests.Session
    """
    session = requests.Session()
    
    # 🎯 核心修复：禁用系统代理检测
    session.trust_env = False
    
    # 重试策略
    # v4.6.6: 仅重试429(限流)，5xx交给上层request_api处理，避免双重试放大
    # Shadowrocket VPN TUN模式下，503/ReadTimeout源于隧道不稳定，urllib3重试无法解决
    if status_forcelist is None:
        status_forcelist = [429]
    
    retry_strategy = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"],
    )
    adapter = HTTPAdapter(
        max_retries=retry_strategy,
        pool_connections=10,
        pool_maxsize=20,
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.verify = verify
    
    return session


# 全局默认 Session（懒加载）
_default_session: Optional[requests.Session] = None


def get_default_session() -> requests.Session:
    """获取全局默认 Session（懒加载）"""
    global _default_session
    if _default_session is None:
        _default_session = get_session()
    return _default_session


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 3: 便捷请求函数（替代 requests.get/post 的直接调用）
# ═══════════════════════════════════════════════════════════════════════════════

def get(url: str, **kwargs) -> requests.Response:
    """代理绕过的 GET 请求"""
    session = kwargs.pop('session', None) or get_default_session()
    kwargs.setdefault('timeout', 15)
    return session.get(url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    """代理绕过的 POST 请求"""
    session = kwargs.pop('session', None) or get_default_session()
    kwargs.setdefault('timeout', 15)
    return session.post(url, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 4: requests 模块猴子补丁（捕获所有未使用 Session 的 requests.get/post）
# ═══════════════════════════════════════════════════════════════════════════════

_ORIGINAL_REQUESTS_GET = requests.get
_ORIGINAL_REQUESTS_POST = requests.post
_ORIGINAL_SESSION_INIT = requests.Session.__init__


def _patched_requests_get(url, **kwargs):
    """自动绕过代理的 requests.get"""
    kwargs.setdefault('timeout', 15)
    session = get_default_session()
    return session.get(url, **kwargs)


def _patched_requests_post(url, **kwargs):
    """自动绕过代理的 requests.post"""
    kwargs.setdefault('timeout', 15)
    session = get_default_session()
    return session.post(url, **kwargs)


def _patched_session_init(self, *args, **kwargs):
    """确保所有新创建的 Session 都禁用代理"""
    _ORIGINAL_SESSION_INIT(self, *args, **kwargs)
    self.trust_env = False


def enable_monkey_patch():
    """
    启用 requests 模块猴子补丁。
    
    警告：这会全局修改 requests 标准行为，仅应在明确知晓后果时启用。
    影响：所有 requests.get/post/Session 创建都会跳过系统代理。
    
    建议：优先使用本模块的 get_session() 和 get()/post() 函数。
    仅当需要覆盖第三方库（如 akshare 内部使用 requests.get）时启用此补丁。
    """
    requests.get = _patched_requests_get
    requests.post = _patched_requests_post
    requests.Session.__init__ = _patched_session_init
    _logger.info("🔧 已启用 requests 猴子补丁（全局代理绕过）")


def disable_monkey_patch():
    """禁用 requests 猴子补丁"""
    requests.get = _ORIGINAL_REQUESTS_GET
    requests.post = _ORIGINAL_REQUESTS_POST
    requests.Session.__init__ = _ORIGINAL_SESSION_INIT
    _logger.info("🔧 已禁用 requests 猴子补丁")


# ═══════════════════════════════════════════════════════════════════════════════
# Layer 5: 诊断工具
# ═══════════════════════════════════════════════════════════════════════════════

def diagnose_proxy() -> dict:
    """
    诊断当前环境的代理状态。
    
    Returns:
        包含代理检测信息的字典
    """
    import urllib.request as ureq
    
    result = {
        "env_HTTP_PROXY": os.environ.get("HTTP_PROXY", "<unset>"),
        "env_HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "<unset>"),
        "env_NO_PROXY": os.environ.get("NO_PROXY", "<unset>"),
        "urllib_sys_proxies": ureq.getproxies(),
        "requests_session_trust_env": get_default_session().trust_env,
        "session_proxies": get_default_session().proxies,
    }
    
    # 测试实际连接是否绕过
    test_results = {}
    for url, label in [
        ("https://httpbin.org/ip", "通用外网"),
        ("https://api.mairuiapi.com/hsrl/ssjy/000001/test", "Mairui API"),
    ]:
        try:
            start = __import__('time').time()
            r = get(url, timeout=5)
            elapsed = __import__('time').time() - start
            test_results[label] = {
                "status": r.status_code,
                "elapsed_s": round(elapsed, 2),
                "ok": r.status_code < 500,
            }
        except Exception as e:
            test_results[label] = {
                "status": "ERROR",
                "error": str(e)[:100],
                "ok": False,
            }
    
    result["connection_tests"] = test_results
    return result


# 模块导入时自动执行
_logger.debug(f"proxy_bypass 已初始化, trust_env={get_default_session().trust_env}")
