# -*- coding: utf-8 -*-
"""dsl_data_sdk_fixed.py
修复版数据SDK - 支持多种股票代码格式
"""

import os
import json
import time
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests

# v4.5.20: 加载 .env.local（统一密钥文件），确保 MAIRUI_LICENCE 等环境变量可用
# v4.6.6: 集成熔断器 + per-source失败追踪 + 并行fetch
# v4.6.6b: 跨进程源状态文件 + 共享Session
_ENV_LOADED = False


def _load_env_once():
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    try:
        from dotenv import load_dotenv
        _project_root = os.path.dirname(os.path.abspath(__file__))
        _env_path = os.path.join(_project_root, ".env")
        _env_local = os.path.join(_project_root, ".env.local")
        if os.path.exists(_env_path):
            load_dotenv(_env_path, override=False)
        if os.path.exists(_env_local):
            load_dotenv(_env_local, override=True)
        _ENV_LOADED = True
    except Exception:
        pass


_load_env_once()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# P0: 生产环境环境变量控制 — v4.5.9: 默认生产模式, 显式设 DSL_PRODUCTION_MODE=false 才用mock
_PRODUCTION_MODE = os.environ.get("DSL_PRODUCTION_MODE", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------
def _cache_path(key: str) -> Path:
    """Return a cache file path for the given key."""
    filename = f"{key}.json"
    return CACHE_DIR / filename

def _read_cache(key: str, max_age_seconds: int = 300) -> Optional[Any]:
    # v4.6.6: 交易时段缓存提升到30s减少cache miss
    import datetime as _dt
    _now = _dt.datetime.now()
    _t = _now.hour * 60 + _now.minute
    if _now.weekday() < 5 and (555 <= _t <= 905):  # 交易时段 9:15-15:05
        max_age_seconds = 30
    p = _cache_path(key)
    if not p.is_file():
        return None
    try:
        mtime = p.stat().st_mtime
        if time.time() - mtime > max_age_seconds:
            return None
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _write_cache(key: str, data: Any) -> None:
    p = _cache_path(key)
    try:
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# v4.6.6b: 共享requests.Session（连接复用+DNS缓存，减少握手开销）
_SHARED_SESSION = requests.Session()
_SHARED_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
})
# v4.6.6b: 绕过macOS系统代理
_SHARED_SESSION.trust_env = False

def _session_get(url, params=None, headers=None, timeout=10, **kwargs):
    """使用共享Session的GET请求，跳过macOS代理
    v4.6.6b: 统一入口，复用连接池
    """
    try:
        merged_headers = headers or {}
        resp = _SHARED_SESSION.get(url, params=params, headers=merged_headers, timeout=timeout, **kwargs)
        if resp.status_code == 200:
            return resp
    except Exception:
        pass
    return None

def _http_get(url: str, params=None, headers=None, timeout=10):
    """HTTP GET with error handling.
    v4.5.21: proxies={"http":"","https":""} 跳过macOS系统代理
    v4.6.6b: 使用共享Session
    """
    try:
        resp = _session_get(url, params=params, headers=headers, timeout=timeout, proxies={"http": "", "https": ""})
        if resp:
            return resp.json()
    except Exception:
        pass
    return None

def normalize_symbol(symbol: str) -> str:
    """
    标准化股票代码格式
    支持格式:
    - 000001.SZ -> sz000001
    - 600519.SH -> sh600519
    - sz000001 -> sz000001
    - sh600519 -> sh600519
    - 000001 -> sz000001 (自动判断)
    - 600519 -> sh600519 (自动判断)
    """
    symbol = str(symbol).strip().upper()
    
    # 如果已经有市场前缀，直接返回
    if symbol.startswith(('SH', 'SZ')):
        return symbol.lower()
    
    # 处理 .SZ/.SH 格式
    if symbol.endswith('.SZ'):
        return 'sz' + symbol[:-3]
    elif symbol.endswith('.SH'):
        return 'sh' + symbol[:-3]
    
    # 纯数字代码，自动判断市场
    if symbol.isdigit():
        if symbol.startswith('6'):
            return 'sh' + symbol
        elif symbol.startswith('0') or symbol.startswith('3'):
            return 'sz' + symbol
        elif symbol.startswith('9'):  # 上海B股
            return 'sh' + symbol
        elif symbol.startswith('2'):  # 深圳B股
            return 'sz' + symbol
    
    # 无法识别，返回原样
    return symbol

# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------
def get_price(symbol: str, market: str = 'cn') -> Dict[str, Any]:
    """
    获取股票实时价格
    支持多种股票代码格式
    
    Args:
        symbol: 股票代码，支持多种格式
        market: 市场，目前仅支持'cn'
    
    Returns:
        Dict with price data or raises RuntimeError
    """
    if market != 'cn':
        raise ValueError(f"Unsupported market: {market}")
    
    # 标准化股票代码
    normalized_symbol = normalize_symbol(symbol)
    cache_key = f"price_{normalized_symbol}"
    
    # 检查缓存
    cached = _read_cache(cache_key)
    if cached:
        return cached
    
        # v4.6.6b: 跨进程源可用性文件 — 所有cron进程共享，避免每个进程独立重试
    _source_status_file = "/tmp/dsl_source_status.json"
    _source_status = {}
    try:
        if os.path.exists(_source_status_file):
            with open(_source_status_file, "r") as _sf:
                _raw = json.load(_sf)
            _now_ts = time.time()
            _source_status = {
                k: v for k, v in _raw.items()
                if v.get("until", 0) > _now_ts
            }
    except Exception:
        _source_status = {}
    
    def _is_source_available(src_name):
        """检查某个源是否可用（跨进程）"""
        entry = _source_status.get(src_name, {})
        if entry.get("available", True) is False and entry.get("until", 0) > time.time():
            return False
        return True
    
    def _atomic_write_json(path, data):
        """原子写入JSON，防并发进程写竞争"""
        _tmp_path = path + ".tmp"
        try:
            with open(_tmp_path, "w") as _sf:
                json.dump(data, _sf)
            os.rename(_tmp_path, path)  # 原子替换
        except Exception:
            try:
                if os.path.exists(_tmp_path):
                    os.unlink(_tmp_path)
            except Exception:
                pass
    
    def _mark_source_unavailable(src_name, cooldown=120):
        """标记某个源不可用（跨进程）"""
        _source_status[src_name] = {"available": False, "until": time.time() + cooldown}
        _atomic_write_json(_source_status_file, _source_status)
    
    def _mark_source_available(src_name):
        """恢复源可用"""
        _source_status.pop(src_name, None)
        _atomic_write_json(_source_status_file, _source_status)
    
    # v4.6.6b: 集成熔断器
    _cb_skip_all = False
    try:
        from core.stability_simple import stability_system as _cb_system
        _cb = _cb_system.circuit_breakers.get("data_fetch")
        if _cb and not _cb.allow_request():
            print(f"[DSL_DATA_SDK] 熔断器 data_fetch OPEN, 跳过常规串行源")
            _cb_skip_all = True
    except Exception:
        pass
    
    if not _cb_skip_all:
        # v4.6.6b: 并行fetch top-2（麦蕊+新浪），用交叉进程状态跳过已知死源
        import concurrent.futures as _cf
        _parallel_sources = []
        if _is_source_available('mairui'):
            _parallel_sources.append(_get_price_from_mairui)
        if _is_source_available('sina'):
            _parallel_sources.append(_get_price_from_sina)
        
        if _parallel_sources:
            with _cf.ThreadPoolExecutor(max_workers=len(_parallel_sources)) as executor:
                _futures = {
                    executor.submit(_fn, normalized_symbol): _fn.__name__
                    for _fn in _parallel_sources
                }
                for _future in _cf.as_completed(_futures, timeout=4):
                    _name = _futures[_future]
                    try:
                        _result = _future.result()
                        if _result and _result.get("price"):
                            _mark_source_available(_name.replace('_get_price_from_', ''))
                            _write_cache(cache_key, _result)
                            return _result
                        _mark_source_unavailable(_name.replace('_get_price_from_', ''))
                    except Exception as e:
                        print(f"[DSL_DATA_SDK] {_name} parallel failed: {e}")
                        _mark_source_unavailable(_name.replace('_get_price_from_', ''))
    
    # v4.6.6b: 串行降级（腾讯→东财，用跨进程状态跳过已死源）
    _serial_sources = []
    if _is_source_available('tencent') and not _cb_skip_all:
        _serial_sources.append(('tencent', _get_price_from_tencent))
    if _is_source_available('eastmoney') or _cb_skip_all:
        _serial_sources.append(('eastmoney', _get_price_from_eastmoney))
    
    for src_name, source_func in _serial_sources:
        try:
            result = source_func(normalized_symbol)
            if result and result.get("price"):
                _mark_source_available(src_name)
                _write_cache(cache_key, result)
                return result
            _mark_source_unavailable(src_name)
        except Exception as e:
            print(f"[DSL_DATA_SDK] {source_func.__name__} failed: {e}")
            _mark_source_unavailable(src_name)
            continue
    
    # P0: 仅在非生产环境允许模拟数据fallback
    if not _PRODUCTION_MODE:
        try:
            result = _get_mock_price(normalized_symbol)
            if result:
                _write_cache(cache_key, result)
                return result
        except Exception:
            pass
    
    # 所有源都失败
    raise RuntimeError(f"Unable to fetch realtime price for {symbol} from all sources")

