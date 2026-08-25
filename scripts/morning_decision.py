#!/usr/bin/env python3
"""
A股/港股盘前决策 - 轻量化版本
运行时间：工作日8:20(A股)、9:00(港股)
功能：读取前一晚缓存的预案数据、融合信号、生成最终决策报告
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

# 配置环境变量
os.environ['TZ'] = 'Asia/Shanghai'
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'

# 路径配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)
CACHE_ROOT = os.path.join(os.environ.get('CACHE_DIR', f'{PROJECT_ROOT}/cache'), 'pre_market')

# 导入内部模块
import subprocess
import numpy as np
from agents.analysts.macro_analyst import MacroAnalyst
from common.feishu_utils import send_markdown

# ⏸️ 假日检查：evening mode检查下一个交易日，morning mode检查今天
try:
    from config.holiday_calendar import is_trading_day, get_next_trading_day
    _arg_parser = argparse.ArgumentParser()
    _arg_parser.add_argument('--mode', default='morning')
    _arg_parser.add_argument('--market', default='a')
    _args, _ = _arg_parser.parse_known_args()
    _today = datetime.now().date()
    if _args.mode == 'evening':
        # 晚间预案：检查目标日（下一个交易日）是否为交易日
        _target_date = get_next_trading_day(market='A_SHARE', current_date=_today)
        if not is_trading_day(check_date=_target_date, market='A_SHARE'):
            print(f"⏸️ 下一交易日 {_target_date.strftime('%Y-%m-%d')} 非交易日，跳过")
            sys.exit(0)
        # v4.6.3: 距离检查 — 下个交易日>2天时跳过
        # 例如周四晚跑出的周一预案会在周日被覆盖，多余执行
        _days_gap = (_target_date - _today).days
        if _days_gap > 2:
            print(f"⏸️ 下个交易日 {_target_date.strftime('%Y-%m-%d')} 距今{_days_gap}天(>2天)，跳过。周日运行将覆盖。")
            # 写skipped进度文件 → Dashboard显示"跳过"而非"missed"
            try:
                _pd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache", "progress")
                _slot = "pre_market_plan"
                _skip_id = f"{_slot}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                os.makedirs(_pd, exist_ok=True)
                with open(os.path.join(_pd, f"{_skip_id}.json"), "w") as _sf:
                    json.dump({
                        "task_id": _skip_id,
                        "task_name": _slot,
                        "status": "skipped",
                        "progress": {"step": 0, "total": 1},
                        "message": f"⏸️ 目标日{_target_date}距今{_days_gap}天(>2天)，跳过",
                        "started_at": datetime.now().isoformat(),
                        "updated_at": datetime.now().isoformat(),
                        "completed_at": datetime.now().isoformat(),
                    }, _sf, ensure_ascii=False, indent=2)
            except Exception:
                pass
            sys.exit(0)
        print(f"📋 晚间预案：生成 {_target_date.strftime('%Y-%m-%d')} 交易决策...")
    else:
        # 早盘决策：检查今天
        if not is_trading_day(check_date=_today, market='A_SHARE'):
            print(f"⏸️ {_today.strftime('%Y-%m-%d')} 非交易日，跳过")
            sys.exit(0)
        print(f"📋 生成 {_today.strftime('%Y-%m-%d')} 交易决策...")
except ImportError:
    pass


def get_trade_date():
    """获取当前交易日"""
    today = datetime.now()
    # 周末顺延到周一（盘前决策只在工作日运行）
    if today.weekday() >=5:
        offset = 7 - today.weekday()
        today = today + timedelta(days=offset)
    return today.strftime('%Y%m%d'), today.strftime('%Y-%m-%d')

def _load_black_swan_baseline():
    """v4.6.9i(审计F1-1): 黑天鹅真相源统一 — 优先 data/black_swan_status.json (每日08:10 LPPL cron更新, 新鲜),
    fallback adaptive_params risk.black_swan_position_ratio。
    背景: adaptive_params risk.* 曾冻结2个月(0.68), 执行层读它导致危机期仓位上限比正确数据宽松1倍(审计P0-1)。
    返回 (position_ratio: float, active: bool)
    """
    ratio, active = 0.8, False
    try:
        bs_path = os.path.join(PROJECT_ROOT, 'data', 'black_swan_status.json')
        if os.path.exists(bs_path):
            with open(bs_path, 'r', encoding='utf-8') as _f:
                _bs = json.load(_f)
            _r = _bs.get('position_ratio')
            if _r is not None:
                ratio = float(_r)
            active = bool(_bs.get('active', False))
            return ratio, active
    except Exception:
        pass
    try:
        import yaml
        ap_path = os.path.join(PROJECT_ROOT, 'config', 'adaptive_params.yaml')
        if os.path.exists(ap_path):
            with open(ap_path, 'r', encoding='utf-8') as _f:
                ap = yaml.safe_load(_f)
            ratio = float(ap.get('risk', {}).get('black_swan_position_ratio', 0.8))
            active = ap.get('risk', {}).get('black_swan_active', False)
    except Exception:
        pass
    return ratio, active


def _load_cached_json(fpath, default, label):
    """独立加载单个缓存文件，失败时返回默认值（不阻断其他文件）"""
    try:
        if os.path.exists(fpath):
            with open(fpath, 'r', encoding='utf-8') as f:
                return json.load(f)
        else:
            print(f"  ℹ️ 缓存缺失: {label} ({fpath})")
    except Exception as e:
        print(f"  ⚠️ 缓存损坏[{label}]: {e}")
    return default


def load_cached_data(date_str):
    """加载前一晚缓存的预案数据，每个文件独立容错
    
    v4.5.9d: 改为独立加载每个文件，不会因一张表缺失而全军覆没。
    缺失时自动填默认值：sectors=[], stocks=[], events=结构, risk=0.8
    
    P1-FIX: 晚间模式用昨日日期(20260519)写入_stocks_evening.json,
    但晨间模式读今日日期(20260520)。增加前一日退路查找。
    """
    # 独立加载4个缓存文件，每个单独try/except
    sector_scores = _load_cached_json(
        f'{CACHE_ROOT}/{date_str}_sectors.json', [], 'sectors')
    alpha_scores = _load_cached_json(
        f'{CACHE_ROOT}/{date_str}_stocks.json', [], 'stocks')
    event_signals = _load_cached_json(
        f'{CACHE_ROOT}/{date_str}_events.json',
        {'high_priority': [], 'medium_priority': [], 'low_priority': []},
        'events')
    risk_data = _load_cached_json(
        f'{CACHE_ROOT}/{date_str}_risk.json',
        {'position_ratio': 0.8, 'recommended_position_ratio': 0.8, 'max_drawdown_threshold': 5},
        'risk')

    # v4.5.9b: 优先使用晚间缓存的多因子评分（若有）
    evening_path = f'{CACHE_ROOT}/{date_str}_stocks_evening.json'
    if not os.path.exists(evening_path):
        _prev = datetime.strptime(date_str, '%Y%m%d') - timedelta(days=1)
        for _ in range(3):  # 最多回溯3天(跳过周末)
            _prev_str = _prev.strftime('%Y%m%d')
            _prev_evening = f'{CACHE_ROOT}/{_prev_str}_stocks_evening.json'
            if os.path.exists(_prev_evening):
                evening_path = _prev_evening
                print(f"  ℹ️ 当日无晚间缓存, 回溯至 {_prev_str}_stocks_evening.json")
                break
            _prev -= timedelta(days=1)
    if os.path.exists(evening_path):
        try:
            with open(evening_path, 'r', encoding='utf-8') as _f:
                _evening = json.load(_f)
            if _evening and (any(s.get('action_signal', '') != '中性' for s in _evening)
                             or any(s.get('pred_signal', 'hold') != 'hold' for s in _evening)):
                alpha_scores = _evening
                print(f"  ✅ 使用晚间预案多因子评分({len(alpha_scores)}只)")
            else:
                print(f"  ⚠️ 晚间缓存无有效信号，使用pre_market_refresh缓存")
        except Exception as e:
            print(f"  ⚠️ 晚间缓存读取失败: {e}")
    else:
        n = len(alpha_scores) if alpha_scores else 0
        print(f"  ℹ️ 无晚间缓存，使用pre_market_refresh缓存({n}只)")

    return sector_scores, alpha_scores, event_signals, risk_data

def load_evening_plan(target_date: str) -> list:
    """v4.5.7: 加载晚间生成的计划交易，供早盘模式增量调整

    读取 cache/planned_trades.json，验证目标日期匹配且不为空。
    用户手动编辑后的版本会被保留然后与新鲜数据融合。

    Returns:
        list of trades (空列表如果不存在/无效/不同日期)
    """
    planned_path = os.path.join(PROJECT_ROOT, 'cache', 'planned_trades.json')
    if not os.path.exists(planned_path):
        return []
    try:
        with open(planned_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            if len(data) == 0:
                return []
            # 检查是否同一目标日期
            target = data[0].get('target_date', '')
            if target and target != target_date:
                print(f"ℹ️ planned_trades.json 目标日期{target}≠{target_date}，丢弃旧计划")
                return []
            print(f"📋 加载晚间预案: {len(data)}笔交易计划")
            return data
        if isinstance(data, dict):
            trades = data.get('trades', [])
            target = data.get('target_date', '')
            if target and target != target_date:
                print(f"ℹ️ planned_trades.json 目标日期{target}≠{target_date}，丢弃旧计划")
                return []
            if trades:
                print(f"📋 加载晚间预案: {len(trades)}笔交易计划")
            return trades
        return []
    except Exception as e:
        print(f"⚠️ 加载晚间预案失败(不重要): {e}")
        return []


LAST_DAILY_PREDICT_FRESHNESS = {
    "status": "unknown",
    "reason": "load_daily_predictions尚未执行",
    "predict_date": "",
    "predict_time": "",
}


def load_daily_predictions():
    """v4.5.3c 加载 batch_predict 产出的每日ML预测缓存，含时效校验
    v4.5.13: 注入h20d信号到每个预测的h20d dict中，供plan_trades交叉验证
    """
    pred_file = os.path.join(PROJECT_ROOT, 'cache', 'daily_predict.json')
    global LAST_DAILY_PREDICT_FRESHNESS
    try:
        from core.data_freshness import assess_daily_predict_freshness
        LAST_DAILY_PREDICT_FRESHNESS = assess_daily_predict_freshness(pred_file)
    except Exception as e:
        LAST_DAILY_PREDICT_FRESHNESS = {
            "status": "invalid",
            "allow_buy": False,
            "allow_sell": True,
            "predict_date": "",
            "predict_time": "",
            "reason": f"freshness检查失败: {e}",
        }

    if not os.path.exists(pred_file):
        print("ℹ️ daily_predict.json 不存在，跳过ML预测增强")
        return {}
    try:
        with open(pred_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # v4.5.13: 提取h20d信号列表 (优先读_symbols列表, 降级读int计数)
        raw_buy = data.get('h20d_buy_symbols') or data.get('h20d_buy', [])
        raw_sell = data.get('h20d_sell_symbols') or data.get('h20d_sell', [])
        h20d_buy_set = set(raw_buy) if isinstance(raw_buy, list) else set()
        h20d_sell_set = set(raw_sell) if isinstance(raw_sell, list) else set()
        
        # P0: 预测时效硬阻断 — stale/missing/invalid 禁止ML驱动BUY
        predict_date = data.get('predict_date', '')
        today = datetime.now().strftime('%Y-%m-%d')
        freshness_status = LAST_DAILY_PREDICT_FRESHNESS.get("status", "unknown")
        STALE = not LAST_DAILY_PREDICT_FRESHNESS.get("allow_buy", False)
        if STALE:
            print(f"⛔ ML预测不可用于买入: {LAST_DAILY_PREDICT_FRESHNESS.get('reason', freshness_status)}")
        elif predict_date and predict_date != today:
            try:
                days_old = (datetime.strptime(today, '%Y-%m-%d')
                            - datetime.strptime(predict_date, '%Y-%m-%d')).days
                if days_old >= 1 or freshness_status == "decayed":
                    print(f"⚠️ 预测1天前, 置信度折减20%")
            except Exception:
                pass
        
        predictions = {}
        if not STALE:
            for p in data.get('predictions', []):
                sym = p.get('symbol', '')
                # v4.6.x: 显式跳过观察池(suspended)标的，避免依赖confidence=0隐式过滤
                if p.get("confidence_level") == "suspended":
                    continue
                if predict_date and predict_date != today:
                    try:
                        d = (datetime.strptime(today, '%Y-%m-%d')
                             - datetime.strptime(predict_date, '%Y-%m-%d')).days
                        if d == 1:
                            p['confidence'] = round(p.get('confidence', 0) * 0.80, 4)
                            p['confidence_decayed'] = True
                    except Exception:
                        pass
                # v4.5.13: 注入h20d信号
                if isinstance(p.get('h20d'), dict):
                    if sym in h20d_buy_set:
                        p['h20d']['signal'] = 'buy'
                    elif sym in h20d_sell_set:
                        p['h20d']['signal'] = 'sell'
                    else:
                        p['h20d']['signal'] = 'hold'
                predictions[sym] = p
            print(f"📊 加载ML预测: {len(predictions)}条 (预测日{predict_date}, h20d买{len(h20d_buy_set)}卖{len(h20d_sell_set)}, freshness={freshness_status})")
        else:
            print(f"📊 ML预测已过期, 回退纯技术信号 (0条)")
        return predictions
    except Exception as e:
        print(f"⚠️ 读取daily_predict.json失败: {e}")
        return {}

def fetch_overnight_data():
    """v4.6.5 获取隔夜外盘数据: 美股收盘/S&P500/纳指/VIX/USDCNY
    
    数据源策略（二级降级）:
      1. yfinance — S&P500/纳指/VIX (py_mini_racer与Python3.14不兼容, 跳过akshare_sina)
      2. Playwright Google Finance — VIX最后降级（yfinance限流时兜底）
    
    修复：v4.6.5 yfinance升为Tier-1, akshare_sina因py_mini_racer 3.14不兼容被移除
    """
    # 强制代理绕过（akshare/东方财富/麦蕊API等）
    os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'
    result = {
        "sp500": None, "sp500_change": None,
        "nasdaq": None, "nasdaq_change": None,
        "vix": None,
        "usdcny": None,
        "fetched": False,
        "is_intraday": False,
        "source": "yfinance",
    }
    
    try:
        import yfinance as yf
        
        # ── S&P500 和 纳斯达克 (yfinance Tier-1) ──
        for yf_sym, key, name in [("^GSPC", "sp500", "S&P500"), ("^IXIC", "nasdaq", "纳斯达克")]:
            try:
                ticker = yf.Ticker(yf_sym)
                hist = ticker.history(period='5d')
                if hist is not None and len(hist) >= 2:
                    closes = hist['Close'].values
                    result[key] = float(closes[-1])
                    result[f"{key}_change"] = round(float((closes[-1] / closes[-2] - 1) * 100), 2)
            except Exception as e:
                print(f"⚠️ {name} 获取失败 (yfinance): {e}")
        
        # ── VIX恐慌指数 (yfinance Tier-1) ──
        try:
            vix_ticker = yf.Ticker('^VIX')
            vix_hist = vix_ticker.history(period='5d')
            if vix_hist is not None and len(vix_hist) > 0:
                result["vix"] = round(float(vix_hist['Close'].iloc[-1]), 2)
                print(f"✅ VIX (yfinance): {result['vix']}")
        except Exception as e:
            print(f"⚠️ VIX yfinance失败: {e}")
            # Tier 2 fallback: Sina HTTP 直连
            try:
                import requests
                resp = requests.get("https://hq.sinajs.cn/list=gb_vix", 
                                   headers={"Referer": "https://finance.sina.com.cn"}, timeout=5)
                if resp.status_code == 200:
                    parts = resp.text.split('"')[1].split(',')
                    if len(parts) > 1:
                        result["vix"] = float(parts[1])
                        print(f"✅ VIX (Sina降级): {result['vix']}")
            except Exception as e2:
                print(f"⚠️ VIX Sina HTTP也失败: {e2}")
        
        # ── USD/CNY 汇率 (三级降级: 东方财富 → Sina → exchangerate.host) ──
        # 降级1: 东方财富 push2
        try:
            import requests
            resp = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get?secid=133.USDCNH&fields=f43,f44,f45,f46,f60",
                timeout=5
            )
            if resp.status_code == 200:
                em_data = resp.json()
                if em_data and em_data.get("data"):
                    result["usdcny"] = round(float(em_data["data"].get("f43", 0)) / 10000, 4)
        except Exception as e:
            print(f"⚠️ USDCNY 东方财富失败: {e}")
        
        # 降级2: Sina Finance (USDCNY汇率)
        if result.get("usdcny") is None:
            try:
                import requests
                resp = requests.get(
                    "https://hq.sinajs.cn/list=fx_susdcny",
                    headers={"Referer": "https://finance.sina.com.cn"},
                    timeout=5
                )
                if resp.status_code == 200:
                    parts = resp.text.split('"')[1].split(',')
                    if len(parts) > 1:
                        result["usdcny"] = round(float(parts[1]), 4)
            except Exception as e:
                print(f"⚠️ USDCNY Sina失败: {e}")
        
        # 降级3: exchangerate.host (无需API key)
        if result.get("usdcny") is None:
            try:
                import requests
                resp = requests.get(
                    "https://api.frankfurter.app/latest?from=USD&symbols=CNY",
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success") and "CNY" in data.get("rates", {}):
                        result["usdcny"] = round(data["rates"]["CNY"], 4)
            except Exception as e:
                print(f"⚠️ USDCNY exchangerate.host失败: {e}")
        
        # ── 检查结果 ──
        result["fetched"] = any(
            v is not None for k, v in result.items() 
            if k not in ("fetched", "is_intraday", "source")
        )
        
        # ── VIX Tier-2 fallback: Playwright Google Finance (yfinance限流时兜底) ──
        if result.get("vix") is None:
            try:
                result["vix"] = _fetch_vix_playwright()
                if result.get("vix") is not None:
                    print(f"🌍 VIX [Playwright Tier-2]: {result['vix']:.1f}")
            except Exception as e:
                print(f"⚠️ VIX Playwright降级也失败: {e}")
        
        if result["fetched"] or result.get("vix") is not None:
            sp_str = f'{result.get("sp500",0):.0f}' if result.get("sp500") else "-"
            sp_chg = f'{result.get("sp500_change",0):+.1f}%' if result.get("sp500_change") is not None else "-"
            print(f"🌍 隔夜外盘[yfinance]: S&P500 {sp_str} ({sp_chg}) | VIX {result.get('vix','-')} | USDCNY {result.get('usdcny','-')}")
        
        return result
        
    except Exception as e:
        print(f"⚠️ yfinance外盘数据获取失败: {e}")
        return result  # 返回已有数据，VIX可能为None由上层处理


def _fetch_vix_playwright():
    """VIX Tier-4: Playwright Google Finance，作为yfinance也失败时的最后降级"""
    try:
        import os
        for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
            os.environ.pop(k, None)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
            page = browser.new_page()
            page.goto("https://www.google.com/finance/quote/VIX:INDEXCBOE",
                      wait_until="domcontentloaded", timeout=30000)
            # 额外等待SPA渲染
            import time
            time.sleep(8)
            all_text = page.inner_text("body", timeout=5000)
            browser.close()
        import re
        m = re.search(r'VIX\s*[^0-9]*\n?\s*([\d]+\.\d+)', all_text)
        if m:
            return float(m.group(1))
        nums = re.findall(r'\b(\d{2}\.\d{2})\b', all_text[:5000])
        if nums:
            # 取合理范围(15-25)的第一个候选
            for n in nums:
                v = float(n)
                if 15 <= v <= 25:
                    return v
            return float(nums[0])
        return None
    except Exception as e:
        print(f"  ⚠️ Playwright Google VIX获取失败: {e}")
    # Yahoo Finance 备用
    try:
        import os
        for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
            os.environ.pop(k, None)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-proxy-server"])
            page = browser.new_page()
            page.goto("https://finance.yahoo.com/quote/%5EVIX/",
                      wait_until="domcontentloaded", timeout=30000)
            import time
            time.sleep(5)
            text = page.inner_text("body", timeout=5000)
            browser.close()
        m = re.search(r'"regularMarketPrice".*?"raw":([\d.]+)', text)
        if m:
            return float(m.group(1))
        # Fallback: find VIX-like numbers in text
        nums = re.findall(r'\b(\d{2}\.\d{2})\b', text[:5000])
        for n in nums:
            v = float(n)
            if 15 <= v <= 25:
                return v
        return None
    except Exception as e2:
        print(f"  ⚠️ Playwright Yahoo VIX也失败: {e2}")
        return None


def _fetch_overnight_yfinance_fallback():
    """v4.5.2 yfinance作为最终降级源（仅在akshare/Sina全部失败时使用）"""
    result = {
        "sp500": None, "sp500_change": None,
        "nasdaq": None, "nasdaq_change": None,
        "vix": None, "usdcny": None,
        "fetched": False, "is_intraday": False,
        "source": "yfinance_fallback",
    }
    try:
        import yfinance as yf
        tickers = ["^GSPC", "^IXIC", "^VIX", "CNY=X"]
        data = yf.download(tickers, period="5d", progress=False)
        if data is None or data.empty:
            return result
        close_data = data.get("Close", data)
        for col in close_data.columns if hasattr(close_data, 'columns') else []:
            col_str = str(col)
            closes = close_data[col].dropna()
            if col_str == "^GSPC" and len(closes) >= 1:
                result["sp500"] = float(closes.iloc[-1])
                if len(closes) >= 2:
                    result["sp500_change"] = round(float((closes.iloc[-1]/closes.iloc[-2]-1)*100),2)
            elif col_str == "^IXIC" and len(closes) >= 1:
                result["nasdaq"] = float(closes.iloc[-1])
                if len(closes) >= 2:
                    result["nasdaq_change"] = round(float((closes.iloc[-1]/closes.iloc[-2]-1)*100),2)
            elif col_str == "^VIX" and len(closes) >= 1:
                result["vix"] = float(closes.iloc[-1])
            elif col_str == "CNY=X" and len(closes) >= 1:
                result["usdcny"] = round(float(closes.iloc[-1]), 4)
        result["fetched"] = any(v is not None for k, v in result.items() if k not in ("fetched","is_intraday","source"))
        if result["fetched"]:
            print(f"🌍 隔夜外盘[yfinance降级]: S&P500 {result.get('sp500','-')} | VIX {result.get('vix','-')}")
    except Exception as e:
        print(f"⚠️ yfinance降级也失败: {e}")
    return result


# ═══════════════════════════════════════════════
# v4.5.7: 晚间预案 — 收盘后实时数据获取
# 替代 morning mode 下读取12h前的pre_market缓存
# 数据源顺序: 麦蕊API → akshare(东方财富) → 空值
# ═══════════════════════════════════════════════

def fetch_evening_market_sectors() -> list:
    """收盘后获取今日板块涨跌幅排行

    数据源优先级:
      1. 麦蕊API get_industry_concept_tree (仅获取板块结构 + get_category_stocks 标的)
         但麦蕊无板块涨跌幅接口→降级到akshare
      2. akshare stock_board_industry_name_em (行业板块实时行情)
         stock_board_concept_name_em (概念板块实时行情)
    """
    result = []

    # === 行业板块TOP10 (麦蕊无板块涨跌幅→直接akshare) ===
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_board_industry_name_em()
        if df is not None and len(df) > 0:
            for _, row in df.head(10).iterrows():
                name = str(row.get("板块名称", ""))
                chg = float(row.get("涨跌幅", 0) or 0)
                score = 5.0 + (chg / 2)
                score = max(0, min(10, score))
                result.append({
                    "name": f"行业-{name}",
                    "code": str(row.get("板块代码", "")),
                    "change_pct": chg,
                    "total_score": round(score, 1),
                    "recommend_level": "推荐" if chg > 2 else "关注" if chg > 0.5 else "中性" if chg > -1 else "回避",
                })
            print(f"  📊 行业板块: {len(df.head(10))}个 (akshare)")
    except Exception as e:
        print(f"  ⚠️ 行业板块获取失败: {e}")
        # 降级: 同花顺
        try:
            from scripts.pre_market_refresh import _fetch_thshy_sectors
            ths = _fetch_thshy_sectors()
            result.extend(ths[:10])
        except Exception:
            pass

    # === 概念板块TOP3 (麦蕊无涨跌幅→直接akshare) ===
    try:
        import akshare as ak
        df_cc = ak.stock_board_concept_name_em()
        if df_cc is not None and len(df_cc) > 0:
            for _, row in df_cc.head(3).iterrows():
                name = str(row.get("板块名称", ""))
                chg = float(row.get("涨跌幅", 0) or 0)
                score = 5.0 + (chg / 2)
                score = max(0, min(10, score))
                result.append({
                    "name": f"概念-{name}",
                    "code": str(row.get("板块代码", "")),
                    "change_pct": chg,
                    "total_score": round(score, 1),
                    "recommend_level": "推荐" if chg > 2 else "关注" if chg > 0.5 else "中性" if chg > -1 else "回避",
                })
            print(f"  💡 概念板块: {len(df_cc.head(3))}个 (akshare)")
    except Exception as e:
        print(f"  ⚠️ 概念板块获取失败: {e}")
        try:
            from scripts.pre_market_refresh import _fetch_thshy_concepts
            ths_cc = _fetch_thshy_concepts()
            result.extend(ths_cc[:3])
        except Exception:
            pass

    return result


def fetch_evening_capital_flow() -> dict:
    """收盘后获取今日资金流向数据

    返回包含: 北向资金净流入, 行业资金流排行
    """
    result = {
        "north_flow": None,       # 北向资金当日净流入(亿元)
        "north_sh_flow": None,     # 沪股通净流入
        "north_sz_flow": None,     # 深股通净流入
        "top_inflow_sectors": [],  # 资金流入TOP5行业
        "top_outflow_sectors": [], # 资金流出TOP5行业
    }

    # === 北向资金: 麦蕊无接口，直接用akshare ===
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_hsgt_fund_flow_summary_em()
        if df is not None and len(df) > 0:
            # 过滤北上资金（沪股通+深股通）
            north = df[df['资金方向'] == '北向']
            if len(north) > 0:
                total_flow = north['成交净买额'].sum()
                result["north_flow"] = round(float(total_flow), 2)
                result["north_sh_flow"] = round(float(north[north['板块'] == '沪股通']['成交净买额'].iloc[0]), 2)
                result["north_sz_flow"] = round(float(north[north['板块'] == '深股通']['成交净买额'].iloc[0]), 2)
                print(f"  🧭 北向资金: 合计{result['north_flow']}亿 (沪{result['north_sh_flow']}亿+深{result['north_sz_flow']}亿)")
        
        # 沪深港通板块排行
        try:
            df_rank = ak.stock_hsgt_board_rank_em()
            if df_rank is not None and len(df_rank) > 0:
                top_in = df_rank.head(5)
                for _, r in top_in.iterrows():
                    result["top_inflow_sectors"].append({
                        "name": str(r.get("板块名称", "")),
                        "flow": round(float(r.get("板块成交额", 0)), 2),
                    })
        except Exception:
            pass
    except Exception as e:
        print(f"  ⚠️ 北向资金获取失败: {e}")

    # === 行业资金流排行 ===
    try:
        import akshare as ak
        df_flow = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        if df_flow is not None and len(df_flow) > 0:
            for _, r in df_flow.head(3).iterrows():
                result["top_inflow_sectors"].append({
                    "name": str(r.get("名称", "")),
                    "flow": round(float(r.get("主力净流入-净额", 0) or 0), 2),
                })
            for _, r in df_flow.tail(3).iterrows():
                result["top_outflow_sectors"].append({
                    "name": str(r.get("名称", "")),
                    "flow": round(float(r.get("主力净流入-净额", 0) or 0), 2),
                })
            print(f"  💰 行业资金流: {len(df_flow)}个板块")
        else:
            raise ValueError("akshare返回空")
    except Exception as e:
        print(f"  ⚠️ 行业资金流失败: {e}")
        # 降级: 同花顺
        try:
            from scripts.pre_market_refresh import _fetch_thshy_capital_flow
            ths_flow = _fetch_thshy_capital_flow()
            result["top_inflow_sectors"] = ths_flow.get("top_inflow", [])[:3]
            result["top_outflow_sectors"] = ths_flow.get("top_outflow", [])[:3]
        except Exception:
            pass

    return result


def fetch_evening_dragon_tiger() -> dict:
    """收盘后获取今日涨停/跌停/龙虎榜数据

    数据源: 麦蕊API hslt/ztgc + hslt/dtgc (涨停/跌停)
           akshare stock_lhb_* (龙虎榜 — 麦蕊无此接口)

    Returns:
        {"limit_up": [...], "limit_down": [...], "dragon_tiger": [...]}
    """
    result = {"limit_up": [], "limit_down": [], "dragon_tiger": []}
    today_str = datetime.now().strftime("%Y-%m-%d")

    # === 涨停列表（麦蕊API —— 优先） ===
    try:
        from config.mairui_api_config import get_limit_up_list
        up_list = get_limit_up_list(today_str)
        if up_list and len(up_list) > 0:
            for item in up_list[:15]:
                result["limit_up"].append({
                    "symbol": str(item.get("dm", item.get("code", ""))),
                    "name": str(item.get("mc", item.get("name", ""))),
                    "change_pct": float(item.get("zf", item.get("涨跌幅", 0)) or 0),
                    "type": "涨停",
                    "industry": str(item.get("hy", "")),
                    "first_time": str(item.get("fbt", "")),
                    "consecutive": str(item.get("lbc", "")),
                })
            print(f"  🚀 涨停(麦蕊): {len(up_list)}只")
        else:
            raise ValueError("麦蕊返回空")
    except Exception as e:
        print(f"  ⚠️ 麦蕊涨停失败({str(e)[:30]}), 降级akshare...")
        try:
            import akshare as ak
            df = ak.stock_zt_pool_em(date=datetime.now().strftime("%Y%m%d"))
            if df is not None and len(df) > 0:
                for _, r in df.head(10).iterrows():
                    result["limit_up"].append({
                        "symbol": str(r.get("代码", "")),
                        "name": str(r.get("名称", "")),
                        "change_pct": float(r.get("涨跌幅", 0) or 0),
                        "type": "涨停",
                        "reason": str(r.get("涨停原因", "")),
                    })
                print(f"  🚀 涨停(akshare降级): {len(df)}只")
        except Exception as e2:
            print(f"  ⚠️ akshare涨停也失败: {e2}")

    # === 跌停列表（麦蕊API） ===
    try:
        from config.mairui_api_config import get_limit_down_list
        down_list = get_limit_down_list(today_str)
        if down_list and len(down_list) > 0:
            for item in down_list[:5]:
                result["limit_down"].append({
                    "symbol": str(item.get("dm", item.get("code", ""))),
                    "name": str(item.get("mc", item.get("name", ""))),
                    "change_pct": float(item.get("zf", 0) or 0),
                    "type": "跌停",
                })
            print(f"  📉 跌停(麦蕊): {len(down_list)}只")
    except Exception as e:
        print(f"  ⚠️ 麦蕊跌停跳过: {e}")

    # === 龙虎榜（麦蕊无接口 → akshare） ===
    try:
        import akshare as ak
        import pandas as pd
        df_lhb = ak.stock_lhb_detail_em(start_date=today_str, end_date=today_str)
        if isinstance(df_lhb, pd.DataFrame) and len(df_lhb) > 0:
            for _, r in df_lhb.head(5).iterrows():
                result["dragon_tiger"].append({
                    "symbol": str(r.get("代码", "")),
                    "name": str(r.get("名称", "")),
                    "change_pct": float(r.get("涨跌幅", 0) or 0),
                    "reason": str(r.get("原因", "")),
                })
            print(f"  🐉 龙虎榜(akshare): {len(df_lhb)}只")
    except Exception as e:
        print(f"  ⚠️ 龙虎榜跳过(akshare): {str(e)[:50]}")

    return result


def fetch_evening_pool_prices(pool_symbols: list) -> dict:
    """收盘后获取股票池今日收盘价和涨跌幅

    v4.5.13: 改用批量API get_multi_stock_real(≤20只/次)，避免42次单股调用触发403限流
    数据源: 麦蕊API get_multi_stock_real
    降级: akshare stock_zh_a_spot (新浪, 东财push2已封锁, 审计F1-3)

    Returns:
        {symbol: {"name": str, "close": float, "change_pct": float}, ...}
    """
    prices = {}

    # 主数据源: 麦蕊API批量获取 (20只/组, 最多3次调用)
    try:
        from config.mairui_api_config import get_multi_stock_real
        from common.retry_decorator import retry_call

        for i in range(0, len(pool_symbols), 20):
            batch = pool_symbols[i:i+20]
            try:
                batch_data = retry_call(get_multi_stock_real, batch, max_attempts=2, backoff=2.0)
                if batch_data:
                    for item in batch_data:
                        # 批量API返回缩写字段名: dm=代码, p=现价, pc=涨跌幅
                        # 标准化兼容两种格式
                        sym = (item.get("symbol") or item.get("stock_code") or
                               item.get("dm", "") or "")
                        price = (item.get("current_price") or
                                 item.get("p", 0) or 0)
                        chg = (item.get("change_percent") or
                               item.get("change_pct") or
                               item.get("pc", 0) or 0)
                        if sym and float(price) > 0:
                            prices[str(sym)] = {
                                "name": item.get("name", sym),
                                "close": float(price),
                                "change_pct": float(chg or 0),
                            }
            except Exception as e:
                print(f"  ⚠️ 批量{len(batch)}只失败: {str(e)[:60]}")
            import time
            time.sleep(0.5)  # 批量调用间延迟0.5s

        if prices:
            print(f"  ✅ 麦蕊批量API: {len(prices)}只收盘价 ({len(range(0, len(pool_symbols), 20))}次调用)")
            return prices
    except Exception as e:
        print(f"  ⚠️ 麦蕊API收盘价失败: {e}")

    # 降级: akshare 全市场行情
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_zh_a_spot()  # v4.6.9i(审计F1-3): 东财→新浪(东财push2实测封锁, 新浪实测可用)
        if df is not None and len(df) > 0:
            df["code"] = df["代码"].str.replace(r"^(sh|sz|bj)", "", regex=True)
            for sym in pool_symbols:
                row = df[df["code"] == sym]
                if len(row) > 0:
                    r = row.iloc[0]
                    prices[sym] = {
                        "name": str(r.get("名称", sym)),
                        "close": float(r.get("最新价", 0)),
                        "change_pct": float(r.get("涨跌幅", 0) or 0),
                    }
            print(f"  ✅ akshare行情: {len(prices)}只收盘价")
            return prices
    except Exception as e:
        print(f"  ⚠️ akshare行情失败: {e}")

    return prices


def fetch_evening_market_data(pool_symbols: list = None) -> tuple:
    """v4.5.7: 收盘后获取今日全部市场数据（晚间预案专用）

    Args:
        pool_symbols: 股票池代码列表（加载收盘价用）

    Returns:
        (sector_scores, alpha_scores, event_signals, risk_data, market_summary)
        与 load_cached_data 返回格式兼容
    """
    print(f"\n{'='*50}")
    print(f"📡 收盘后实时数据获取 (晚间预案专用)")
    print(f"{'='*50}")

    # 1. 板块涨跌幅
    sector_scores = fetch_evening_market_sectors() or []

    # 2. 资金流向
    capital_flow = fetch_evening_capital_flow() or {}

    # 3. 涨停/跌停/龙虎榜
    dragon_tiger_data = fetch_evening_dragon_tiger() or {}
    limit_up_list = dragon_tiger_data.get("limit_up", [])
    limit_down_list = dragon_tiger_data.get("limit_down", [])
    dragon_tiger_list = dragon_tiger_data.get("dragon_tiger", [])

    # 4. 个股收盘价
    pool_prices = {}
    if pool_symbols:
        pool_prices = fetch_evening_pool_prices(pool_symbols) or {}

    # 5. 组装 alpha_scores (与 load_cached_data 的 stocks.json 格式兼容)
    # v4.5.8: alpha_scores 多因子评分 (动量+ML确认+量价配合+风险调整)
    # P0修复: 旧版纯动量(chg/4) → 新版多因子, 消除"涨停=推荐"的伪逻辑
    #
    # 因子架构:
    #   F1 动量因子 (40%) — 当日涨跌幅, 有天花板(±2%→±0.8分)
    #   F2 ML确认因子 (35%) — daily_predict的方向一致性和置信度
    #   F3 量价因子 (15%) — 成交量异动(放量涨>缩量涨)
    #   F4 风险调整 (10%) — 黑天鹅/回撤/波动率惩罚
    #
    # 关键改进:
    #   1. 动量天花板: chg±2%以上不再线性加分(防止涨停股虚高)
    #   2. ML方向矛盾降分: 动量涨但ML说sell → 动量因子打折50%
    #   3. 无ML数据标的: F2=中性(0分), 不惩罚也不加分

    # 加载ML预测数据(多因子F2用)
    ml_predictions = {}
    try:
        pred_file = os.path.join(PROJECT_ROOT, 'cache', 'daily_predict.json')
        if os.path.exists(pred_file):
            with open(pred_file, 'r', encoding='utf-8') as f:
                pred_data = json.load(f)
            for p in pred_data.get('predictions', []):
                ml_predictions[p.get('symbol', '')] = p
    except Exception as e:
        print(f"  ⚠️ 加载ML预测用于多因子评分失败: {e}")

    # 加载风险参数(F4用) — v4.6.9i(审计F1-1): 真相源统一为 black_swan_status.json, adaptive_params仅fallback
    risk_position_ratio, black_swan_active = _load_black_swan_baseline()

    # v4.5.8.2: 修复#1 — pool_prices可能无name(麦蕊API不返回name字段)
    # 从stock_pool/daily_predict补充名称
    stock_name_map = {}
    try:
        stock_pool_path = os.path.join(PROJECT_ROOT, 'config', 'master_stock_pool.yaml')
        if os.path.exists(stock_pool_path):
            import yaml as yml2
            with open(stock_pool_path, 'r', encoding='utf-8') as f:
                pool_cfg = yml2.safe_load(f)
            for cat in ['bluechip', 'core', 'growth', 'cyclical', 'flex']:
                for stk in pool_cfg.get(cat, []):
                    if isinstance(stk, dict):
                        code = stk.get('code', stk.get('symbol', ''))
                        name = stk.get('name', '')
                        if code and name:
                            stock_name_map[code] = name
    except Exception:
        pass
    # 也从daily_predict补充
    try:
        pred_file = os.path.join(PROJECT_ROOT, 'cache', 'daily_predict.json')
        if os.path.exists(pred_file):
            with open(pred_file, 'r', encoding='utf-8') as f:
                pred_data = json.load(f)
            for p in pred_data.get('predictions', []):
                sym = p.get('symbol', '')
                name = p.get('name', '')
                if sym and name and sym not in stock_name_map:
                    stock_name_map[sym] = name
    except Exception:
        pass

    alpha_scores = []
    for sym, info in pool_prices.items():
        chg = info.get("change_pct", 0)
        # 确保有名称: pool_prices > stock_name_map > 代码
        stock_name = info.get("name", "")
        if not stock_name or stock_name == sym:
            stock_name = stock_name_map.get(sym, sym)
            info["name"] = stock_name  # 回写以确保后续可用

        # 使用统一评分函数 (core/production_signal.py)
        from core.production_signal import compute_alpha_score as _cas
        result = _cas(
            symbol=sym,
            change_pct=chg,
            volume_ratio=info.get('volume_ratio', 0),
            ml_pred=ml_predictions.get(sym, {}),
            risk_position_ratio=risk_position_ratio,
            black_swan_active=black_swan_active,
        )
        score = result["total_score"]
        action = result["action_signal"]
        pred_signal = result["pred_signal"]
        factors = result["raw_factors"]

        alpha_scores.append({
            "symbol": sym,
            "name": info.get("name", sym),
            "total_score": score,
            "action_signal": action,
            "change_pct": round(chg, 2),
            "predicted_return": round(chg / 100, 4),
            "pred_signal": pred_signal,
            "_factors": factors,
        })
    if alpha_scores:
        print(f"  📊 个股评分(多因子): {len(alpha_scores)}只, ML覆盖{len(ml_predictions)}只")

    # 6. 事件信号
    event_signals = {
        "high_priority": [],
        "medium_priority": [],
        "low_priority": [],
    }
    # 资金流向异常
    north = capital_flow.get("north_flow", 0)
    if north and abs(north) > 50:
        direction = "大幅流入" if north > 0 else "大幅流出"
        event_signals["high_priority"].append({
            "title": f"北向资金{direction}",
            "content": f"今日北向资金净{'流入' if north > 0 else '流出'}{abs(north)}亿元",
            "severity": 4 if abs(north) > 100 else 3,
        })
    # 涨停潮
    if len(limit_up_list) >= 10:
        # 从麦蕊获取总数
        up_count = len(limit_up_list)
        event_signals["medium_priority"].append({
            "title": f"涨停{up_count}只",
            "content": f"今日涨停{up_count}只，跌停{len(limit_down_list)}只，龙虎榜{len(dragon_tiger_list)}只",
            "severity": 3 if up_count > 20 else 2,
        })

    # 7. 风险数据
    risk_data = {
        "refresh_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_date": datetime.now().strftime("%Y%m%d"),
        "black_swan_active": False,
        "black_swan_max_severity": 0,
        "position_ratio": 0.8,
        "overnight_risk": "normal",
        "asian_risk": "normal",
    }
    # 尝试加载最新黑天鹅数据
    try:
        from scripts.pre_market_refresh import fetch_black_swan_latest
        bs = fetch_black_swan_latest()
        if isinstance(bs, list) and len(bs) > 0:
            # fetch_black_swan_latest返回events list
            max_sev = max((e.get("severity", 0) for e in bs), default=0)
            # position_ratio 统一从 adaptive_params.yaml 读取 (单一真相源)
            # 不再使用硬编码 severity→ratio 映射
            risk_data["black_swan_active"] = max_sev >= 3
            risk_data["black_swan_max_severity"] = max_sev
            risk_data["position_ratio"] = 0.8  # 默认值，后续被 adaptive_params 覆盖
            risk_data["overnight_risk"] = "high" if max_sev >= 5 else ("elevated" if max_sev >= 4 else "normal")
            if max_sev >= 3:
                print(f"  🦢 黑天鹅风险: severity={max_sev}, position_ratio={risk_data['position_ratio']}")
        elif isinstance(bs, dict) and bs:
            # 兼容旧格式: dict with active/position_ratio
            risk_data["black_swan_active"] = bs.get("active", False)
            risk_data["black_swan_max_severity"] = bs.get("max_severity", 0)
            risk_data["position_ratio"] = bs.get("position_ratio", 0.8)
            risk_data["overnight_risk"] = bs.get("overnight_risk", "normal")
            if bs.get("active"):
                print(f"  🦢 黑天鹅风险: severity={bs.get('max_severity',0)}, position_ratio={risk_data['position_ratio']}")
    except Exception as e:
        print(f"  ⚠️ 黑天鹅数据跳过: {e}")

    # 也检查 adaptive_params.yaml
    # v4.6.9i(审计F1-1): bs基线改读black_swan_status.json(每日新鲜), adaptive_params仅fallback
    try:
        bs_ratio, _ = _load_black_swan_baseline()
        risk_data['position_ratio'] = min(risk_data['position_ratio'], float(bs_ratio))
    except Exception:
        pass

    # v4.0: 注入LPPL泡沫检测数据
    try:
        lppl_file = os.path.join(PROJECT_ROOT, 'cache', 'reports',
                                 f'lppl_daily_{datetime.now().strftime("%Y%m%d")}.json')
        if os.path.exists(lppl_file):
            with open(lppl_file, 'r', encoding='utf-8') as f:
                lppl_report = json.load(f)
            from black_swan_optimized.lppl_to_dsl import compute_lppl_position_ratio
            lp = compute_lppl_position_ratio(lppl_report)
            # v4.6.9i(审计F1-1): LPPL比率统一取black_swan_status.lppl(单一真相源, 与Dashboard/熔断器同源),
            # 原lppl_daily.suggested_position_ratio口径不一致(0.55 vs status 0.31)造成双口径分裂(审计P0-1)
            _bs_st_path = os.path.join(PROJECT_ROOT, 'data', 'black_swan_status.json')
            if os.path.exists(_bs_st_path):
                try:
                    with open(_bs_st_path, 'r', encoding='utf-8') as _sf:
                        _st_lppl = (json.load(_sf).get('lppl') or {})
                    if _st_lppl.get('position_ratio') is not None:
                        lp['lppl_position_ratio'] = float(_st_lppl['position_ratio'])
                        lp['lppl_risk_score'] = float(_st_lppl.get('risk_score', lp['lppl_risk_score']))
                        lp['urgency'] = _st_lppl.get('urgency', lp['urgency'])
                except Exception:
                    pass
            risk_data['lppl'] = {
                'timestamp': datetime.now().isoformat(),
                'risk_score': lp['lppl_risk_score'],
                'position_ratio': lp['lppl_position_ratio'],
                'urgency': lp['urgency'],
                'min_days_to_critical': lp.get('min_days_to_critical'),
                'targets': lp['cn_target_details'],
                'sector_signals': lppl_report.get('opportunities', [])[:5],
            }
            # v4.6: LPPL加权融合 — 黑天鹅基线60% + LPPL修正40%
            bs_ratio = risk_data.get('position_ratio', 0.8)
            lppl_ratio = lp['lppl_position_ratio']
            # 取min(融合值, 黑天鹅基线) → 确保不因LPPL放低保守性, LPPL加权最低0.25
            fused = max(0.25, bs_ratio * 0.6 + lppl_ratio * 0.4)
            risk_data['position_ratio'] = min(bs_ratio, fused)
            risk_data['lppl_weighted'] = True
            print(f"  🫧 LPPL: risk={lp['lppl_risk_score']:.0f}, lppl_ratio={lppl_ratio:.2f}, bs_ratio={bs_ratio:.2f} → fused={risk_data['position_ratio']:.2f}")
    except Exception as e:
        pass  # LPPL数据加载失败不阻断主流程

    # v4.6.x: 市场体制调整 (第三层)
    try:
        from core.market_regime_detector import load_regime, run_detection
        regime = load_regime()
        if regime is None:
            regime = run_detection()
        adjust = regime.get("position_ratio_adjustment", 1.0)
        before_regime = risk_data['position_ratio']
        risk_data['position_ratio'] = round(before_regime * adjust, 4)
        risk_data['market_regime'] = {
            'regime': regime.get('regime_summary', 'UNKNOWN'),
            'consensus': regime.get('consensus', 0),
            'volatility': regime.get('overall_volatility', 'unknown'),
            'adjustment': adjust,
        }
        print(f"  🌡️ 市场体制: {regime.get('regime_summary', '?')} | 波动={regime.get('overall_volatility', '?')} | 乘数={adjust:.2f} → {risk_data['position_ratio']:.4f}")
    except Exception as e:
        print(f"  ⚠️ 市场体制加载失败: {e}")

    return sector_scores, alpha_scores, event_signals, risk_data


def _pair_group_single_buy(candidates: list, held_symbols: set) -> list:
    """v4.7.0 P2-3: 同组单买裁决 — 政策pair_controls组内当日最多1个新买单
    组内按(调用方已排序的tier+score)取第一名; 已持仓加仓不受限
    """
    try:
        import yaml as _yaml
        _pp = os.path.join(PROJECT_ROOT, "config", "pool_structure_policy.yaml")
        with open(_pp, encoding="utf-8") as _f:
            _policy = _yaml.safe_load(_f) or {}
        groups = {}
        for pc in _policy.get("pair_controls", []) or []:
            syms = set()
            for s in (pc.get("symbols") or []):
                syms.add(str(s).zfill(6))
            for s in (pc.get("active_symbols") or []):
                syms.add(str(s).zfill(6))
            for s in (pc.get("observation_symbols") or []):
                syms.add(str(s).zfill(6))
            if len(syms) >= 2:
                groups[pc.get("name", "g")] = syms
        if not groups:
            return candidates
        kept, filtered = [], []
        used = set()
        for c in candidates:
            sym = str(c.get("symbol", "")).zfill(6)
            if sym in held_symbols:  # 已持仓加仓不受限
                kept.append(c)
                continue
            gname = None
            for gn, syms in groups.items():
                if sym in syms:
                    gname = gn
                    break
            if gname is None:
                kept.append(c)
            elif gname not in used:
                used.add(gname)
                kept.append(c)
            else:
                filtered.append((sym, gname))
        if filtered:
            print(f"  🔗 同组单买裁决: 过滤{len(filtered)}只 → {filtered}")
        return kept
    except Exception as _e:
        print(f"  ⚠️ 同组单买裁决失败, 跳过: {_e}")
        return candidates


def plan_trades(alpha_scores, daily_predictions, final_position, trader=None,
                existing_trades: list = None):
    """v4.6.x 从评分和ML预测生成计划交易清单

    v4.5.7: 新增 existing_trades 参数。
    morning mode 下加载晚间预案作为基础，融合新鲜数据做增量调整。

    v4.6.x: 删除 stock_accuracy_map 参数。改用 batch_predict 的 sigmoid 连续
    signal_weight 字段做熔断判定，替换旧的硬精度阈值(0.50)。
    """
    if trader is None:
        try:
            from paper_trader import PaperTrader
            trader = PaperTrader()
        except Exception as e:
            print(f"⚠️ PaperTrader不可用: {e}")
            return []

    # v4.6.x: 用 batch_predict 的 sigmoid 连续 signal_weight 替代硬精度熔断
    # 不再单独读取 stock_accuracy_map 做熔断——signal_weight 已包含精度×收益联合信息
    _sw_block = 0.12    # signal_weight < 0.12 → 熔断 (v4.6.5: 0.20→0.12, 适配整体精度54%现实)
    _sw_downgrade = 0.30  # signal_weight < 0.30 → 降级Tier-2 (v4.6.5: 0.40→0.30)

    def _check_signal_weight(sym: str) -> tuple:
        """v4.6.x: 用连续signal_weight替代硬精度门槛
        signal_weight 来自 batch_predict 的 sigmoid 加权（精度×收益幅度）

        Returns:
            (blocked: bool, downgrade_to_tier2: bool, reason: str)
        """
        pred = daily_predictions.get(sym, {})
        sw = pred.get('signal_weight', None)
        if sw is None:
            return False, False, "无signal_weight(不拦截)"

        if sw < _sw_block:
            return True, False, f"signal_weight={sw:.3f}<{_sw_block}(熔断)"
        if sw < _sw_downgrade:
            return False, True, f"signal_weight={sw:.3f}<{_sw_downgrade}(降级T2)"
        return False, False, f"signal_weight={sw:.3f}≥{_sw_downgrade}"
    
    trades = []
    today_str = datetime.now().strftime("%Y%m%d")
    
    # v4.5.8: plan_trades 交叉验证机制
    # 候选池分三个优先级:
    #   Tier-1 (双确认): alpha_scores增持 AND ML高信度buy → 最优, 满仓分配
    #   Tier-2 (单确认): 仅alpha_scores增持 OR 仅ML高信度buy → 降级, 半仓分配
    #   Tier-3 (矛盾):   动量涨+ML说sell / 动量跌+ML说buy → 过滤, 不买入
    
    alpha_map = {s["symbol"]: s for s in alpha_scores} if alpha_scores else {}
    
    # P0: 精度熔断统计
    _sw_blocked = 0
    _sw_downgraded = 0

    # Step 1: 构建买入候选(带交叉验证分级)
    buy_candidates = []

    # 1a. 遍历alpha_scores的增持信号 (v4.6.5: 也纳入中性但pred_signal=buy的ML候选)
    for s in alpha_scores:
        sig = s.get("action_signal", "")
        # P1: 中性但pred_signal=buy且ML高信度 → 准入(填补晚间评分<5的缺口)
        _pred_sig = s.get('pred_signal', 'hold')
        _ml_pred = daily_predictions.get(s['symbol'], {})
        _ml_conf_check = _ml_pred.get('confidence', 0) if _ml_pred else 0
        if sig not in ("增持", "关注"):
            if sig == "中性" and _pred_sig == "buy" and _ml_conf_check >= 0.58:
                # P1: 晚间评分<5但ML说buy → 作为关注级处理
                sig = "关注"  # override for this iteration
            else:
                continue
        sym = s["symbol"]
        ml_pred = daily_predictions.get(sym, {})

        # ── P0: 精度熔断检查 ──
        _blocked, _downgrade_t2, _reason = _check_signal_weight(sym)
        if _blocked:
            _sw_blocked += 1
            print(f"  🔴 {sym} {s.get('name','')}: {_reason} → 信号熔断")
            continue
        if _downgrade_t2:
            _sw_downgraded += 1
            # 如果原为Tier-1, 强制降为Tier-2
            _force_downgrade = True
            print(f"  🟡 {sym} {s.get('name','')}: {_reason} → 降级Tier-2")
        else:
            _force_downgrade = False
        ml_signal = ml_pred.get("signal", "hold") if ml_pred else "hold"
        ml_conf = ml_pred.get("confidence", 0) if ml_pred else 0
        factors = s.get("_factors", {})
        
        if ml_signal == "buy" and ml_conf >= 0.58:
            # v4.6.9f P1-1: 2日确认 — 连续2交易日同向信号才保持Tier-1
            # 首日信号(未确认)降Tier-2, 减少单日噪声触发
            _confirmed2 = bool(ml_pred.get("confirmed_2d", False))
            if _confirmed2:
                tier = 1
                adjusted_score = s.get("total_score", 5) + 1.0  # 双确认+2日确认加分
                source = f"双确认+2日确认(评分{s.get('total_score',5):.1f}+ML置信{ml_conf:.0%})"
            else:
                tier = 2
                adjusted_score = s.get("total_score", 5) * 0.9  # 未确认降Tier-2
                source = f"双确认但首日(评分{s.get('total_score',5):.1f}+ML置信{ml_conf:.0%})待2日确认"
            # P0: 精度降级 — 双确认但精度不足→降Tier-2
            if _force_downgrade:
                tier = 2
                adjusted_score = s.get("total_score", 5) * 0.8
                source += " [精度降级T2]"
        elif ml_signal == "sell" and ml_conf >= 0.58:
            # Tier-3: 方向矛盾 — 动量涨但ML说卖 → 过滤
            print(f"  🚫 {sym} {s.get('name','')}: 动量{sig}但ML说sell(conf={ml_conf:.0%})→过滤")
            continue
        elif ml_signal == "hold" or not ml_pred:
            # Tier-2: ML中性或无数据 → 仅动量驱动, 降级
            # v4.5.19b: 置信度<40%直接排除, 模型基本是随机猜测
            if ml_conf < 0.40:
                print(f"  🚫 {sym} {s.get('name','')}: 置信度{ml_conf:.0%}<40%, 排除买入候选")
                continue
            tier = 2
            adjusted_score = s.get("total_score", 5) * 0.7  # 单确认降分
            source = f"动量单确认(评分{s.get('total_score',5):.1f},ML={ml_signal})"
        else:
            # ML buy但置信度不够
            # v4.5.19b: 置信度<40%直接排除
            if ml_conf < 0.40:
                print(f"  🚫 {sym} {s.get('name','')}: 置信度{ml_conf:.0%}<40%, 排除买入候选")
                continue
            tier = 2
            adjusted_score = s.get("total_score", 5) * 0.8
            source = f"动量确认(评分{s.get('total_score',5):.1f},ML置信不足{ml_conf:.0%})"
        
        buy_candidates.append({
            "symbol": sym,
            "name": s.get("name", sym),
            "score": adjusted_score,
            "predicted_return": s.get("predicted_return", 0),
            "source": source,
            "tier": tier,
            "ml_confidence": round(ml_conf, 4),  # P1: Kelly动态仓位
            "total_score": s.get("total_score", 5.0),  # P1: Kelly alpha
        })
    
    # 1b. 补充ML高信度buy但alpha_scores中未标记增持的标的
    # v4.5.8.2: 修复BUG — 旧版`if sym in alpha_map: continue`跳过了alpha中性的ML buy标的
    # 如300418(alpha中性5.x) + ML buy(63%) → 被错误跳过
    already_added = {b["symbol"] for b in buy_candidates}  # 已在1a中添加的
    for sym, pred in daily_predictions.items():
        sig_weight = pred.get("signal_weight", 1.0)
        if pred.get("signal") == "buy" and pred.get("confidence", 0) >= 0.58:
            if sym in already_added:
                continue  # 已在1a中添加(增持+ML双确认), 跳过

            # ── P0: 精度熔断检查 ──
            _b2, _d2, _r2 = _check_signal_weight(sym)
            if _b2:
                _sw_blocked += 1
                print(f"  🔴 {sym} {pred.get('name','')}: ML信号{_r2} → 熔断")
                continue
            if _d2:
                _sw_downgraded += 1
                print(f"  🟡 {sym} {pred.get('name','')}: ML信号{_r2} → 降级Tier-2")

            # ML高信度buy但alpha未标记增持 → ML单确认(Tier-2)
            alpha_s = alpha_map.get(sym, {})
            alpha_action = alpha_s.get("action_signal", "")
            alpha_score_val = alpha_s.get("total_score", 5.0)
            adjusted_score = pred.get("score", 5) * sig_weight + 0.5  # ML高信度加分
            if sig_weight < 0.5:
                print(f"  ⚠️ {sym} {pred.get('name','')}: pool_fallback低精度, signal_weight={sig_weight}")
            source = f"ML单确认(置信{pred.get('confidence',0):.0%}"
            if alpha_action:
                source += f",alpha={alpha_action}({alpha_score_val:.1f})"
            source += ")"
            buy_candidates.append({
                "symbol": sym,
                "name": pred.get("name", sym),
                "score": adjusted_score,
                "predicted_return": pred.get("predicted_return", 0),
                "source": source,
                "tier": 2,
                "ml_confidence": round(pred.get("confidence", 0.5), 4),  # P1: Kelly动态仓位
                "total_score": alpha_score_val,  # P1: Kelly alpha
            })
    
    # Step 2: 交叉验证总结
    tier1_count = sum(1 for b in buy_candidates if b.get("tier") == 1)
    tier2_count = sum(1 for b in buy_candidates if b.get("tier") == 2)
    filtered_count = len(alpha_scores) - len(buy_candidates)  # 被矛盾过滤的
    if buy_candidates:
        print(f"  📊 买入候选: Tier-1双确认{tier1_count}只, Tier-2单确认{tier2_count}只")
    else:
        print("ℹ️ 无符合条件的买入计划")
    # P0: 精度熔断统计汇总
    if _sw_blocked > 0 or _sw_downgraded > 0:
        print(f"  🔴 P0精度熔断: {_sw_blocked}只阻断 + {_sw_downgraded}只降级T2")
    
    # 获取组合状态(在提取卖出信号前)
    summary = trader.get_portfolio_summary()
    
    # 3. 提取卖出信号（三级来源）
    # v4.5.8.1: 修复#1 — 持仓建议说减持但无SELL计划
    # 信号源: A) ML高信度sell → 强制卖出  B) alpha_scores减持+无ML buy → 建议卖出
    #         C) v4.5.12: 黑天鹅持仓超限 → 强制减仓至ratio上限
    sell_candidates = []
    current_positions = {p["stock_code"]: p for p in summary.get("positions", [])}
    
    # v4.5.12 P0-2: 黑天鹅/风险持仓超限强制减仓
    # v4.5.22: 使用 final_position(已融合LPPL+adaptive_params) 而非
    #          直接从YAML读 black_swan_position_ratio，避免LPPL硬上限被绕过
    # 当current_ratio > final_position时，按浮盈从小到大排序减仓
    try:
        _bs_ratio = final_position  # v4.5.22: 已包含LPPL+adaptive_params双层硬上限
        _bs_active = (_bs_ratio < 1.0)  # 任何风险压制即视为active
        _total_val = summary.get('total_value', 1)
        _pos_val = summary.get('positions_value', 0)
        _current_ratio = _pos_val / _total_val if _total_val > 0 else 0
        
        if _bs_active and _current_ratio > _bs_ratio and _bs_ratio < 1.0:
            _excess = _current_ratio - _bs_ratio
            _excess_value = _total_val * _excess
            print(f"  🦢⚠️ 黑天鹅持仓超限! 当前{_current_ratio*100:.1f}% >> 上限{_bs_ratio*100:.0f}%")
            print(f"     需减仓约¥{_excess_value:,.0f}(占比{_excess*100:.1f}%)")
            
            # 按浮盈从高到低排序(优先卖出盈利最少的/亏损的)
            _pos_with_pnl = []
            for sym, pos in current_positions.items():
                avg_cost = pos.get('avg_cost', 0)
                cur_price = pos.get('current_price', 0)
                if avg_cost > 0 and cur_price > 0:
                    pnl_pct = (cur_price - avg_cost) / avg_cost
                    _pos_with_pnl.append((sym, pos, pnl_pct))
            _pos_with_pnl.sort(key=lambda x: x[2])  # 亏损排前
            
            _reduced_value = 0
            for sym, pos, pnl_pct in _pos_with_pnl:
                if _reduced_value >= _excess_value:
                    break
                if sym in {s['symbol'] for s in sell_candidates}:
                    continue  # 已在卖出候选中
                cur_price = pos.get('current_price', 0)
                qty = pos.get('quantity', 0)
                if cur_price <= 0 or qty <= 0:
                    continue
                # 计算需要卖出多少股
                _need_value = _excess_value - _reduced_value
                _sell_qty = int(_need_value / cur_price / 100) * 100
                _sell_qty = max(100, min(_sell_qty, qty))  # 至少1手
                if _sell_qty < 100:
                    continue
                sell_candidates.append({
                    "symbol": sym,
                    "name": pos.get('name', sym),
                    "quantity": _sell_qty,
                    "current_price": cur_price,
                    "predicted_return": 0,
                    "source": f"黑天鹅减仓(浮盈{pnl_pct*100:+.1f}%, 目标≤{_bs_ratio*100:.0f}%)",
                })
                _reduced_value += _sell_qty * cur_price
                print(f"     🦢 计划减仓 {sym} {_sell_qty}股 @{cur_price:.2f} (浮盈{pnl_pct*100:+.1f}%)")
            
            if _reduced_value < _excess_value * 0.8:
                print(f"  🦢⚠️ 减仓不足! 已减¥{_reduced_value:,.0f} < 需减¥{_excess_value:,.0f}")
    except Exception as e:
        print(f"  ⚠️ 黑天鹅减仓检测异常: {e}")
        _bs_active = False
        _bs_ratio = 1.0  # 异常时不触发超限保护
    
    # Build alpha_scores lookup for sell signals
    alpha_sell_map = {}
    for s in alpha_scores:
        if s.get("action_signal") in ("减持", "回避"):
            alpha_sell_map[s["symbol"]] = s
    
    for sym, pos in current_positions.items():
        ml_pred = daily_predictions.get(sym, {})
        ml_signal = ml_pred.get("signal", "hold") if ml_pred else "hold"
        ml_conf = ml_pred.get("confidence", 0) if ml_pred else 0
        alpha_s = alpha_sell_map.get(sym, {})
        alpha_action = alpha_s.get("action_signal", "")
        alpha_score = alpha_s.get("total_score", 5.0)
        
        sell_reason = None
        
        # A) ML高信度sell → 强制卖出
        if ml_signal == "sell" and ml_conf >= 0.58:
            sell_reason = f"ML强制卖出(置信{ml_conf:.0%})"
            if alpha_action:  # ML与alpha_scores同向卖出, 更强信号
                sell_reason += f"+{alpha_action}({alpha_score:.1f}分)"
        elif ml_conf < 0.58 and alpha_action:
            # B) 无ML強力确认, 但alpha_scores说减持/回避 → 建议卖出
            # 但如果ML说buy则过滤(方向矛盾, 让ML信号优先)
            if ml_signal == "buy" and ml_conf >= 0.50:
                print(f"  ⚠️ {sym}: alpha说{alpha_action}({alpha_score:.1f}分)但ML倾向buy(conf={ml_conf:.0%}), 跳过卖出")
                continue
            sell_reason = f"评分{alpha_action}({alpha_score:.1f}分, ML={ml_signal})"
        elif not ml_pred and alpha_action:
            sell_reason = f"评分{alpha_action}({alpha_score:.1f}分, 无ML)"
        
        if sell_reason:
            sell_candidates.append({
                "symbol": sym,
                "name": pos.get("name", sym),
                "quantity": pos["quantity"],
                "current_price": pos["current_price"],
                "predicted_return": ml_pred.get("predicted_return", 0) if ml_pred else 0,
                "source": sell_reason,
            })

    # v4.6.9h P0: degraded 持仓质量退出 — 精度<50%且亏损>5% → 减50%分批
    # 决策(James 2026-08-01): 只退 degraded 且亏损>5% 的; 盈利的 degraded 保留观察; 减50%分批
    # 从 master_stock_pool.yaml 读取 degraded 标记 + 从 calibration 读最新精度
    try:
        import yaml as _yaml
        _pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        with open(_pool_path, "r", encoding="utf-8") as _f:
            _pool_cfg = _yaml.safe_load(_f) or {}
        _deg_codes = {s["symbol"] for s in _pool_cfg.get("master_pool", []) if s.get("degraded")}
        # 从 calibration 补充精度(与 pool 的 accuracy 字段对齐)
        _calib_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        _calib_acc = {}
        if os.path.exists(_calib_path):
            with open(_calib_path, "r", encoding="utf-8") as _f:
                _calib = json.load(_f)
            for _sym, _info in (_calib.get("stock_accuracy", {}) or {}).items():
                if isinstance(_info, dict) and _info.get("last_accuracy"):
                    _calib_acc[_sym] = float(_info["last_accuracy"])
    except Exception:
        _deg_codes, _calib_acc = set(), {}

    if _deg_codes:
        for sym, pos in current_positions.items():
            if sym not in _deg_codes:
                continue
            # 已在卖出候选(避免重复)
            if sym in {s["symbol"] for s in sell_candidates}:
                continue
            _avg = pos.get("avg_cost", 0)
            _cur = pos.get("current_price", 0)
            _qty = pos.get("quantity", 0)
            if _avg <= 0 or _cur <= 0 or _qty <= 0:
                continue
            _pnl_pct = (_cur - _avg) / _avg
            _acc = _calib_acc.get(sym, 0)
            # 规则: degraded(精度<50%) AND 亏损>5% → 减50%分批
            if _pnl_pct < -0.05:
                _sell_qty = int(_qty * 0.5 / 100) * 100
                _sell_qty = max(100, min(_sell_qty, _qty))
                if _sell_qty >= 100:
                    sell_candidates.append({
                        "symbol": sym,
                        "name": pos.get("name", sym),
                        "quantity": _sell_qty,
                        "current_price": _cur,
                        "predicted_return": 0,
                        "source": f"degraded质量退出(精度{_acc:.0%}, 亏损{_pnl_pct*100:+.1f}%, 减50%分批)",
                    })
                    print(f"  🚨 {sym}: degraded({_acc:.0%})亏损{_pnl_pct*100:+.1f}% → 减仓{_sell_qty}股(50%)")
                else:
                    print(f"  ⏭️ {sym}: degraded但持仓过小({_qty}股), 不拆分")
            elif _pnl_pct >= 0:
                print(f"  ℹ️ {sym}: degraded({_acc:.0%})但盈利{_pnl_pct*100:+.1f}%, 保留观察")
            else:
                print(f"  ℹ️ {sym}: degraded({_acc:.0%})亏损{_pnl_pct*100:+.1f}%<5%, 未达阈值")
    
    # v4.5.3b: 盘中信号撤销过滤 (v4.6.5: 自动过期当日之前的撤销信号)
    override_file = os.path.join(PROJECT_ROOT, 'cache', 'signal_override.json')
    if os.path.exists(override_file):
        try:
            with open(override_file, 'r') as f:
                overrides = json.load(f)
        except Exception:
            overrides = []
        # P1-FIX: 只保留今日的撤销信号, 跨日残留的自动清除
        today_yyyymmdd = datetime.now().strftime('%Y%m%d')
        today_overrides = [o for o in overrides
                          if str(o.get('date', '') or o.get('timestamp', '')[:8]) == today_yyyymmdd]
        if len(today_overrides) < len(overrides):
            stale = len(overrides) - len(today_overrides)
            print(f"🧹 清除{stale}条跨日残留撤销信号, 保留{today_overrides and len(today_overrides) or 0}条今日信号")
            # 写回清理后的文件
            try:
                with open(override_file, 'w') as f:
                    json.dump(today_overrides, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        cancelled_symbols = {o['symbol'] for o in today_overrides if o.get('action') == 'cancel'}
        if cancelled_symbols:
            buy_candidates = [b for b in buy_candidates if b['symbol'] not in cancelled_symbols]
            sell_candidates = [s for s in sell_candidates if s['symbol'] not in cancelled_symbols]
            print(f"🚫 过滤今日盘中撤销信号: {len(cancelled_symbols)}只")
        else:
            print(f"  ℹ️ 今日无盘中撤销信号")
    
    # v4.5.7: morning mode + 存在晚间计划 → 合并而非完全重算
    if existing_trades and len(existing_trades) >= len(buy_candidates) * 0.5:
        # 用晚间计划的买卖列表作为基础
        evening_buy = [t for t in existing_trades if t.get('action') == 'BUY' or t.get('type') == 'buy']
        evening_sell = [t for t in existing_trades if t.get('action') == 'SELL' or t.get('type') == 'sell']
        if evening_buy or evening_sell:
            print(f"  🔄 融合晚间预案: {len(evening_buy)}笔买入 + {len(evening_sell)}笔卖出")
            # 保留晚间买入标的，更新其评分和来源
            evening_symbols = {t.get('symbol', t.get('code', '')) for t in evening_buy}
            fresh_only = [b for b in buy_candidates if b.get('symbol', '') not in evening_symbols]
            merged_buy = []
            for e in evening_buy:
                sym = e.get('symbol', e.get('code', ''))
                fresh = next((b for b in buy_candidates if b.get('symbol', '') == sym), None)
                if fresh:
                    merged_buy.append({
                        **e,
                        'score': fresh['score'],
                        'predicted_return': fresh['predicted_return'],
                        'source': f"晚间预案+融合(评分{fresh['score']:.1f})",
                    })
                else:
                    # 晚间预案条目可能无score/symbol/source(用code), 补齐
                    if 'score' not in e:
                        e['score'] = 5.0
                    if 'symbol' not in e and 'code' in e:
                        e['symbol'] = e['code']
                    if 'source' not in e:
                        e['source'] = '晚间预案'
                    merged_buy.append(e)  # 保留用户手动添加的
            buy_candidates = merged_buy + fresh_only
            print(f"    融合后: {len(buy_candidates)}只买入候选")
    
    # 按评分排序取前5，但受持仓上限约束
    # v4.5.8.1: 修复#2 — 持仓无上限 (MAX_POSITIONS=8)
    # v4.5.9b: 修复#3 — Tier-1(双确认)永远优先于Tier-2(单确认)；已持仓加仓不占新买名额
    MAX_POSITIONS = 8  # 最大持仓数
    current_count = len(current_positions)
    sell_count = len(sell_candidates)
    max_new_buys = MAX_POSITIONS - (current_count - sell_count)
    
    if buy_candidates:
        # v4.5.9b: 按Tier升序+评分降序排列，确保双确认优先
        buy_candidates.sort(key=lambda x: (x.get("tier", 9), -x.get("score", 5.0)))
        # 分离新开仓和已持仓加仓
        held_symbols = set(current_positions.keys())
        new_open = [b for b in buy_candidates if b.get('symbol', '') not in held_symbols]
        existing_add = [b for b in buy_candidates if b.get('symbol', '') in held_symbols]
        # 新开仓受max_new_buys限制，已持仓加仓不受限
        top_buy = new_open[:max(0, max_new_buys)] + existing_add[:3]
        # 重新按Tier+评分排序
        top_buy.sort(key=lambda x: (x.get("tier", 9), -x.get("score", 5.0)))
        # v4.7.0 P2-3: 高相关组当日单买裁决 (组内最多1个新买单)
        top_buy = _pair_group_single_buy(top_buy, held_symbols)
        print(f"  📊 买入候选: 新开{len(new_open)}只→可{max(0,max_new_buys)}只, 加仓{len(existing_add)}只→不限")
    else:
        top_buy = []
    
    if max_new_buys < 0:
        print(f"  ⚠️ 持仓已达{current_count}只(上限{MAX_POSITIONS}), 需减持{sell_count}只后才能新开")
    elif max_new_buys < 5 and top_buy:
        print(f"  📊 持仓{current_count}只→卖出{sell_count}只→可新开{max_new_buys}只(上限{MAX_POSITIONS})")
    
    if not top_buy and not sell_candidates:
        return []
    
    # v4.5.22: 风险超限时禁止买入 — 当前仓位已超风险上限，只卖不买
    # 先通过sell_candidates减仓回上限以下，明天morning_decision再评估是否买入
    _current_ratio_buy = summary.get('positions_value', 0) / max(summary.get('total_value', 1), 1)
    if _bs_active and _current_ratio_buy > _bs_ratio:
        print(f"  🦢🚫 风险超限({_current_ratio_buy*100:.1f}%>{_bs_ratio*100:.0f}%) → 阻止所有买入，仅执行减仓")
        if top_buy:
            print(f"     已撤销 {len(top_buy)} 笔买入计划")
        top_buy = []
    
    total_value = summary.get("total_value", 1000000)
    # P1: 凯利启发式动态仓位
    def _kelly_pct(tier, ml_conf, alpha_score, vol_p):
        base = 0.15 if tier == 1 else 0.07
        conf_m = max(0.3, min(1.5, (ml_conf - 0.4) * 2.5))
        vol_m = min(1.5, 0.02 / max(vol_p, 0.005))
        alpha_m = max(0.5, min(1.5, (alpha_score - 3.0) / 7.0))
        return min(0.30, max(0.02, base * conf_m * vol_m * alpha_m))

    all_ret = [c.get('predicted_return', 0) for c in buy_candidates if c.get('predicted_return')]
    vol_p = float(np.std(all_ret)) if len(all_ret) >= 2 else 0.02

    per_stock_pct_t1 = final_position * 0.15
    per_stock_pct_t2 = final_position * 0.07
    
    print(f"\n📋 计划交易: SELL {len(sell_candidates)}只 + BUY {len(top_buy)}只 (9:20预计算)")
    
    # ── 第一步: 卖出信号处理 (先卖后买, 释放资金) ──
    for c in sell_candidates:
        code = c["symbol"]
        try:
            quantity = c["quantity"]  # 全仓卖出
            price = c.get("current_price", 0)
            if price <= 0:
                from dsl_data_sdk_original import get_kline
                kline = get_kline(code, (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
                                 datetime.now().strftime("%Y-%m-%d"))
                if kline and len(kline) >= 1:
                    price = float(kline[-1].get("close", 0))
            if price <= 0 or quantity < 100:
                continue
            trades.append({
                "code": code, "symbol": code, "name": c["name"], "action": "SELL",
                "price": price, "quantity": quantity, "reason": c["source"],
            })
            print(f"  📋 {code:6s} {c['name']:8s} 计划 SELL {quantity}股 @{price:.2f} [{c['source']}]")
        except Exception as e:
            print(f"  ❌ {code:6s} {c['name']:8s} SELL异常: {e}")
    
    # ── 第二步: 买入信号处理 ──
    for c in top_buy:
        code = c["symbol"]
        try:
            from dsl_data_sdk_original import get_kline
            kline = get_kline(code, (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d"),
                             datetime.now().strftime("%Y-%m-%d"))
            if not kline or len(kline) < 1:
                print(f"  ⚠️ {code} {c['name']}: 无法获取行情")
                continue
            price = float(kline[-1].get("close", 0))
            if price <= 0:
                continue
            
            # P1: 凯利动态仓位
            tier = c.get("tier", 2)
            ml_c = c.get("ml_confidence", 0.5)
            alpha_s = c.get("total_score", 5.0)
            dyn_pct = _kelly_pct(tier, ml_c, alpha_s, vol_p)
            pct = final_position * dyn_pct
            alloc_amount = total_value * pct
            quantity = int(alloc_amount / price / 100) * 100
            if quantity < 100:
                print(f"  ⚠️ {code} {c['name']}: 可买数量不足1手(Tier-{tier}, 预算¥{alloc_amount:.0f})")
                continue
            
            trades.append({
                "code": code, "symbol": code, "name": c["name"], "action": "BUY",
                "price": price, "quantity": quantity, "reason": c["source"],
                "tier": tier,
                "confidence": c.get("ml_confidence", 0.5),
                "predicted_return": c.get("predicted_return", 0),
            })
            tier_label = f"T{tier}"
            print(f"  📋 {code:6s} {c['name']:8s} 计划 BUY {quantity}股 @{price:.2f}(前收盘) [{tier_label}] {c['source']}")
        except Exception as e:
            print(f"  ❌ {code:6s} {c['name']:8s} 异常: {e}")
    
    return trades


def calculate_final_position(macro_score, sector_scores, risk_data):
    """三层信号融合计算最终仓位：宏观30% + 行业30% + 风险40%"""
    # 1. 宏观得分映射到仓位比例
    macro_position = min(1.0, macro_score / 10)
    
    # 2. 行业平均得分映射到仓位比例
    avg_sector_score = 7
    if sector_scores:
        avg_sector_score = sum([s['total_score'] for s in sector_scores[:10]]) / 10
    sector_position = min(1.0, avg_sector_score / 10)
    
    # 3. 风险预算仓位
    # v4.5.5 S6: pre_market_refresh写入position_ratio, 不是recommended_position_ratio
    # 同时fallback读取adaptive_params.yaml的black_swan_position_ratio
    risk_position = risk_data.get('position_ratio', None)
    if risk_position is None:
        risk_position = risk_data.get('recommended_position_ratio', None)
    if risk_position is None:
        # v4.6.9i(审计F1-1): fallback真相源 black_swan_status.json, adaptive_params仅次选
        risk_position, _ = _load_black_swan_baseline()
        print(f"  ✅ fallback: black_swan_baseline = {risk_position}")
    
    # 加权融合
    # 黑天鹅活跃时提高风险权重 (0.2:0.2:0.6)，防止宏观/行业评分稀释风险限制
    bs_active = risk_data.get('black_swan_active', False) or (risk_position < 1.0)
    if bs_active:
        final_position = macro_position * 0.2 + sector_position * 0.2 + risk_position * 0.6
    else:
        final_position = macro_position * 0.3 + sector_position * 0.3 + risk_position * 0.4
    
    # 回撤控制：从大到小检查（先严格后宽松）
    max_drawdown = get_current_drawdown()
    if max_drawdown >= 10:
        final_position = min(final_position, 0.3)
        print(f"🛑 最大回撤{max_drawdown:.1f}% ≥10%，强制降仓到{final_position*100:.0f}%")
    elif max_drawdown >= 5:
        final_position = min(final_position, 0.5)
        print(f"⚠️ 最大回撤{max_drawdown:.1f}% ≥5%，强制降仓到{final_position*100:.0f}%")
    elif max_drawdown >= 3:
        final_position = min(final_position, 0.7)
        print(f"🔶 最大回撤{max_drawdown:.1f}% ≥3%，建议降仓到{final_position*100:.0f}%")
    
    return round(final_position, 2)

def get_current_drawdown():
    """获取当前组合最大回撤（从paper_trader或adaptive_params读取）
    v4.5.3c fix: 确保scripts/在sys.path中, 修复黑天鹅涨仓误报
    """
    # v4.5.3c: 确保import路径兼容
    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
    try:
        from paper_trader import PaperTrader
        pt = PaperTrader()
        summary = pt.get_portfolio_summary()
        ret_pct = summary.get('total_return_pct', 0) if summary else 0
        if ret_pct < 0:
            return min(abs(ret_pct), 30.0)
        # 盈利状态 → 无需回撤保护
        return 0.0
    except Exception:
        pass
    # fallback: 黑天鹅风险推断（当paper_trader不可用时）
    # v4.5.5 S6: 统一使用adaptive_params.yaml的black_swan_position_ratio
    try:
        import yaml
        with open(os.path.join(PROJECT_ROOT, 'config', 'adaptive_params.yaml'), 'r') as f:
            params = yaml.safe_load(f)
        bs_active = params.get('risk', {}).get('black_swan_active', False)
        bs_ratio = float(params.get('risk', {}).get('black_swan_position_ratio', 1.0))
        if bs_active and bs_ratio < 0.6:
            print(f"  🛡️ 黑天鹅活跃: position_ratio={bs_ratio}, 触发回撤保护")
            return 8.0 if bs_ratio < 0.5 else 5.0
    except Exception:
        pass
    return 0.0

def generate_final_report(market, display_date, macro_score, macro_result, final_position, sector_scores, alpha_scores, event_signals, risk_data, daily_predictions=None, overnight=None, trades=None, mode='morning'):
    """生成决策报告（mode=evening时标题标注为晚间预案预览）"""
    market_name = "A股" if market == "a" else "港股"
    title_prefix = "📋晚间预案预览" if mode == 'evening' else "🚀盘前最终决策"
    
    if daily_predictions is None:
        daily_predictions = {}
    if overnight is None:
        overnight = {}
    if trades is None:
        trades = []
    
    # Top3推荐行业
    top_sectors = sector_scores[:3]
    # v4.5.8: 报告标签修正 — "Top3推荐"→"综合评分Top3"(非纯推荐)
    # top_stocks已按多因子评分排序, 但高分≠应买入, 需标注信号来源
    top_stocks = alpha_scores[:3] if alpha_scores else []
    # 交叉验证统计
    tier1_stocks = [s for s in alpha_scores if s.get('_factors',{}).get('ml_signal') == 'buy' and s.get('_factors',{}).get('ml_conf',0) >= 0.58 and s.get('total_score',0) >= 6.5] if alpha_scores else []
    
    # 高优先级事件（兼容新旧缓存格式）
    if 'black_swan' in event_signals and 'events' in event_signals.get('black_swan', {}):
        high_events = event_signals['black_swan']['events']
    else:
        high_events = event_signals.get('high_priority', [])
    
    content = f"""## {title_prefix} | {market_name} | {display_date}

