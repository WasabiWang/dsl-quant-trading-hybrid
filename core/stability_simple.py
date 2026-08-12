#!/usr/bin/env python3
"""
DSL稳定性系统 - 简化版

核心功能：
1. 熔断保护
2. 自动重试
3. 健康监控
"""

import time
import threading
import logging
from datetime import datetime
from typing import Dict, Any, Optional, Callable
from collections import deque
from enum import Enum

logger = logging.getLogger(__name__)

class HealthStatus(Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"

class CircuitBreaker:
    """简化熔断器"""
    
    def __init__(self, name: str, threshold: int = 3, timeout: int = 30):
        self.name = name
        self.threshold = threshold
        self.timeout = timeout
        
        self.state = "CLOSED"  # CLOSED, OPEN
        self.failures = 0
        self.last_failure = 0
        self.lock = threading.RLock()
    
    def allow_request(self) -> bool:
        """是否允许请求"""
        with self.lock:
            if self.state == "OPEN":
                # 检查是否超时
                if time.time() - self.last_failure > self.timeout:
                    self.state = "CLOSED"
                    self.failures = 0
                    logger.info(f"熔断器 [{self.name}] 超时恢复")
                    return True
                return False
            return True
    
    def record_failure(self):
        """记录失败"""
        with self.lock:
            self.failures += 1
            self.last_failure = time.time()
            
            if self.failures >= self.threshold:
                self.state = "OPEN"
                logger.warning(f"熔断器 [{self.name}] 触发熔断")
    
    def record_success(self):
        """记录成功"""
        with self.lock:
            if self.state == "CLOSED" and self.failures > 0:
                self.failures = max(0, self.failures - 1)
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        with self.lock:
            return {
                "name": self.name,
                "state": self.state,
                "failures": self.failures,
                "last_failure": self.last_failure,
                "threshold": self.threshold,
                "timeout": self.timeout
            }
    
    def reset(self):
        """重置熔断器"""
        with self.lock:
            self.state = "CLOSED"
            self.failures = 0
            logger.info(f"熔断器 [{self.name}] 已重置")

class StabilitySystem:
    """简化稳定性系统"""
    
    def __init__(self):
        # 熔断器
        self.circuit_breakers = {
            "data_fetch": CircuitBreaker("data_fetch", threshold=3, timeout=30),
            "network": CircuitBreaker("network", threshold=5, timeout=60),
        }
        
        # 错误记录
        self.errors = deque(maxlen=100)
        
        # 健康状态
        self.health = HealthStatus.GREEN
        
        logger.info("稳定性系统初始化完成")
    
    def execute(self, 
                func: Callable, 
                module: str,
                operation: str,
                fallback_func: Optional[Callable] = None,
                max_retries: int = 2) -> Any:
        """
        执行函数，带稳定性保障
        
        Args:
            func: 主函数
            module: 模块名称
            operation: 操作描述
            fallback_func: 降级函数
            max_retries: 最大重试次数
        """
        # 检查熔断器
        circuit_breaker = self.circuit_breakers.get(module)
        if circuit_breaker and not circuit_breaker.allow_request():
            logger.warning(f"模块 {module} 已熔断: {operation}")
            
            if fallback_func:
                try:
                    return fallback_func()
                except Exception as e:
                    logger.error(f"降级函数失败: {e}")
            
            raise Exception(f"模块 {module} 已熔断: {operation}")
        
        # 带重试的执行
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                result = func()
                
                # 记录成功
                if circuit_breaker:
                    circuit_breaker.record_success()
                
                if attempt > 0:
                    logger.info(f"重试成功 (第{attempt}次): {operation}")
                
                return result
                
            except Exception as e:
                last_error = e
                
                # 记录错误
                self.errors.append({
                    "timestamp": time.time(),
                    "module": module,
                    "operation": operation,
                    "error": str(e)
                })
                
                # 更新熔断器
                if circuit_breaker:
                    circuit_breaker.record_failure()
                
                if attempt < max_retries:
                    # 指数退避
                    delay = 2 ** attempt
                    logger.warning(f"执行失败，{delay}秒后重试 (第{attempt + 1}次): {operation}")
                    time.sleep(delay)
                else:
                    logger.error(f"所有重试均失败: {operation}")
        
        # 所有重试失败，尝试降级
        if fallback_func:
            logger.warning(f"使用降级函数: {operation}")
            try:
                return fallback_func()
            except Exception as e:
                logger.error(f"降级函数也失败: {e}")
        
        # 无降级函数或降级失败，抛出异常
        if last_error:
            raise last_error
    
    def get_status(self) -> Dict[str, Any]:
        """获取系统状态"""
        # 更新健康状态
        self._update_health()
        
        return {
            "health": self.health.value,
            "circuit_breakers": {
                name: cb.get_status()
                for name, cb in self.circuit_breakers.items()
            },
            "error_count_last_hour": len([
                e for e in self.errors
                if time.time() - e["timestamp"] <= 3600
            ]),
            "timestamp": datetime.now().isoformat()
        }
    
    def _update_health(self):
        """更新健康状态"""
        # 检查熔断器状态
        open_circuits = 0
        for cb in self.circuit_breakers.values():
            if cb.state == "OPEN":
                open_circuits += 1
        
        # 检查错误频率
        one_hour_ago = time.time() - 3600
        recent_errors = [e for e in self.errors if e["timestamp"] > one_hour_ago]
        
        # 判断健康状态
        if open_circuits >= 2:
            new_health = HealthStatus.RED
        elif open_circuits >= 1 or len(recent_errors) > 10:
            new_health = HealthStatus.YELLOW
        else:
            new_health = HealthStatus.GREEN
        
        # 记录状态变化
        if new_health != self.health:
            old_health = self.health
            self.health = new_health
            logger.info(f"健康状态变化: {old_health.value} → {new_health.value}")
    
    def reset_circuit_breaker(self, module: str):
        """重置熔断器"""
        if module in self.circuit_breakers:
            self.circuit_breakers[module].reset()
            logger.info(f"熔断器 {module} 已重置")

# 全局实例
stability_system = StabilitySystem()

# 测试函数
def test_stability_system():
    """测试稳定性系统"""
    print("🧪 测试稳定性系统")
    print("=" * 60)
    
    # 模拟失败函数
    call_count = 0
    
    def failing_function():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError(f"模拟失败 (第{call_count}次)")
        return f"成功 (第{call_count}次)"
    
    def fallback_function():
        return "降级模式: 使用缓存"
    
    print("\n🔍 测试带熔断和重试的执行:")
    try:
        result = stability_system.execute(
            failing_function,
            module="network",
            operation="测试网络调用",
            fallback_func=fallback_function,
            max_retries=3
        )
        print(f"  ✅ 执行结果: {result}")
    except Exception as e:
        print(f"  ❌ 执行失败: {e}")
    
    print("\n📊 测试系统状态:")
    status = stability_system.get_status()
    print(f"  健康状态: {status['health']}")
    print(f"  熔断器状态:")
    for name, cb_status in status['circuit_breakers'].items():
        print(f"    {name}: {cb_status['state']}")
    
    print("\n🎯 稳定性系统测试完成")

if __name__ == "__main__":
    test_stability_system()