#!/usr/bin/env python3
"""DSL v4.5.9 — 独立飞书通知 (不依赖 OpenClaw CLI)

替代所有 subprocess(['openclaw', 'message', 'send']) 调用。
直接调用飞书开放平台 API。

使用:
    from common.feishu_direct import send_feishu_message
    send_feishu_message("内容", msg_type="text")
"""

import os
import json
import time
import requests
from datetime import datetime
from typing import Optional, Dict, Any


class FeishuClient:
    """独立飞书客户端 — 零 OpenClaw 依赖"""

    def __init__(self, app_id: str = None, app_secret: str = None):
        self.app_id = app_id or os.getenv("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.getenv("FEISHU_APP_SECRET", "")
        self.receive_user = os.getenv("FEISHU_RECEIVE_USER", "")
        self._token = None
        self._token_expire = 0

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def _get_token(self) -> Optional[str]:
        now = time.time()
        if self._token and now < self._token_expire - 60:
            return self._token
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10
            )
            data = resp.json()
            if data.get("code") == 0:
                self._token = data["tenant_access_token"]
                self._token_expire = now + data.get("expire", 7200)
                return self._token
        except Exception:
            pass
        return None

    def send(self, content: str, msg_type: str = "text",
             title: str = "DSL系统通知", receive_user: str = None) -> bool:
        """发送飞书消息"""
        if not self.configured:
            return False

        token = self._get_token()
        if not token:
            return False

        receive = receive_user or self.receive_user
        if not receive:
            return False

        if msg_type == "text":
            content_data = {"text": content}
        elif msg_type == "post":
            content_data = {
                "post": {"zh_cn": {
                    "title": title,
                    "content": [[{"tag": "text", "text": content}]]
                }}
            }
        else:
            content_data = {"text": content}

        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8"
                },
                json={
                    "receive_id": receive,
                    "msg_type": msg_type,
                    "content": json.dumps(content_data, ensure_ascii=False)
                },
                timeout=10
            )
            return resp.json().get("code") == 0
        except Exception:
            return False

    def send_alert(self, level: str, title: str, content: str) -> bool:
        """发送分级告警"""
        emoji = {"CRITICAL": "🔴", "ERROR": "🟠", "WARNING": "🟡", "INFO": "🟢"}
        e = emoji.get(level, "⚪")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{e} [{level}] {title}\n\n{content}\n\n⏰ {ts}"
        return self.send(formatted, msg_type="post", title=f"{e} {level}")


# ── 全局单例 ──
_client: Optional[FeishuClient] = None


def get_feishu() -> FeishuClient:
    global _client
    if _client is None:
        _client = FeishuClient()
    return _client


def send_feishu_message(content: str, **kwargs) -> bool:
    """便捷函数 — 兼容旧 OpenClaw message send 接口"""
    return get_feishu().send(content, **kwargs)


def send_feishu_alert(level: str, title: str, content: str) -> bool:
    """便捷函数 — 发送告警"""
    return get_feishu().send_alert(level, title, content)
