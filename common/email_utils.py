#!/usr/bin/env python3
"""
email_utils.py — DSL v4.5.12 邮件告警工具 (P2-13 双通道告警备用通道)

依赖: smtplib (标准库, 零依赖)
配置: 从 common.config 读取 SMTP_SERVER / SMTP_USER / SMTP_PASSWORD / ALERT_EMAIL
"""
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

logger = logging.getLogger("email_utils")

# 懒加载 config 避免循环导入
_config = None
def _get_config():
    global _config
    if _config is None:
        from common.config import config as _cfg
        _config = _cfg
    return _config


def send_alert(
    subject: str,
    body: str,
    to: Optional[str] = None,
    timeout: int = 10,
) -> bool:
    """发送告警邮件

    Args:
        subject: 邮件标题
        body: 邮件正文 (纯文本)
        to: 收件人, 默认从 config.alert_email 读取
        timeout: SMTP 连接超时(秒)

    Returns:
        True=发送成功, False=失败
    """
    cfg = _get_config()
    smtp_server = cfg.smtp_server
    smtp_port = cfg.smtp_port
    smtp_user = cfg.smtp_user
    smtp_password = cfg.smtp_password
    to_addr = to or cfg.alert_email

    if not all([smtp_server, smtp_port, smtp_user, smtp_password, to_addr]):
        logger.warning("邮件配置不完整, 跳过邮件告警")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = smtp_user
        msg["To"] = to_addr
        msg["Subject"] = f"[DSL Quant] {subject}"
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(smtp_server, smtp_port, timeout=timeout) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.sendmail(smtp_user, [to_addr], msg.as_string())

        logger.info(f"邮件告警已发送: {subject} → {to_addr}")
        return True
    except Exception as e:
        logger.error(f"邮件发送失败: {e}")
        return False
