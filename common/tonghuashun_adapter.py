#!/usr/bin/env python3
"""
同花顺+巨潮数据适配器 — v4.6.4 (修正API端点)
基于 a-stock-data V3.2.3 原始 SKILL.md 逐API校对

数据源:
  - 同花顺强势股: zx.10jqka.com.cn (HTTP, 零鉴权, 73ms)
  - 同花顺北向资金: data.hexin.cn (HTTP, hsgtApi)
  - 巨潮公告: cninfo.com.cn (POST, 动态orgId映射)

优先级: 麦蕊(主) → mootdx(TCP行情) → 同花顺(信号) → 新浪(全量行情)
"""
import time
import json
import logging
from typing import Optional, Dict, Any, List
from pathlib import Path

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ─── 共享Headers ───────────────────────────────────────────────────────
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36"


def _get_session(headers: dict = None) -> requests.Session:
    """获取基础Session（trust_env=False，绕过系统代理）"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({'User-Agent': UA})
    if headers:
        s.headers.update(headers)
    return s


# ══════════════════════════════════════════════════════════════════════════
# §3.1 同花顺强势股 + 题材归因 (独家)
# ══════════════════════════════════════════════════════════════════════════

def get_hot_stocks(date: str = None) -> Optional[pd.DataFrame]:
    """
    同花顺当日强势股归因（编辑部人工标注的题材标签）
    
    Args:
        date: 'YYYY-MM-DD', None=今天
    
    Returns:
        DataFrame columns: 代码, 名称, 题材归因, 涨幅%, 换手率%, 成交额
        实测: 73ms, ~125只
    """
    if date is None:
        from datetime import date as _date
        date = _date.today().strftime("%Y-%m-%d")
    
    try:
        url = (f"http://zx.10jqka.com.cn/event/api/getharden/"
               f"date/{date}/orderby/date/orderway/desc/charset/GBK/")
        
        s = _get_session()
        r = s.get(url, timeout=10)
        
        if r.status_code != 200:
            logger.warning(f"同花顺热点HTTP {r.status_code}")
            return None
        
        data = r.json()
        if data.get("errocode", 0) != 0:
            logger.warning(f"同花顺热点API error: {data.get('errormsg','')}")
            return None
        
        rows = data.get("data") or []
        if not rows:
            return None
        
        df = pd.DataFrame(rows)
        # API返回字段: id, name, code, reason, date, market
        # (注: V3.2.3 SKILL.md记载更多字段, 但当前API仅返回这6个)
        rename_map = {
            "name": "名称", "code": "代码", "reason": "题材归因",
            "date": "日期", "market": "市场",
        }
        df = df.rename(columns={k:v for k,v in rename_map.items() if k in df.columns})
        
        logger.info(f"同花顺热点: {len(df)} 只强势股")
        return df
    
    except Exception as e:
        logger.warning(f"同花顺热点失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# §3.2 同花顺北向资金 (hsgtApi)
# ══════════════════════════════════════════════════════════════════════════

HSGT_HEADERS = {
    "User-Agent": UA,
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}


def get_north_flow_minutes(date: str = None) -> Optional[pd.DataFrame]:
    """
    沪深股通当日实时分钟流向（262个时间点，含集合竞价09:10-15:00）
    
    Returns:
        DataFrame columns: time, hgt_yi(沪股通累计净买入,亿元), sgt_yi(深股通累计净买入,亿元)
    """
    try:
        s = _get_session(HSGT_HEADERS)
        url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        r = s.get(url, timeout=10)
        
        if r.status_code != 200:
            logger.warning(f"北向资金HTTP {r.status_code}")
            return None
        
        d = r.json()
        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])
        
        n = len(times)
        df = pd.DataFrame({
            "time": times,
            "hgt_yi": hgt[:n] + [None] * max(0, n - len(hgt)),
            "sgt_yi": sgt[:n] + [None] * max(0, n - len(sgt)),
        })
        
        # 数值化
        df['hgt_yi'] = pd.to_numeric(df['hgt_yi'], errors='coerce')
        df['sgt_yi'] = pd.to_numeric(df['sgt_yi'], errors='coerce')
        df['net_yi'] = df['hgt_yi'] + df['sgt_yi']
        
        logger.info(f"同花顺北向: {len(df)} 分钟点")
        return df
        
    except Exception as e:
        logger.warning(f"同花顺北向失败: {e}")
        return None


def get_north_flow_daily(days: int = 30) -> Optional[pd.DataFrame]:
    """
    北向资金日级历史（本地CSV自缓存）
    注: 东财北向数据自2024-08后净买额返回NaN/0，改为此本地缓存模式。
    """
    cache_path = Path.home() / ".tradingagents" / "cache" / "northbound_daily.csv"
    
    if not cache_path.exists():
        logger.warning(f"北向缓存不存在: {cache_path}")
        return None
    
    try:
        df = pd.read_csv(cache_path)
        df = df.tail(days)
        return df
    except Exception as e:
        logger.warning(f"北向历史读取失败: {e}")
        return None


def save_northbound_snapshot(date: str, hgt: float, sgt: float):
    """写入/更新当天北向收盘数据到CSV（收盘后由定时任务调用）"""
    cache_path = Path.home() / ".tradingagents" / "cache" / "northbound_daily.csv"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    
    rows = {}
    if cache_path.exists():
        for line in cache_path.read_text().strip().split("\n")[1:]:
            parts = line.split(",")
            if len(parts) == 3:
                rows[parts[0]] = line
    rows[date] = f"{date},{hgt},{sgt}"
    
    with open(cache_path, "w") as f:
        f.write("date,hgt,sgt\n")
        for d in sorted(rows.keys()):
            f.write(rows[d] + "\n")


# ══════════════════════════════════════════════════════════════════════════
# §7.1 巨潮公告 (#19动态orgId映射)
# ══════════════════════════════════════════════════════════════════════════

_CNINFO_ORGID_MAP = {}


def _cninfo_orgid(code: str) -> str:
    """
    查股票真实orgId。
    巨潮orgId并非统一 gssx0{code} 格式（如601318→9900002221）
    #19修复: 动态查官方映射表 szse_stock.json (6198只股)
    """
    global _CNINFO_ORGID_MAP
    if not _CNINFO_ORGID_MAP:
        try:
            s = _get_session()
            r = s.get("http://www.cninfo.com.cn/new/data/szse_stock.json", timeout=15)
            _CNINFO_ORGID_MAP = {
                item["code"]: item["orgId"]
                for item in r.json().get("stockList", [])
            }
            logger.info(f"巨潮orgId映射: {len(_CNINFO_ORGID_MAP)} stocks loaded")
        except Exception as e:
            logger.warning(f"巨潮orgId映射拉取失败: {e}")
    
    org = _CNINFO_ORGID_MAP.get(code)
    if org:
        return org
    
    # fallback: 硬编码格式
    if code.startswith("6"):
        return f"gssh0{code}"
    elif code.startswith(("8", "4")):
        return f"gsbj0{code}"
    return f"gssz0{code}"


def get_announcements(symbol: str, pages: int = 1, page_size: int = 30) -> Optional[List[Dict]]:
    """
    巨潮公告全文检索（沪深北全量）
    
    Args:
        symbol: '600519'
        pages: 获取页数
        page_size: 每页数量
    
    Returns:
        [{title, type, date, url}]
    """
    try:
        code = symbol.zfill(6)
        org_id = _cninfo_orgid(code)
        
        url = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
        headers = {
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.cninfo.com.cn/new/disclosure",
            "Origin": "https://www.cninfo.com.cn",
        }
        
        results = []
        for page in range(1, pages + 1):
            payload = {
                "stock": f"{code},{org_id}",
                "tabName": "fulltext",
                "pageSize": str(page_size),
                "pageNum": str(page),
                "column": "",
                "category": "",
                "plate": "",
                "seDate": "",
                "searchkey": "",
                "secid": "",
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            }
            
            s = _get_session(headers)
            r = s.post(url, data=payload, timeout=15)
            
            if r.status_code != 200:
                logger.warning(f"巨潮公告HTTP {r.status_code}")
                break
            
            d = r.json()
            for item in d.get("announcements", []) or []:
                # announcementTime是Unix毫秒
                ts = item.get("announcementTime")
                if isinstance(ts, (int, float)):
                    from datetime import datetime
                    dt = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                else:
                    dt = str(ts)[:10] if ts else ""
                
                results.append({
                    "标题": item.get("announcementTitle", ""),
                    "日期": dt,
                    "类型": item.get("announcementTypeName", ""),
                    "URL": f"https://www.cninfo.com.cn/new/disclosure/detail?annoId={item.get('announcementId', '')}",
                })
        
        logger.info(f"巨潮公告 {symbol}: {len(results)} found")
        return results
        
    except Exception as e:
        logger.warning(f"巨潮公告失败 {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# 同花顺一致预期EPS (保留, 使用10jqka basic端点)
# ══════════════════════════════════════════════════════════════════════════

def get_consensus_eps(symbol: str) -> Optional[Dict[str, Any]]:
    """
    获取机构一致预期EPS (同花顺basic.10jqka.com.cn)
    注: 此端点返回HTML需解析, 精确数据建议用东财reportapi
    """
    try:
        code = symbol.zfill(6)
        url = f"https://basic.10jqka.com.cn/{code}/"
        s = _get_session()
        r = s.get(url, timeout=10)
        
        if r.status_code != 200:
            return None
        
        text = r.text
        import re
        
        result = {'代码': code}
        eps_pattern = r'"yysr":\s*\[([^\]]+)\]'
        eps_match = re.search(eps_pattern, text)
        if eps_match:
            values = [float(v) for v in eps_match.group(1).split(',') if v.strip()]
            if len(values) >= 3:
                result['eps_this_year'] = values[0]
                result['eps_next_year'] = values[1]
                result['eps_year_after'] = values[2]
        
        return result if len(result) > 1 else None
        
    except Exception as e:
        logger.warning(f"一致预期失败 {symbol}: {e}")
        return None
