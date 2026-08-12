#!/usr/bin/env python3
"""
Dashboard 登录密码设置

Usage:
  python3 setup_auth.py                          # 交互式设置密码
  python3 setup_auth.py --password <password>    # 命令行指定密码
  python3 setup_auth.py --show                   # 显示当前配置
  python3 setup_auth.py --reset                  # 重置密码

输出: ../config/web_auth.json
密码使用 bcrypt 等效方案 (pbkdf2_hmac + sha256, 高迭代) 存储。
"""

import os, sys, json, hashlib, base64, secrets, getpass

CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config"))
AUTH_FILE = os.path.join(CONFIG_DIR, "web_auth.json")


def _hash_password(password: str) -> dict:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000)
    return {
        "hash_algorithm": "pbkdf2_hmac-sha256",
        "iterations": 600_000,
        "salt": salt,
        "hash": base64.b64encode(key).decode(),
        "session_secret": base64.b64encode(secrets.token_bytes(32)).decode(),
        "username": "admin",
        "created_at": __import__("datetime").datetime.now().isoformat(),
    }


def verify_password(password: str, cfg: dict) -> bool:
    if cfg.get("hash_algorithm") != "pbkdf2_hmac-sha256":
        return False
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), cfg["salt"].encode(), cfg["iterations"]
    )
    return base64.b64encode(key).decode() == cfg["hash"]


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)

    if "--show" in sys.argv:
        if os.path.exists(AUTH_FILE):
            cfg = json.load(open(AUTH_FILE))
            print(f"用户: {cfg.get('username', 'admin')}")
            print(f"Hash: {cfg['hash'][:12]}... (salt: {cfg['salt'][:8]}...)")
            print(f"创建于: {cfg.get('created_at', 'unknown')}")
            print(f"配置文件: {AUTH_FILE}")
        else:
            print("❌ 未配置密码。运行: python3 setup_auth.py")
        return

    if "--reset" in sys.argv:
        if os.path.exists(AUTH_FILE):
            os.remove(AUTH_FILE)
            print("✅ 已重置，下次启动时需重新设置密码")
        else:
            print("ℹ️  无配置需要重置")
        return

    # 交互式或命令行设置
    if "--password" in sys.argv:
        idx = sys.argv.index("--password")
        if idx + 1 < len(sys.argv):
            password = sys.argv[idx + 1]
        else:
            print("❌ --password 需要参数")
            return
    else:
        print("🔐 设置 Dashboard 登录密码")
        print("   (至少8位，建议使用大写+小写+数字+符号)")
        while True:
            p1 = getpass.getpass("密码: ")
            if len(p1) < 8:
                print("   ❌ 密码至少8位")
                continue
            p2 = getpass.getpass("再次输入: ")
            if p1 == p2:
                password = p1
                break
            print("   ❌ 两次输入不一致")

    cfg = _hash_password(password)
    with open(AUTH_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(AUTH_FILE, 0o600)
    print(f"✅ 密码已设置 (用户: {cfg['username']})")
    print(f"   配置文件: {AUTH_FILE}")
    print(f"   请重启 Dashboard 后生效")


if __name__ == "__main__":
    main()