> ⏰ 本报告为**晚间预案预览**，基于今日收盘数据生成次日交易计划。\n> 📝 如需调整：编辑 `cache/planned_trades.json`，明早09:20将读取调整后的版本。\n> ⚡ 09:20早盘决策将融合明日盘前新鲜数据后产出最终版本。\n

### 📊 核心决策
| 指标 | 数值 |
|------|------|
| 宏观环境评分 | {macro_score}/10 |
| 风险等级 | {macro_result.get('risk_level', 'neutral')} |
| 融合推荐仓位 | {final_position*100:.0f}% |
| 🦢 黑天鹅硬上限 | {risk_data.get('position_ratio', 1.0)*100:.0f}% |
| 最大回撤阈值 | {risk_data.get('max_drawdown_threshold', 5)}% |
| VaR(95%) | {risk_data.get('var_95', 2.3)}% |
| ML高信度信号 | {sum(1 for p in daily_predictions.values() if p.get('signal','hold') != 'hold' and p.get('confidence',0) >= 0.58)}条(共{len(daily_predictions)}只) |
| 今日计划交易 | {len(trades)}笔 |
"""
    
    # v4.0: LPPL泡沫检测信号注入
    lppl = risk_data.get('lppl', {})
    if lppl and lppl.get('risk_score', 0) > 0:
        urgency = lppl.get('urgency', 'N/A')
        lp_emoji = '🔴' if urgency == 'CRITICAL' else ('🟠' if urgency == 'HIGH' else '🟡')
        content += f"""
