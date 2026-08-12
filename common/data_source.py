#!/usr/bin/env python3
# 统一数据源抽象层，支持多源自动故障转移、熔断降级、缓存
import time
import json
import os
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
import pandas as pd
import requests
from .config import config
from .logger import get_logger, catch_exception

logger = get_logger("data_source")
CACHE_FILE = os.path.join(config.cache_dir, "stock_cache.json")

# 缓存结构：key=股票代码+日期，value=数据，过期时间=DATA_SOURCE_CACHE_TTL
_cache: Dict[str, Dict[str, Any]] = {}
_last_cache_save_time: float = 0

def _load_cache():
    """加载本地缓存"""
    global _cache
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                _cache = json.load(f)
            logger.info(f"加载缓存成功，共{len(_cache)}条记录")
        except Exception as e:
            logger.warning(f"加载缓存失败：{str(e)}")

def _save_cache():
    """保存缓存到本地，每10分钟保存一次"""
    global _last_cache_save_time
    now = time.time()
    if now - _last_cache_save_time < 600:  # 10分钟
        return
    try:
        with open(CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2)
        _last_cache_save_time = now
        logger.debug("缓存保存成功")
    except Exception as e:
        logger.warning(f"保存缓存失败：{str(e)}")

def _get_cache_key(stock_code: str, data_type: str = "quote") -> str:
    """生成缓存key"""
    today = time.strftime("%Y%m%d")
    return f"{stock_code}_{data_type}_{today}"

# 加载缓存
_load_cache()

class BaseDataSource(ABC):
    """数据源基类，所有数据源都要实现这个接口"""
    name = "base"
    
    @abstractmethod
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """
        获取股票实时行情
        :param stock_code: 股票代码，比如000001.SZ、000001.SS、00700.HK
        :return: 行情数据字典，包含字段：code, name, price, change, change_pct, open, high, low, volume, amount
        """
        pass

# P1-FIX: 懒加载dsl_data_sdk，避免模块级import导致整个数据层崩溃
try:
    from dsl_data_sdk import get_price as sdk_get_price
except ImportError:
    sdk_get_price = None

class MootdxDataSource(BaseDataSource):
    """通达信TCP数据源 — v4.6.4: TCP直连, 不受HTTP WAF封锁影响, 46字段"""
    name = "mootdx"
    priority = 2  # 麦蕊(1) → mootdx(2) → 新浪(3)
    
    @catch_exception(logger, alert=False)
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        from .mootdx_adapter import get_realtime_quote
        
        code = stock_code.split('.')[0] if '.' in stock_code else stock_code
        result = get_realtime_quote(code)
        
        if result and code in result:
            q = result[code]
            return {
                "code": stock_code,
                "name": q.get('名称', ''),
                "price": q.get('最新价', 0),
                "change": q.get('涨跌额', 0),
                "change_pct": q.get('涨跌幅', 0),
                "open": q.get('今开', 0),
                "high": q.get('最高', 0),
                "low": q.get('最低', 0),
                "volume": q.get('成交量', 0),
                "amount": q.get('成交额', 0),
                "pe": q.get('市盈率', 0),
                "high_limit": q.get('涨停价', 0),
                "low_limit": q.get('跌停价', 0),
            }
        return None


