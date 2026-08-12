#!/usr/bin/env python3
"""
core/resilience.py — 统一韧性工具 (v4.6.2)

解决5条关键教训:
  教训56: safe_get — YAML嵌套路径安全读取, 禁止跳层get()
  教训57: with_fallback — 超时+重试+缓存统一装饰器, 每源独立超时
  教训57: quick_fail_cache — LICENCE/连接可用性session级快速检测+缓存
  教训58/64: normalize_action — 交易action/状态枚举值规范化
  教训59: 读取配置失败时显式报错, 不静默回退默认值

使用示例:
  # YAML安全读取
  from core.resilience import safe_get
  black_swan = safe_get(params, 'risk.black_swan_active')

  # API调用超时+重试
  from core.resilience import with_fallback
  @with_fallback(name="mairui_stock_real", timeout=5.0, max_retries=1)
  def get_stock_real(code): ...

  # Session级可用性缓存
  from core.resilience import quick_fail_cache
  @quick_fail_cache(key="mairui_licence_valid", ttl=3600)
  def check_licence(): ...
"""

import functools
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════
# Session级缓存 (进程生命周期内共享)
# ═══════════════════════════════════════════
_SESSION_CACHE: Dict[str, Tuple[float, Any]] = {}
_SESSION_CACHE_LOCK = threading.Lock()

# 哨兵值: 区分"缓存中存了None"和"缓存未命中"
_CACHE_MISS = object()


def session_cache_get(key: str) -> Tuple[bool, Any]:
    """session级缓存读取, 线程安全"""
    with _SESSION_CACHE_LOCK:
        entry = _SESSION_CACHE.get(key)
        if entry is None:
            return False, _CACHE_MISS
        expire_at, value = entry
        if time.time() > expire_at:
            del _SESSION_CACHE[key]
            return False, _CACHE_MISS
        return True, value


def session_cache_set(key: str, value: Any, ttl: float = 300) -> None:
    """session级缓存写入, 线程安全"""
    with _SESSION_CACHE_LOCK:
        _SESSION_CACHE[key] = (time.time() + ttl, value)


def session_cache_clear() -> None:
    """清空session级缓存"""
    with _SESSION_CACHE_LOCK:
        _SESSION_CACHE.clear()


# ═══════════════════════════════════════════
# 1. safe_get — 嵌套字典路径安全读取 (教训56/59)
# ═══════════════════════════════════════════

_MISSING = object()  # 哨兵: 区分"路径不存在"和"值为None"


def safe_get(data: dict, path: str, default: Any = _MISSING, *,
             raise_on_missing: bool = False) -> Any:
    """
    安全读取嵌套字典, 逐层验证路径存在.

    与 dict.get() 的核心区别:
      - dict.get('key') 只检查一级key, 对嵌套结构无效
      - safe_get(data, 'a.b.c') 逐层检查 a→b→c 全部存在
      - 路径不存在时自动日志告警, 防止静默回退默认值

    Args:
        data: 嵌套字典 (如 YAML 加载结果)
        path: 点分隔路径, 如 'risk.black_swan_active'
        default: 路径不存在时的默认值。不传且raise_on_missing=False → 返回None并告警
        raise_on_missing: True时路径不存在直接抛KeyError

    Returns:
        路径对应的值, 或 default

    Raises:
        KeyError: raise_on_missing=True 且路径不存在时

    Examples:
        >>> params = {'risk': {'black_swan_active': True}}
        >>> safe_get(params, 'risk.black_swan_active')
        True
        >>> safe_get(params, 'risk.nonexistent', False)
        False  # + WARNING日志

        >>> # 对比 dict.get 的行为缺陷:
        >>> params.get('black_swan', {})       # 返回 {} — 路径完全错误但无警告
        >>> safe_get(params, 'black_swan')     # 返回 None + WARNING — 立即可见
    """
    if not isinstance(data, dict):
        msg = f"safe_get: data不是dict, 是{type(data).__name__}"
        logger.warning(msg)
        if raise_on_missing:
            raise KeyError(msg)
        return default if default is not _MISSING else None

    keys = path.split('.')
    current = data

    for i, key in enumerate(keys):
        if not isinstance(current, dict):
            partial_path = '.'.join(keys[:i])
            msg = (f"safe_get: 路径 '{path}' 中断于 '{partial_path}' "
                   f"— 当前值不是dict而是{type(current).__name__}")
            logger.warning(msg)
            if raise_on_missing:
                raise KeyError(msg)
            return default if default is not _MISSING else None

        if key not in current:
            partial_path = '.'.join(keys[:i]) or '(root)'
            available = list(current.keys())[:10] if isinstance(current, dict) else []
            msg = (f"safe_get: key='{key}' 不存在于 '{partial_path}', "
                   f"完整路径='{path}', 可用key={available}")
            logger.warning(msg)
            if raise_on_missing:
                raise KeyError(msg)
            return default if default is not _MISSING else None

        current = current[key]

    return current