### 🫧 LPPL泡沫检测 (v4.0)
| 指标 | 数值 |
|------|------|
| 综合风险分 | {lppl.get('risk_score', 0):.0f}/100 {lp_emoji} |
| 泡沫紧急度 | {urgency} |
| LPPL建议仓位 | {lppl.get('position_ratio', 1.0)*100:.0f}% |
"""
        targets = lppl.get('targets', {})
        for tname, td in targets.items():
            if td.get('strength', 0) > 0.5:
                tc = td.get('critical_date') or 'N/A'
                days = td.get('days_to_critical', '-')
                content += f"| {tname} | 强度{td['strength']:.0%} 崩溃{td['crash_prob']:.0%} tc={tc}(+{days}d) |\n"
        content += "\n"
        # 行业信号
        sector_sigs = lppl.get('sector_signals', [])
        if sector_sigs:
            buy_sigs = [s for s in sector_sigs if s['action'] == 'BUY']
            sell_sigs = [s for s in sector_sigs if s['action'] == 'SELL']
            if buy_sigs:
                content += "**🟢 LPPL看好**: " + ", ".join(f"{s['sector']}({s['confidence']:.0%})" for s in buy_sigs[:3]) + "\n"
            if sell_sigs:
                content += "**🔴 LPPL回避**: " + ", ".join(f"{s['sector']}({s['confidence']:.0%})" for s in sell_sigs[:3]) + "\n"
    
    content += """