_MAIRUI_403_CACHED = None
_MAIRUI_UNSTABLE_UNTIL = 0  # 麦蕊不稳定窗口到期时间戳(秒), 0=稳定
_MAIRUI_UNSTABLE_COOLDOWN = 120  # 不稳定标记持续120秒
import time as _time_module

def _mairui_available() -> bool:
    """检测麦蕊LICENCE是否有效 (v4.5.19)
    
    首次调用尝试一次，403则标记不可用，后续直接跳过。
    v4.5.21: proxies绕过macOS系统代理（麦蕊不需要代理）
    """
    import os
    global _MAIRUI_403_CACHED
    if _MAIRUI_403_CACHED is not None:
        return _MAIRUI_403_CACHED
    
    licence = os.environ.get("MAIRUI_LICENCE", "")
    if not licence:
        _MAIRUI_403_CACHED = False
        return False
    
    # 快速检测LICENCE有效性用stock_real_network（最快返回）
    try:
        resp = _session_get(
            f"https://api.mairuiapi.com/hsrl/ssjy/000001/{licence}",
            timeout=3,
            proxies={"http": "", "https": ""}
        )
        _MAIRUI_403_CACHED = (resp is not None and resp.status_code == 200)
        return _MAIRUI_403_CACHED
    except Exception:
        _MAIRUI_403_CACHED = False
        return False

def _get_price_from_mairui(symbol: str) -> Optional[Dict[str, Any]]:
    """从麦蕊API获取实时行情（主数据源）
    v4.5.21b: 使用get_stock_real_fast (timeout=5s, retry=1), 避免卡住降级链
    v4.5.19: 提前检测LICENCE有效性，避免每次调用重试空耗
    v4.5.19b: 加不稳定标记 — 一次失败后120秒内跳过
    """
    global _MAIRUI_UNSTABLE_UNTIL, _MAIRUI_403_CACHED
    if not _mairui_available():
        return None
    if _MAIRUI_UNSTABLE_UNTIL > _time_module.time():
        return None
    try:
        from config.mairui_api_config import get_stock_real_fast
        code = symbol[2:] if symbol.startswith(('sh', 'sz')) else symbol
        data = get_stock_real_fast(code)
        if data and data.get('current_price'):
            price = float(data['current_price'])
            prev_close = float(data.get('prev_close', price))
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close != 0 else 0.0
            
            # 麦蕊实时行情不返回name，尝试从stock_list缓存获取
            name = _get_stock_name_from_cache(code)
            
            # 麦蕊volume单位是万手，需转换
            vol_raw = data.get('volume', 0)
            volume = float(vol_raw) * 100 if float(vol_raw) < 100000 else float(vol_raw)
            
            return {
                "symbol": symbol,
                "name": name,
                "price": price,
                "prev_close": prev_close,
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "volume": volume,
                "amount": float(data.get('turnover', 0)),
                "high": float(data.get('high', price)),
                "low": float(data.get('low', price)),
                "source": "mairui",
                "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                # 麦蕊额外字段
                "pe_ttm": data.get('pe_ttm'),
                "turnover_rate": data.get('turnover_rate'),
                "volume_ratio": data.get('volume_ratio'),
                "amplitude": data.get('amplitude'),
                "market_cap": data.get('market_cap'),
                "pb_ratio": data.get('pb_ratio'),
                "open": float(data.get('open', price)),
            }
    except Exception as e:
        if '403' in str(e):
            _MAIRUI_403_CACHED = False  # LICENCE过期
        else:
            _MAIRUI_UNSTABLE_UNTIL = _time_module.time() + _MAIRUI_UNSTABLE_COOLDOWN  # 标记不稳定120秒
        print(f"[Mairui] Error: {e}")
    return None

