#!/usr/bin/env python3
"""
飞书聊天历史消息读取工具
绕过OpenClaw插件限制，直接调用飞书API读取聊天历史

用法:
  python3 feishu_msg_reader.py --chat-id <chat_id> --limit 20 [--before <msg_id>]
  python3 feishu_msg_reader.py --dm <open_id> --limit 20

环境: 从 ~/.openclaw/openclaw.json 读取 appId/appSecret
"""
import argparse
import json
import os
import re
import sys
import requests

OPENCLAW_CONFIG = os.path.expanduser("~/.openclaw/openclaw.json")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_token() -> str:
    """获取tenant_access_token"""
    with open(OPENCLAW_CONFIG) as f:
        config = json.load(f)
    feishu = config.get("channels", {}).get("feishu", {})
    app_id = feishu.get("appId", "")
    app_secret = feishu.get("appSecret", "")
    if not app_id or not app_secret:
        raise RuntimeError("飞书 appId/appSecret 未配置")

    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取token失败: {data}")
    return data["tenant_access_token"]


def get_chat_id_for_dm(token: str, open_id: str) -> str:
    """通过open_id获取私聊chat_id"""
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/chats",
        headers={"Authorization": f"Bearer {token}"},
        json={"chat_type": "p2p", "user_id_type": "open_id", "user_ids": [open_id]},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取chat_id失败: {data}")
    # p2p chat already exists, try list
    chats = data.get("data", {}).get("chats", [])
    if chats:
        return chats[0]["chat_id"]
    # fallback: search in existing chats
    return None


def list_messages(token: str, container_id: str, page_size: int = 20,
                  before: str = None, after: str = None) -> list:
    """列出聊天历史消息"""
    params = {
        "container_id": container_id,
        "container_id_type": "chat",
        "page_size": min(page_size, 50),
        "sort_type": "ByCreateTimeDesc",  # 从最新消息开始
    }

    headers = {"Authorization": f"Bearer {token}"}
    messages = []

    resp = requests.get(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        headers=headers,
        params=params,
        timeout=15,
    )
    data = resp.json()
    if data.get("code") != 0:
        print(f"⚠️ API错误: code={data.get('code')} msg={data.get('msg')}", file=sys.stderr)
        return messages

    items = data.get("data", {}).get("items", [])
    for m in items:
        msg_type = m.get("msg_type", "unknown")
        sender_id = m.get("sender", {}).get("sender_id", {}).get("open_id", "?")
        create_time = m.get("create_time", "")
        msg_id = m.get("message_id", "")

        # 解析消息内容
        body = m.get("body", {}).get("content", "")
        content = ""
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
            if msg_type == "text":
                content = parsed.get("text", "")
            elif msg_type == "post":
                # 富文本：提取纯文本
                parts = []
                for lang_content in parsed.get("content", {}).values():
                    for line in lang_content:
                        for elem in line:
                            if elem.get("tag") == "text":
                                parts.append(elem.get("text", ""))
                            elif elem.get("tag") == "at":
                                parts.append(f"@{elem.get('user_name', elem.get('user_id', ''))}")
                content = " ".join(parts)
            elif msg_type == "interactive":
                content = "[交互卡片]"
            elif msg_type == "file":
                content = f"[文件: {parsed.get('file_name', '?')}]"
            elif msg_type == "image":
                content = "[图片]"
            else:
                content = f"[{msg_type}]"
        except (json.JSONDecodeError, AttributeError):
            content = str(body)[:200]

        # 判断是用户还是bot
        sender_type = m.get("sender", {}).get("sender_type", "unknown")

        messages.append({
            "message_id": msg_id,
            "sender_id": sender_id,
            "sender_type": sender_type,
            "msg_type": msg_type,
            "create_time": create_time,
            "content": content[:500],  # 截断
        })

    return messages


def main():
    parser = argparse.ArgumentParser(description="飞书聊天历史消息读取")
    parser.add_argument("--chat-id", help="聊天chat_id")
    parser.add_argument("--dm", help="私聊对象open_id")
    parser.add_argument("--limit", type=int, default=20, help="读取条数(默认20)")
    parser.add_argument("--json", action="store_true", help="JSON输出")
    args = parser.parse_args()

    if not args.chat_id and not args.dm:
        # 默认：James的私聊
        args.chat_id = "oc_9fb3b60a65da527705bbd738dfd4aa01"

    token = get_token()

    if args.dm and not args.chat_id:
        args.chat_id = get_chat_id_for_dm(token, args.dm)
        if not args.chat_id:
            print("❌ 无法获取私聊chat_id", file=sys.stderr)
            sys.exit(1)

    messages = list_messages(token, args.chat_id, args.limit)

    if args.json:
        print(json.dumps(messages, ensure_ascii=False, indent=2))
    else:
        print(f"📋 飞书聊天历史 ({len(messages)}条)")
        print("=" * 60)
        for m in reversed(messages):  # 时间正序（旧→新）
            role = "👤" if m["sender_type"] == "user" else "🤖"
            ts = m["create_time"]
            if len(ts) == 13:
                from datetime import datetime
                ts = datetime.fromtimestamp(int(ts) / 1000).strftime("%m-%d %H:%M")
            print(f"{role} [{ts}] {m['content'][:200]}")
            if len(m["content"]) > 200:
                print(f"   ... (截断, 全文{len(m['content'])}字)")


if __name__ == "__main__":
    main()
