#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
迈瑞沪深行情API配置

v4.6.2: 集成 macOS 系统代理绕过（修复 Mairui 503 + akshare ProxyError 根因）
- macOS SystemConfiguration 导致 requests 自动使用 127.0.0.1:1082 (Shadowrocket)
- VPN 隧道不稳 → 503 / ProxyError
- 修复: trust_env=False + 环境变量清理 + ProxyError 重试

内置3次指数退避重试机制，自动处理网络波动、接口临时故障
"""
import requests
import time
from functools import lru_cache
from requests.exceptions import ConnectionError, Timeout, ChunkedEncodingError, HTTPError, ProxyError

# 授权凭证（您提供的license编号）
# v4.5.18: 从环境变量读取Licence，未设置时降级为空字符串（API调用将返回错误但不会崩溃）
import os
import logging

# ─── macOS 系统代理绕过（必须在 requests.get() 之前生效） ───────────────────
from common.proxy_bypass import clean_env_proxies, get_session
# v4.6.6: retries=1 减少urllib3层重试，避免与request_api双重试放大（P0+VPN直连后服务端超时由上层处理）
_MAIRUI_SESSION = get_session(retries=1)  # trust_env=False, 不读系统代理
_proxy_cleaned = clean_env_proxies()
# ─────────────────────────────────────────────────────────────────────────────
_logger = logging.getLogger(__name__)
# 自动加载 .env / .env.local (确保非交互环境如cron/agentTurn也能获取密钥)
_env_loaded = os.environ.get("_MAIRUI_DOTENV_LOADED")
if not _env_loaded:
    try:
        from dotenv import load_dotenv
        _base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _env_path = os.path.join(_base, ".env")
        _env_local_path = os.path.join(_base, ".env.local")
        if os.path.exists(_env_local_path):
            load_dotenv(_env_local_path, override=True)
        if os.path.exists(_env_path):
            load_dotenv(_env_path, override=False)
    except ImportError:
        pass  # python-dotenv 未安装, 仅依赖os.environ
    os.environ["_MAIRUI_DOTENV_LOADED"] = "1"

LICENCE = os.environ.get("MAIRUI_LICENCE", "")
if not LICENCE:
    _logger.warning(
        "⚠️ 环境变量 MAIRUI_LICENCE 未设置。迈瑞API功能将不可用。\n"
        "  如需使用迈瑞数据源(基本面/技术指标/实时行情)，请设置:\n"
        "    export MAIRUI_LICENCE=<your-licence-here>\n"
        "  或写入 .env 文件:\n"
        "    MAIRUI_LICENCE=your-licence-here\n"
        "  系统将以降级模式运行(使用akshare/eastmoney等免费数据源)。"
    )

# API地址模板
API_TPL = {
    # 网络数据源-单股实时行情
    "stock_real_network": "https://api.mairuiapi.com/hsrl/ssjy/{stock_code}/" + LICENCE,
    # 网络数据源-逐笔交易
    "stock_transaction_detail": "https://api.mairuiapi.com/hsrl/zbjy/{stock_code}/" + LICENCE,
    # 券商数据源-单股实时行情
    "stock_real_broker": "https://api.mairuiapi.com/hsstock/real/time/{stock_code}/" + LICENCE,
    # 券商数据源-五档盘口
    "stock_five_level": "https://api.mairuiapi.com/hsstock/real/five/{stock_code}/" + LICENCE,
    # 网络数据源-全量股票实时行情（包年/钻石版可用，每分钟限1次）
    "all_stock_real_network": "https://a.mairuiapi.com/hsrl/real/all/" + LICENCE,
    # 券商数据源-全量股票实时行情（包年/钻石版可用，每分钟限1次）
    "all_stock_real_broker": "https://a.mairuiapi.com/hsrl/ssjy/all/" + LICENCE,
    # 多股实时行情（最多20只）
    "multi_stock_real": "https://a.mairuiapi.com/hsrl/ssjy_more/" + LICENCE + "?stock_codes={stock_codes}",
    # 资金流向历史数据
    "stock_capital_flow": "https://api.mairuiapi.com/hsstock/history/transaction/{stock_code}/" + LICENCE,
    # 股票基础列表
    "stock_list": "https://api.mairuiapi.com/hslt/list/" + LICENCE,
    # 指数、行业、概念树
    "industry_concept_tree": "https://api.mairuiapi.com/hszg/list/" + LICENCE,
    # 根据行业/概念/指数代码查成分股
    "industry_concept_stocks": "https://api.mairuiapi.com/hszg/gg/{category_code}/" + LICENCE,
    # 根据股票代码查所属行业/概念
    "stock_related_concepts": "https://api.mairuiapi.com/hszg/zg/{stock_code}/" + LICENCE,
    # 财务报表-资产负债表
    "financial_balance_sheet": "https://api.mairuiapi.com/hsstock/financial/balance/{stock_code}/" + LICENCE,
    # 财务报表-利润表
    "financial_income_statement": "https://api.mairuiapi.com/hsstock/financial/income/{stock_code}/" + LICENCE,
    # 财务报表-现金流量表
    "financial_cashflow_statement": "https://api.mairuiapi.com/hsstock/financial/cashflow/{stock_code}/" + LICENCE,
    # 财务主要指标
    "financial_key_indicators": "https://api.mairuiapi.com/hsstock/financial/pershareindex/{stock_code}/" + LICENCE,
    # 公司股本结构
    "financial_capital_structure": "https://api.mairuiapi.com/hsstock/financial/capital/{stock_code}/" + LICENCE,
    # 十大股东
    "financial_top_holders": "https://api.mairuiapi.com/hsstock/financial/topholder/{stock_code}/" + LICENCE,
    # 十大流通股东
    "financial_top_flow_holders": "https://api.mairuiapi.com/hsstock/financial/flowholder/{stock_code}/" + LICENCE,
    # 股东户数
    "financial_holder_count": "https://api.mairuiapi.com/hsstock/financial/hm/{stock_code}/" + LICENCE,
    # 技术指标-均线MA
    "indicator_ma": "https://api.mairuiapi.com/hsstock/history/ma/{stock_code}/{period}/{adjust}/" + LICENCE,
    # 技术指标-MACD
    "indicator_macd": "https://api.mairuiapi.com/hsstock/history/macd/{stock_code}/{period}/{adjust}/" + LICENCE,
    # 技术指标-布林带BOLL
    "indicator_boll": "https://api.mairuiapi.com/hsstock/history/boll/{stock_code}/{period}/{adjust}/" + LICENCE,
    # 技术指标-KDJ
    "indicator_kdj": "https://api.mairuiapi.com/hsstock/history/kdj/{stock_code}/{period}/{adjust}/" + LICENCE,
    # 通用技术指标接口（支持RSI/WR/CCI等其他指标）
    # 注意：迈瑞API仅支持MA/MACD/BOLL/KDJ四个kline指标，RSI/WR/CCI无独立端点
    # 如需RSI/WR/CCI，建议使用本地计算或AKShare等备用数据源
    "indicator_generic": "https://api.mairuiapi.com/hsstock/history/{indicator_name}/{stock_code}/{period}/{adjust}/" + LICENCE,
    # 原始K线数据（日/周/月）
    "stock_kline": "https://api.mairuiapi.com/hsstock/history/{stock_code}/{period}/{adjust}/" + LICENCE,
    # 涨停股票列表（按日期获取当日涨停板）
    "stock_limit_up": "https://api.mairuiapi.com/hslt/ztgc/{trade_date}/" + LICENCE,
    # 跌停股票列表（按日期获取当日跌停板）
    "stock_limit_down": "https://api.mairuiapi.com/hslt/dtgc/{trade_date}/" + LICENCE,

    # === v4.5.9 新增 (2026-05-11, 文档有但未配置) ===
    # 新股日历
    "stock_new_calendar": "https://api.mairuiapi.com/hslt/new/" + LICENCE,
    # 概念指数列表（券商数据源）
    "sectors_list": "https://api.mairuiapi.com/hslt/sectorslist/" + LICENCE,
    # 一级市场板块列表（券商数据源）
    "primary_list": "https://api.mairuiapi.com/hslt/primarylist/" + LICENCE,
    # 板块明细列表（券商数据源）
    "sectors_detail": "https://api.mairuiapi.com/hslt/sectors/{sector_name}/" + LICENCE,
    # 强势股池
    "strong_stock_pool": "https://api.mairuiapi.com/hslt/qsgc/{trade_date}/" + LICENCE,
    # 次新股池
    "new_stock_pool": "https://api.mairuiapi.com/hslt/cxgc/{trade_date}/" + LICENCE,
    # 炸板股池
    "broken_board_pool": "https://api.mairuiapi.com/hslt/zbgc/{trade_date}/" + LICENCE,
    # 股票基础信息
    "stock_instrument": "https://api.mairuiapi.com/hsstock/instrument/{stock_code}/" + LICENCE,
    # 历史涨跌停价格
    "stop_price_history": "https://api.mairuiapi.com/hsstock/stopprice/history/{stock_code}/" + LICENCE,
    # 行情指标
    "stock_indicators": "https://api.mairuiapi.com/hsstock/indicators/{stock_code}/" + LICENCE,
}

# 请求频率限制（按体验版配置，1分钟1000次，留余量按1分钟900次，即每请求间隔最少0.067秒）
REQUEST_INTERVAL = 0.07  # 秒，避免触发频率限制
# v4.6.6: 财报类端点数据量大(41KB+)，易VPN超时 → 间隔加长降低并发冲击
FINANCIAL_REQUEST_INTERVAL = 0.3  # 秒
_FINANCIAL_ENDPOINTS = {
    "financial_balance_sheet", "financial_income_statement",
    "financial_cashflow_statement", "financial_key_indicators",
    "financial_capital_structure", "financial_top_holders",
    "financial_top_flow_holders", "financial_holder_count",
}
_last_request_time = 0

def _rate_limit(api_name=None):
    """请求限流，财报类端点使用更长间隔"""
    global _last_request_time
    interval = FINANCIAL_REQUEST_INTERVAL if api_name in _FINANCIAL_ENDPOINTS else REQUEST_INTERVAL
    now = time.time()
    if now - _last_request_time < interval:
        time.sleep(interval - (now - _last_request_time))
    _last_request_time = time.time()

def request_api(api_name, max_retries=3, request_timeout=15, **kwargs):
    """
    通用API请求方法，内置指数退避重试机制
    :param api_name: API名称，对应API_TPL的key
    :param max_retries: 最大重试次数（实时行情用1快速失败，批量数据用3）
    :param request_timeout: 请求超时秒数（实时行情5s，批量数据15s）
    :param kwargs: 模板参数，如stock_code, stock_codes, st, et, lt等
    :return: 解析后的JSON数据
    """
    MAX_RETRIES = max_retries
    retry_delay = 1  # 初始重试间隔1秒，指数退避：1s->2s->4s
    
    for attempt in range(MAX_RETRIES):
        try:
            _rate_limit(api_name)
            url = API_TPL[api_name].format(**kwargs)
            # v4.6.7: trust_env=False + 显式空代理双保险 (防macOS代理503)
            _no_proxy = {"http": "", "https": ""}
            if api_name == "stock_capital_flow":
                params = {}
                if "st" in kwargs: params["st"] = kwargs["st"]
                if "et" in kwargs: params["et"] = kwargs["et"]
                if "lt" in kwargs: params["lt"] = kwargs["lt"]
                response = _MAIRUI_SESSION.get(url, params=params, timeout=request_timeout, proxies=_no_proxy)
            else:
                # 过滤掉url模板里已经用过的参数，剩下的作为query参数
                used_params = [key for key in kwargs.keys() if key in API_TPL[api_name]]
                params = {k: v for k, v in kwargs.items() if k not in used_params}
                response = _MAIRUI_SESSION.get(url, params=params, timeout=request_timeout, proxies=_no_proxy)
            
            # HTTP错误处理
            # v4.5.13: 403可能是临时限流, 包年版(3000次/分)应重试而非直接放弃
            if response.status_code == 403 and attempt < MAX_RETRIES - 1:
                print(f"⚠️ [API重试 {attempt+1}/{MAX_RETRIES}] {api_name} 返回403(可能限流), 等待{retry_delay:.1f}s后重试...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            if response.status_code >= 500 and attempt < MAX_RETRIES - 1:
                print(f"⚠️ [API重试 {attempt+1}/{MAX_RETRIES}] {api_name} 接口返回{response.status_code}服务端错误，等待{retry_delay}s后重试...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            response.raise_for_status()
            return response.json()
        
        except (ConnectionError, Timeout, ChunkedEncodingError, ProxyError) as e:
            if attempt < MAX_RETRIES - 1:
                print(f"⚠️ [API重试 {attempt+1}/{MAX_RETRIES}] {api_name} 接口网络异常: {type(e).__name__}: {str(e)}，等待{retry_delay}秒后重试...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            else:
                print(f"❌ [API失败] {api_name} 接口重试{MAX_RETRIES}次全部失败，网络错误: {type(e).__name__}: {str(e)}")
                raise
        except HTTPError as e:
            # 4xx客户端错误直接抛出不重试（参数错误/权限不足等）
            print(f"❌ [API失败] {api_name} 接口HTTP错误: {str(e)}，调用参数错误，无需重试")
            raise
        except Exception as e:
            # 其他未知错误最多重试1次
            if attempt < MAX_RETRIES - 2:
                print(f"⚠️ [API重试 {attempt+1}/{MAX_RETRIES}] {api_name} 接口未知异常: {type(e).__name__}: {str(e)}，等待{retry_delay}秒后重试...")
                time.sleep(retry_delay)
                retry_delay *= 2
                continue
            else:
                print(f"❌ [API失败] {api_name} 接口重试{MAX_RETRIES}次全部失败，未知错误: {type(e).__name__}: {str(e)}")
                raise

@lru_cache(maxsize=1)
def get_all_stock_list():
    """获取全量股票列表（缓存1天）"""
    data = request_api("stock_list")
    # 列表接口返回数组，统一格式
    return data if isinstance(data, list) else [data]

def get_stock_real(stock_code, use_broker_source=False):
    """
    获取单股实时行情
    :param stock_code: 股票代码，如000001
    :param use_broker_source: 是否使用券商数据源
    :return: 行情数据（字典）
    """
    api_name = "stock_real_broker" if use_broker_source else "stock_real_network"
    data = request_api(api_name, stock_code=stock_code)
    # 字段映射（适配返回的短字段名）
    field_map = {
        "p": "current_price",
        "pc": "change_percent",
        "ud": "change_amount",
        "v": "volume",
        "cje": "turnover",
        "zf": "amplitude",
        "hs": "turnover_rate",
        "pe": "pe_ttm",
        "lb": "volume_ratio",
        "fm": "five_min_change",
        "h": "high",
        "l": "low",
        "o": "open",
        "yc": "prev_close",
        "sz": "market_cap",
        "lt": "float_market_cap",
        "zs": "rise_speed",
        "sjl": "pb_ratio",
        "zdf60": "change_60d",
        "zdfnc": "change_ytd",
        "t": "update_time"
    }
    return {field_map.get(k, k): v for k, v in data.items()}

def get_stock_real_fast(stock_code, use_broker_source=False):
    """
    快速实时行情查询(v4.5.21b) — timeout=5s, max_retries=1
    v4.6.6: timeout 5s→3s, 加快降级链
    用于盘中实时查询，快速失败而非等15s×3次重试
    :param stock_code: 股票代码，如000001
    :param use_broker_source: 是否使用券商数据源
    :return: 行情数据（字典），失败返回空字典
    """
    try:
        api_name = "stock_real_broker" if use_broker_source else "stock_real_network"
        data = request_api(api_name, max_retries=1, request_timeout=3, stock_code=stock_code)
        field_map = {
            "p": "current_price", "pc": "change_percent", "ud": "change_amount",
            "v": "volume", "cje": "turnover", "zf": "amplitude",
            "hs": "turnover_rate", "pe": "pe_ttm", "lb": "volume_ratio",
            "fm": "five_min_change", "h": "high", "l": "low", "o": "open",
            "yc": "prev_close", "sz": "market_cap", "lt": "float_market_cap",
            "zs": "rise_speed", "sjl": "pb_ratio",
            "zdf60": "change_60d", "zdfnc": "change_ytd", "t": "update_time"
        }
        return {field_map.get(k, k): v for k, v in data.items()}
    except Exception as e:
        print(f"[MairuiFast] get_stock_real_fast({stock_code}) failed: {e}")
        return {}

def get_multi_stock_real(stock_codes):
    """
    批量获取多股实时行情（最多20只）
    :param stock_codes: 股票代码列表，如["000001", "600519"]
    :return: 行情数据列表
    """
    if len(stock_codes) > 20:
        raise ValueError("批量查询最多支持20只股票")
    data = request_api("multi_stock_real", stock_codes=",".join(stock_codes))
    return data if isinstance(data, list) else [data]

def get_stock_five_level(stock_code):
    """获取单股五档盘口数据"""
    data = request_api("stock_five_level", stock_code=stock_code)
    return data if isinstance(data, list) else [data]

def get_industry_concept_tree():
    """获取全量指数、行业、概念树结构（每周六更新）"""
    return request_api("industry_concept_tree")

def get_category_stocks(category_code):
    """
    根据行业/概念/指数代码查询成分股列表
    :param category_code: 行业/概念/指数代码，从get_industry_concept_tree接口的code字段获取
    :return: 成分股列表，包含dm(代码)、mc(名称)、jys(交易所)
    """
    return request_api("industry_concept_stocks", category_code=category_code)

def get_stock_concepts(stock_code):
    """
    根据股票代码查询所属的所有行业、概念、指数
    :param stock_code: 股票代码，如000001
    :return: 所属分类列表，包含code(分类代码)、name(分类名称)
    """
    return request_api("stock_related_concepts", stock_code=stock_code)

def get_balance_sheet(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取资产负债表
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 资产负债表数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_balance_sheet", stock_code=stock_code, **params)

def get_income_statement(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取利润表
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 利润表数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_income_statement", stock_code=stock_code, **params)

def get_cashflow_statement(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取现金流量表
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 现金流量表数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_cashflow_statement", stock_code=stock_code, **params)

def get_financial_indicators(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取财务主要指标（每股收益、ROE、毛利率、增长率等）
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 财务指标数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_key_indicators", stock_code=stock_code, **params)

def get_capital_structure(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取公司股本结构
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 股本结构数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_capital_structure", stock_code=stock_code, **params)

def get_top_holders(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取十大股东
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 十大股东数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_top_holders", stock_code=stock_code, **params)

def get_top_flow_holders(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取十大流通股东
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 十大流通股东数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_top_flow_holders", stock_code=stock_code, **params)

def get_holder_count(stock_code, start_date=None, end_date=None, limit=None):
    """
    获取股东户数
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期，格式YYYYMMDD，可选
    :param end_date: 结束日期，格式YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: 股东户数数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("financial_holder_count", stock_code=stock_code, **params)

