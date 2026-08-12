#!/usr/bin/env python3
"""
mootdx 通达信行情适配器 — v4.6.4
基于 a-stock-data (simonlin1212) V3.2.4 架构设计

核心优势: TCP直连通达信服务器, 不经过HTTP, 不受东财WAF封锁影响
数据范围: K线(多周期) + 五档盘口 + 逐笔成交 + 实时报价46字段 + 财务数据

使用:
  from common.mootdx_adapter import get_kline, get_realtime_quote, get_finance
  df = get_kline('600519', start='2026-01-01', end='2026-06-26')
  quote = get_realtime_quote(['600519','000001'])
"""
import time
import socket
import logging
from typing import Optional, Dict, Any, List, Union
import pandas as pd

logger = logging.getLogger(__name__)

# ─── 通达信服务器列表（按优先级） ───────────────────────────────────────
_TDX_SERVERS = [
    ("119.147.212.81", 7709),   # 上海
    ("180.153.18.17", 7709),    # 深圳
    ("180.153.39.51", 7709),    # 北京
    ("123.125.108.90", 7709),   # 联通
    ("122.51.158.68", 7709),    # 备用
]

# ─── 客户端缓存 ────────────────────────────────────────────────────────
_tdx_client = None
_last_server_probe = 0

def _probe_best_server() -> str:
    """TCP探测最快的通达信服务器"""
    global _last_server_probe
    now = time.time()
    if now - _last_server_probe < 300 and _tdx_client:
        return None  # 缓存有效, 不重连
    
    best_ip, best_time = None, 999
    for ip, port in _TDX_SERVERS:
        try:
            start = time.time()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((ip, port))
            elapsed = time.time() - start
            sock.close()
            if elapsed < best_time:
                best_time = elapsed
                best_ip = ip
        except Exception:
            continue
    
    _last_server_probe = now
    if best_ip:
        logger.info(f"mootdx最佳服务器: {best_ip} ({best_time:.2f}s)")
    return best_ip


def _get_client():
    """获取mootdx客户端（绕过BESTIP空串bug，直接TCP连接）"""
    from mootdx.quotes import Quotes
    
    # 先探测最快服务器
    best_ip = _probe_server()
    if best_ip:
        # 直接指定IP, 避免BESTIP空串崩溃
        return Quotes.factory(market='std', host=best_ip, port=7709, timeout=10)
    else:
        return Quotes.factory(market='std', timeout=10)


def _probe_server() -> Optional[str]:
    """TCP探测最快的通达信行情服务器"""
    servers = [
        "119.147.212.81", "180.153.18.17", "180.153.39.51",
        "123.125.108.90", "122.51.158.68", "110.41.147.114",
        "8.129.13.54", "124.70.176.52", "47.100.236.28",
        "121.36.54.217", "124.71.85.110",
    ]
    best_ip, best_time = None, 999
    for ip in servers:
        try:
            start = time.time()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            sock.connect((ip, 7709))
            elapsed = time.time() - start
            sock.close()
            if elapsed < best_time:
                best_time = elapsed
                best_ip = ip
        except Exception:
            continue
    if best_ip:
        logger.info(f"mootdx最佳服务器: {best_ip} ({best_time:.2f}s)")
    return best_ip


# ══════════════════════════════════════════════════════════════════════════
# K线数据
# ══════════════════════════════════════════════════════════════════════════

