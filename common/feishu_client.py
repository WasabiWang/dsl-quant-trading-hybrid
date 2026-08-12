#!/usr/bin/env python3
"""飞书客户端封装 — 委托到 feishu_utils 和 feishu_bitable"""
from .feishu_utils import send_alert

class FeishuClient:
    """飞书客户端单例"""
    def send_alert(self, title: str, content: str = "", webhook_url: str = None):
        return send_alert(title, content)

feishu_client = FeishuClient()