def get_ma(stock_code, period="d", adjust="n", start_date=None, end_date=None, limit=None):
    """
    获取均线MA指标数据
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param period: 周期：5/15/30/60(分钟)/d(日线)/w(周线)/m(月线)/y(年线)，默认d
    :param adjust: 除权类型：n(不复权)/f(前复权)/b(后复权)，分钟级只能用n，默认n
    :param start_date: 开始日期：YYYYMMDD/YYYYMMDDhhmmss，可选
    :param end_date: 结束日期，可选
    :param limit: 返回最新数据条数，可选
    :return: MA数据，包含MA3/MA5/MA10/MA20/MA30/MA60/MA120/MA200/MA250
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("indicator_ma", stock_code=stock_code, period=period, adjust=adjust, **params)

def get_macd(stock_code, period="d", adjust="n", start_date=None, end_date=None, limit=None):
    """
    获取MACD指标数据
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param period: 周期：5/15/30/60(分钟)/d(日线)/w(周线)/m(月线)/y(年线)，默认d
    :param adjust: 除权类型：n(不复权)/f(前复权)/b(后复权)，分钟级只能用n，默认n
    :param start_date: 开始日期：YYYYMMDD/YYYYMMDDhhmmss，可选
    :param end_date: 结束日期，可选
    :param limit: 返回最新数据条数，可选
    :return: MACD数据，包含DIFF/DEA/MACD/EMA12/EMA26
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("indicator_macd", stock_code=stock_code, period=period, adjust=adjust, **params)