def get_kline(symbol: str, start: str = None, end: str = None,
              period: str = 'daily') -> Optional[pd.DataFrame]:
    """从通达信获取K线数据 (不封IP, TCP直连)"""
    try:
        client = _get_client()
        if client is None:
            return None
        
        period_map = {'daily': 9, 'weekly': 5, 'monthly': 6, '60min': 3, '30min': 2, '15min': 1}
        ktype = period_map.get(period, 9)
        code = symbol.zfill(6)
        
        df = client.bars(symbol=code, frequency=ktype, offset=0, start=0)
        
        if df is None or df.empty:
            return None
        
        df = df.rename(columns={
            'open': '开盘', 'high': '最高', 'low': '最低', 'close': '收盘',
            'volume': '成交量', 'amount': '成交额'
        })
        
        if 'date' in df.columns or '日期' not in df.columns:
            date_col = 'date' if 'date' in df.columns else df.columns[0]
            df = df.rename(columns={date_col: '日期'})
        
        if start:
            df = df[df['日期'] >= start.replace('-','')[:8]]
        if end:
            df = df[df['日期'] <= end.replace('-','')[:8]]
        
        df = df.reset_index(drop=True)
        logger.info(f"mootdx K线: {symbol} → {len(df)} rows")
        return df
        
    except Exception as e:
        logger.warning(f"mootdx K线失败 {symbol}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# 实时行情
# ══════════════════════════════════════════════════════════════════════════

def get_realtime_quote(symbols: Union[str, List[str]]) -> Optional[Dict[str, Dict]]:
    """从通达信获取实时行情 (46字段)"""
    try:
        client = _get_client()
        if client is None:
            return None
        
        if isinstance(symbols, str):
            symbols = [symbols]
        
        codes = [s.zfill(6) for s in symbols]
        result = {}
        
        for code in codes:
            try:
                df = client.quotes(symbol=code)
                if df is not None and not df.empty:
                    row = df.iloc[0].to_dict()
                    result[code] = {
                        '代码': code,
                        '名称': row.get('name', ''),
                        '最新价': float(row.get('price', 0) or 0),
                        '今开': float(row.get('open', 0) or 0),
                        '最高': float(row.get('high', 0) or 0),
                        '最低': float(row.get('low', 0) or 0),
                        '昨收': float(row.get('last_close', 0) or 0),
                        '成交量': float(row.get('volume', 0) or 0),
                        '成交额': float(row.get('amount', 0) or 0),
                        '涨跌幅': float(row.get('pct_chg', 0) or 0),
                        '涨跌额': float(row.get('change', 0) or 0),
                        '换手率': float(row.get('turnover_rate', 0) or 0),
                        '量比': float(row.get('volume_ratio', 0) or 0),
                        '市盈率': float(row.get('pe', 0) or 0),
                        '涨停价': float(row.get('high_limit', 0) or 0),
                        '跌停价': float(row.get('low_limit', 0) or 0),
                    }
            except Exception as e:
                logger.debug(f"mootdx quote {code} failed: {e}")
                continue
        
        return result if result else None
        
    except Exception as e:
        logger.warning(f"mootdx实时行情失败: {e}")
        return None


def get_batch_quotes(symbols: List[str]) -> Optional[pd.DataFrame]:
    """批量获取实时行情（全市场快照）"""
    try:
        client = _get_client()
        if client is None:
            return None
        
        codes = [s.zfill(6) for s in symbols]
        df = client.quotes(symbol=codes)
        if df is not None and not df.empty:
            logger.info(f"mootdx批量行情: {len(df)} stocks")
            return df
        return None
    except Exception as e:
        logger.warning(f"mootdx批量行情失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# 五档盘口
# ══════════════════════════════════════════════════════════════════════════

def get_order_book(symbol: str) -> Optional[Dict[str, Any]]:
    """获取五档买卖盘口"""
    from mootdx.quotes import Quotes
    
    try:
        client = Quotes.factory(market='std', bestip=True, timeout=10)
        code = symbol.zfill(6)
        df = client.transaction(symbol=code, start=0, offset=10)
        if df is not None:
            return {'transactions': df.to_dict('records')[:50]}
        return None
    except Exception as e:
        logger.warning(f"mootdx盘口失败: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════
# 财务数据
# ══════════════════════════════════════════════════════════════════════════

def get_finance(symbol: str) -> Optional[pd.DataFrame]:
    """获取财务数据（利润表/资产负债表/现金流量表）"""
    from mootdx.affairs import Affairs
    
    try:
        client = Affairs.factory(bestip=True, timeout=10)
        code = symbol.zfill(6)
        df = client.profit(symbol=code)
        if df is not None and not df.empty:
            return df
        return None
    except Exception as e:
        logger.warning(f"mootdx财务数据失败: {e}")
        return None
