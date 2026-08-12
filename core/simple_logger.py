#!/usr/bin/env python3
"""
DSL v4.5.9 简化日志模块

无需外部依赖的简化版本

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

class SimpleFormatter(logging.Formatter):
    """简化日志格式化器"""
    
    def __init__(self):
        format_str = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        super().__init__(format_str, datefmt='%Y-%m-%d %H:%M:%S')

class SimpleLogger:
    """简化日志管理器"""
    
    def __init__(self, 
                 name: str = 'dsl',
                 log_dir: str = 'logs',
                 level: str = 'INFO'):
        """
        初始化日志管理器
        
        Args:
            name: 日志名称
            log_dir: 日志目录
            level: 日志级别
        """
        self.name = name
        self.log_dir = Path(log_dir)
        
        # 创建日志目录
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 配置日志
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level.upper()))
        self.logger.propagate = False
        
        # 移除现有处理器
        self.logger.handlers.clear()
        
        # 添加控制台处理器
        self._add_console_handler()
        
        # 添加文件处理器
        self._add_file_handler()
    
    def _add_console_handler(self):
        """添加控制台处理器"""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.logger.level)
        console_handler.setFormatter(SimpleFormatter())
        self.logger.addHandler(console_handler)
    
    def _add_file_handler(self):
        """添加文件处理器"""
        log_file = self.log_dir / f'{self.name}.log'
        
        file_handler = logging.FileHandler(
            filename=log_file,
            encoding='utf-8'
        )
        
        file_handler.setLevel(self.logger.level)
        file_handler.setFormatter(SimpleFormatter())
        self.logger.addHandler(file_handler)
    
    def get_logger(self) -> logging.Logger:
        """获取日志记录器"""
        return self.logger
    
    def info(self, message: str):
        """记录INFO级别日志"""
        self.logger.info(message)
    
    def debug(self, message: str):
        """记录DEBUG级别日志"""
        self.logger.debug(message)
    
    def warning(self, message: str):
        """记录WARNING级别日志"""
        self.logger.warning(message)
    
    def error(self, message: str, exc_info: bool = False):
        """记录ERROR级别日志"""
        self.logger.error(message, exc_info=exc_info)
    
    def critical(self, message: str):
        """记录CRITICAL级别日志"""
        self.logger.critical(message)

# 全局日志管理器实例
global_logger = SimpleLogger()

def get_simple_logger(name: str = 'dsl') -> logging.Logger:
    """
    获取简化日志记录器
    
    Args:
        name: 日志名称
        
    Returns:
        日志记录器
    """
    return SimpleLogger(name=name).get_logger()

# 使用示例
if __name__ == "__main__":
    # 创建日志管理器
    logger_manager = SimpleLogger(
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
    
    # 测试异常日志
    try:
        raise ValueError("测试异常")
    except Exception as e:
        logger.error("捕获到异常", exc_info=True)
    
    print("\n✅ 简化日志模块测试完成")