def get_boll(stock_code, period="d", adjust="n", start_date=None, end_date=None, limit=None):
    """
    获取布林带BOLL指标数据
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param period: 周期：5/15/30/60(分钟)/d(日线)/w(周线)/m(月线)/y(年线)，默认d
    :param adjust: 除权类型：n(不复权)/f(前复权)/b(后复权)，分钟级只能用n，默认n
    :param start_date: 开始日期：YYYYMMDD/YYYYMMDDhhmmss，可选
    :param end_date: 结束日期，可选
    :param limit: 返回最新数据条数，可选
    :return: BOLL数据，包含上轨u/中轨m/下轨d
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("indicator_boll", stock_code=stock_code, period=period, adjust=adjust, **params)

def get_kdj(stock_code, period="d", adjust="n", start_date=None, end_date=None, limit=None):
    """
    获取KDJ指标数据
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param period: 周期：5/15/30/60(分钟)/d(日线)/w(周线)/m(月线)/y(年线)，默认d
    :param adjust: 除权类型：n(不复权)/f(前复权)/b(后复权)，分钟级只能用n，默认n
    :param start_date: 开始日期：YYYYMMDD/YYYYMMDDhhmmss，可选
    :param end_date: 结束日期，可选
    :param limit: 返回最新数据条数，可选
    :return: KDJ数据，包含K/D/J值
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("indicator_kdj", stock_code=stock_code, period=period, adjust=adjust, **params)

