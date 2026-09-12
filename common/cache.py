#!/usr/bin/env python3
"""
统一缓存模块 - 本地内存+Redis双级缓存
"""
import time
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Callable
from functools import wraps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FX-4: 缓存来源快照 (source snapshot)
# 写入时附带元数据、读取时校验，避免「来源不明的缓存值」进入回测/交易决策。
# 字段:
#   source         数据来源标识 (如 "akshare:stock_zh_a_spot_em" / "本地CSV")
#   fetch_time     抓取时间 ISO8601 字符串
#   schema_version 载荷结构版本号，与 CACHE_SCHEMA_VERSION 比对
#   adjust         复权方式 ("qfq"|"hfq"|"none"|"unknown")
# 兼容性: 未附带元数据的旧缓存仍可读取，校验状态标记为 "legacy"。
# ---------------------------------------------------------------------------
CACHE_SCHEMA_VERSION = 1
_META_KEY = "__cache_meta__"
REQUIRED_META_FIELDS = ("source", "fetch_time", "schema_version", "adjust")


class CacheMetaError(ValueError):
    """缓存元数据缺失或非法"""


def make_cache_meta(source: str, adjust: str = "unknown",
                    schema_version: int = CACHE_SCHEMA_VERSION,
                    fetch_time: Optional[str] = None) -> Dict[str, Any]:
    """构造缓存来源快照(写入时使用)"""
    return {
        "source": source,
        "fetch_time": fetch_time or datetime.now().isoformat(timespec="seconds"),
        "schema_version": schema_version,
        "adjust": adjust,
    }


def write_file_cache_meta(path: str, source: str, adjust: str = "unknown") -> str:
    """为本地文件缓存写入 sidecar 元数据 `<path>.meta.json` (FX-4)

    返回写入的元数据文件路径。仅新建文件，不修改被缓存的数据文件本身。
    """
    meta = make_cache_meta(source=source, adjust=adjust)
    mp = f"{path}.meta.json"
    with open(mp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return mp

class Cache:
    """双级缓存：本地内存 + Redis"""
    
    def __init__(self, prefix: str = "dsl", 
                 local_expire: int = 3600,
                 redis_expire: int = 86400):
        self.prefix = prefix
        self.local_expire = local_expire
        self.redis_expire = redis_expire
        self._local_cache = {}  # 本地内存缓存
        self._redis = None  # Redis客户端（可选）
    
    def _make_key(self, key: str) -> str:
        """生成缓存key"""
        return f"{self.prefix}:{key}"
    
    def get(self, key: str) -> Optional[Any]:
        """获取缓存值(读取时校验元数据；不合法仅告警，不阻断旧缓存)"""
        return self.get_with_meta(key)[0]

    def get_with_meta(self, key: str):
        """获取缓存，同时返回 (value, meta, status)

        status: "ok" 元数据完整且 schema_version 匹配
                "legacy" 无元数据(旧格式缓存)
                "invalid" 元数据缺字段或版本不匹配
                "miss" 未命中
        """
        # 先查本地缓存
        cache_key = self._make_key(key)
        raw = None
        if cache_key in self._local_cache:
            value, expire_at = self._local_cache[cache_key]
            if expire_at > time.time():
                raw = value
            else:
                del self._local_cache[cache_key]

        # 再查Redis
        if raw is None and self._redis:
            try:
                import redis
                if not self._redis:
                    self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
                redis_value = self._redis.get(cache_key)
                if redis_value:
                    raw = json.loads(redis_value)
                    # 回填本地缓存
                    self._local_cache[cache_key] = (raw, time.time() + self.local_expire)
            except Exception:
                pass

        if raw is None:
            return None, None, "miss"
        # 旧格式：无元数据信封
        if not (isinstance(raw, dict) and _META_KEY in raw):
            logger.warning(f"缓存 {cache_key} 无来源快照(legacy)，建议用 make_cache_meta() 写入")
            return raw, None, "legacy"
        meta = raw.get(_META_KEY) or {}
        missing = [f for f in REQUIRED_META_FIELDS if f not in meta]
        if missing or meta.get("schema_version") != CACHE_SCHEMA_VERSION:
            logger.warning(
                f"缓存 {cache_key} 来源快照不合法: missing={missing} "
                f"schema_version={meta.get('schema_version')} (期望 {CACHE_SCHEMA_VERSION})"
            )
            return raw.get("value"), meta, "invalid"
        return raw.get("value"), meta, "ok"

    def set(self, key: str, value: Any, expire: Optional[int] = None,
            meta: Optional[Dict[str, Any]] = None):
        """设置缓存

        meta: 来源快照(见 make_cache_meta)。提供后读写均校验；
              不提供则按旧格式写入(读取时标记 legacy)。
        """
        cache_key = self._make_key(key)
        expire_at = time.time() + (expire or self.local_expire)
        payload = value
        if meta is not None:
            missing = [f for f in REQUIRED_META_FIELDS if f not in meta]
            if missing:
                raise CacheMetaError(f"缓存 {key} 来源快照缺少字段: {missing}")
            payload = {_META_KEY: dict(meta), "value": value}

        # 写入本地缓存
        self._local_cache[cache_key] = (payload, expire_at)

        # 写入Redis
        if self._redis:
            try:
                import redis
                if not self._redis:
                    self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
                self._redis.setex(cache_key, expire or self.redis_expire, json.dumps(payload))
            except Exception:
                pass
    
    def delete(self, key: str):
        """删除缓存"""
        cache_key = self._make_key(key)
        if cache_key in self._local_cache:
            del self._local_cache[cache_key]
        if self._redis:
            try:
                if not self._redis:
                    import redis
                    self._redis = redis.Redis(host='localhost', port=6379, db=0)
                self._redis.delete(cache_key)
            except:
                pass
    
    def exists(self, key: str) -> bool:
        """检查缓存是否存在"""
        return self.get(key) is not None
    
    def cacheable(self, expire: int = 3600):
        """装饰器：自动缓存函数返回结果"""
        def decorator(func: Callable):
            @wraps(func)
            def wrapper(*args, **kwargs):
                # 生成缓存key
                key_args = f"{func.__name__}:{str(args)}:{str(kwargs)}"
                cache_key = hashlib.md5(key_args.encode()).hexdigest()
                
                # 尝试从缓存获取
                cached = self.get(cache_key)
                if cached is not None:
                    return cached

                # 执行函数并缓存结果(附带来源快照)
                result = func(*args, **kwargs)
                self.set(cache_key, result, expire, meta=make_cache_meta(
                    source=f"{func.__module__}.{func.__name__}", adjust="unknown"))
                return result
            return wrapper
        return decorator

# 全局缓存实例
cache = Cache()

if __name__ == "__main__":
    # 测试
    c = Cache()
    c.set("test_key", {"data": "hello"}, 60)
    print(c.get("test_key"))
    print(c.exists("test_key"))
    c.delete("test_key")
    print(c.get("test_key"))