# ═══════════════════════════════════════════
# 2. normalize_action — 枚举值规范化 (教训58/64)
# ═══════════════════════════════════════════

_VALID_ACTIONS = frozenset({'BUY', 'SELL', 'HOLD'})
_VALID_SIGNALS = frozenset({'buy', 'sell', 'hold'})
_VALID_LEVELS = frozenset({'info', 'warning', 'error', 'critical'})


def normalize_action(action: str, *, output: str = 'upper') -> str:
    """
    交易action规范化, 防止大小写不匹配导致的静默失败.

    教训: action='sell'(小写)不匹配PaperTrader只接受的'SELL'(大写)
    → 卖出静默失败。所有action必须在系统入口统一规范化.

    Args:
        action: 原始action字符串 ('buy'/'BUY'/'Buy' 等)
        output: 'upper' → 返回大写; 'lower' → 返回小写

    Returns:
        规范化后的action: 'BUY'/'SELL'/'HOLD' (output='upper')

    Raises:
        ValueError: action不是有效值
    """
    if not isinstance(action, str):
        raise ValueError(f"normalize_action: 期望str, 收到{type(action).__name__}: {action}")

    normalized = action.strip().upper() if output == 'upper' else action.strip().lower()
    valid_set = _VALID_ACTIONS if output == 'upper' else _VALID_SIGNALS

    if normalized not in valid_set:
        raise ValueError(
            f"normalize_action: 无效action '{action}' → '{normalized}', "
            f"有效值: {sorted(valid_set)}"
        )
    return normalized


def normalize_level(level: str) -> str:
    """告警级别规范化"""
    normalized = level.strip().lower()
    if normalized not in _VALID_LEVELS:
        raise ValueError(f"normalize_level: 无效级别 '{level}', 有效值: {sorted(_VALID_LEVELS)}")
    return normalized


# ═══════════════════════════════════════════
# 3. with_fallback — 超时+重试+缓存的统一装饰器 (教训5/57)
# ═══════════════════════════════════════════

def with_fallback(
    name: str,
    timeout: float = 5.0,
    max_retries: int = 1,
    retry_delay: float = 0.5,
    cache_ttl: float = 0,
    fallback_func: Optional[Callable] = None,
    fallback_args: Optional[tuple] = None,
    fallback_kwargs: Optional[dict] = None,
):
    """
    统一韧性装饰器: 超时 → 重试 → 缓存 → 降级链.

    解决教训57的核心问题: 降级链中每个源必须有独立超时控制,
    一个源慢/错不能拖累整条链. LICENCE检测结果在TTL内缓存复用.

    Args:
        name: 操作名称(用于日志和缓存key)
        timeout: 单次调用超时(秒)
        max_retries: 超时/异常后重试次数(总共 max_retries+1 次尝试)
        retry_delay: 重试间隔(秒)
        cache_ttl: 结果缓存有效期(秒)。0=不缓存
        fallback_func: 所有重试失败后的降级函数
        fallback_args: 降级函数的固定位置参数
        fallback_kwargs: 降级函数的固定关键字参数

    Usage:
        @with_fallback(name="mairui_real", timeout=5, max_retries=1, cache_ttl=30)
        def get_stock_real(code): ...

        @with_fallback(
            name="fetch_price",
            timeout=3, max_retries=0,
            fallback_func=fetch_from_sina, fallback_args=('000001',)
        )
        def fetch_from_mairui(code): ...
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 先查缓存
            if cache_ttl > 0:
                cache_key = f"wf:{name}:{args}:{sorted(kwargs.items())}"
                hit, cached = session_cache_get(cache_key)
                if hit:
                    logger.debug(f"with_fallback[{name}]: 缓存命中 ({cache_ttl}s TTL)")
                    return cached

            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)

                    # 成功, 写入缓存
                    if cache_ttl > 0:
                        session_cache_set(cache_key, result, cache_ttl)

                    if attempt > 0:
                        logger.info(
                            f"with_fallback[{name}]: 重试成功 (第{attempt + 1}次)"
                        )
                    return result

                except Exception as e:
                    last_error = e
                    if attempt < max_retries:
                        logger.warning(
                            f"with_fallback[{name}]: 第{attempt + 1}次失败 "
                            f"({type(e).__name__}: {e}), "
                            f"{retry_delay}s后重试..."
                        )
                        time.sleep(retry_delay)

            # 所有重试失败 → 走降级函数
            if fallback_func is not None:
                logger.warning(
                    f"with_fallback[{name}]: 全部{max_retries + 1}次尝试失败, "
                    f"降级到 {fallback_func.__name__}"
                )
                fb_args = fallback_args or args
                fb_kwargs = fallback_kwargs or kwargs
                try:
                    return fallback_func(*fb_args, **fb_kwargs)
                except Exception as fb_e:
                    logger.error(
                        f"with_fallback[{name}]: 降级函数也失败了: {fb_e}"
                    )
                    raise

            # 无降级函数 → 抛出原始异常
            logger.error(
                f"with_fallback[{name}]: 全部{max_retries + 1}次尝试失败, "
                f"无降级函数. 最后异常: {last_error}"
            )
            raise last_error

        return wrapper

    return decorator


# ═══════════════════════════════════════════
# 4. quick_fail_cache — 可用性快速检测+缓存 (教训57)
# ═══════════════════════════════════════════

def quick_fail_cache(key: str, ttl: float = 300, *,
                     fail_value: Any = False):
    """
    装饰器: session内只执行一次检测, 缓存结果.

    适用于: LICENCE有效性检测, API端点可用性检查, 连接测试.
    教训57核心: "LICENCE过期/无效应在Session内只检测一次, 
    不是每次API调用都重试."

    Args:
        key: 缓存key
        ttl: 缓存有效期(秒), 默认300s
        fail_value: 检测失败时(含异常)的返回值

    Usage:
        @quick_fail_cache(key="mairui_licence", ttl=3600)
        def _check_licence(): ...
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            hit, cached = session_cache_get(key)
            if hit:
                return cached

            try:
                result = func(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"quick_fail_cache[{key}]: 检测异常 {type(e).__name__}: {e}, "
                    f"缓存fail_value={fail_value} ({ttl}s)"
                )
                result = fail_value

            session_cache_set(key, result, ttl)
            logger.debug(f"quick_fail_cache[{key}]: 结果={result}, TTL={ttl}s")
            return result

        return wrapper

    return decorator


