#!/usr/bin/env python3
# 统一飞书工具，所有飞书操作都走这里，避免重复代码
import os
import requests
import json
from typing import Optional, Dict, Any
from .config import config
from .logger import get_logger

logger = get_logger("feishu_utils")

# 飞书API缓存，避免重复获取token
_tenant_access_token: Optional[str] = None
_token_expire_at: int = 0

def get_tenant_access_token() -> Optional[str]:
    """获取飞书租户访问令牌，自动缓存，过期自动刷新"""
    global _tenant_access_token, _token_expire_at
    import time
    now = int(time.time())
    if _tenant_access_token and now < _token_expire_at - 60:  # 提前1分钟刷新
        return _tenant_access_token
    
    if not config.feishu_app_id or not config.feishu_app_secret:
        logger.error("飞书APP_ID或APP_SECRET未配置")
        return None
    
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    payload = {
        "app_id": config.feishu_app_id,
        "app_secret": config.feishu_app_secret
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            _tenant_access_token = data["tenant_access_token"]
            _token_expire_at = now + data["expire"]
            logger.debug("成功获取飞书访问令牌，有效期至：{}".format(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(_token_expire_at))))
            return _tenant_access_token
        else:
            logger.error("获取飞书访问令牌失败：{}".format(data.get("msg")))
            return None
    except Exception as e:
        logger.error("获取飞书访问令牌异常：{}".format(str(e)))
        return None

def send_markdown(title: str, content: str, receive_id: str = None) -> bool:
    """
    发送markdown格式飞书消息
    :param title: 消息标题
    :param content: markdown格式内容
    :param receive_id: 接收人/群ID，默认发配置的默认群
    :return: 是否发送成功
    """
    token = get_tenant_access_token()
    if not token:
        return False
    
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "receive_id": receive_id or "oc_9fb3b60a65da527705bbd738dfd4aa01",  # 默认交易群
        "content": json.dumps({
            "zh_cn": {
                "title": title,
                "content": [[{"tag": "md", "text": content}]]
            }
        }),
        "msg_type": "post"
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            logger.info("飞书消息发送成功：{}".format(title))
            return True
        else:
            logger.error("飞书消息发送失败：{}".format(data.get("msg")))
            return False
    except Exception as e:
        logger.error("发送飞书消息异常：{}".format(str(e)))
        return False

# v4.5.12 P2-14: 本地告警队列 — 飞书不可达时排队，下次重试
_ALERT_QUEUE_PATH = None


def _get_alert_queue_path() -> str:
    global _ALERT_QUEUE_PATH
    if _ALERT_QUEUE_PATH is None:
        _ALERT_QUEUE_PATH = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", ".alert_queue.json"
        )
    return _ALERT_QUEUE_PATH


def _load_alert_queue() -> list:
    path = _get_alert_queue_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_alert_queue(queue: list):
    path = _get_alert_queue_path()
    try:
        with open(path, "w") as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def flush_alert_queue() -> int:
    """重试队列中所有失败的告警，返回成功数"""
    queue = _load_alert_queue()
    if not queue:
        return 0
    succeed = []
    remained = []
    for alert in queue:
        ok = send_alert(alert.get("title", ""), alert.get("content", ""),
                        alert.get("level", "warning"))
        if ok:
            succeed.append(alert)
        else:
            remained.append(alert)
    if remained:
        _save_alert_queue(remained)
    else:
        _save_alert_queue([])
    if succeed:
        logger.info(f"📤 告警队列: 重试成功{len(succeed)}条, 剩余{len(remained)}条")
    return len(succeed)


def send_alert(title: str, content: str, level: str = "warning") -> bool:
    """
    发送告警消息，自动加告警前缀
    发送失败时自动入队，下次重试
    :param title: 告警标题
    :param content: 告警内容
    :param level: 告警级别：info/warning/error/critical
    :return: 是否发送成功
    """
    level_emoji = {
        "info": "ℹ️",
        "warning": "⚠️",
        "error": "❌",
        "critical": "🚨"
    }.get(level, "⚠️")
    full_title = f"{level_emoji} {title}"
    ok = send_markdown(full_title, content)
    if not ok:
        # 入队等待重试
        queue = _load_alert_queue()
        queue.append({"title": title, "content": content, "level": level,
                       "time": __import__('datetime').datetime.now().isoformat()})
        _save_alert_queue(queue)
    return ok

def update_bitable_record(app_token: str, table_id: str, record_id: str, fields: Dict[str, Any]) -> bool:
    """
    更新飞书多维表格记录
    :param app_token: 多维表格APP_TOKEN
    :param table_id: 表格ID
    :param record_id: 记录ID
    :param fields: 要更新的字段
    :return: 是否更新成功
    """
    token = get_tenant_access_token()
    if not token:
        return False
    
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {
        "fields": fields
    }
    try:
        resp = requests.put(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0:
            logger.info(f"多维表格记录更新成功：{record_id}")
            return True
        else:
            logger.error(f"多维表格记录更新失败：{data.get('msg')}")
            return False
    except Exception as e:
        logger.error(f"更新多维表格异常：{str(e)}")
        return False
