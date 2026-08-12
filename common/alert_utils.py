#!/usr/bin/env python3
"""
alert_utils.py — DSL v4.5.12 双通道告警统一入口 (P2-13)

发送顺序:
  1. 飞书消息 (primary)
  2. 邮件 (fallback, 仅在飞书失败时发送)
  3. Webhook (独立并行, 用于无APP_ID的场景)

Usage:
    from common.alert_utils import send_alert
    send_alert("交易执行告警", "触发止损: 茅台 -8.2%", level="error")
"""
import logging
from typing import Optional

logger = logging.getLogger("alert_utils")


def send_alert(
    title: str,
    message: str,
    level: str = "info",
    force_both: bool = False,
) -> bool:
    """双通道发送告警

    Args:
        title: 告警标题
        message: 告警内容 (纯文本或Markdown)
        level: 告警级别 (info/warning/error/critical)
        force_both: True=强制双通道都发送; False=飞书失败才走邮件

    Returns:
        True=至少一个通道成功, False=全部失败
    """
    success = False

    # ── 通道1: 飞书 ──
    feishu_ok = _send_feishu(title, message, level)
    if feishu_ok:
        success = True
        if not force_both:
            return True

    # ── 通道2: 邮件 (飞书失败 或 force_both) ──
    email_ok = _send_email(title, message, level)
    if email_ok:
        success = True

    # ── 通道3: Webhook (独立发送, 不依赖APP_ID) ──
    webhook_ok = _send_webhook(title, message, level)
    if webhook_ok:
        success = True

    if not success:
        logger.error(f"所有告警通道均失败: {title}")

    return success


def _send_feishu(title: str, message: str, level: str) -> bool:
    """通过飞书APP发送消息"""
    try:
        from common.feishu_utils import send_markdown
        return send_markdown(title=title, content=message)
    except Exception as e:
        logger.warning(f"飞书通道失败: {e}")
        return False


def _send_email(title: str, message: str, level: str) -> bool:
    """通过SMTP发送邮件"""
    try:
        from common.email_utils import send_alert as send_email
        prefix = {"info": "", "warning": "[警告] ", "error": "[错误] ", "critical": "[严重] "}
        return send_email(subject=f"{prefix.get(level, '')}{title}", body=message)
    except Exception as e:
        logger.warning(f"邮件通道失败: {e}")
        return False


def _send_webhook(title: str, message: str, level: str) -> bool:
    """通过飞书Webhook URL发送 (不需要APP_ID/APP_SECRET)"""
    try:
        from common.config import config
        url = config.feishu_webhook_url
        if not url:
            return False
        import requests
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [[{"tag": "text", "text": message}]],
                    }
                }
            },
        }
        resp = requests.post(url, json=payload, timeout=10)
        return resp.ok
    except Exception as e:
        logger.debug(f"Webhook通道失败: {e}")
        return False