### 🌍 隔夜外盘数据
"""
    if overnight.get("fetched"):
        intraday_note = "\n> ⏰ 美股尚未收盘，数据为盘中实时/前日收盘，涨跌幅待收盘后更新\n" if overnight.get("is_intraday") else ""
        content += f"| 指标 | 数值 | 变动 |\n|------|------|------|\n"
        if overnight.get("sp500"):
            change_str = f"{overnight.get('sp500_change',0):+.2f}%" if overnight.get('sp500_change') is not None else "待收盘"
            content += f"| S&P 500 | {overnight['sp500']:.0f} | {change_str} |\n"
        if overnight.get("nasdaq"):
            change_str = f"{overnight.get('nasdaq_change',0):+.2f}%" if overnight.get('nasdaq_change') is not None else "待收盘"
            content += f"| 纳斯达克 | {overnight['nasdaq']:.0f} | {change_str} |\n"
        if overnight.get("vix"):
            content += f"| VIX恐慌指数 | {overnight['vix']:.1f} | {'⚠️ 高波动' if overnight['vix'] >= 25 else '✅ 正常'} |\n"
        if overnight.get("usdcny"):
            content += f"| USD/CNY | {overnight['usdcny']:.4f} | {'⚠️ 人民币偏弱' if overnight['usdcny'] >= 6.9 else '✅ 正常'} |\n"
        content += intraday_note
    else:
        content += "⚠️ 隔夜外盘数据获取失败\n"
    
    # v4.5.8: 报告标签修正 — 明确"综合评分"含义, 区分双确认/单确认
    content += "\n### 🏆 综合评分Top3行业\n"
    for i, sector in enumerate(top_sectors, 1):
        content += f"{i}. **{sector['name']}**：{sector['total_score']}分，{sector['recommend_level']}\n"
    
    # 交叉验证统计
    xval_count = len(tier1_stocks)
    xval_note = f"（含{xval_count}只双确认）" if xval_count > 0 else "（无双确认标的）"
    
    content += f"""