_MAIRUI_STOCK_LIST_OK = None

def _check_stock_list_available() -> bool:
    """检测stock_list接口是否可用 (v4.5.19)
    
    用超短timeout快速探测，避免get_all_stock_list触发3次重试耗7s。
    v4.5.21: proxies绕过macOS系统代理
    """
    global _MAIRUI_STOCK_LIST_OK
    if _MAIRUI_STOCK_LIST_OK is not None:
        return _MAIRUI_STOCK_LIST_OK
    
    import os
    licence = os.environ.get("MAIRUI_LICENCE", "")
    if not licence:
        return False
    try:
        resp = _session_get(
            f"https://api.mairuiapi.com/hslt/list/{licence}",
            timeout=3,
            proxies={"http": "", "https": ""}
        )
        _MAIRUI_STOCK_LIST_OK = (resp is not None and resp.status_code == 200)
    except Exception:
        _MAIRUI_STOCK_LIST_OK = False
    return _MAIRUI_STOCK_LIST_OK

def _get_stock_name_from_cache(code: str) -> str:
    """从本地stock_list缓存获取股票名称
    v4.5.19: 检测到麦蕊stock_list不可用后跳过，避免额外重试超时
    v4.5.19b: 独立检测stock_list接口，不依赖_mairui_available
    """
    global _STOCK_NAME_MAP
    
    if _STOCK_NAME_MAP is None:
        _STOCK_NAME_MAP = {}
        if not _check_stock_list_available():
            return ''
        try:
            from config.mairui_api_config import get_all_stock_list
            stocks = get_all_stock_list()
            for s in stocks:
                dm = s.get('dm', '')
                mc = s.get('mc', '')
                if dm and mc:
                    pure_code = dm.split('.')[0]
                    _STOCK_NAME_MAP[pure_code] = mc
        except Exception:
            pass
    
    return _STOCK_NAME_MAP.get(code, '')

_STOCK_NAME_MAP = None  # 模块级缓存