class SinaDataSource(BaseDataSource):
    """新浪财经数据源已被统一数据 SDK 替代，内部实现直接调用 SDK"""
    name = "sina"
    
    @catch_exception(logger, alert=False)
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """使用 DSL 数据 SDK 获取行情，并补全 `change` 与 `change_pct` 字段以保持原有接口兼容"""
        if sdk_get_price is None:
            logger.warning("dsl_data_sdk未安装，SinaDataSource不可用")
            return None
        data = sdk_get_price(stock_code)
        if not data:
            return None
        # 计算前收盘价（close_prev）如果不存在则尝试从 price - change 估算
        price = data.get('price')
        close_prev = data.get('close_prev')
        change = data.get('change')
        if close_prev is None:
            if price is not None and change is not None:
                close_prev = price - change
            else:
                close_prev = price
        # 计算 change 和 change_pct 如原实现所需（A 股）
        if "HK" in stock_code:
            # 港股已提供 change、change_pct，直接返回
            result = {
                "code": stock_code,
                "name": data.get('name'),
                "price": price,
                "change": data.get('change'),
                "change_pct": data.get('change_pct'),
                "open": data.get('open'),
                "high": data.get('high'),
                "low": data.get('low'),
                "volume": data.get('volume'),
                "amount": data.get('amount')
            }
        else:
            # A 股：自行计算 change 与 change_pct
            computed_change = price - close_prev if price is not None and close_prev is not None else 0.0
            computed_change_pct = (computed_change / close_prev) * 100 if close_prev and close_prev != 0 else 0.0
            result = {
                "code": stock_code,
                "name": data.get('name'),
                "price": price,
                "change": computed_change,
                "change_pct": computed_change_pct,
                "open": data.get('open'),
                "high": data.get('high'),
                "low": data.get('low'),
                "volume": data.get('volume'),
                "amount": data.get('amount')
            }
        return result

class TencentDataSource(BaseDataSource):
    """腾讯财经数据源，备用源"""
    name = "tencent"
    
    @catch_exception(logger, alert=False)
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        # 转换腾讯代码格式
        if stock_code.endswith(".SZ"):
            tx_code = f"sz{stock_code[:6]}"
        elif stock_code.endswith(".SH"):
            tx_code = f"sh{stock_code[:6]}"
        elif stock_code.endswith(".HK"):
            tx_code = f"hk{stock_code[:5]}"
        else:
            tx_code = stock_code
        
        url = f"https://qt.gtimg.cn/q={tx_code}"
        resp = requests.get(url, timeout=config.data_source_timeout, proxies={"http": "", "https": ""})
        resp.encoding = "gbk"
        data = resp.text.split('"')[1].split('~')
        
        if len(data) < 30:
            return None
        
        return {
            "code": stock_code,
            "name": data[1],
            "price": float(data[3]),
            "change": float(data[31]),
            "change_pct": float(data[32]),
            "open": float(data[5]),
            "high": float(data[33]),
            "low": float(data[34]),
            "volume": float(data[36]),
            "amount": float(data[37])
        }

class EastmoneyDataSource(BaseDataSource):
    """东方财富数据源 — v4.6.4: push2 API已封锁，标记为不可用，自动跳过"""
    name = "eastmoney"
    
    @catch_exception(logger, alert=False)
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        # v4.6.4: push2.eastmoney.com API已封锁，立即返回None触发降级
        logger.debug(f"EastmoneyDataSource已弃用(push2 API封锁)，跳过{stock_code}")
        return None

class AkshareDataSource(BaseDataSource):
    """Akshare数据源 — v4.6.4: 新浪优先, 港股用akshare"""
    name = "akshare"
    
    @catch_exception(logger, alert=False)
    def get_stock_quote(self, stock_code: str) -> Optional[Dict[str, Any]]:
        import akshare as ak
        if stock_code.endswith(".HK"):
            df = ak.stock_hk_spot()
            row = df[df["代码"] == stock_code[:5]]
            if len(row) == 0:
                return None
            row = row.iloc[0]
            return {
                "code": stock_code,
                "name": row["名称"],
                "price": float(row["最新价"]),
                "change": float(row["涨跌额"]),
                "change_pct": float(row["涨跌幅"]),
                "open": float(row["开盘价"]),
                "high": float(row["最高价"]),
                "low": float(row["最低价"]),
                "volume": float(row["成交量"]),
                "amount": float(row["成交额"])
            }
        else:
            # v4.6.4: A股用新浪数据源(东财push2已封锁)
            code = stock_code[:6]
            try:
                df = ak.stock_zh_a_spot()
            except Exception:
                df = ak.stock_zh_a_spot_em()
            # 新浪代码格式: sh600519, sz000001 等
            codes = df["代码"].astype(str)
            row = df[(codes == code) | 
                     (codes.str.upper() == 'SH' + code) |
                     (codes.str.upper() == 'SZ' + code) |
                     (codes == 'sh' + code) |
                     (codes == 'sz' + code)]
            if len(row) == 0:
                return None
            row = row.iloc[0]
            # 新浪数据源列名兼容
            col_map = {
                '代码': 'code', '名称': 'name', '最新价': 'price',
                '涨跌额': 'change', '涨跌幅': 'change_pct', '今开': 'open',
                '最高': 'high', '最低': 'low', '成交量': 'volume', '成交额': 'amount'
            }
            # 尝试匹配列名
            def _get_col(row_obj, *keys):
                for k in keys:
                    if k in row_obj.index:
                        return float(row_obj[k])
                return 0.0
            
            return {
                "code": stock_code,
                "name": row.get("名称", row.get("name", "")),
                "price": _get_col(row, "最新价", "close"),
                "change": _get_col(row, "涨跌额", "change"),
                "change_pct": _get_col(row, "涨跌幅", "pct_chg"),
                "open": _get_col(row, "今开", "开盘", "open"),
                "high": _get_col(row, "最高", "high"),
                "low": _get_col(row, "最低", "low"),
                "volume": _get_col(row, "成交量", "volume"),
                "amount": _get_col(row, "成交额", "amount")
            }