### 🚀 综合评分Top3个股{xval_note}
"""
    for i, stock in enumerate(top_stocks[:3], 1):
        factors = stock.get('_factors', {})
        ml_sig = factors.get('ml_signal', 'none')
        ml_c = factors.get('ml_conf', 0)
        ml_tag = f"[ML:{ml_sig} {ml_c:.0%}]" if ml_sig != 'none' else "[无ML]"
        content += f"{i}. **{stock['name']}({stock['symbol']})**：{stock['total_score']}分，{stock['action_signal']} {ml_tag}\n"
    
    # 持仓个股建议 (v4.5.5 S6: 仅显示实际持仓)
    # v4.5.8.2: 修复#2 — 盈亏从PaperTrader API获取(ledger.json中cost/value全0)
    #            修复#3 — 名称从stock_name_map查找
    # Build name lookup (same as in alpha_scores section)
    report_name_map = {}
    for s in alpha_scores:
        name = s.get('name', '')
        if name and name != s.get('symbol', ''):
            report_name_map[s['symbol']] = name
    for sym, pred in daily_predictions.items():
        name = pred.get('name', '')
        if name and name != sym:
            report_name_map[sym] = name
    try:
        stock_pool_path2 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'master_stock_pool.yaml')
        if os.path.exists(stock_pool_path2):
            import yaml as yml3
            with open(stock_pool_path2, 'r', encoding='utf-8') as f:
                pool_cfg2 = yml3.safe_load(f)
            for cat in ['bluechip', 'core', 'growth', 'cyclical', 'flex']:
                for stk in pool_cfg2.get(cat, []):
                    if isinstance(stk, dict):
                        c2 = stk.get('code', stk.get('symbol', ''))
                        n2 = stk.get('name', '')
                        if c2 and n2 and c2 not in report_name_map:
                            report_name_map[c2] = n2
    except Exception:
        pass

    try:
        # 优先从PaperTrader API获取持仓(有实时盈亏)
        api_positions = []
        try:
            import requests as req
            r = req.get('http://localhost:8888/api/portfolio', timeout=5)
            if r.status_code == 200:
                api_positions = r.json()
                if isinstance(api_positions, dict) and 'positions' in api_positions:
                    api_positions = api_positions['positions']
        except Exception:
            pass
        
        # Fallback: 从ledger读取
        actual_positions = {}
        if not api_positions:
            led_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_trading_ledger.json")
            if os.path.exists(led_path):
                with open(led_path, "r", encoding="utf-8") as f:
                    led = json.load(f)
                for p in led.get("positions", []):
                    actual_positions[p["code"]] = p
        
        if api_positions or actual_positions:
            content += """