def _get_price_from_sina(symbol: str) -> Optional[Dict[str, Any]]:
    """从新浪财经获取价格"""
    # v4.5.21: trust_env=False 跳过macOS系统代理
    # v4.6.6: timeout 5s→3s, 加快降级链
    url = f"https://hq.sinajs.cn/list={symbol}"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        resp = _session_get(url, headers=headers, timeout=3, proxies={"http": "", "https": ""})
        if resp and resp.status_code == 200:
            # 解析新浪格式: var hq_str_sh600519="贵州茅台,1920.50,...";
            content = resp.text.strip()
            if '="' in content:
                data_str = content.split('="')[1].rstrip('";')
                fields = data_str.split(',')
                
                if len(fields) >= 32:
                    name = fields[0]
                    
                    # 最新价 (字段3)
                    price_str = fields[3]
                    if not price_str or price_str == '0.000':
                        price_str = fields[1]  # 开盘价
                    
                    # 昨收价 (字段2)
                    prev_close_str = fields[2]
                    
                    try:
                        price = float(price_str) if price_str else 0.0
                        prev_close = float(prev_close_str) if prev_close_str else price
                        change = price - prev_close
                        change_pct = (change / prev_close * 100) if prev_close != 0 else 0.0
                        
                        return {
                            "symbol": symbol,
                            "name": name,
                            "price": price,
                            "prev_close": prev_close,
                            "change": change,
                            "change_pct": change_pct,
                            "volume": float(fields[8]) if len(fields) > 8 and fields[8] else 0,
                            "amount": float(fields[9]) if len(fields) > 9 and fields[9] else 0,
                            "high": float(fields[4]) if len(fields) > 4 and fields[4] else 0,
                            "low": float(fields[5]) if len(fields) > 5 and fields[5] else 0,
                            "source": "sina",
                            "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                    except (ValueError, IndexError) as e:
                        print(f"[Sina] Parse error: {e}")
    except Exception as e:
        print(f"[Sina] Request error: {e}")
    
    return None

def _get_price_from_tencent(symbol: str) -> Optional[Dict[str, Any]]:
    """从腾讯财经获取价格"""
    # v4.5.21: trust_env=False 跳过macOS系统代理
    # v4.6.6: timeout 5s→3s, 加快降级链
    # 腾讯使用原始代码格式，需要转换
    tencent_symbol = symbol[2:] if symbol.startswith(('sh', 'sz')) else symbol
    url = f"https://qt.gtimg.cn/q={tencent_symbol}"
    
    try:
        resp = _session_get(url, timeout=3, proxies={"http": "", "https": ""})
        if resp and resp.status_code == 200:
            # 格式: v_sh600519="1~贵州茅台~600519~1920.50~1910.00~..."
            content = resp.text.strip()
            if '="' in content:
                data_str = content.split('="')[1].rstrip('";')
                parts = data_str.split('~')
                
                if len(parts) >= 5:
                    name = parts[1]
                    price = float(parts[3]) if parts[3] else 0.0
                    prev_close = float(parts[4]) if parts[4] else price
                    change = price - prev_close
                    change_pct = (change / prev_close * 100) if prev_close != 0 else 0.0
                    
                    return {
                        "symbol": symbol,
                        "name": name,
                        "price": price,
                        "prev_close": prev_close,
                        "change": change,
                        "change_pct": change_pct,
                        "volume": float(parts[6]) if len(parts) > 6 and parts[6] else 0,
                        "amount": float(parts[37]) * 10000 if len(parts) > 37 and parts[37] else 0,
                        "high": float(parts[33]) if len(parts) > 33 and parts[33] else 0,
                        "low": float(parts[34]) if len(parts) > 34 and parts[34] else 0,
                        "source": "tencent",
                        "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
    except Exception as e:
        print(f"[Tencent] Error: {e}")
    
    return None

# v4.5.21: 东财push2不可用窗口标记 — 失败后120秒内跳过，避免10s超时空耗
_EASTMONEY_UNAVAILABLE = False
_EASTMONEY_BLACKOUT_UNTIL = 0  # 黑窗到期时间戳
_EASTMONEY_BLACKOUT_SECONDS = 60  # 失败后跳过窗口 (v4.6.6b: 2s快检, 窗口减半)

def _check_eastmoney_available() -> bool:
    """检测东财push2接口是否可达
    v4.5.21b: 改为窗口缓存, 失败后120秒跳过而非永久, 东财间歇可用
    """
    global _EASTMONEY_UNAVAILABLE, _EASTMONEY_BLACKOUT_UNTIL
    import time as _tm
    if _EASTMONEY_UNAVAILABLE and _EASTMONEY_BLACKOUT_UNTIL > _tm.time():
        return False  # 黑窗期内跳过
    if _EASTMONEY_BLACKOUT_UNTIL <= _tm.time():
        _EASTMONEY_UNAVAILABLE = False  # 黑窗到期, 重新检测
    try:
        resp = _session_get(
            "https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001&fields=f43",
            timeout=2, proxies={"http": "", "https": ""}
        )
        ok = (resp is not None and resp.status_code == 200)
        _EASTMONEY_UNAVAILABLE = not ok
        if not ok:
            _EASTMONEY_BLACKOUT_UNTIL = _tm.time() + _EASTMONEY_BLACKOUT_SECONDS
        return ok
    except Exception:
        _EASTMONEY_UNAVAILABLE = True
        _EASTMONEY_BLACKOUT_UNTIL = _tm.time() + _EASTMONEY_BLACKOUT_SECONDS
        return False

def _get_price_from_eastmoney(symbol: str) -> Optional[Dict[str, Any]]:
    """从东方财富获取价格"""
    # v4.5.21b: 窗口跳过而非永久—东财间歇可用
    # v4.6.6: timeout 5s→3s, 加快降级链
    global _EASTMONEY_UNAVAILABLE
    if not _check_eastmoney_available():
        return None
    
    # 东方财富需要secid格式: 1.600519 (上海) 或 0.000001 (深圳)
    if symbol.startswith('sh'):
        secid = f"1.{symbol[2:]}"
    elif symbol.startswith('sz'):
        secid = f"0.{symbol[2:]}"
    else:
        secid = f"1.{symbol}"  # 默认上海
    
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f169,f170"
    }
    
    try:
        resp = _session_get(url, params=params, timeout=2, proxies={"http": "", "https": ""})
        if resp and resp.status_code == 200:
            data = resp.json()
            if data.get("rc") == 0 and "data" in data:
                d = data["data"]
                
                # 东方财富价格是乘以100的
                price = d.get("f43", 0) / 100
                prev_close = d.get("f169", 0) / 100
                change = d.get("f170", 0) / 100
                change_pct = d.get("f171", 0) / 100
                
                return {
                    "symbol": symbol,
                    "name": d.get("f58", ""),
                    "price": price,
                    "prev_close": prev_close,
                    "change": change,
                    "change_pct": change_pct,
                    "volume": d.get("f47", 0),
                    "amount": d.get("f48", 0),
                    "high": d.get("f44", 0) / 100,
                    "low": d.get("f45", 0) / 100,
                    "source": "eastmoney",
                    "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
    except Exception as e:
        print(f"[Eastmoney] Error: {e}")
    
    return None

def _get_mock_price(symbol: str) -> Dict[str, Any]:
    """
    获取模拟价格数据（当所有真实数据源都失败时使用）
    用于开发和测试环境
    """
    import random
    import hashlib
    
    # 基于symbol生成确定性但随机的价格
    seed = int(hashlib.md5(symbol.encode()).hexdigest()[:8], 16)
    random.seed(seed)
    
    base_price = 10.0 + (seed % 1000) / 10.0
    change = random.uniform(-0.05, 0.05) * base_price
    price = base_price + change
    
    # 股票名称映射
    name_map = {
        'sh600519': '贵州茅台',
        'sz000001': '平安银行',
        'sz000002': '万科A',
        'sz000858': '五粮液',
        'sz002415': '海康威视',
        'sh601318': '中国平安',
        'sh600036': '招商银行',
        'sz000333': '美的集团',
        'sz000651': '格力电器',
        'sz002594': '比亚迪',
        'sz300750': '宁德时代',
        'sh600887': '伊利股份',
        'sh600276': '恒瑞医药',
        'sh601888': '中国中免',
        'sh600309': '万华化学',
        'sz000568': '泸州老窖',
        'sh600585': '海螺水泥',
        'sh601012': '隆基绿能',
        'sz002475': '立讯精密',
        'sz300059': '东方财富'
    }
    
    name = name_map.get(symbol, f"股票{symbol}")
    
    # P0-FIX: 标记mock数据为不可靠，下游决策必须拒绝
    return {
        "symbol": symbol,
        "name": name,
        "price": round(price, 2),
        "prev_close": round(base_price, 2),
        "change": round(change, 2),
        "change_pct": round((change / base_price * 100), 2),
        "volume": random.randint(1000000, 10000000),
        "amount": random.randint(50000000, 500000000),
        "high": round(price * 1.02, 2),
        "low": round(price * 0.98, 2),
        "source": "mock_random",
        "data_quality": "unreliable",
        "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "⚠️ 模拟数据：所有真实数据源失败，禁止用于交易决策"
    }

# ---------------------------------------------------------------------------
# Additional functions
# ---------------------------------------------------------------------------
def _get_kline_from_local(symbol: str, start: str, end: str) -> Optional[List[Dict[str, Any]]]:
    """从本地 Parquet 历史数据库读取（Tier 0：最快）"""
    try:
        from data_db import get_kline as local_get_kline
        result = local_get_kline(symbol, start, end)
        if result and len(result) > 0:
            return result
    except Exception:
        pass
    return None


def get_kline(symbol: str, start: str, end: str, freq: str = 'day') -> List[Dict[str, Any]]:
    """获取K线数据 — 本地Parquet Tier0 → 麦蕊 Tier1 → 新浪 Tier2 → Mock"""
    normalized_symbol = normalize_symbol(symbol)
    cache_key = f"kline_{normalized_symbol}_{start}_{end}_{freq}"
    
    # 检查缓存（K线数据缓存1小时）
    cached = _read_cache(cache_key, max_age_seconds=3600)
    if cached:
        return cached
    
    # Tier 0: 本地 Parquet 历史数据库（2021年~昨日，毫秒级）
    result = _get_kline_from_local(normalized_symbol, start, end)
    if result:
        _write_cache(cache_key, result)
        return result
    
    # Tier 1: 麦蕊
    result = _get_kline_from_mairui(normalized_symbol, start, end, freq)
    if result:
        _write_cache(cache_key, result)
        return result
    
    # Tier 2: 新浪
    result = _get_kline_from_sina(normalized_symbol, start, end, freq)
    if result:
        _write_cache(cache_key, result)
        return result
    
    # 最后降级：模拟数据（仅非生产环境）
    if not _PRODUCTION_MODE:
        return _get_kline_mock(normalized_symbol, start, end, freq)
    
    raise RuntimeError(f"Unable to fetch kline for {symbol} from all sources")

def _get_kline_from_mairui(symbol: str, start: str, end: str, freq: str = 'day') -> Optional[List[Dict[str, Any]]]:
    """从麦蕊API获取K线数据（主数据源）
    
    麦蕊K线端点: hsstock/history/{code}/{period}/{adjust}/
    返回字段: t(时间), o(开), h(高), l(低), c(收), v(量), a(额), pc(昨收), sf(复权因子)
    """
    try:
        # 频率映射: day->d, week->w, month->m
        period_map = {'day': 'd', 'week': 'w', 'month': 'm', '1d': 'd', '1w': 'w', '1M': 'm'}
        period = period_map.get(freq, 'd')
        
        # 麦蕊使用纯数字代码
        code = symbol[2:] if symbol.startswith(('sh', 'sz')) else symbol
        
        _licence = os.getenv("MAIRUI_LICENCE")
        if not _licence:
            raise RuntimeError("环境变量 MAIRUI_LICENCE 未设置")
        url = f"https://api.mairuiapi.com/hsstock/history/{code}/{period}/f/" + _licence
        data = _http_get(url, timeout=15)
        
        if not data or not isinstance(data, list):
            return None
        
        # 解析并过滤日期范围
        result = []
        start_dt = datetime.datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.datetime.strptime(end, "%Y-%m-%d")
        
        # v4.5.13: 日志抑制计数器（麦蕊坏数据会无限刷屏）
        _bad_items_logged = 0
        _MAX_BAD_LOG = 3
        
        for item in data:
            try:
                # 防御：item不是dict时直接跳过（麦蕊API偶发非dict元素）
                if not isinstance(item, dict):
                    _bad_items_logged += 1
                    if _bad_items_logged <= _MAX_BAD_LOG:
                        print(f"  [Mairui Kline] 非dict元素已跳过 (type={type(item).__name__}, idx={len(result)+_bad_items_logged})")
                    continue
                
                # 防御：t字段非字符串或格式异常时跳过（麦蕊偶返回纯数字时间戳如"1062"）
                t_raw = item.get('t')
                if t_raw is None or not isinstance(t_raw, str):
                    _bad_items_logged += 1
                    if _bad_items_logged <= _MAX_BAD_LOG:
                        print(f"  [Mairui Kline] t字段非字符串已跳过 (got={type(t_raw).__name__}:{t_raw})")
                    continue
                
                # 麦蕊时间格式: "2026-04-24 00:00:00" 或 "2026-04-24"
                t_str = t_raw[:10]
                # 长度检查：有效日期字符串至少10个字符(YYYY-MM-DD)
                if len(t_str) < 10 or '-' not in t_str:
                    _bad_items_logged += 1
                    continue
                try:
                    t_dt = datetime.datetime.strptime(t_str, "%Y-%m-%d")
                except ValueError:
                    _bad_items_logged += 1
                    continue
                
                if t_dt < start_dt or t_dt > end_dt:
                    continue
                
                result.append({
                    "date": t_str,
                    "open": float(item.get('o', 0)),
                    "high": float(item.get('h', 0)),
                    "low": float(item.get('l', 0)),
                    "close": float(item.get('c', 0)),
                    "volume": float(item.get('v', 0)),
                    "amount": float(item.get('a', 0)),
                    "prev_close": float(item.get('pc', 0)),
                    "source": "mairui",
                })
            except (ValueError, TypeError, AttributeError):
                _bad_items_logged += 1
                if _bad_items_logged <= _MAX_BAD_LOG:
                    import traceback
                    print(f"  [Mairui Kline] 解析跳过 (idx={len(result)+_bad_items_logged})")
                    traceback.print_exc(limit=1)
                continue
        
        if _bad_items_logged > _MAX_BAD_LOG:
            print(f"  [Mairui Kline] ...及以上{_bad_items_logged}条坏数据已跳过（日志已抑制）")
        
        return result if result else None
    except Exception as e:
        print(f"[Mairui Kline] Error: {e}")
    return None

def _get_kline_from_sina(symbol: str, start: str, end: str, freq: str = 'day') -> Optional[List[Dict[str, Any]]]:
    """从新浪财经获取K线数据（降级备用）"""
    try:
        # 新浪K线接口
        code = symbol[2:] if symbol.startswith(('sh', 'sz')) else symbol
        prefix = symbol[:2] if symbol.startswith(('sh', 'sz')) else ('sh' if code.startswith('6') else 'sz')
        
        freq_map = {'day': 'daily', 'week': 'weekly', 'month': 'monthly'}
        sina_freq = freq_map.get(freq, 'daily')
        
        url = f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{prefix}{code}_{sina_freq}"
        resp = requests.get(url, timeout=10)
        
        if resp.status_code == 200:
            import re
            text = resp.text.strip()
            # 去掉JSONP包装
            json_match = re.search(r'\(\s*(\{.*\})\s*\)', text, re.DOTALL)
            if not json_match:
                return None
            data = json.loads(json_match.group(1))
            
            start_dt = datetime.datetime.strptime(start, "%Y-%m-%d")
            end_dt = datetime.datetime.strptime(end, "%Y-%m-%d")
            
            result = []
            for item in data.get('data', []):
                try:
                    t_str = item.get('d', '')
                    t_dt = datetime.datetime.strptime(t_str, "%Y-%m-%d")
                    if t_dt < start_dt or t_dt > end_dt:
                        continue
                    result.append({
                        "date": t_str,
                        "open": float(item.get('o', 0)),
                        "high": float(item.get('h', 0)),
                        "low": float(item.get('l', 0)),
                        "close": float(item.get('c', 0)),
                        "volume": float(item.get('v', 0)),
                        "source": "sina",
                    })
                except (ValueError, TypeError):
                    continue
            return result if result else None
    except Exception as e:
        print(f"[Sina Kline] Error: {e}")
    return None

def _get_kline_mock(symbol: str, start: str, end: str, freq: str = 'day') -> List[Dict[str, Any]]:
    """模拟K线数据（仅非生产环境使用）"""
    import random
    from datetime import timedelta
    
    result = []
    current = datetime.datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.datetime.strptime(end, "%Y-%m-%d")
    
    base_price = 10.0 + hash(symbol) % 100 / 10.0
    
    while current <= end_date:
        change = random.uniform(-0.05, 0.05)
        price = base_price * (1 + change)
        
        result.append({
            "date": current.strftime("%Y-%m-%d"),
            "open": round(price * 0.99, 2),
            "high": round(price * 1.02, 2),
            "low": round(price * 0.98, 2),
            "close": round(price, 2),
            "volume": random.randint(1000000, 10000000),
            "source": "mock_random",
            "data_quality": "unreliable",
        })
        
        current += timedelta(days=1)
        base_price = price
    
    return result

def get_fundamentals(symbol: str) -> Dict[str, Any]:
    """获取基本面数据（简化版）"""
    normalized_symbol = normalize_symbol(symbol)
    
    return {
        "symbol": normalized_symbol,
        "pe": 15.5,
        "pb": 2.1,
        "dividend_yield": 2.5,
        "market_cap": 50000000000,
        "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "模拟基本面数据"
    }

# ---------------------------------------------------------------------------
# Test function
# ---------------------------------------------------------------------------
def test_data_sdk():
    """测试数据SDK功能"""
    test_symbols = [
        "000001.SZ",  # 标准格式
        "600519.SH",  # 标准格式
        "sz000001",   # 新浪格式
        "sh600519",   # 新浪格式
        "000001",     # 纯数字
        "600519",     # 纯数字
    ]
    
    print("🧪 测试数据SDK修复版")
    print("=" * 60)
    
    for symbol in test_symbols:
        try:
            print(f"\n🔍 测试: {symbol}")
            result = get_price(symbol)
            
            print(f"   名称: {result.get('name')}")
            print(f"   价格: {result.get('price')}")
            print(f"   涨跌: {result.get('change')} ({result.get('change_pct')}%)")
            print(f"   数据源: {result.get('source')}")
            print(f"   更新时间: {result.get('update_time')}")
            
        except Exception as e:
            print(f"   ❌ 失败: {e}")

if __name__ == "__main__":
    test_data_sdk()