def get_kline_history(stock_code: str, period: str = "d", adjust: str = "f", start_date=None, end_date=None, limit=None):
    """
    获取原始K线数据（OHLCV）
    :param stock_code: 股票代码6位数字，如688525
    :param period: d=日线 w=周线 m=月线 y=年线 5/15/30/60=分钟
    :param adjust: f=前复权 n=不复权 b=后复权
    :param start_date: 开始日期 YYYYMMDD，可选
    :param end_date: 结束日期 YYYYMMDD，可选
    :param limit: 返回最新数据条数，可选
    :return: [{t,o,h,l,c,v,a,pc,sf}, ...]
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("stock_kline", stock_code=stock_code, period=period, adjust=adjust, **params)


def get_indicator(indicator_name, stock_code, period="d", adjust="n", start_date=None, end_date=None, limit=None):
    """
    通用技术指标接口，支持RSI/WR/CCI等其他指标
    注意：迈瑞API仅MA/MACD/BOLL/KDJ有独立端点，RSI/WR/CCI的API接口可能存在
    如无独立端点请改用本地计算或AKShare数据
    :param indicator_name: 指标名，如rsi/wr/cci
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param period: 周期：5/15/30/60(分钟)/d(日线)/w(周线)/m(月线)/y(年线)，默认d
    :param adjust: 除权类型：n(不复权)/f(前复权)/b(后复权)，分钟级只能用n，默认n
    :param start_date: 开始日期：YYYYMMDD/YYYYMMDDhhmmss，可选
    :param end_date: 结束日期，可选
    :param limit: 返回最新数据条数，可选
    :return: 对应指标数据
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    if limit: params["lt"] = limit
    return request_api("indicator_generic", indicator_name=indicator_name, stock_code=stock_code, period=period, adjust=adjust, **params)


