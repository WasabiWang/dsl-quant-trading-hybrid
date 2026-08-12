"""
Parquet 存储引擎 — 读写历史K线数据
"""
import os
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from typing import Optional, Union, List
from datetime import datetime, timedelta
from .config import (
    DAILY_DIR, META_DIR, KLINE_FIELDS, ALL_FIELDS, PARQUET_COMPRESSION
)

# ======================================================================
# 工具函数
# ======================================================================

def _stock_file(code: str) -> str:
    """股票对应的 Parquet 文件名"""
    return f"{code}.parquet"


def _stock_path(code: str) -> str:
    return os.path.join(DAILY_DIR, _stock_file(code))


def _normalize_code(code: str) -> str:
    """统一代码格式: 去除 sh/sz 前缀和 .SH/.SZ 后缀"""
    code = code.upper().strip()
    # 去掉 .SH/.SZ/.BJ 后缀
    for suf in [".SH", ".SZ", ".BJ"]:
        code = code.replace(suf, "")
    # 去掉 sh/sz 前缀
    if code.startswith(("SH", "SZ")):
        code = code[2:]
    return code


def _ensure_suffix(code: str) -> str:
    """给纯数字代码加交易所后缀"""
    c = _normalize_code(code)
    if c.startswith("6") or c.startswith("9"):
        return f"{c}.SH"
    elif c.startswith("0") or c.startswith("3") or c.startswith("2"):
        return f"{c}.SZ"
    elif c.startswith("4") or c.startswith("8"):
        return f"{c}.BJ"
    return c


# ======================================================================
# 写入
# ======================================================================

def write_kline(df: pd.DataFrame, code: str, append: bool = True) -> bool:
    """写入日线数据到 Parquet

    Args:
        df: 必须包含 date,open,high,low,close,volume 列
        code: 股票代码（纯数字或带后缀）
        append: 是否追加（True=合并去重，False=覆盖）

    Returns:
        bool: 是否写入成功
    """
    try:
        code = _ensure_suffix(_normalize_code(code))
        path = _stock_path(code)

        # 标准化字段
        df = df.copy()
        df.columns = [c.lower().strip() for c in df.columns]
        if "date" not in df.columns and "trade_date" in df.columns:
            df.rename(columns={"trade_date": "date"}, inplace=True)
        if "date" not in df.columns:
            raise ValueError(f"数据缺少 date 列: {df.columns.tolist()}")

        # 确保日期是字符串 YYYY-MM-DD
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

        # 数值列转换
        for col in KLINE_FIELDS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        # 缺失字段补零
        for col in ALL_FIELDS:
            if col not in df.columns and col != "date":
                df[col] = 0.0

        # 只保留需要的字段
        keep_cols = [c for c in ALL_FIELDS if c in df.columns]
        df = df[keep_cols]

        if append and os.path.exists(path):
            existing = pd.read_parquet(path)
            existing["date"] = existing["date"].astype(str)
            df["date"] = df["date"].astype(str)
            combined = pd.concat([existing, df], ignore_index=True)
            combined.drop_duplicates(subset=["date"], keep="last", inplace=True)
            combined.sort_values("date", inplace=True)
            combined.reset_index(drop=True, inplace=True)
            table = pa.Table.from_pandas(combined, preserve_index=False)
        else:
            df.sort_values("date", inplace=True)
            df.reset_index(drop=True, inplace=True)
            table = pa.Table.from_pandas(df, preserve_index=False)

        pq.write_table(table, path, compression=PARQUET_COMPRESSION)
        return True

    except Exception as e:
        print(f"  ❌ 写入失败 {code}: {e}")
        return False


# ======================================================================
# 查询
# ======================================================================

def read_kline(code: str, start: Optional[str] = None,
               end: Optional[str] = None,
               fields: Optional[List[str]] = None) -> Optional[pd.DataFrame]:
    """读取单只股票的日线数据

    Args:
        code: 股票代码（纯数字或带后缀 .SH/.SZ）
        start: 起始日期 YYYY-MM-DD（None=不限）
        end: 结束日期 YYYY-MM-DD（None=不限）
        fields: 返回字段（None=返回全部）

    Returns:
        DataFrame 或 None（本地无数据）
    """
    code = _ensure_suffix(_normalize_code(code))
    path = _stock_path(code)

    if not os.path.exists(path):
        return None

    try:
        df = pd.read_parquet(path)
        df["date"] = df["date"].astype(str)

        # 内部用pctchg, 对外统一暴露pctChg
        if "pctchg" in df.columns and "pctChg" not in df.columns:
            df["pctChg"] = df["pctchg"]

        if start:
            df = df[df["date"] >= start]
        if end:
            df = df[df["date"] <= end]
        if fields:
            # 自动映射 field 中的 pctChg → pctchg
            mapped = []
            for f in fields:
                if f in df.columns:
                    mapped.append(f)
                elif f == "pctChg" and "pctchg" in df.columns:
                    mapped.append("pctchg")
                elif f == "pctchg" and "pctChg" in df.columns:
                    mapped.append("pctChg")
            df = df[mapped] if mapped else df

        return df.sort_values("date").reset_index(drop=True)

    except Exception as e:
        print(f"  ⚠️ 读取失败 {code}: {e}")
        return None


def get_data(codes: Union[str, List[str]],
             start: Optional[str] = None,
             end: Optional[str] = None,
             freq: str = "day",
             fq: Optional[str] = "forward",
             fields: Optional[List[str]] = None) -> pd.DataFrame:
    """统一查询接口 — 对齐 free-stockdb 的 get_data() 语义

    Args:
        codes: 单只股票代码或列表
        start: 起始日期 YYYY-MM-DD
        end: 结束日期 YYYY-MM-DD
        freq: 频率，目前仅支持 "day"
        fq: 复权类型: "forward"(前复权), "backward"(后复权), None(不复权)
        fields: 返回字段

    Returns:
        DataFrame: 多股票纵向拼接，含 code 列区分来源
    """
    if isinstance(codes, str):
        codes = [codes]

    all_parts = []
    for code in codes:
        df = read_kline(code, start, end, fields)
        if df is not None and not df.empty:
            df["code"] = _ensure_suffix(_normalize_code(code))
            all_parts.append(df)

    if not all_parts:
        return pd.DataFrame()

    result = pd.concat(all_parts, ignore_index=True)
    result = result.sort_values(["code", "date"]).reset_index(drop=True)

    # 内部用 lowercase, 对外暴露 pctChg (大写C)
    if "pctchg" in result.columns and "pctChg" not in result.columns:
        result["pctChg"] = result["pctchg"]
        result.drop(columns=["pctchg"], inplace=True)

    return result


# ======================================================================
# 元数据：本地有哪些股票
# ======================================================================

def list_local_stocks() -> List[str]:
    """返回本地已缓存的所有股票代码列表"""
    if not os.path.exists(DAILY_DIR):
        return []
    files = [f for f in os.listdir(DAILY_DIR) if f.endswith(".parquet")]
    return sorted([f.replace(".parquet", "") for f in files])


def get_local_stock_count() -> int:
    return len(list_local_stocks())


def get_local_data_range(code: str) -> Optional[tuple]:
    """返回 (最早日期, 最晚日期) 或 None"""
    df = read_kline(code)
    if df is None or df.empty:
        return None
    dates = sorted(df["date"].dropna().unique())
    if not dates:
        return None
    return (dates[0], dates[-1])
