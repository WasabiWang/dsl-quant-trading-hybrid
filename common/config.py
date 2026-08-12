#!/usr/bin/env python3
# 统一配置加载工具，所有脚本都用这个来获取配置
import os
import base64
import yaml
from dotenv import load_dotenv
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet

# 解密功能直接写到config里，避免循环导入
KEY_PATH = os.path.expanduser("~/.openclaw_encrypt_key")
def decrypt(encrypted_text: str) -> str:
    """解密字符串，输入是加密后的以ENC:开头的字符串，返回明文"""
    if not encrypted_text.startswith("ENC:"):
        return encrypted_text  # 没加密的直接返回
    try:
        if not os.path.exists(KEY_PATH):
            return encrypted_text
        with open(KEY_PATH, "rb") as f:
            key = f.read()
        cipher = Fernet(key)
        encrypted = base64.b64decode(encrypted_text[4:])
        return cipher.decrypt(encrypted).decode("utf-8")
    except Exception as e:
        return encrypted_text  # 解密失败返回原字符串，避免影响系统运行

class Config:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load_config()
        return cls._instance
    
    def _load_config(self):
        # P0-FIX: 使用相对路径替代硬编码路径
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        # 加载 .env（公共模板，空值）
        env_path = os.path.join(project_root, ".env")
        if not os.path.exists(env_path):
            env_path = os.path.join(os.getcwd(), ".env")
        load_dotenv(env_path, override=False)
        # 加载 .env.local（本地密钥，覆盖 .env）
        env_local_path = os.path.join(project_root, ".env.local")
        if os.path.exists(env_local_path):
            load_dotenv(env_local_path, override=True)
        
        # 加载yaml配置文件
        self.yaml_config: Dict[str, Any] = {}
        yaml_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")
        if os.path.exists(yaml_path):
            try:
                with open(yaml_path, "r", encoding="utf-8") as f:
                    self.yaml_config = yaml.safe_load(f)
            except Exception as e:
                print(f"⚠️ 加载配置文件失败：{e}")
        
        # 加载系统环境变量，自动解密敏感字段
        self.workspace = self.get("WORKSPACE", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # P0-FIX: 使用相对路径替代硬编码
        self.dsl_root = self.get("DSL_ROOT", os.path.join(self.workspace, "dsl-quant-trading-hybrid"))
        self.log_dir = self.get("LOG_DIR", os.path.join(self.workspace, "logs"))
        self.cache_dir = self.get("CACHE_DIR", os.path.join(self.workspace, "cache"))
        # 飞书配置
        self.feishu_app_id = self.get("FEISHU_APP_ID")
        self.feishu_app_secret = self.get("FEISHU_APP_SECRET")
        self.feishu_bitable_token = self.get("FEISHU_BITABLE_TOKEN", self.yaml_config.get("feishu", {}).get("bitable_token", ""))
        self.feishu_bitable_url = self.get("FEISHU_BITABLE_URL")
        # 邮箱告警配置 (v4.5.12 P2-13)
        self.smtp_server = self.get("SMTP_SERVER", "")
        self.smtp_port = int(self.get("SMTP_PORT", 587))
        self.smtp_user = self.get("SMTP_USER", "")
        self.smtp_password = self.get("SMTP_PASSWORD", "")
        self.alert_email = self.get("ALERT_EMAIL", "")
        # 飞书Webhook URL (与APP_ID/APP_SECRET互备)
        self.feishu_webhook_url = self.get("FEISHU_WEBHOOK_URL", "")
        # 数据源配置
        self.data_source_priority = [s.strip() for s in self.get("DATA_SOURCE_PRIORITY", "mairui,mootdx,sina,tencent,akshare").split(",")]
        self.data_source_timeout = int(self.get("DATA_SOURCE_TIMEOUT", 10))
        self.data_source_retry_times = int(self.get("DATA_SOURCE_RETRY_TIMES", 3))
        self.data_source_cache_ttl = int(self.get("DATA_SOURCE_CACHE_TTL", 3600))
        # 创建必要目录
        os.makedirs(self.log_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def get_yaml(self, path: str, default: Any = None) -> Any:
        """从yaml配置获取值，支持点号分隔的路径，比如 'scoring.sector_recommend_threshold'"""
        keys = path.split(".")
        value = self.yaml_config
        try:
            for key in keys:
                value = value[key]
            return value
        except (KeyError, TypeError):
            return default
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        value = os.getenv(key, default)
        if value and isinstance(value, str):
            return decrypt(value)
        return value

# 全局配置实例，所有脚本直接导入这个实例即可
config = Config()

def get_config(path: str = None, default: Any = None) -> Any:
    """获取配置值。支持点号分隔的路径(如 'data_client.default_source')，或返回配置实例"""
    if path is None:
        return config
    return config.get_yaml(path, default)
