#!/usr/bin/env python3
"""
DSL v4.5.9 日志配置模块

功能：
1. 统一日志配置
2. 结构化日志输出
3. 日志轮转和归档
4. 日志分析和监控

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import sys
import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import colorlog

class DSLFormatter(logging.Formatter):
    """DSL自定义日志格式化器"""
    
    # 颜色配置
    COLORS = {
        'DEBUG': 'cyan',
        'INFO': 'green',
        'WARNING': 'yellow',
        'ERROR': 'red',
        'CRITICAL': 'bold_red',
    }
    
    def __init__(self, use_color: bool = True):
        """初始化格式化器"""
        if use_color:
            format_str = '%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            formatter = colorlog.ColoredFormatter(
                format_str,
                datefmt='%Y-%m-%d %H:%M:%S',
                log_colors=self.COLORS
            )
        else:
            format_str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            formatter = logging.Formatter(
                format_str,
                datefmt='%Y-%m-%d %H:%M:%S'
            )
        
        super().__init__()
        self.formatter = formatter
    
    def format(self, record):
        """格式化日志记录"""
        # 添加额外字段
        if not hasattr(record, 'module'):
            record.module = record.name
        
        if not hasattr(record, 'function'):
            record.function = record.funcName if hasattr(record, 'funcName') else 'unknown'
        
        # 处理异常信息
        if record.exc_info:
            # 格式化异常信息
            import traceback
            exc_text = self.formatter.formatException(record.exc_info)
            record.exc_text = exc_text
        else:
            record.exc_text = ''
        
        return self.formatter.format(record)

class StructuredFormatter(logging.Formatter):
    """结构化日志格式化器（JSON格式）"""
    
    def format(self, record):
        """格式化为JSON"""
        log_entry = {
            'timestamp': datetime.now().isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'module': getattr(record, 'module', 'unknown'),
            'function': getattr(record, 'function', 'unknown'),
            'message': record.getMessage(),
            'thread': record.threadName,
            'process': record.processName,
        }
        
        # 添加额外字段
        if hasattr(record, 'extra_fields'):
            log_entry.update(record.extra_fields)
        
        # 异常信息
        if record.exc_info:
            import traceback
            log_entry['exception'] = {
                'type': record.exc_info[0].__name__,
                'message': str(record.exc_info[1]),
                'traceback': traceback.format_exception(*record.exc_info)
            }
        
        # 栈信息
        if hasattr(record, 'stack_info') and record.stack_info:
            log_entry['stack_info'] = record.stack_info
        
        return json.dumps(log_entry, ensure_ascii=False)

class DSLLogger:
    """DSL日志管理器"""
    
    def __init__(self, 
                 name: str = 'dsl',
                 log_dir: str = 'logs',
                 level: str = 'INFO',
                 enable_file_log: bool = True,
                 enable_console_log: bool = True,
                 max_file_size_mb: int = 100,
                 backup_count: int = 10):
        """
        初始化日志管理器
        
        Args:
            name: 日志名称
            log_dir: 日志目录
            level: 日志级别
            enable_file_log: 是否启用文件日志
            enable_console_log: 是否启用控制台日志
            max_file_size_mb: 单个日志文件最大大小（MB）
            backup_count: 备份文件数量
        """
        self.name = name
        self.log_dir = Path(log_dir)
        self.level = getattr(logging, level.upper())
        
        # 创建日志目录
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 配置日志
        self.logger = logging.getLogger(name)
        self.logger.setLevel(self.level)
        self.logger.propagate = False
        
        # 移除现有处理器
        self.logger.handlers.clear()
        
        # 添加处理器
        if enable_console_log:
            self._add_console_handler()
        
        if enable_file_log:
            self._add_file_handler(max_file_size_mb, backup_count)
            self._add_structured_file_handler(max_file_size_mb, backup_count)
        
        # 添加错误处理器
        self._add_error_handler()
    
    def _add_console_handler(self):
        """添加控制台处理器"""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.level)
        console_handler.setFormatter(DSLFormatter(use_color=True))
        self.logger.addHandler(console_handler)
    
    def _add_file_handler(self, max_file_size_mb: int, backup_count: int):
        """添加文件处理器"""
        log_file = self.log_dir / f'{self.name}.log'
        
        file_handler = logging.handlers.RotatingFileHandler(
            filename=log_file,
            maxBytes=max_file_size_mb * 1024 * 1024,  # 转换为字节
            backupCount=backup_count,
            encoding='utf-8'
        )
        
        file_handler.setLevel(self.level)
        file_handler.setFormatter(DSLFormatter(use_color=False))
        self.logger.addHandler(file_handler)
    
    def _add_structured_file_handler(self, max_file_size_mb: int, backup_count: int):
        """添加结构化文件处理器（JSON格式）"""
        json_log_file = self.log_dir / f'{self.name}_structured.json'
        
        json_handler = logging.handlers.RotatingFileHandler(
            filename=json_log_file,
            maxBytes=max_file_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding='utf-8'
        )
        
        json_handler.setLevel(self.level)
        json_handler.setFormatter(StructuredFormatter())
        self.logger.addHandler(json_handler)
    
    def _add_error_handler(self):
        """添加错误处理器（专门处理ERROR及以上级别）"""
        error_log_file = self.log_dir / f'{self.name}_error.log'
        
        error_handler = logging.handlers.RotatingFileHandler(
            filename=error_log_file,
            maxBytes=50 * 1024 * 1024,  # 50MB
            backupCount=5,
            encoding='utf-8'
        )
        
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(DSLFormatter(use_color=False))
        
        # 添加过滤器，只处理ERROR及以上级别
        error_handler.addFilter(lambda record: record.levelno >= logging.ERROR)
        self.logger.addHandler(error_handler)
    
    def get_logger(self) -> logging.Logger:
        """获取日志记录器"""
        return self.logger
    
    def log_with_context(self, 
                        level: str,
                        message: str,
                        extra_fields: Optional[Dict[str, Any]] = None,
                        **kwargs):
        """
        记录带上下文的日志
        
        Args:
            level: 日志级别
            message: 日志消息
            extra_fields: 额外字段
            **kwargs: 其他参数
        """
        log_method = getattr(self.logger, level.lower())
        
        # 创建日志记录
        if extra_fields:
            # 使用extra参数传递额外字段
            record = self.logger.makeRecord(
                name=self.logger.name,
                level=getattr(logging, level.upper()),
                fn='',
                lno=0,
                msg=message,
                args=(),
                exc_info=None,
                extra={'extra_fields': extra_fields}
            )
            self.logger.handle(record)
        else:
            log_method(message, **kwargs)
    
    def debug_with_context(self, message: str, extra_fields: Optional[Dict[str, Any]] = None):
        """记录DEBUG级别带上下文的日志"""
        self.log_with_context('DEBUG', message, extra_fields)
    
    def info_with_context(self, message: str, extra_fields: Optional[Dict[str, Any]] = None):
        """记录INFO级别带上下文的日志"""
        self.log_with_context('INFO', message, extra_fields)
    
    def warning_with_context(self, message: str, extra_fields: Optional[Dict[str, Any]] = None):
        """记录WARNING级别带上下文的日志"""
        self.log_with_context('WARNING', message, extra_fields)
    
    def error_with_context(self, message: str, extra_fields: Optional[Dict[str, Any]] = None):
        """记录ERROR级别带上下文的日志"""
        self.log_with_context('ERROR', message, extra_fields)
    
    def critical_with_context(self, message: str, extra_fields: Optional[Dict[str, Any]] = None):
        """记录CRITICAL级别带上下文的日志"""
        self.log_with_context('CRITICAL', message, extra_fields)
    
    def log_performance(self, 
                       operation: str,
                       execution_time: float,
                       memory_usage: Optional[float] = None,
                       additional_info: Optional[Dict[str, Any]] = None):
        """
        记录性能日志
        
        Args:
            operation: 操作名称
            execution_time: 执行时间（秒）
            memory_usage: 内存使用（MB）
            additional_info: 额外信息
        """
        extra_fields = {
            'log_type': 'performance',
            'operation': operation,
            'execution_time_seconds': execution_time,
            'timestamp': datetime.now().isoformat()
        }
        
        if memory_usage is not None:
            extra_fields['memory_usage_mb'] = memory_usage
        
        if additional_info:
            extra_fields.update(additional_info)
        
        # 根据执行时间选择日志级别
        if execution_time > 10.0:
            level = 'ERROR'
        elif execution_time > 5.0:
            level = 'WARNING'
        elif execution_time > 2.0:
            level = 'INFO'
        else:
            level = 'DEBUG'
        
        message = f"性能日志: {operation} 耗时 {execution_time:.3f}秒"
        if memory_usage:
            message += f", 内存使用 {memory_usage:.2f}MB"
        
        self.log_with_context(level, message, extra_fields)
    
    def log_operation(self,
                     operation: str,
                     status: str,
                     details: Optional[Dict[str, Any]] = None):
        """
        记录操作日志
        
        Args:
            operation: 操作名称
            status: 操作状态（success, failed, warning）
            details: 操作详情
        """
        extra_fields = {
            'log_type': 'operation',
            'operation': operation,
            'status': status,
            'timestamp': datetime.now().isoformat()
        }
        
        if details:
            extra_fields['details'] = details
        
        # 根据状态选择日志级别
        level_map = {
            'success': 'INFO',
            'warning': 'WARNING',
            'failed': 'ERROR'
        }
        
        level = level_map.get(status, 'INFO')
        message = f"操作日志: {operation} - 状态: {status}"
        
        self.log_with_context(level, message, extra_fields)
    
    def analyze_logs(self, 
                    log_file: Optional[str] = None,
                    time_range: Optional[tuple] = None) -> Dict[str, Any]:
        """
        分析日志
        
        Args:
            log_file: 日志文件路径（默认使用结构化日志）
            time_range: 时间范围 (start_time, end_time)
            
        Returns:
            分析结果
        """
        if log_file is None:
            log_file = self.log_dir / f'{self.name}_structured.json'
        
        if not os.path.exists(log_file):
            return {"error": "日志文件不存在"}
        
        try:
            logs = []
            with open(log_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            log_entry = json.loads(line.strip())
                            logs.append(log_entry)
                        except json.JSONDecodeError:
                            continue
            
            # 按时间范围过滤
            if time_range:
                start_time, end_time = time_range
                logs = [
                    log for log in logs
                    if start_time <= log.get('timestamp', '') <= end_time
                ]
            
            # 分析统计
            total_logs = len(logs)
            
            # 按级别统计
            level_counts = {}
            for log in logs:
                level = log.get('level', 'UNKNOWN')
                level_counts[level] = level_counts.get(level, 0) + 1
            
            # 按模块统计
            module_counts = {}
            for log in logs:
                module = log.get('module', 'unknown')
                module_counts[module] = module_counts.get(module, 0) + 1
            
            # 错误统计
            error_logs = [log for log in logs if log.get('level') in ['ERROR', 'CRITICAL']]
            
            # 性能日志统计
            performance_logs = [log for log in logs if log.get('extra_fields', {}).get('log_type') == 'performance']
            avg_performance = None
            if performance_logs:
                exec_times = [log.get('extra_fields', {}).get('execution_time_seconds', 0) for log in performance_logs]
                avg_performance = sum(exec_times) / len(exec_times)
            
            return {
                "total_logs": total_logs,
                "time_range": {
                    "start": logs[0].get('timestamp') if logs else None,
                    "end": logs[-1].get('timestamp') if logs else None
                },
                "level_distribution": level_counts,
                "module_distribution": module_counts,
                "error_count": len(error_logs),
                "recent_errors": error_logs[-5:] if error_logs else [],
                "performance_stats": {
                    "performance_logs": len(performance_logs),
                    "avg_execution_time": avg_performance,
                    "slowest_operations": sorted(
                        performance_logs,
                        key=lambda x: x.get('extra_fields', {}).get('execution_time_seconds', 0),
                        reverse=True
                    )[:5]
                } if performance_logs else None
            }
            
        except Exception as e:
            self.logger.error(f"日志分析失败: {e}")
            return {"error": str(e)}
    
    def cleanup_old_logs(self, days_to_keep: int = 30):
        """
        清理旧日志文件
        
        Args:
            days_to_keep: 保留天数
        """
        import time
        from datetime import datetime, timedelta
        
        cutoff_time = time.time() - (days_to_keep * 24 * 60 * 60)
        
        for log_file in self.log_dir.glob('*.log'):
            if os.path.getmtime(log_file) < cutoff_time:
                try:
                    os.remove(log_file)
                    self.logger.info(f"已删除旧日志文件: {log_file}")
                except Exception as e:
                    self.logger.error(f"删除日志文件失败 {log_file}: {e}")
        
        for json_file in self.log_dir.glob('*.json'):
            if os.path.getmtime(json_file) < cutoff_time:
                try:
                    os.remove(json_file)
                    self.logger.info(f"已删除旧JSON日志文件: {json_file}")
                except Exception as e:
                    self.logger.error(f"删除JSON日志文件失败 {json_file}: {e}")

# 全局日志管理器实例
global_logger = DSLLogger()

def get_logger(name: str = 'dsl') -> logging.Logger:
    """
    获取日志记录器（简化接口）
    
    Args:
        name: 日志名称
        
    Returns:
        日志记录器
    """
    return DSLLogger(name=name).get_logger()

# 使用示例
if __name__ == "__main__":
    # 创建日志管理器
    logger_manager = DSLLogger(
        name='dsl_demo',
        log_dir='demo_logs',
        level='DEBUG'
    )
    
    logger = logger_manager.get_logger()
    
    # 基本日志
    logger.debug("这是一条DEBUG日志")
    logger.info("这是一条INFO日志")
    logger.warning("这是一条WARNING日志")
    logger.error("这是一条ERROR日志")
    
    # 带上下文的日志
    logger_manager.info_with_context(
        "用户登录成功",
        extra_fields={
            'user_id': 'user123',
            'ip_address': '192.168.1.100',
            'action': 'login'
        }
    )
    
    # 性能日志
    logger_manager.log_performance(
        operation='数据加载',
        execution_time=2.5,
        memory_usage=150.3,
        additional_info={'data_size': '100MB', 'source': 'database'}
    )
    
    # 操作日志
    logger_manager.log_operation(
        operation='策略回测',
        status='success',
        details={
            'strategy': 'momentum',
            'period': '2025-01-01 to 2025-12-31',
            'return': 0.15
        }
    )
    
    # 分析日志
    print("\n📊 日志分析结果:")
    analysis = logger_manager.analyze_logs()
    print(json.dumps(analysis, indent=2, ensure_ascii=False))
    
    # 清理旧日志（演示）
    # logger_manager.cleanup_old_logs(days_to_keep=7)