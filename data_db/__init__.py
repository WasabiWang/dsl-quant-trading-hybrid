"""
data_db — DSL 本地历史数据库

统一接口:
  get_data(codes, start, end, freq, fq, fields) → DataFrame
  get_kline(symbol, start, end, freq) → List[dict]  (兼容 dsl_data_sdk_original)

管理接口:
  sync_daily()                — 每日增量同步
  full_sync()                 — 首次全量同步
  sync_stock_list()           — 同步股票代码
  verify_integrity()          — 校验完整性
  get_local_stock_count()     — 本地缓存股票数
"""
from .store import get_data, read_kline, list_local_stocks, get_local_stock_count, get_local_data_range
from .sync import (
    sync_daily, full_sync, sync_stock_list, verify_integrity,
    load_stock_list, _load_sync_state as get_sync_status,
)
from .meta import sync_sector_map, get_stock_sectors
from .config import DATA_ROOT, ONEDRIVE_BACKUP_DIR


def get_kline(symbol: str, start: str, end: str, freq: str = "day") -> list:
    """兼容 dsl_data_sdk_original.get_kline 接口

    从本地 Parquet 读取，返回 List[dict] 格式
    """
    df = read_kline(symbol, start=start, end=end)
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")
