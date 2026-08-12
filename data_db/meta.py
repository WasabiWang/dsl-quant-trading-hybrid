"""
板块映射 + 股票元数据管理
"""
import pandas as pd
import akshare as ak
from typing import Optional, Dict, List
from .config import META_DIR

SECTOR_FILE = META_DIR / "sector_map.parquet"


def sync_sector_map() -> pd.DataFrame:
    """同步申万行业 + 概念板块映射"""
    results = {}
    try:
        df = ak.stock_board_industry_name_em()
        if df is not None:
            df.columns = [c.lower().strip() for c in df.columns]
            results["shenwan"] = {"count": len(df)}
            df.to_parquet(META_DIR / "board_industry.parquet", compression="zstd")
            print(f"  ✅ 申万行业: {len(df)} 个板块")
    except Exception as e:
        print(f"  ⚠️ 申万行业获取失败: {e}")

    try:
        df = ak.stock_board_concept_name_em()
        if df is not None:
            df.columns = [c.lower().strip() for c in df.columns]
            results["concept"] = {"count": len(df)}
            df.to_parquet(META_DIR / "board_concept.parquet", compression="zstd")
            print(f"  ✅ 概念板块: {len(df)} 个板块")
    except Exception as e:
        print(f"  ⚠️ 概念板块获取失败: {e}")

    # 合并元数据
    meta = pd.DataFrame([results])
    meta.to_parquet(SECTOR_FILE, compression="zstd")
    return meta


def get_stock_sectors(code: str) -> Dict[str, List[str]]:
    """获取单只股票所属板块"""
    result = {"industry": [], "concept": []}
    try:
        df = ak.stock_board_industry_cons_em(symbol=code)
        if df is not None and "板块名称" in df.columns:
            result["industry"] = df["板块名称"].tolist()
    except Exception:
        pass
    try:
        df = ak.stock_board_concept_cons_em(symbol=code)
        if df is not None and "板块名称" in df.columns:
            result["concept"] = df["概念名称"].tolist() if "概念名称" in df.columns else df["板块名称"].tolist()
    except Exception:
        pass
    return result