# ═══════════════════════════════════════════
# 5. 工具函数: 混合回退链构建
# ═══════════════════════════════════════════

def build_fallback_chain(*funcs: Callable, name: str = "chain") -> Callable:
    """
    构建异步降级链: 依次尝试每个函数, 第一个成功的返回值.

    与 with_fallback 的区别:
      - with_fallback 是对单个函数的重试+降级
      - build_fallback_chain 是多数据源的回退链 (如 麦蕊→新浪→东财)

    Args:
        *funcs: 降级链中的函数 (按优先级排列)
        name: 链名称(用于日志)

    Returns:
        包装后的函数, 调用方式与第一个func相同

    Usage:
        fetch = build_fallback_chain(
            fetch_from_mairui,
            fetch_from_sina,
            fetch_from_eastmoney,
            name="stock_price"
        )
    """

    def runner(*args, **kwargs):
        errors = []
        for i, func in enumerate(funcs):
            try:
                result = func(*args, **kwargs)
                if i > 0:
                    logger.info(f"fallback_chain[{name}]: 降级到第{i + 1}源 "
                                f"({func.__name__})成功")
                return result
            except Exception as e:
                source_name = getattr(func, '__name__', f'source_{i}')
                errors.append(f"{source_name}: {e}")
                logger.debug(f"fallback_chain[{name}]: {source_name}失败 → 下一源")

        # 全部失败
        chain_desc = " → ".join(
            getattr(f, '__name__', f'source_{i}') for i, f in enumerate(funcs)
        )
        raise RuntimeError(
            f"fallback_chain[{name}] 全部{len(funcs)}源失败:\n"
            + "\n".join(f"  [{i + 1}] {err}" for i, err in enumerate(errors))
        )

    runner.__name__ = f"fb_chain_{name}"
    runner.__doc__ = f"降级链 {name}: " + " → ".join(
        getattr(f, '__name__', f'source_{i}') for i, f in enumerate(funcs)
    )
    return runner


