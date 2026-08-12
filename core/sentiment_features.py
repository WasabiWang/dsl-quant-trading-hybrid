#!/usr/bin/env python3
"""
core/sentiment_features.py — DSL v4.6.5 情绪/资金流特征工程

新增因子（跨标的共享，注入build_features）：
  1. 北向资金净流入 (north_flow_net) — 当日/5日/20日均值
  2. 融资融券余额变化 (margin_balance_chg) — 杠杆情绪
  3. 市场情绪 (涨停数/跌停数/炸板率) — 极端情绪检测
  4. VIX恐慌指数 — 波动率预期
  5. 行业板块动量 — 所属行业涨跌幅排名

数据源优先级：麦蕊 > akshare > 缓存降级
"""
import os, sys, json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CACHE_DIR = os.path.join(PROJECT_ROOT, "cache", "sentiment")
os.makedirs(CACHE_DIR, exist_ok=True)


def fetch_north_flow(use_cache: bool = True) -> dict:
    """获取北向资金净流入数据（缓存24h）
    
    Returns: {"net_flow_1d": float, "net_flow_5d": float, "net_flow_20d": float}
    """
    cache_file = os.path.join(CACHE_DIR, "north_flow.json")
    today = datetime.now().strftime("%Y%m%d")
    
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("date") == today:
                return cached["data"]
        except Exception:
            pass
    
    result = {"net_flow_1d": 0.0, "net_flow_5d": 0.0, "net_flow_20d": 0.0}
    
    try:
        import akshare as ak
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is not None and len(df) > 0:
            north = df[df['资金方向'] == '北向']
            if len(north) > 0:
                result["net_flow_1d"] = round(float(north['成交净买额'].sum()), 2)
    except Exception as e:
        print(f"  ⚠️ 北向资金获取失败: {e}")
    
    # 降级：读历史缓存做5日/20日估算
    try:
        hist_file = os.path.join(CACHE_DIR, "north_flow_history.json")
        if os.path.exists(hist_file):
            with open(hist_file) as f:
                hist = json.load(f)
            flows = hist[-20:] if len(hist) >= 20 else hist
            result["net_flow_5d"] = round(np.mean(flows[-5:]), 2) if len(flows) >= 5 else result["net_flow_1d"]
            result["net_flow_20d"] = round(np.mean(flows), 2)
    except Exception:
        pass
    
    # 写缓存
    try:
        with open(cache_file, 'w') as f:
            json.dump({"date": today, "data": result}, f)
        # 追加历史
        hist = []
        hist_file = os.path.join(CACHE_DIR, "north_flow_history.json")
        if os.path.exists(hist_file):
            with open(hist_file) as f:
                hist = json.load(f)
        hist.append(result["net_flow_1d"])
        hist = hist[-60:]  # 保留60天
        with open(hist_file, 'w') as f:
            json.dump(hist, f)
    except Exception:
        pass
    
    return result


def fetch_margin_balance(use_cache: bool = True) -> float:
    """获取融资余额变化率（周度）"""
    cache_file = os.path.join(CACHE_DIR, "margin_balance.json")
    today = datetime.now().strftime("%Y%m%d")
    
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("date") == today:
                return cached["data"]
        except Exception:
            pass
    
    result = 0.0
    try:
        import akshare as ak
        df = ak.stock_margin_detail_sse(date=today)
        if df is not None and len(df) > 0:
            total_balance = df['融资余额'].sum()
            # 与前5日均值对比
            prev_dates = []
            for i in range(1, 8):
                d = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
                prev_dates.append(d)
            prev_dfs = []
            for d in prev_dates[:3]:
                try:
                    pdf = ak.stock_margin_detail_sse(date=d)
                    if pdf is not None and len(pdf) > 0:
                        prev_dfs.append(pdf['融资余额'].sum())
                except Exception:
                    pass
            if prev_dfs:
                prev_mean = np.mean(prev_dfs)
                if prev_mean > 0:
                    result = round((total_balance / prev_mean - 1) * 100, 2)
    except Exception:
        pass
    
    try:
        with open(cache_file, 'w') as f:
            json.dump({"date": today, "data": result}, f)
    except Exception:
        pass
    
    return result


