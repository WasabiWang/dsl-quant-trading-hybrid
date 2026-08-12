"""
data_db 配置 — 本地Parquet历史数据库路径和参数
"""
import os
from pathlib import Path

# ====== 数据存储根目录 ======
# 大文件（Parquet）与 DSL 项目分离，避免混淆版本管理
DATA_ROOT = Path(os.path.expanduser("~/.openclaw/workspace/dsl-quant-data"))

# 日线 Parquet: daily/000001.SZ.parquet （每只股票一个文件）
DAILY_DIR = DATA_ROOT / "daily"

# 元数据: 股票列表、板块映射
META_DIR = DATA_ROOT / "meta"

# 同步状态: 记录各数据源最后同步时间
SYNC_DIR = DATA_ROOT / "sync"

# ====== OneDrive 备份目标 ======
ONEDRIVE_BACKUP_DIR = Path(os.path.expanduser("~/OneDrive/DSL_Backup/dsl_hist_data"))

# ====== 同步参数 ======
# 初始全量时取多少年历史
INITIAL_YEARS = 5

# 增量同步时最多重试次数
MAX_RETRIES = 3

# 并行下载线程数（akshare 有限频，不宜过大）
SYNC_WORKERS = 8

# Parquet 压缩格式
PARQUET_COMPRESSION = "zstd"

# ====== 默认字段 ======
KLINE_FIELDS = ["open", "high", "low", "close", "volume", "amount", "pctchg"]
ALL_FIELDS = ["date", "open", "high", "low", "close", "volume", "amount", "pctchg", "turn", "adjust_factor"]

# ====== 确保目录存在 ======
for d in [DAILY_DIR, META_DIR, SYNC_DIR, ONEDRIVE_BACKUP_DIR]:
    d.mkdir(parents=True, exist_ok=True)