### 📋 持仓个股操作建议
"""
            alpha_map = {s["symbol"]: s for s in alpha_scores} if alpha_scores else {}
            
            if api_positions:
                for pos in api_positions:
                    code = pos.get('symbol', pos.get('code', ''))
                    stock_info = alpha_map.get(code, {})
                    action = stock_info.get("action_signal", "中性")
                    score = stock_info.get("total_score", 5.0)
                    # 名称: API可能有name, 否则从name_map查
                    stock_name = pos.get('name', '') or report_name_map.get(code, code)
                    cost = pos.get('cost', 0)
                    current_price = pos.get('current_price', pos.get('avg_price', 0))
                    shares = pos.get('shares', pos.get('quantity', 0))
                    pnl = (current_price * shares) - cost if cost > 0 else 0
                    pnl_pct = (pnl / cost * 100) if cost > 0 else 0
                    pnl_str = f"{'+' if pnl>=0 else ''}{pnl:.0f}({pnl_pct:+.1f}%)"
                    content += f"- **{stock_name}**({code}) "
                    content += f"| {shares}股 @{pos.get('avg_price',0):.2f} "
                    content += f"| 盈亏: ¥{pnl_str} | 建议: {action}({score}分)\n"
            else:
                for code, pos in actual_positions.items():
                    stock_info = alpha_map.get(code, {})
                    action = stock_info.get("action_signal", "中性")
                    score = stock_info.get("total_score", 5.0)
                    stock_name = report_name_map.get(code, pos.get('name', code))
                    pnl = pos.get("value", 0) - pos.get("cost", 0)
                    pnl_str = f"{'+' if pnl>=0 else ''}{pnl:.0f}"
                    content += f"- **{stock_name}**({code}) "
                    content += f"| {pos.get('shares',0)}股 @{pos.get('avg_price',0):.2f} "
                    content += f"| 盈亏: ¥{pnl_str} | 建议: {action}({score}分)\n"
    except Exception as e:
        # fallback: 显示全部alpha_scores
        if alpha_scores:
            content += """
