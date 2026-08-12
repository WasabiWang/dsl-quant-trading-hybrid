#!/usr/bin/env python3
# 幂等性校验工具，防止重复执行任务、重复推送消息、重复写数据
import os
import time
import json
from typing import Optional
from .config import config
from .logger import get_logger

logger = get_logger("idempotent")
IDEMPOTENT_DIR = os.path.join(config.cache_dir, "idempotent")
os.makedirs(IDEMPOTENT_DIR, exist_ok=True)

def is_executed(task_id: str, ttl: int = 86400) -> bool:
    """
    检查任务是否已经执行过
    :param task_id: 任务唯一ID，比如daily_report_20260415、update_bitable_000001_20260415
    :param ttl: 有效期，单位秒，默认1天，超过有效期自动失效
    :return: 已经执行过返回True，没执行过返回False
    """
    file_path = os.path.join(IDEMPOTENT_DIR, f"{task_id}.json")
    if not os.path.exists(file_path):
        return False
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            execute_time = data.get("execute_time", 0)
            if time.time() - execute_time < ttl:
                logger.debug(f"任务{task_id}已经在{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(execute_time))}执行过，有效期内，跳过执行")
                return True
            else:
                logger.debug(f"任务{task_id}执行记录已过期，允许重新执行")
                return False
    except Exception as e:
        logger.warning(f"读取幂等性记录异常：{str(e)}，视为未执行过")
        return False

def mark_executed(task_id: str, ttl: int = 86400, extra_info: dict = None) -> None:
    """
    标记任务已经执行过
    :param task_id: 任务唯一ID
    :param ttl: 有效期，单位秒，默认1天
    :param extra_info: 额外信息，会存在记录里
    """
    file_path = os.path.join(IDEMPOTENT_DIR, f"{task_id}.json")
    try:
        data = {
            "task_id": task_id,
            "execute_time": time.time(),
            "ttl": ttl,
            "extra": extra_info or {}
        }
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"标记任务{task_id}已执行，有效期{ttl}秒")
        # 自动清理过期的记录
        _clean_expired()
    except Exception as e:
        logger.warning(f"标记幂等性记录异常：{str(e)}")

def _clean_expired() -> None:
    """清理过期的幂等性记录，只保留最近7天的"""
    now = time.time()
    for filename in os.listdir(IDEMPOTENT_DIR):
        if not filename.endswith(".json"):
            continue
        file_path = os.path.join(IDEMPOTENT_DIR, filename)
        try:
            mtime = os.path.getmtime(file_path)
            if now - mtime > 7 * 86400:  # 超过7天
                os.remove(file_path)
                logger.debug(f"清理过期幂等性记录：{filename}")
        except Exception as e:
            logger.warning(f"清理过期幂等性记录异常：{str(e)}")

def idempotent(task_id_prefix: str, ttl: int = 86400, auto_mark: bool = True):
    """
    幂等性装饰器，加在函数上，同一个task_id每天只执行一次
    :param task_id_prefix: 任务ID前缀，比如daily_report，会自动加上日期
    :param ttl: 有效期，单位秒，默认1天
    :param auto_mark: 函数执行成功后自动标记已执行
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            today = time.strftime("%Y%m%d")
            task_id = f"{task_id_prefix}_{today}"
            if is_executed(task_id, ttl):
                logger.info(f"任务{task_id}已经执行过，跳过执行")
                return None
            result = func(*args, **kwargs)
            if auto_mark:
                mark_executed(task_id, ttl)
            return result
        return wrapper
    return decorator