def fetch_market_sentiment(use_cache: bool = True) -> dict:
    """获取市场情绪：涨停数/跌停数/炸板率
    
    Returns: {"limit_up_count": int, "limit_down_count": int, "broken_board_ratio": float}
    """
    cache_file = os.path.join(CACHE_DIR, "market_sentiment.json")
    today = datetime.now().strftime("%Y-%m-%d")
    
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("date") == today:
                return cached["data"]
        except Exception:
            pass
    
    result = {"limit_up_count": 0, "limit_down_count": 0, "broken_board_ratio": 0.0}
    
    try:
        from config.mairui_api_config import get_limit_up_list, get_limit_down_list, get_broken_board
        up_list = get_limit_up_list(today) or []
        down_list = get_limit_down_list(today) or []
        broken_list = get_broken_board(today) or []
        result["limit_up_count"] = len(up_list)
        result["limit_down_count"] = len(down_list)
        total_boards = len(up_list) + len(broken_list)
        if total_boards > 0:
            result["broken_board_ratio"] = round(len(broken_list) / total_boards, 3)
    except Exception as e:
        print(f"  ⚠️ 麦蕊情绪数据失败: {e}")
        # 降级 akshare
        try:
            import akshare as ak
            date_str = today.replace("-", "")
            df_up = ak.stock_zt_pool_em(date=date_str)
            if df_up is not None:
                result["limit_up_count"] = len(df_up)
        except Exception:
            pass
    
    try:
        with open(cache_file, 'w') as f:
            json.dump({"date": today, "data": result}, f)
    except Exception:
        pass
    
    return result