def get_limit_up_list(trade_date: str) -> list:
    """
    获取指定日期的涨停股票列表
    :param trade_date: 交易日，格式 yyyy-MM-dd，如 2026-05-08
    :return: 涨停股票列表，每项含代码/名称/涨跌幅/封板时间等
    """
    data = request_api("stock_limit_up", trade_date=trade_date)
    return data if isinstance(data, list) else []


def get_limit_down_list(trade_date: str) -> list:
    """
    获取指定日期的跌停股票列表
    :param trade_date: 交易日，格式 yyyy-MM-dd，如 2026-05-08
    :return: 跌停股票列表
    """
    data = request_api("stock_limit_down", trade_date=trade_date)
    return data if isinstance(data, list) else []


# === v4.5.9 新增便捷函数 (2026-05-11) ===

def get_new_stock_calendar() -> list:
    """获取新股日历，按申购日期倒序"""
    data = request_api("stock_new_calendar")
    return data if isinstance(data, list) else []


def get_sectors_list() -> list:
    """获取概念指数列表（券商数据源）"""
    data = request_api("sectors_list")
    return data if isinstance(data, list) else []


def get_primary_list() -> list:
    """获取一级市场板块列表（券商数据源）"""
    data = request_api("primary_list")
    return data if isinstance(data, list) else []


