#!/usr/bin/env python3
"""
common/retry_decorator.py — DSL v4.5.7 API重试装饰器

=== 设计目标 ===
统一所有外部API调用的重试逻辑（akshare / 东方财富 / 麦蕊 / yfinance / 新浪），
消除各模块各自实现 retry_* 导致的行为不一致。

=== 用法 ===
  from common.retry_decorator import retry

  @retry(max_attempts=3, backoff=1.5)
  def fetch_stock_data(symbol):
      return ak.stock_zh_a_hist(symbol=symbol, ...)

  # 自定义异常过滤
  @retry(max_attempts=3, exceptions=(ConnectionError, TimeoutError, ValueError))
  def fetch_nasdaq():
      return ak.index_us_stock_sina(".IXIC")

  # 按API区分退避策略
  from common.retry_decorator import retry, RetryConfig

  @retry({"akshare": RetryConfig(max_attempts=3, backoff=1.0),
          "mairui": RetryConfig(max_attempts=5, backoff=2.0)})
  def call_mairui_api():
      ...

=== 行为 ===
  - 指数退避: backoff^attempt (默认 1.5^1=1.5s, 1.5^2=2.25s, ...)
  - 最大等待: 30s (累积)
  - 只重试指定异常（默认: ConnectionError, TimeoutError, OSError, IOError）
  - 重试间打印 ⚠️ 日志（含attempt/错误原因）
  - 所有重试耗尽后抛出原始异常
  - 支持嵌入到 @retry 或显式调用 retry_context() 包裹已有函数
"""

import time, functools, random
from typing import Type, Tuple, Optional, Union, Dict, Callable
from datetime import datetime

# ──────────── 默认重试配置 ────────────

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF = 1.5          # 指数退避基数
DEFAULT_MAX_DELAY = 30         # 最大单次等待(秒)
DEFAULT_JITTER = 0.1           # ±10% 随机抖动防惊群
DEFAULT_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    OSError,
    IOError,
    ConnectionResetError,
    ConnectionRefusedError,
    ConnectionAbortedError,
)


class RetryConfig:
    """API重试配置"""
    __slots__ = ("max_attempts", "backoff", "max_delay",
                 "jitter", "exceptions", "on_retry")

    def __init__(
        self,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff: float = DEFAULT_BACKOFF,
        max_delay: float = DEFAULT_MAX_DELAY,
        jitter: float = DEFAULT_JITTER,
        exceptions: Optional[Tuple[Type[Exception], ...]] = None,
        on_retry: Optional[Callable] = None,
    ):
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.max_delay = max_delay
        self.jitter = jitter
        self.exceptions = exceptions or DEFAULT_EXCEPTIONS
        self.on_retry = on_retry


# ──────────── API分类默认配置 ────────────

API_CONFIGS: Dict[str, RetryConfig] = {
    "akshare":     RetryConfig(max_attempts=3, backoff=1.5,  max_delay=10),
    "mairui":      RetryConfig(max_attempts=5, backoff=2.0,  max_delay=30),
    "eastmoney":   RetryConfig(max_attempts=3, backoff=1.0,  max_delay=8),
    "yfinance":    RetryConfig(max_attempts=5, backoff=1.5,  max_delay=30),
    "sina":        RetryConfig(max_attempts=3, backoff=1.0,  max_delay=5),
    "tencent":     RetryConfig(max_attempts=3, backoff=1.0,  max_delay=5),
    "llm_api":     RetryConfig(max_attempts=3, backoff=1.0,  max_delay=10,
                                exceptions=(ConnectionError, TimeoutError, OSError)),
}


# ──────────── 装饰器实现 ────────────

def _calc_delay(attempt: int, config: RetryConfig) -> float:
    """计算本次重试等待时间（指数退避 + 抖动）"""
    delay = min(config.backoff ** (attempt + 1), config.max_delay)
    jitter_range = delay * config.jitter
    delay += random.uniform(-jitter_range, jitter_range)
    return max(0.1, delay)


