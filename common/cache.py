#!/usr/bin/env python3
"""
统一缓存模块 - 本地内存+Redis双级缓存
"""
import time
import hashlib
import json
from typing import Any, Optional, Callable
from functools import wraps

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
        """获取缓存"""
        # 先查本地缓存
        cache_key = self._make_key(key)
        if cache_key in self._local_cache:
            value, expire_at = self._local_cache[cache_key]
            if expire_at > time.time():
                return value
            else:
                del self._local_cache[cache_key]
        
        # 再查Redis
        if self._redis:
            try:
                import redis
                if not self._redis:
                    self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
                redis_value = self._redis.get(cache_key)
                if redis_value:
                    value = json.loads(redis_value)
                    # 回填本地缓存
                    self._local_cache[cache_key] = (value, time.time() + self.local_expire)
                    return value
            except:
                pass
        
        return None
    
    def set(self, key: str, value: Any, expire: Optional[int] = None):
        """设置缓存"""
        cache_key = self._make_key(key)
        expire_at = time.time() + (expire or self.local_expire)
        
        # 写入本地缓存
        self._local_cache[cache_key] = (value, expire_at)
        
        # 写入Redis
        if self._redis:
            try:
                import redis
                if not self._redis:
                    self._redis = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
                self._redis.setex(cache_key, expire or self.redis_expire, json.dumps(value))
            except:
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
                
                # 执行函数并缓存结果
                result = func(*args, **kwargs)
                self.set(cache_key, result, expire)
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