### 📋 全部个股评分
"""
            for stock in alpha_scores[:5]:
                content += f"- **{stock['name']}({stock['symbol']})**：{stock['action_signal']}（{stock['total_score']}分）\n"
    
    if high_events:
        content += """
### 🔔 高优先级事件提醒
"""
        for event in high_events:
            summary = event.get('content') or event.get('summary') or event.get('description', '')
            content += f"- ⚠️ **{event['title']}**：{summary}\n"
    
    if daily_predictions:
        # v4.5.8.2: 修复#4 — 只显示非hold信号(排序: buy>sell, 置信度高优先), top5
        non_hold_preds = [(sym, pred) for sym, pred in daily_predictions.items()
                          if pred.get('signal', 'hold') != 'hold']
        # 排序: buy优先, 置信度降序
        non_hold_preds.sort(key=lambda x: (0 if x[1].get('signal') == 'buy' else 1, -x[1].get('confidence', 0)))
        hc_count = sum(1 for _, p in non_hold_preds if p.get('confidence', 0) >= 0.58)
        # v4.5.8.2: 修复#4 — ML信号展示增加交叉验证状态
        content += f"""
### 🤖 ML预测信号 (非hold {len(non_hold_preds)}只, 高信度≥58% {hc_count}只)
> 💡 ✅=双确认(同向) ⚠️=单确认(ML独立) ❌=矛盾(方向冲突)
"""
        for sym, pred in non_hold_preds[:5]:
            sig_emoji = "🟢" if pred.get('signal') == 'buy' else "🔴" if pred.get('signal') == 'sell' else "🟡"
            # 交叉验证状态
            xval = ""
            alpha_s = alpha_map.get(sym, {}) if alpha_scores else {}
            alpha_sig = alpha_s.get('action_signal', '')
            if pred.get('signal') != 'hold' and pred.get('confidence',0) >= 0.58:
                if alpha_sig in ('增持', '关注') and pred.get('signal') == 'buy':
                    xval = "✅双确认"
                elif alpha_sig in ('减持', '回避') and pred.get('signal') == 'sell':
                    xval = "✅双确认"
                elif alpha_sig in ('减持', '回避') and pred.get('signal') == 'buy':
                    xval = "❌矛盾"
                elif alpha_sig in ('增持', '关注') and pred.get('signal') == 'sell':
                    xval = "❌矛盾"
                else:
                    xval = "⚠️单确认"
            content += f"- {sig_emoji} **{pred.get('name', sym)}({sym})**: {pred.get('signal','?')} "
            content += f"| 置信{pred.get('confidence',0):.0%} | 收益{pred.get('predicted_return',0):+.2%} {xval}\n"
    
    if trades:
        # v4.5.8: 交易计划展示增加Tier分级
        content += "\n### 📋 今日计划交易 (" + str(len(trades)) + "笔)\n"
        content += "> 📊 Tier-1=双确认(满仓分配) | Tier-2=单确认(半仓分配)\n"
        for t in trades:
            tier = t.get('tier', 2)
            tier_tag = f"[T{tier}]" if tier else ""
            action = t.get('action', 'BUY').upper()
            act_icon = "🟢" if action == "BUY" else "🔴"
            act_label = "买入" if action == "BUY" else "卖出"
            content += f"- {act_icon} **{act_label} {t['name']}({t['code']})**: {t['quantity']}股 @{t['price']:.2f} {tier_tag} | {t['reason']}\n"
    
    content += "\n---\n"
    # v4.7.0 P1-3: 权威系统状态段 (防agent脑补异常清单)
    content += _system_status_notes()
    content += "报告生成时间：" + datetime.now().strftime('%Y-%m-%d %H:%M:%S') + "\n"
    content += "⚠️ 决策仅供参考，投资有风险，入市需谨慎\n"
    
    # 发送飞书
    report_title = f"{title_prefix} | {market_name} | {display_date}"
    send_markdown(title=report_title, content=content)
    print(f"✅ {report_title} 已发送到飞书")
    return content

def _system_status_notes() -> str:
    """v4.7.0 P1-3: 权威系统状态段 — 供晚间/盘前报告引用
    目的: 避免cron agent自行脑补"Cron缺失/状态unknown"等异常清单
    真值来源: 任务日志文件 + 黑天鹅状态文件 (非cron_status.json, 后者键不全)
    """
    lines = ["\n### 🔍 系统状态(自动)\n"]

    # 1. Cron 白名单 (设计内停用需标注, 避免误报缺失)
    cron_state = [
        ("盘前决策(09:20)", "存在", "✅"),
        ("自我反思(22:00)", "存在", "✅"),
        ("收盘日报(15:10)", "设计内停用(08-01 James取消)", "⏸️"),
        ("健康检查(15:40)", "设计内停用(v4.6.8)", "⏸️"),
        ("黑天鹅验证(Mon+Thu)", "设计内停用(v4.6.8)", "⏸️"),
    ]
    for name, state, icon in cron_state:
        lines.append(f"- {icon} {name}: {state}")

    # 2. 09:30交易执行状态 (脚本层真值: 最新task_log)
    try:
        _log_dir = os.path.join(PROJECT_ROOT, "data", "task_logs", "trade_execution_0930")
        _files = sorted(os.listdir(_log_dir)) if os.path.isdir(_log_dir) else []
        if _files:
            _latest = _files[-1]
            _ld = os.path.join(_log_dir, _latest)
            with open(_ld, encoding="utf-8") as _f:
                _lj = json.load(_f)
            _msg = _lj.get("message", "")[:80]
            _dt = _latest[:8]
            _icon = "✅" if _lj.get("status") == "completed" else "🔴"
            lines.append(f"- {_icon} 交易执行(09:30): 最近日志 {_dt} — {_msg}")
        else:
            lines.append("- ⚠️ 交易执行(09:30): 无任务日志")
    except Exception:
        lines.append("- ⚠️ 交易执行(09:30): 日志读取失败")

    # 3. 黑天鹅一致性 (cache真相源 vs 最新复盘analysis)
    try:
        _bs_path = os.path.join(PROJECT_ROOT, "data", "black_swan_status.json")
        if os.path.exists(_bs_path):
            with open(_bs_path, encoding="utf-8") as _f:
                _bs = json.load(_f)
            _active = _bs.get("active", "?")
            _ratio = _bs.get("position_ratio", "?")
            _upd = str(_bs.get("last_updated", "?"))[:10]
            _today = datetime.now().strftime("%Y-%m-%d")
            _fresh = "✅" if _upd == _today else f"⚠️ 状态文件停更({_upd})"
            lines.append(f"- {_fresh} 黑天鹅: active={_active}, 仓位上限={float(_ratio or 0)*100:.0f}% (状态文件更新于{_upd})")
        else:
            lines.append("- ⚠️ 黑天鹅状态文件缺失")
    except Exception:
        pass

    # 4. 数据源降级 (已知常态: 东财push2封锁→新浪/同花顺)
    lines.append("- ℹ️ 数据源: 东财push2封锁(已知)→新浪/同花顺降级链, 降级属设计内行为")

    lines.append("")
    return "\n".join(lines)
    parser.add_argument('--market', type=str, required=True, choices=['a', 'hk'], help='市场类型：a=A股，hk=港股')
    parser.add_argument('--mode', type=str, default='morning', choices=['evening', 'morning'],
                        help='运行模式: evening=晚间预案预览(不执行交易), morning=早盘最终决策(执行交易)')
    args = parser.parse_args()
    
    # 进度追踪
    from common.progress_tracker import ProgressTracker
    _task_name = "pre_market_plan" if args.mode == "evening" else "pre_market_decision"
    tracker = ProgressTracker(_task_name, total_steps=9)
    tracker.step(1, f"启动{args.market.upper()}股盘前决策")
    
    print("="*70)
    print(f"🚀 {args.market.upper()}股盘前决策 - v4.5.1")
    print("="*70)
    
    date_str, display_date = get_trade_date()
    # v4.6.8: evening模式应返回下一交易日，非当日
    # 原get_trade_date()无论mode都返回today，周一~四晚间错误标记当日
    # 周日workaround: weekend偏移正确返回周一，周一到四晚间需显式计算
    if args.mode == 'evening':
        _next_date = get_next_trading_day(market='A_SHARE')
        if _next_date:
            date_str = _next_date.strftime('%Y%m%d')
            display_date = _next_date.strftime('%Y-%m-%d')
    print(f"📅 生成{display_date}的{args.market.upper()}股盘前决策")
    
    # 1. 运行自检
    tracker.step(2, "运行自检")
    try:
        subprocess.run([sys.executable, os.path.expanduser("~/.openclaw/workspace/scripts/auto_verifier.py")], check=True, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"  ⚠️ auto_verifier子进程跳过: {e}")
        print(f"  ℹ️ 自检已在import阶段完成，继续执行")
    
    # 2. 获取隔夜外盘数据 🆕
    tracker.step(3, "获取隔夜外盘数据")
    overnight = fetch_overnight_data()
    
    # v4.5.9c: VIX缺失保守模式 — VIX不可用时压制风险评级
    # VIX是波动率锚点，缺失意味着风险不可见，应降低仓位
    if overnight.get('vix') is None:
        print("  ⚠️ VIX缺失: 三级降级链均失败，启用保守模式")
        # 标记overnight_risk=cautious，risk_data会在fetch_evening_market_data中处理
        overnight['vix_missing_cautious'] = True
    
    # 3. 计算宏观评分
    tracker.step(4, "计算宏观评分")
    analyst = MacroAnalyst()
    macro_result = analyst.analyze("all", {})
    macro_score = macro_result.get("score", 7)
    print(f"📊 宏观环境评分: {macro_score}/10, 风险等级: {macro_result.get('risk_level', 'neutral')}")
    
    # 4. 加载市场数据（evening模式用收盘后实时数据，morning模式用09:00缓存）
    pool_symbols = []
    if args.mode == 'evening':
        # 加载股票池代码，用于收盘价获取
        try:
            import yaml
            pool_path = os.path.join(PROJECT_ROOT, 'config', 'master_stock_pool.yaml')
            with open(pool_path, 'r', encoding='utf-8') as f:
                pool_data = yaml.safe_load(f)
            pool_symbols = [s['symbol'] for s in pool_data.get('master_pool', []) if s.get('symbol')]
            print(f"📋 股票池: {len(pool_symbols)}只标的")
        except Exception as e:
            print(f"⚠️ 加载股票池失败: {e}")

    if args.mode == 'evening':
        # v4.5.7: 晚间模式使用收盘后实时数据
        tracker.step(4, "获取收盘后实时数据")
        sector_scores, alpha_scores, event_signals, risk_data = fetch_evening_market_data(pool_symbols)
        # v4.5.9b: 缓存alpha_scores等数据到磁盘，供次日morning mode读取
        # 保存两份: 主文件(pre_market_refresh可能覆盖) + _evening后缀(专用)
        try:
            os.makedirs(CACHE_ROOT, exist_ok=True)
            dp = date_str
            for suffix, data in [
                ("_stocks.json", alpha_scores),
                ("_stocks_evening.json", alpha_scores),
                ("_sectors.json", sector_scores),
                ("_events.json", event_signals),
                ("_risk.json", risk_data),
            ]:
                with open(f'{CACHE_ROOT}/{dp}{suffix}', 'w', encoding='utf-8') as _f:
                    json.dump(data, _f, ensure_ascii=False, indent=2)
            print(f"💾 alpha_scores多因子评分已缓存({len(alpha_scores)}只): {CACHE_ROOT}/{dp}_*")
        except Exception as _e:
            print(f"⚠️ 缓存alpha_scores失败: {_e}")
    else:
        # morning mode: 使用09:00 pre_market_refresh 缓存
        sector_scores, alpha_scores, event_signals, risk_data = load_cached_data(date_str)
    
    # v4.5.9c: VIX缺失保守模式 — VIX不可用时压制风险评级和仓位
    if overnight.get('vix_missing_cautious'):
        risk_data['overnight_risk'] = 'cautious'
        risk_data['position_ratio'] = min(risk_data.get('position_ratio', 0.8), risk_data.get('position_ratio', 0.8) * 0.9)
        print(f"  ⚠️ VIX缺失保守模式: overnight_risk=cautious, position_ratio={risk_data['position_ratio']:.3f}")
    
    # 4b. v4.5.1: 加载每日ML预测缓存
    daily_predictions = load_daily_predictions()
    
    # 5. 计算最终仓位
    tracker.step(5, "加载缓存+计算仓位")
    final_position = calculate_final_position(macro_score, sector_scores, risk_data)
    
    # v4.5.22: risk_position 是硬上限约束，与 regime_adj / drawdown 一致用 min() 截断
    # LPPL/黑天鹅提供的风险仓位上限必须硬执行，不能被宏观/行业评分稀释
    risk_pos = risk_data.get('position_ratio', 1.0)
    if risk_pos < 1.0:
        final_position = min(final_position, risk_pos)
    
    # 5b. P1-3: 市场状态检测 — 根据市场体制调整仓位
    try:
        from core.market_regime_detector import MarketRegimeDetector
        regime_detector = MarketRegimeDetector()
        regime_result = regime_detector.detect()
        regime_adj = regime_result.get('position_adj', 0.6)
        final_position = min(final_position, regime_adj)
        print(f"📊 市场状态: {regime_result['label']} ({regime_result['regime']}) → 仓位上限{regime_adj*100:.0f}%")
    except Exception as e:
        print(f"⚠️ 市场状态检测失败(不影响主流程): {e}")
    
    # 6. 输出仓位信息
    if final_position <= 0.3:
        print(f"⚠️ 建议低仓位运行：{final_position*100:.0f}%")
    elif final_position <= 0.7:
        print(f"ℹ️ 建议中性仓位运行：{final_position*100:.0f}%")
    else:
        print(f"✅ 建议高仓位运行：{final_position*100:.0f}%")
    
    # 7. v4.5.1: 生成计划交易 🆕
    tracker.step(6, "市场状态检测+生成计划交易")
    
    # v4.5.7: morning mode 先加载晚间预案，再融合新鲜数据
    evening_trades = []
    if args.mode == 'morning':
        evening_trades = load_evening_plan(display_date)
        if evening_trades:
            print(f"📋 已加载晚间预案({len(evening_trades)}笔)，与新鲜数据融合中...")
    
    trades = plan_trades(alpha_scores, daily_predictions, final_position,
                         existing_trades=evening_trades if args.mode == 'morning' else None)
    for trade in trades:
        code = trade.get("code") or trade.get("symbol") or ""
        if code:
            trade["code"] = code
            trade["symbol"] = code
    
    # v4.5.10/🅰️: 统一写入dict格式(含final_position), 供执行脚本直接读取
    # 融合推荐仓位作为执行上限, 不再由执行脚本独立读取adaptive_params硬上限
    planned_trades_path = os.path.join(PROJECT_ROOT, 'cache', 'planned_trades.json')
    os.makedirs(os.path.dirname(planned_trades_path), exist_ok=True)
    save_data = {
        "generated_at": datetime.now().isoformat(),
        "market": args.market,
        "target_date": display_date,
        "final_position": final_position,
        "ml_freshness_status": LAST_DAILY_PREDICT_FRESHNESS.get("status", "unknown"),
        "ml_freshness_reason": LAST_DAILY_PREDICT_FRESHNESS.get("reason", ""),
        "source_predict_date": LAST_DAILY_PREDICT_FRESHNESS.get("predict_date", ""),
        "source_predict_time": LAST_DAILY_PREDICT_FRESHNESS.get("predict_time", ""),
        "trades": trades if trades else []
    }
    with open(planned_trades_path, 'w', encoding='utf-8') as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"💾 计划交易已保存: {planned_trades_path} ({len(trades)}笔交易, 仓位{final_position*100:.0f}%)")
    
    # v4.5.7: 交易执行改为09:30独立cron进行
    # 原因: 09:20市场未开盘，价格不可用。仅保存计划由09:30 execute_planned_trades.py执行
    if args.mode == 'morning':
        print(f"⏳ 计划交易已保存，将在09:30开盘后由独立执行器执行")
        print(f"   (检查: A股交易执行(09:30) cron → 获取实际开盘价后执行)")
    else:
        print(f"🔶 evening mode: 跳过交易执行，用户可在09:20前手动调整 {planned_trades_path}")
    
    tracker.step(9, "生成最终报告")

    # 9. 生成最终报告并发送（传递 mode）
    generate_final_report(args.market, display_date, macro_score, macro_result, final_position,
                          sector_scores, alpha_scores, event_signals, risk_data,
                          daily_predictions, overnight, trades, mode=args.mode)
    
    print("\n" + "="*70)
    print(f"✅ 盘前决策任务完成 {'({len(trades)}笔交易已执行)' if trades else '(仅报告)'}")
    print("="*70)
    trader_name = f"{args.market.upper()}股盘前决策"
    tracker.complete(trader_name, market=args.market, trades=len(trades) if trades else 0)

if __name__ == "__main__":
    main()
