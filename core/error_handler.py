#!/usr/bin/env python3
"""
DSL Data SDK 错误处理增强模块
提供错误分类、统计和智能恢复功能
"""

import time
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Callable
from enum import Enum
from dataclasses import dataclass, asdict
from pathlib import Path

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ErrorCategory(Enum):
    """错误分类枚举"""
    NETWORK = "network"           # 网络错误
    TIMEOUT = "timeout"           # 超时错误
    DATA_FORMAT = "data_format"   # 数据格式错误
    API_LIMIT = "api_limit"       # API限制错误
    AUTH = "auth"                 # 认证错误
    CACHE = "cache"               # 缓存错误
    VALIDATION = "validation"     # 数据验证错误
    UNKNOWN = "unknown"           # 未知错误

class ErrorSeverity(Enum):
    """错误严重程度"""
    LOW = "low"       # 低 - 可自动恢复
    MEDIUM = "medium" # 中 - 需要关注
    HIGH = "high"     # 高 - 需要立即处理
    CRITICAL = "critical"  # 严重 - 系统不可用

@dataclass
class ErrorRecord:
    """错误记录数据结构"""
    timestamp: str
    category: str
    severity: str
    module: str
    operation: str
    error_message: str
    error_type: str
    stack_trace: Optional[str] = None
    context: Optional[Dict[str, Any]] = None
    recovery_action: Optional[str] = None
    resolved: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

