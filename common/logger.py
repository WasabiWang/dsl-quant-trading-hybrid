#!/usr/bin/env python3
# 统一日志工具，所有脚本都用这个来打日志，统一格式、统一存储
import os
import logging
from logging.handlers import RotatingFileHandler
from .config import config
from datetime import datetime

def get_logger(name: str, log_file_prefix: str = None) -> logging.Logger:
    """
    获取统一格式的日志实例
    :param name: 日志名称，通常是脚本名
    :param log_file_prefix: 日志文件前缀，默认用name
    :return: logging.Logger实例
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出，按天切割，保留最近7天
    if not log_file_prefix:
        log_file_prefix = name
    log_file = os.path.join(config.log_dir, f"{log_file_prefix}_{datetime.now().strftime('%Y%m%d')}.log")
    file_handler = RotatingFileHandler(
        log_file, 
        maxBytes=10*1024*1024,  # 单个日志文件最大10M
        backupCount=7,  # 保留最近7个备份
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

def catch_exception(logger: logging.Logger, alert: bool = True, exit_on_error: bool = False):
    """
    异常捕获装饰器，自动记录错误日志，可选推送告警、退出脚本
    :param logger: 日志实例
    :param alert: 异常时是否推送飞书告警
    :param exit_on_error: 异常时是否退出脚本
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                import traceback
                error_msg = f"函数 {func.__name__} 执行异常：{str(e)}\n{traceback.format_exc()}"
                logger.error(error_msg)
                if alert:
                    try:
                        from .feishu_utils import send_alert
                        send_alert(f"⚠️ 脚本异常告警", error_msg)
                    except:
                        pass
                if exit_on_error:
                    exit(1)
                return None
        return wrapper
    return decorator