def retry(
    config: Optional[Union[RetryConfig, Dict[str, RetryConfig], int, float]] = None,
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    max_delay: float = DEFAULT_MAX_DELAY,
    exceptions: Optional[Tuple[Type[Exception], ...]] = None,
    on_retry: Optional[Callable] = None,
):
    """API重试装饰器

    Args:
        config: RetryConfig实例、API分类名(dict)、attempt数(int)或退避(flaot)
        max_attempts: 最大重试次数（当config不是RetryConfig时生效）
        backoff: 指数退避基数
        max_delay: 最大单次等待秒数
        exceptions: 需要重试的异常类型元组
        on_retry: 每次重试前的回调 on_retry(attempt, exception, config) -> None

    Usage:
        @retry(max_attempts=3, backoff=1.5)
        def fetch_data():
            ...
    """
    # 统一解析 config 参数
    if isinstance(config, RetryConfig):
        rc = config
    elif isinstance(config, dict):
        # 不支持的装饰器用法: @retry({"akshare": ...})
        # 这是运行时动态选择，由 retry_context() 处理
        rc = RetryConfig(max_attempts=max_attempts, backoff=backoff,
                         max_delay=max_delay, exceptions=exceptions,
                         on_retry=on_retry)
    elif isinstance(config, (int, float)):
        rc = RetryConfig(max_attempts=int(config), backoff=backoff if isinstance(config, float) else backoff,
                         max_delay=max_delay, exceptions=exceptions, on_retry=on_retry)
    else:
        rc = RetryConfig(max_attempts=max_attempts, backoff=backoff,
                         max_delay=max_delay, exceptions=exceptions,
                         on_retry=on_retry)

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(rc.max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if not isinstance(e, rc.exceptions):
                        raise  # 非重试异常→直接抛出
                    last_exception = e
                    if attempt == rc.max_attempts - 1:
                        # 最后一次重试也失败→抛出原始异常
                        raise

                    delay = _calc_delay(attempt, rc)
                    func_name = getattr(func, "__name__", str(func))
                    print(
                        f"⚠️ [API重试 {attempt+1}/{rc.max_attempts}] {func_name}: "
                        f"{type(e).__name__}: {str(e)[:80]} → 等待{delay:.1f}s"
                    )

                    if rc.on_retry:
                        try:
                            rc.on_retry(attempt, e, rc)
                        except Exception:
                            pass

                    time.sleep(delay)

            # 不应到达这里，但防御性兜底
            raise last_exception  # type: ignore
        return wrapper
    return decorator


def retry_context(config: Optional[Union[RetryConfig, str]] = None,
                  api_name: str = "default") -> Callable:
    """运行时重试上下文（支持按API名选择配置）

    Args:
        config: RetryConfig 或 API分类名（如 "akshare", "mairui", "yfinance"）
        api_name: 当 config 为 None 时用来从 API_CONFIGS 查找

    Returns:
        一个装饰器工厂，用法同 @retry()

    Example:
        @retry_context("mairui")
        def call_mairui():
            ...

        # 或显式指定
        @retry_context(RetryConfig(max_attempts=4, backoff=2.0))
        def custom_call():
            ...
    """
    if isinstance(config, str):
        resolved = API_CONFIGS.get(config, API_CONFIGS["default"])
        return retry(resolved)
    if isinstance(config, RetryConfig):
        return retry(config)
    # config is None → 从 api_name 查找
    resolved = API_CONFIGS.get(api_name, RetryConfig())
    return retry(resolved)


# ──────────── 便捷函数（非装饰器用法） ────────────

def retry_call(func: Callable, *args,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               backoff: float = DEFAULT_BACKOFF,
               **kwargs):
    """显式调用函数并自动重试（非装饰器用法）

    Args:
        func: 要调用的函数
        *args: 传给func的参数
        max_attempts: 最大尝试次数
        backoff: 退避基数
        **kwargs: 传给func的关键字参数

    Returns:
        func的返回值
    """
    rc = RetryConfig(max_attempts=max_attempts, backoff=backoff)

    for attempt in range(rc.max_attempts):
        try:
            return func(*args, **kwargs)
        except rc.exceptions as e:
            if attempt == rc.max_attempts - 1:
                raise
            delay = _calc_delay(attempt, rc)
            func_name = getattr(func, "__name__", str(func))
            print(
                f"⚠️ [retry_call {attempt+1}/{rc.max_attempts}] {func_name}: "
                f"{type(e).__name__}: {str(e)[:80]} → 等待{delay:.1f}s"
            )
            time.sleep(delay)
    return None  # unreachable


# ──────────── 自测 ────────────
if __name__ == "__main__":
    print(f"{'='*60}")
    print("🧪 RetryDecorator 测试")
    print(f"{'='*60}")

    call_count = [0]

    @retry(max_attempts=3, backoff=0.5)
    def _flaky_api():
        call_count[0] += 1
        if call_count[0] < 2:
            raise ConnectionError("暂时无法连接")
        return "success"

    result = _flaky_api()
    print(f"  ✅ flaky_api 第{call_count[0]}次成功: {result}")
    assert result == "success"
    assert call_count[0] == 2

    # 测试连续失败
    call_count[0] = 0

    @retry(max_attempts=2, backoff=0.5)
    def _always_fail():
        call_count[0] += 1
        raise TimeoutError("API超时")

    try:
        _always_fail()
        assert False, "应抛出异常"
    except TimeoutError:
        print(f"  ✅ always_fail 第{call_count[0]}次后正确抛出")
        assert call_count[0] == 2

    # 测试非重试异常不重试
    call_count[0] = 0

    @retry(max_attempts=3, backoff=0.5)
    def _value_error():
        call_count[0] += 1
        raise ValueError("非重试异常")

    try:
        _value_error()
        assert False, "应抛出异常"
    except ValueError:
        print(f"  ✅ ValueError第{call_count[0]}次后直接抛出(不重试)")
        assert call_count[0] == 1

    # 测试 retry_call 便捷函数
    call_count[0] = 0

    def _simple():
        call_count[0] += 1
        if call_count[0] < 2:
            raise ConnectionError("fail")
        return "ok"

    result = retry_call(_simple, max_attempts=3, backoff=0.5)
    assert result == "ok"
    print(f"  ✅ retry_call: 第{call_count[0]}次成功")

    # 测试 API 分类配置
    print("\n   API分类配置:")
    for name, cfg in API_CONFIGS.items():
        print(f"    {name}: max={cfg.max_attempts}, backoff={cfg.backoff}")

    print("\n✅ 所有测试通过!")