# 数据源实例
_data_sources: Dict[str, BaseDataSource] = {
    "mairui": None,  # 懒加载, 见 get_stock_quote
    "mootdx": MootdxDataSource(),
    "sina": SinaDataSource(),
    "tencent": TencentDataSource(),
    "eastmoney": EastmoneyDataSource(),
    "akshare": AkshareDataSource()
}

# 熔断计数器，连续失败超过5次则熔断该数据源10分钟
_circuit_breaker: Dict[str, Dict[str, Any]] = {
    name: {"fail_count": 0, "open_time": 0} for name in _data_sources.keys()
}

def get_stock_quote(stock_code: str, use_cache: bool = True) -> Optional[Dict[str, Any]]:
    """
    统一行情获取入口，自动按优先级尝试所有数据源，支持缓存
    :param stock_code: 股票代码，比如000001.SZ、00700.HK
    :param use_cache: 是否使用缓存
    :return: 行情数据字典
    """
    # 先查缓存
    if use_cache:
        cache_key = _get_cache_key(stock_code)
        if cache_key in _cache:
            cache_data = _cache[cache_key]
            if time.time() - cache_data["timestamp"] < config.data_source_cache_ttl:
                logger.debug(f"缓存命中：{stock_code}")
                return cache_data["data"]
    
    # 遍历数据源，按优先级尝试
    now = time.time()
    for source_name in config.data_source_priority:
        # 检查熔断
        breaker = _circuit_breaker[source_name]
        if breaker["open_time"] > now:
            logger.debug(f"数据源{source_name}处于熔断状态，跳过")
            continue
        
        source = _data_sources.get(source_name)
        if not source:
            continue
        
        try:
            data = source.get_stock_quote(stock_code)
            if data and data["price"] > 0:
                # 成功获取数据，重置熔断计数器
                breaker["fail_count"] = 0
                logger.debug(f"数据源{source_name}获取{stock_code}成功")
                # 写入缓存
                if use_cache:
                    cache_key = _get_cache_key(stock_code)
                    _cache[cache_key] = {
                        "data": data,
                        "timestamp": now
                    }
                    _save_cache()
                return data
            else:
                logger.warning(f"数据源{source_name}获取{stock_code}失败，返回空数据")
                breaker["fail_count"] += 1
        except Exception as e:
            logger.warning(f"数据源{source_name}获取{stock_code}异常：{str(e)}")
            breaker["fail_count"] += 1
        
        # 连续失败超过5次，熔断10分钟
        if breaker["fail_count"] >= 5:
            breaker["open_time"] = now + 600
            logger.warning(f"数据源{source_name}连续失败5次，熔断10分钟")
    
    # 所有数据源都失败
    logger.error(f"所有数据源获取{stock_code}都失败")
    return None