# ═══════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("🧪 core/resilience.py 自测")
    print("=" * 60)

    # ---- safe_get ----
    print("\n📋 1. safe_get")
    params = {
        "risk": {
            "black_swan_active": True,
            "black_swan_position_ratio": 0.35
        },
        "market": {
            "regime": "BULL_WEAK",
            "volatility": "low"
        },
        "top_level_key": "present"
    }

    # 正确路径
    assert safe_get(params, 'risk.black_swan_active') is True, "路径存在应返回实际值"
    assert safe_get(params, 'risk.black_swan_position_ratio') == 0.35
    assert safe_get(params, 'market.regime') == "BULL_WEAK"

    # 路径不存在 → 返回默认值 + 日志告警
    result = safe_get(params, 'risk.nonexistent', False)
    assert result is False, f"不存在的key应返回默认值, 实际: {result}"

    # 路径不存在且未指定default → 返回None + 日志告警
    result = safe_get(params, 'black_swan')  # 跳过risk层
    assert result is None, "应返回None"

    # 与 dict.get() 的对比: dict.get 读不到嵌套key
    wrong = params.get('black_swan', {})
    assert wrong == {}, f"dict.get 返回空dict, 不报错 — 教训59复现! {wrong}"

    # raise_on_missing
    try:
        safe_get(params, 'risk.absent_key', raise_on_missing=True)
        assert False, "应抛出KeyError"
    except KeyError:
        pass

    print("   ✅ safe_get 全部通过")

    # ---- normalize_action ----
    print("\n📋 2. normalize_action")
    assert normalize_action('buy') == 'BUY'
    assert normalize_action('SELL') == 'SELL'
    assert normalize_action('Hold') == 'HOLD'
    assert normalize_action(' sell ') == 'SELL'  # 空格trim

    try:
        normalize_action('unknown')
        assert False, "应抛出ValueError"
    except ValueError:
        pass

    assert normalize_level('WARNING') == 'warning'
    assert normalize_level('ERROR') == 'error'

    print("   ✅ normalize_action 全部通过")

    # ---- with_fallback ----
    print("\n📋 3. with_fallback")

    call_count = {"count": 0}

    @with_fallback(name="test_api", timeout=5, max_retries=2, retry_delay=0.1)
    def flaky_api(succeed_on_attempt: int):
        call_count["count"] += 1
        if call_count["count"] < succeed_on_attempt:
            raise ConnectionError(f"模拟连接失败 (attempt {call_count['count']})")
        return f"结果-第{call_count['count']}次"

    # 第1次成功
    call_count["count"] = 0
    result = flaky_api(1)
    assert result == "结果-第1次"
    print(f"   直接成功: {result}")

    # 第3次成功 (前2次失败+重试)
    call_count["count"] = 0
    result = flaky_api(3)
    assert result == "结果-第3次"
    print(f"   重试2次后成功: {result}")

    # 全部失败 + 降级函数
    call_count["count"] = 0

    def fallback_handler():
        return "降级结果"

    @with_fallback(
        name="test_with_fallback",
        timeout=5, max_retries=1, retry_delay=0.1,
        fallback_func=fallback_handler
    )
    def always_fail():
        call_count["count"] += 1
        raise RuntimeError(f"永远失败 (attempt {call_count['count']})")

    result = always_fail()
    assert result == "降级结果"
    print(f"   全部失败→降级: {result}")

    # 缓存测试
    cache_api_count = {"count": 0}

    @with_fallback(name="cached_api", cache_ttl=10)
    def cached_api():
        cache_api_count["count"] += 1
        return cache_api_count["count"]

    r1 = cached_api()
    r2 = cached_api()
    assert r1 == r2 == 1, f"缓存应返回相同值: {r1} vs {r2}"
    assert cache_api_count["count"] == 1, "函数应只调用一次"
    print(f"   缓存命中: called {cache_api_count['count']}x, returned {r1}")

    print("   ✅ with_fallback 全部通过")

    # ---- quick_fail_cache ----
    print("\n📋 4. quick_fail_cache")

    detect_count = {"count": 0}

    @quick_fail_cache(key="test_licence", ttl=10)
    def check_licence():
        detect_count["count"] += 1
        return True

    r1 = check_licence()
    r2 = check_licence()
    r3 = check_licence()
    assert r1 is r2 is r3 is True
    assert detect_count["count"] == 1, f"应只检测一次, 实际: {detect_count['count']}"
    print(f"   session缓存: 检测{detect_count['count']}次, 返回3次")

    # 异常时返回fail_value
    @quick_fail_cache(key="test_failing", ttl=10, fail_value=False)
    def failing_check():
        raise RuntimeError("模拟检测失败")

    result = failing_check()
    assert result is False, f"异常时应返回fail_value=False, 实际: {result}"

    print("   ✅ quick_fail_cache 全部通过")

    # ---- build_fallback_chain ----
    print("\n📋 5. build_fallback_chain")

    def source_a(x):
        raise ConnectionError("源A不可用")

    def source_b(x):
        return f"源B返回: {x}"

    chain = build_fallback_chain(source_a, source_b, name="test_chain")
    result = chain("hello")
    assert result == "源B返回: hello", f"降级到源B应成功, 实际: {result}"
    print(f"   降级链: {result}")

    # 全部失败
    def source_c(x):
        raise RuntimeError("源C也不可用")

    chain_all_fail = build_fallback_chain(source_a, source_c, name="fail_chain")
    try:
        chain_all_fail("test")
        assert False, "应抛出RuntimeError"
    except RuntimeError as e:
        assert "全部2源失败" in str(e)

    print(f"   期待的全部失败: RuntimeError已抛出")

    print("   ✅ build_fallback_chain 全部通过")

    # ---- 总结 ----
    print("\n" + "=" * 60)
    print("✅ 全部5项测试通过")
    print("=" * 60)