def fetch_vix(use_cache: bool = True) -> float:
    """获取VIX恐慌指数 (yfinance → Sina降级)"""
    cache_file = os.path.join(CACHE_DIR, "vix.json")
    today = datetime.now().strftime("%Y%m%d")
    
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("date") == today:
                return cached["data"]
        except Exception:
            pass
    
    result = 20.0  # 默认值(历史均值)
    
    try:
        import yfinance as yf
        ticker = yf.Ticker('^VIX')
        hist = ticker.history(period='5d')
        if hist is not None and len(hist) > 0:
            result = round(float(hist['Close'].iloc[-1]), 2)
    except Exception:
        try:
            import requests
            resp = requests.get("https://hq.sinajs.cn/list=gb_vix",
                               headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
            if resp.status_code == 200:
                parts = resp.text.split('"')[1].split(',')
                if len(parts) > 1:
                    result = float(parts[1])
        except Exception:
            pass
    
    try:
        with open(cache_file, 'w') as f:
            json.dump({"date": today, "data": result}, f)
    except Exception:
        pass
    
    return result


def fetch_sector_momentum(symbol: str = None, use_cache: bool = True) -> dict:
    """获取行业板块动量（所属行业涨跌幅排名）
    
    麦蕊API无板块涨跌幅 → 降级akshare stock_board_industry_name_em
    
    Returns: {symbol: {sector_chg, sector_rank_pct, sector_name}}
    """
    cache_file = os.path.join(CACHE_DIR, "sector_momentum.json")
    today = datetime.now().strftime("%Y%m%d")
    
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("date") == today:
                return cached["data"]
        except Exception:
            pass
    
    result = {}
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is not None and len(df) > 0:
            sectors = []
            for _, row in df.iterrows():
                sectors.append({
                    "name": str(row.get("板块名称", "")),
                    "chg": float(row.get("涨跌幅", 0) or 0),
                })
            sectors.sort(key=lambda x: x["chg"], reverse=True)
            total = len(sectors)
            for rank, s in enumerate(sectors):
                s["rank_pct"] = round((total - rank) / total, 3)  # 排名百分位
            # 构建查找表
            for s in sectors:
                result[s["name"]] = {
                    "sector_chg": s["chg"],
                    "sector_rank_pct": s["rank_pct"],
                    "sector_name": s["name"],
                }
    except Exception as e:
        print(f"  ⚠️ 行业板块动量失败: {e}")
    
    try:
        with open(cache_file, 'w') as f:
            json.dump({"date": today, "data": result}, f)
    except Exception:
        pass
    
    return result


def get_sentiment_features(symbol: str, stock_name: str = "", sector: str = "") -> dict:
    """一站式获取全部情绪特征（带缓存降级）
    
    Args:
        symbol: 股票代码
        stock_name: 股票名称（行业匹配用，可留空）
        sector: 行业名（如有则直接匹配，否则从stock_name推断）
    
    Returns:
        dict with: north_flow_1d, north_flow_5d, north_flow_20d,
                   margin_chg, limit_up_count, limit_down_count,
                   broken_board_ratio, vix, sector_chg, sector_rank_pct
    """
    features = {
        "north_flow_1d": 0.0,
        "north_flow_5d": 0.0,
        "north_flow_20d": 0.0,
        "margin_chg": 0.0,
        "limit_up_count": 0,
        "limit_down_count": 0,
        "broken_board_ratio": 0.0,
        "vix": 20.0,
        "sector_chg": 0.0,
        "sector_rank_pct": 0.5,
    }
    
    # 北向资金
    try:
        nf = fetch_north_flow()
        features.update({k: v for k, v in nf.items() if k in features})
    except Exception:
        pass
    
    # 融资融券
    try:
        features["margin_chg"] = fetch_margin_balance()
    except Exception:
        pass
    
    # 市场情绪
    try:
        ms = fetch_market_sentiment()
        features.update({k: v for k, v in ms.items() if k in features})
    except Exception:
        pass
    
    # VIX
    try:
        features["vix"] = fetch_vix()
    except Exception:
        pass
    
    # 行业板块动量
    try:
        sm = fetch_sector_momentum()
        # 尝试匹配行业
        if not sector and stock_name:
            # 从名称推断行业（简单规则）
            if any(kw in stock_name for kw in ["银行", "招商", "平安"]):
                sector = "银行"
            elif any(kw in stock_name for kw in ["白酒", "茅台", "五粮液", "汾酒"]):
                sector = "酿酒行业"
            elif any(kw in stock_name for kw in ["锂", "电池", "能源"]):
                sector = "电池"
            elif any(kw in stock_name for kw in ["药", "医", "生物"]):
                sector = "医药"
            elif any(kw in stock_name for kw in ["电", "光伏", "储能"]):
                sector = "电力"
            elif any(kw in stock_name for kw in ["芯", "半导", "电子", "存储"]):
                sector = "半导体"
            elif any(kw in stock_name for kw in ["信", "通信", "光"]):
                sector = "通信"
        
        if sector:
            for sname, sdata in sm.items():
                if sector in sname or (stock_name and stock_name[:2] in sname):
                    features["sector_chg"] = sdata.get("sector_chg", 0)
                    features["sector_rank_pct"] = sdata.get("sector_rank_pct", 0.5)
                    break
    except Exception:
        pass
    
    return features


# ====== 批量预取（train阶段一次性获取，避免逐股重复请求） ======

_sentiment_cache = None

def preload_sentiment_features(force_refresh: bool = False):
    """批量预取所有情绪特征，供batch_train在循环前调用"""
    global _sentiment_cache
    if _sentiment_cache is not None and not force_refresh:
        return _sentiment_cache
    
    _sentiment_cache = get_sentiment_features("")
    
    # 预取行业动量（全量）
    try:
        _sentiment_cache["_all_sectors"] = fetch_sector_momentum(use_cache=not force_refresh)
    except Exception:
        _sentiment_cache["_all_sectors"] = {}
    
    return _sentiment_cache


def inject_sentiment_to_df(df: pd.DataFrame, symbol: str, stock_name: str = "", 
                           sector: str = "", preloaded: dict = None) -> pd.DataFrame:
    """将情绪特征注入DataFrame（按行广播展开）
    
    情绪特征是日级标量，需要按df的日期索引对齐展开为列。
    如果preloaded已有全量缓存，直接使用（避免逐股请求）。
    """
    if preloaded and "_all_sectors" in preloaded:
        feats = dict(preloaded)
        # 匹配行业
        sm = feats.pop("_all_sectors", {})
        if sector:
            for sname, sdata in sm.items():
                if sector in sname:
                    feats["sector_chg"] = sdata.get("sector_chg", 0)
                    feats["sector_rank_pct"] = sdata.get("sector_rank_pct", 0.5)
                    break
    else:
        feats = get_sentiment_features(symbol, stock_name, sector)
    
    # 按行广播：每个特征扩展为df长度的一列
    for key in ["north_flow_1d", "north_flow_5d", "north_flow_20d",
                "margin_chg", "limit_up_count", "limit_down_count",
                "broken_board_ratio", "vix", "sector_chg", "sector_rank_pct"]:
        val = feats.get(key, 0)
        df[f"sent_{key}"] = float(val) if val is not None else 0.0
    
    return df
