#!/usr/bin/env python3
# 敏感信息加密工具，AES-256-CBC加密，密钥存在当前用户目录下的隐藏文件，只有当前用户可读
import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from .logger import get_logger

logger = get_logger("encrypt")
# 密钥存储路径，当前用户目录下的隐藏文件，权限600只有当前用户可读
KEY_PATH = os.path.expanduser("~/.openclaw_encrypt_key")
SALT = b"openclaw_dsl_quant_2026"  # 固定盐，和密钥配合加密

def generate_key() -> None:
    """生成加密密钥，保存到KEY_PATH，只有第一次运行的时候需要生成"""
    if os.path.exists(KEY_PATH):
        logger.debug("加密密钥已存在，不需要重新生成")
        return
    # 生成Fernet密钥
    key = Fernet.generate_key()
    with open(KEY_PATH, "wb") as f:
        f.write(key)
    # 设置权限，只有当前用户可读可写
    os.chmod(KEY_PATH, 0o600)
    logger.info("加密密钥已生成，保存到：%s", KEY_PATH)

def get_cipher() -> Fernet:
    """获取加密/解密器"""
    if not os.path.exists(KEY_PATH):
        generate_key()
    with open(KEY_PATH, "rb") as f:
        key = f.read()
    return Fernet(key)

def encrypt(plain_text: str) -> str:
    """加密字符串，返回base64编码的加密结果，加密后的字符串以ENC:开头"""
    if plain_text.startswith("ENC:"):
        return plain_text  # 已经加密过的直接返回
    cipher = get_cipher()
    encrypted = cipher.encrypt(plain_text.encode("utf-8"))
    return f"ENC:{base64.b64encode(encrypted).decode('utf-8')}"

def decrypt(encrypted_text: str) -> str:
    """解密字符串，输入是加密后的以ENC:开头的字符串，返回明文"""
    if not encrypted_text.startswith("ENC:"):
        return encrypted_text  # 没加密的直接返回
    try:
        cipher = get_cipher()
        encrypted = base64.b64decode(encrypted_text[4:])
        return cipher.decrypt(encrypted).decode("utf-8")
    except Exception as e:
        logger.error("解密失败：%s", str(e))
        return encrypted_text  # 解密失败返回原字符串，避免影响系统运行

def encrypt_env_file(env_path: str = None) -> None:
    """加密.env文件里的敏感字段，敏感字段名包含：KEY, SECRET, PASSWORD, TOKEN"""
    if not env_path:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        logger.warning("env文件不存在：%s", env_path)
        return
    # 读取原文件
    lines = []
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # 加密敏感字段
    sensitive_keys = ["KEY", "SECRET", "PASSWORD", "TOKEN", "PRIVATE"]
    modified = False
    for i in range(len(lines)):
        line = lines[i].strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 判断是否是敏感字段
        for sk in sensitive_keys:
            if sk in key.upper() and not value.startswith("ENC:"):
                encrypted_val = encrypt(value)
                lines[i] = f'{key}="{encrypted_val}"\n'
                modified = True
                logger.info("加密敏感字段：%s", key)
                break
    # 保存修改后的文件
    if modified:
        # 备份原文件
        backup_path = f"{env_path}.bak.{int(time.time())}"
        with open(backup_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.chmod(backup_path, 0o600)
        # 覆盖原文件
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.chmod(env_path, 0o600)
        logger.info("env文件加密完成，原文件已备份到：%s", backup_path)
    else:
        logger.info("没有需要加密的敏感字段，env文件未修改")
