# 公共库导出
import os

# 从 VERSION 文件读取唯一版本号
_VERSION_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
try:
    with open(_VERSION_PATH, "r") as _f:
        __version__ = _f.read().strip()
except Exception:
    __version__ = "0.0.0"

from .config import config
from .logger import get_logger, catch_exception
from .feishu_utils import send_markdown, send_alert, update_bitable_record
from .data_source import get_stock_quote

__all__ = [
    "__version__",
    "config",
    "get_logger",
    "catch_exception",
    "send_markdown",
    "send_alert",
    "update_bitable_record",
    "get_stock_quote"
]