def get_sectors_detail(sector_name: str) -> list:
    """
    获取板块明细列表（券商数据源）
    :param sector_name: 板块名称，如'概念指数'
    """
    data = request_api("sectors_detail", sector_name=sector_name)
    return data if isinstance(data, list) else []


def get_strong_stock_pool(trade_date: str) -> list:
    """
    获取强势股池，按涨幅倒序
    :param trade_date: 交易日，格式 yyyy-MM-dd
    """
    data = request_api("strong_stock_pool", trade_date=trade_date)
    return data if isinstance(data, list) else []


def get_new_stock_pool(trade_date: str) -> list:
    """
    获取次新股池，按开板几日升序
    :param trade_date: 交易日，格式 yyyy-MM-dd
    """
    data = request_api("new_stock_pool", trade_date=trade_date)
    return data if isinstance(data, list) else []


def get_broken_board_pool(trade_date: str) -> list:
    """
    获取炸板股池，按首次封板时间升序
    :param trade_date: 交易日，格式 yyyy-MM-dd
    """
    data = request_api("broken_board_pool", trade_date=trade_date)
    return data if isinstance(data, list) else []


def get_stock_instrument(stock_code: str) -> dict:
    """
    获取股票基础信息
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    """
    return request_api("stock_instrument", stock_code=stock_code)


def get_stop_price_history(stock_code: str, start_date=None, end_date=None) -> list:
    """
    获取历史涨跌停价格
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期YYYYMMDD
    :param end_date: 结束日期YYYYMMDD
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    data = request_api("stop_price_history", stock_code=stock_code, **params)
    return data if isinstance(data, list) else []


def get_stock_indicators(stock_code: str, start_date=None, end_date=None) -> list:
    """
    获取行情指标（综合技术指标）
    :param stock_code: 股票代码带交易所后缀，如600519.SH
    :param start_date: 开始日期YYYYMMDD
    :param end_date: 结束日期YYYYMMDD
    """
    params = {}
    if start_date: params["st"] = start_date
    if end_date: params["et"] = end_date
    data = request_api("stock_indicators", stock_code=stock_code, **params)
    return data if isinstance(data, list) else []