class ErrorStatistics:
    """错误统计类"""
    
    def __init__(self, window_hours: int = 24):
        """
        初始化错误统计
        
        Args:
            window_hours: 统计时间窗口(小时)
        """
        self.window_hours = window_hours
        self.errors: List[ErrorRecord] = []
        self.stats_file = Path(__file__).parent.parent / "data" / "error_stats.json"
        self.stats_file.parent.mkdir(parents=True, exist_ok=True)
        
        # 加载历史统计
        self.load_stats()
    
    def add_error(self, error: ErrorRecord) -> None:
        """添加错误记录"""
        self.errors.append(error)
        self._clean_old_errors()
        self.save_stats()
    
    def _clean_old_errors(self) -> None:
        """清理过期错误记录"""
        cutoff_time = datetime.now() - timedelta(hours=self.window_hours)
        cutoff_str = cutoff_time.strftime("%Y-%m-%d %H:%M:%S")
        
        self.errors = [
            error for error in self.errors 
            if error.timestamp >= cutoff_str
        ]
    
    def get_category_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取按分类统计的错误数据"""
        stats = {}
        
        for category in ErrorCategory:
            category_errors = [
                e for e in self.errors 
                if e.category == category.value
            ]
            
            if category_errors:
                severity_counts = {
                    severity.value: len([
                        e for e in category_errors 
                        if e.severity == severity.value
                    ])
                    for severity in ErrorSeverity
                }
                
                recent_errors = sorted(
                    category_errors,
                    key=lambda x: x.timestamp,
                    reverse=True
                )[:5]
                
                stats[category.value] = {
                    "total": len(category_errors),
                    "severity_counts": severity_counts,
                    "recent_errors": [e.to_dict() for e in recent_errors],
                    "error_rate": self._calculate_error_rate(category_errors),
                    "avg_resolution_time": self._calculate_avg_resolution_time(category_errors)
                }
        
        return stats
    
    def get_module_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取按模块统计的错误数据"""
        modules = set(e.module for e in self.errors)
        stats = {}
        
        for module in modules:
            module_errors = [e for e in self.errors if e.module == module]
            
            if module_errors:
                category_counts = {}
                for error in module_errors:
                    category_counts[error.category] = category_counts.get(error.category, 0) + 1
                
                stats[module] = {
                    "total": len(module_errors),
                    "category_counts": category_counts,
                    "error_rate": self._calculate_error_rate(module_errors),
                    "most_common_error": self._get_most_common_error(module_errors)
                }
        
        return stats
    
    def get_time_series_stats(self, interval_minutes: int = 60) -> List[Dict[str, Any]]:
        """获取时间序列统计"""
        if not self.errors:
            return []
        
        # 按时间间隔分组
        errors_by_interval = {}
        for error in self.errors:
            dt = datetime.strptime(error.timestamp, "%Y-%m-%d %H:%M:%S")
            interval_key = dt.strftime(f"%Y-%m-%d %H:{dt.minute // interval_minutes * interval_minutes:02d}")
            
            if interval_key not in errors_by_interval:
                errors_by_interval[interval_key] = []
            errors_by_interval[interval_key].append(error)
        
        # 转换为时间序列数据
        time_series = []
        for interval, errors in sorted(errors_by_interval.items()):
            severity_counts = {s.value: 0 for s in ErrorSeverity}
            category_counts = {c.value: 0 for c in ErrorCategory}
            
            for error in errors:
                severity_counts[error.severity] += 1
                category_counts[error.category] = category_counts.get(error.category, 0) + 1
            
            time_series.append({
                "timestamp": interval,
                "total_errors": len(errors),
                "severity_counts": severity_counts,
                "category_counts": category_counts,
                "error_rate": len(errors) / (interval_minutes * 60)  # 错误/秒
            })
        
        return time_series
    
    def _calculate_error_rate(self, errors: List[ErrorRecord]) -> float:
        """计算错误率"""
        if not errors:
            return 0.0
        
        # 假设每个错误对应一次操作
        total_operations = len(errors) * 10  # 估算总操作数
        if total_operations == 0:
            return 0.0
        
        return len(errors) / total_operations
    
    def _calculate_avg_resolution_time(self, errors: List[ErrorRecord]) -> Optional[float]:
        """计算平均解决时间(秒)"""
        resolved_errors = [e for e in errors if e.resolved]
        if not resolved_errors:
            return None
        
        # 这里简化处理，实际应该记录解决时间
        return 300.0  # 默认5分钟
    
    def _get_most_common_error(self, errors: List[ErrorRecord]) -> Optional[Dict[str, Any]]:
        """获取最常见的错误"""
        if not errors:
            return None
        
        error_counts = {}
        for error in errors:
            key = (error.error_type, error.error_message[:100])
            error_counts[key] = error_counts.get(key, 0) + 1
        
        if not error_counts:
            return None
        
        most_common = max(error_counts.items(), key=lambda x: x[1])
        error_type, error_msg = most_common[0]
        
        return {
            "error_type": error_type,
            "error_message": error_msg,
            "count": most_common[1],
            "frequency": most_common[1] / len(errors)
        }
    
    def save_stats(self) -> None:
        """保存统计到文件"""
        try:
            data = {
                "errors": [e.to_dict() for e in self.errors],
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total_errors": len(self.errors)
            }
            
            with open(self.stats_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.info(f"错误统计已保存到 {self.stats_file}")
        except Exception as e:
            logger.error(f"保存错误统计失败: {e}")
    
    def load_stats(self) -> None:
        """从文件加载统计"""
        try:
            if self.stats_file.exists():
                with open(self.stats_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # 转换回ErrorRecord对象
                self.errors = []
                for error_dict in data.get("errors", []):
                    error = ErrorRecord(
                        timestamp=error_dict.get("timestamp"),
                        category=error_dict.get("category"),
                        severity=error_dict.get("severity"),
                        module=error_dict.get("module"),
                        operation=error_dict.get("operation"),
                        error_message=error_dict.get("error_message"),
                        error_type=error_dict.get("error_type"),
                        stack_trace=error_dict.get("stack_trace"),
                        context=error_dict.get("context"),
                        recovery_action=error_dict.get("recovery_action"),
                        resolved=error_dict.get("resolved", False)
                    )
                    self.errors.append(error)
                
                logger.info(f"从 {self.stats_file} 加载了 {len(self.errors)} 条错误记录")
        except Exception as e:
            logger.error(f"加载错误统计失败: {e}")
            self.errors = []
    
    def generate_report(self) -> Dict[str, Any]:
        """生成错误统计报告"""
        category_stats = self.get_category_stats()
        module_stats = self.get_module_stats()
        time_series = self.get_time_series_stats()
        
        # 计算总体指标
        total_errors = len(self.errors)
        unresolved_errors = len([e for e in self.errors if not e.resolved])
        
        severity_distribution = {
            severity.value: len([e for e in self.errors if e.severity == severity.value])
            for severity in ErrorSeverity
        }
        
        category_distribution = {
            category.value: len([e for e in self.errors if e.category == category.value])
            for category in ErrorCategory
        }
        
        report = {
            "summary": {
                "total_errors": total_errors,
                "unresolved_errors": unresolved_errors,
                "resolution_rate": (total_errors - unresolved_errors) / total_errors if total_errors > 0 else 1.0,
                "time_window_hours": self.window_hours,
                "report_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "severity_distribution": severity_distribution,
            "category_distribution": category_distribution,
            "category_details": category_stats,
            "module_details": module_stats,
            "time_series": time_series,
            "recommendations": self._generate_recommendations(category_stats, module_stats)
        }
        
        return report
    
    def _generate_recommendations(self, category_stats: Dict, module_stats: Dict) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        # 检查网络错误
        if "network" in category_stats:
            net_errors = category_stats["network"]
            if net_errors["total"] > 10:
                recommendations.append(
                    "网络错误频繁，建议检查网络连接稳定性，增加重试次数和超时时间"
                )
        
        # 检查超时错误
        if "timeout" in category_stats:
            timeout_errors = category_stats["timeout"]
            if timeout_errors["total"] > 5:
                recommendations.append(
                    "超时错误较多，建议优化数据源响应时间，增加连接池大小"
                )
        
        # 检查API限制错误
        if "api_limit" in category_stats:
            api_errors = category_stats["api_limit"]
            if api_errors["total"] > 3:
                recommendations.append(
                    "API限制错误，建议优化请求频率，考虑使用多个API Key轮询"
                )
        
        # 检查数据获取模块
        if "data_fetch" in module_stats:
            data_errors = module_stats["data_fetch"]
            if data_errors["total"] > 20:
                recommendations.append(
                    "数据获取模块错误较多，建议增强熔断保护和降级策略"
                )
        
        # 检查缓存模块
        if "cache" in module_stats:
            cache_errors = module_stats["cache"]
            if cache_errors["total"] > 5:
                recommendations.append(
                    "缓存模块错误，建议检查缓存存储和清理机制"
                )
        
        # 通用建议
        if len(self.errors) > 50:
            recommendations.append(
                "总体错误数较多，建议进行系统性的错误处理和监控优化"
            )
        
        if not recommendations:
            recommendations.append("系统运行正常，继续保持当前配置")
        
        return recommendations

class ErrorHandler:
    """错误处理器"""
    
    def __init__(self, statistics: Optional[ErrorStatistics] = None):
        self.statistics = statistics or ErrorStatistics()
        self.recovery_strategies = self._init_recovery_strategies()
    
    def _init_recovery_strategies(self) -> Dict[str, Callable]:
        """初始化恢复策略"""
        return {
            ErrorCategory.NETWORK.value: self._recover_network_error,
            ErrorCategory.TIMEOUT.value: self._recover_timeout_error,
            ErrorCategory.API_LIMIT.value: self._recover_api_limit_error,
            ErrorCategory.CACHE.value: self._recover_cache_error,
        }
    
    def handle_error(
        self,
        error: Exception,
        module: str,
        operation: str,
        context: Optional[Dict[str, Any]] = None
    ) -> ErrorRecord:
        """
        处理错误
        
        Args:
            error: 异常对象
            module: 模块名称
            operation: 操作名称
            context: 上下文信息
            
        Returns:
            ErrorRecord: 错误记录
        """
        # 分类错误
        category, severity = self._classify_error(error)
        
        # 创建错误记录
        error_record = ErrorRecord(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            category=category.value,
            severity=severity.value,
            module=module,
            operation=operation,
            error_message=str(error),
            error_type=type(error).__name__,
            stack_trace=self._get_stack_trace(error),
            context=context
        )
        
        # 尝试恢复
        recovery_action = self._try_recover(error_record)
        error_record.recovery_action = recovery_action
        
        # 记录错误
        self.statistics.add_error(error_record)
        
        # 记录日志
        log_message = f"{module}.{operation} - {category.value}({severity.value}): {error}"
        if severity == ErrorSeverity.CRITICAL:
            logger.critical(log_message)
        elif severity == ErrorSeverity.HIGH:
            logger.error(log_message)
        elif severity == ErrorSeverity.MEDIUM:
            logger.warning(log_message)
        else:
            logger.info(log_message)
        
        return error_record
    
    def _classify_error(self, error: Exception) -> tuple:
        """分类错误"""
        error_str = str(error).lower()
        error_type = type(error).__name__
        
        # 网络错误
        if any(keyword in error_str for keyword in ["connection", "network", "socket", "refused"]):
            return ErrorCategory.NETWORK, ErrorSeverity.HIGH
        
        # 超时错误
        if any(keyword in error_str for keyword in ["timeout", "timed out"]):
            return ErrorCategory.TIMEOUT, ErrorSeverity.MEDIUM
        
        # API限制错误
        if any(keyword in error_str for keyword in ["limit", "quota", "rate limit", "429"]):
            return ErrorCategory.API_LIMIT, ErrorSeverity.MEDIUM
        
        # 认证错误
        if any(keyword in error_str for keyword in ["auth", "unauthorized", "403", "401"]):
            return ErrorCategory.AUTH, ErrorSeverity.HIGH
        
        # 数据格式错误
        if any(keyword in error_str for keyword in ["json", "format", "parse", "decode"]):
            return ErrorCategory.DATA_FORMAT, ErrorSeverity.MEDIUM
        
        # 缓存错误
        if any(keyword in error_str for keyword in ["cache", "storage", "disk"]):
            return ErrorCategory.CACHE, ErrorSeverity.LOW
        
        # 默认分类
        return ErrorCategory.UNKNOWN, ErrorSeverity.MEDIUM
    
    def _get_stack_trace(self, error: Exception) -> Optional[str]:
        """获取堆栈跟踪"""
        import traceback
        try:
            return ''.join(traceback.format_exception(type(error), error, error.__traceback__))
        except Exception:
            return None
    
    def _try_recover(self, error_record: ErrorRecord) -> Optional[str]:
        """尝试恢复错误"""
        recovery_func = self.recovery_strategies.get(error_record.category)
        if recovery_func:
            try:
                return recovery_func(error_record)
            except Exception as e:
                logger.error(f"恢复策略执行失败: {e}")
        
        return None
    
    def _recover_network_error(self, error_record: ErrorRecord) -> str:
        """恢复网络错误"""
        # 简单的网络错误恢复策略
        return "等待1秒后重试，最多重试3次"
    
    def _recover_timeout_error(self, error_record: ErrorRecord) -> str:
        """恢复超时错误"""
        return "增加超时时间到10秒，最多重试2次"
    
    def _recover_api_limit_error(self, error_record: ErrorRecord) -> str:
        """恢复API限制错误"""
        return "等待60秒后重试，切换到备用数据源"
    
    def _recover_cache_error(self, error_record: ErrorRecord) -> str:
        """恢复缓存错误"""
        return "清理缓存文件，使用直接获取模式"

class SmartErrorRecovery:
    """智能错误恢复"""
    
    def __init__(self, error_handler: ErrorHandler):
        self.error_handler = error_handler
        self.recovery_history: List[Dict[str, Any]] = []
    
    def attempt_recovery(
        self,
        func: Callable,
        module: str,
        operation: str,
        max_attempts: int = 3,
        backoff_factor: float = 1.5
    ) -> Any:
        """
        尝试智能恢复
        
        Args:
            func: 要执行的函数
            module: 模块名称
            operation: 操作名称
            max_attempts: 最大尝试次数
            backoff_factor: 退避因子
            
        Returns:
            函数执行结果
            
        Raises:
            Exception: 所有尝试都失败后抛出异常
        """
        last_error = None
        
        for attempt in range(max_attempts):
            try:
                result = func()
                
                if last_error:
                    self._record_recovery_success(last_error)
                
                return result
                
            except Exception as e:
                last_error = self.error_handler.handle_error(e, module, operation)
                
                if attempt == max_attempts - 1:
                    self._record_recovery_failure(last_error)
                    raise
                
                wait_time = backoff_factor ** attempt
                logger.info(f"尝试 {attempt + 1}/{max_attempts} 失败，等待 {wait_time:.1f} 秒后重试")
                time.sleep(wait_time)
    
    def _record_recovery_success(self, error_record: ErrorRecord) -> None:
        """记录恢复成功"""
        recovery_record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error_id": id(error_record),
            "module": error_record.module,
            "operation": error_record.operation,
            "recovery_strategy": error_record.recovery_action,
            "status": "success"
        }
        self.recovery_history.append(recovery_record)
    
    def _record_recovery_failure(self, error_record: ErrorRecord) -> None:
        """记录恢复失败"""
        recovery_record = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error_id": id(error_record),
            "module": error_record.module,
            "operation": error_record.operation,
            "recovery_strategy": error_record.recovery_action,
            "status": "failure"
        }
        self.recovery_history.append(recovery_record)
    
    def get_recovery_stats(self) -> Dict[str, Any]:
        """获取恢复统计"""
        total = len(self.recovery_history)
        successful = len([r for r in self.recovery_history if r["status"] == "success"])
        
        return {
            "total_recovery_attempts": total,
            "successful_recoveries": successful,
            "recovery_rate": successful / total if total > 0 else 0.0,
            "recent_recoveries": self.recovery_history[-10:] if self.recovery_history else []
        }

# 全局错误处理器实例
_error_handler = ErrorHandler()
_error_statistics = ErrorStatistics()
_smart_recovery = SmartErrorRecovery(_error_handler)

def handle_error(error: Exception, module: str, operation: str, context: Optional[Dict] = None) -> ErrorRecord:
    """全局错误处理函数"""
    return _error_handler.handle_error(error, module, operation, context)

def get_error_statistics() -> ErrorStatistics:
    """获取错误统计"""
    return _error_statistics

def get_error_report() -> Dict[str, Any]:
    """获取错误报告"""
    return _error_statistics.generate_report()

def smart_recovery(func: Callable, module: str, operation: str, **kwargs) -> Any:
    """智能恢复装饰器"""
    return _smart_recovery.attempt_recovery(func, module, operation, **kwargs)

def save_error_stats() -> None:
    """保存错误统计"""
    _error_statistics.save_stats()

# 使用示例
if __name__ == "__main__":
    # 测试错误处理
    def test_function():
        raise ConnectionError("网络连接失败")
    
    try:
        result = smart_recovery(
            test_function,
            module="test_module",
            operation="test_operation",
            max_attempts=2
        )
    except Exception as e:
        print(f"最终失败: {e}")
    
    # 生成报告
    report = get_error_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))