#!/usr/bin/env python3
"""
DSL v4.5.3 盘前数据刷新 — 08:00 工作日执行
合并三大数据源 → cache/pre_market/{date}_events.json + risk.json

数据源:
  1. 隔夜外盘: S&P500/纳指/VIX/USDCNY (akshare/Sina)
  2. 亚洲市场: 日经225/韩国KOSPI 开盘后1h走势
  3. 黑天鹅: 读取前夜 memory/black-swan/ 最新分析
  4. 个股预测: 读取 cache/daily_predict.json 高信度信号

输出: cache/pre_market/{YYYYMMDD}_events.json + _risk.json
后续: 09:20 A股盘前决策直接读取
"""
import os, sys, json, yaml
from datetime import datetime, timedelta

# 强制代理绕过（akshare/东方财富/麦蕊API等）
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

CACHE_ROOT = os.path.join(PROJECT_ROOT, "cache", "pre_market")
BLACK_SWAN_DIR = os.path.join(PROJECT_ROOT, "..", "memory", "black-swan")
# v4.6.6 T2: 黑天鹅复盘实际写入 skill data 目录，DSL 需同时扫描两处取全局最新，防路径分叉导致读旧数据
BLACK_SWAN_DIR_SKILL = os.path.expanduser("~/.agents/skills/black-swan-monitor/data")
os.makedirs(CACHE_ROOT, exist_ok=True)

# ⏸️ 节假日检查：仅在直接运行时生效（避免import时触发sys.exit）
try:
    from config.holiday_calendar import is_trading_day
    _IS_TRADING_DAY = is_trading_day(market="A_SHARE")
except ImportError:
    _IS_TRADING_DAY = True  # 无法判断时默认运行


def fetch_overnight_data():
    """获取美股收盘数据 (akshare/Sina)"""
    result = {
        "sp500": None, "sp500_change": None,
        "nasdaq": None, "nasdaq_change": None,
        "vix": None, "usdcny": None,
        "fetched": False,
    }
    try:
        import akshare as ak
        
        # S&P500 & 纳斯达克
        for idx_sym, key in [(".INX", "sp500"), (".IXIC", "nasdaq")]:
            try:
                df = ak.index_us_stock_sina(symbol=idx_sym)
                if df is not None and len(df) >= 2:
                    closes = df["close"].values
                    result[key] = float(closes[-1])
                    result[f"{key}_change"] = round(float((closes[-1] / closes[-2] - 1) * 100), 2)
            except Exception as e:
                print(f"  ⚠️ {key} 获取失败: {e}")
        
        # USD/CNY (三级降级: 东方财富 → Sina → exchangerate.host)
        if result.get("usdcny") is None:
            try:
                import requests
                resp = requests.get(
                    "https://push2.eastmoney.com/api/qt/stock/get?secid=133.USDCNH&fields=f43",
                    timeout=5
                )
                if resp.status_code == 200:
                    em = resp.json()
                    if em and em.get("data"):
                        result["usdcny"] = round(float(em["data"].get("f43", 0)) / 10000, 4)
            except Exception as e:
                print(f"  ⚠️ USDCNY 东方财富失败: {e}")
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
                print(f"  ⚠️ USDCNY Sina失败: {e}")
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
                print(f"  ⚠️ USDCNY exchangerate.host失败: {e}")
        
        result["fetched"] = any(v is not None for k, v in result.items() 
                                if k not in ("fetched", "is_intraday"))
        return result
    except Exception as e:
        print(f"⚠️ 外盘数据获取失败: {e}")
        return result


def _fetch_asian_from_sina(index_code: str) -> dict:
    """新浪财经获取全球指数 (异构降级源1: hq.sinajs.cn)
    
    已验证可用代码: b_KOSPI(韩国), b_HSI(恒指), b_FTSE(富时), b_DAX(德国)
    注意: 新浪不支持日经225 (b_N225/b_NK225 均返回空)
    
    Args:
        index_code: 新浪代码, 如 'b_KOSPI', 'b_HSI'
    """
    import requests
    result = {"price": None, "change_pct": None}
    try:
        url = f"https://hq.sinajs.cn/list={index_code}"
        headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=8)
        if resp.status_code == 200 and '="' in resp.text:
            data_str = resp.text.split('="')[1].rstrip('";')
            if not data_str or len(data_str) < 5:
                return result
            fields = data_str.split(',')
            if len(fields) >= 4:
                # b_KOSPI格式: 韩国KOSPI指数,7805.5300,307.53,4.10,...
                # fields[1]=价格, fields[3]=涨跌幅%
                price_str = fields[1]
                change_pct_str = fields[3]
                if price_str:
                    result["price"] = float(price_str)
                if change_pct_str:
                    result["change_pct"] = float(change_pct_str)
            elif len(fields) >= 3:
                # b_HSI格式: 香港恒生指数,26277.80,-115.91,-0.44,...
                price_str = fields[1]
                change_pct_str = fields[3] if len(fields) > 3 else None
                if price_str:
                    result["price"] = float(price_str)
                if change_pct_str:
                    result["change_pct"] = float(change_pct_str)
    except Exception as e:
        print(f"    ⚠️ 新浪全球指数({index_code})失败: {e}")
    return result


def _fetch_asian_from_tencent(code: str) -> dict:
    """腾讯财经获取全球指数 (异构降级源2: qt.gtimg.cn)
    
    已验证可用: usDJI(道琼斯), usIXIC(纳斯达克), usINX(标普), hk00700(腾讯)
    注意: 腾讯不支持日经225/KOSPI (nz225/ks11/jpN225/krKOSPI 均返回none_match)
    
    Args:
        code: 腾讯代码
    """
    import requests
    result = {"price": None, "change_pct": None}
    try:
        url = f"https://qt.gtimg.cn/q={code}"
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200 and '="' in resp.text and 'none_match' not in resp.text:
            data_str = resp.text.split('="')[1].rstrip('";')
            parts = data_str.split('~')
            if len(parts) >= 5:
                price = float(parts[3]) if parts[3] else 0.0
                # 腾讯格式: parts[3]=当前价, parts[4]=昨收, parts[32]=涨跌幅
                if price > 0:
                    result["price"] = price
                prev_close = float(parts[4]) if len(parts) > 4 and parts[4] else 0.0
                if len(parts) > 32 and parts[32]:
                    result["change_pct"] = float(parts[32])
                elif price and prev_close:
                    result["change_pct"] = round((price / prev_close - 1) * 100, 2)
    except Exception as e:
        print(f"    ⚠️ 腾讯全球指数({code})失败: {e}")
    return result


def _fetch_asian_from_em_history(secid: str) -> dict:
    """东方财富历史K线API获取 (异构降级源3: push2his.eastmoney.com)
    
    push2his.eastmoney.com 是与 push2.eastmoney.com 不同的子域名,
    当 push2 RemoteDisconnected 时, push2his 通常仍可用。
    获取最近2个交易日K线, 计算涨跌幅。
    
    Args:
        secid: 东财secid, 如 '100.N225' (日经), '100.KS11' (KOSPI)
    """
    import requests
    result = {"price": None, "change_pct": None}
    try:
        url = (
            f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
            f"secid={secid}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55&"
            f"klt=101&fqt=0&end=20991231&lmt=2"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("rc") == 0 and data.get("data", {}).get("klines"):
                klines = data["data"]["klines"]
                # kline格式: "2026-05-11,63203.44,62486.84,63385.04,62437.20"
                # fields: date,open,close,high,low
                if len(klines) >= 2:
                    parts_today = klines[-1].split(',')
                    parts_prev = klines[-2].split(',')
                    price_today = float(parts_today[2]) if len(parts_today) > 2 else 0
                    price_prev = float(parts_prev[2]) if len(parts_prev) > 2 else 0
                    if price_today > 0 and price_prev > 0:
                        result["price"] = price_today
                        result["change_pct"] = round((price_today / price_prev - 1) * 100, 2)
                elif len(klines) == 1:
                    parts = klines[0].split(',')
                    if len(parts) > 2:
                        result["price"] = float(parts[2])
                        result["change_pct"] = 0.0  # 只有1条数据无法算涨跌幅
    except Exception as e:
        print(f"    ⚠️ 东财历史K线({secid})失败: {e}")
    return result


def fetch_asian_markets():
    """获取日韩开盘数据 (09:00后执行，已有1h交易数据)
    
    v4.5.9 修复: 异构多源降级链
    原bug: 主源(akshare东财)失败后, fallback1/fallback2都访问push2.eastmoney.com,
    等于同一域名重试, RemoteDisconnected时全部失败.
    
    修复后的降级链(5级, 域名完全异构):
      1. akshare index_global_spot_em() → 东方财富 (push2.eastmoney.com)
      2. 新浪财经 b_KOSPI (hq.sinajs.cn) — 异构域名, KOSPI已验证可用
      3. 东方财富历史K线API (push2his.eastmoney.com) — 不同子域名, 日经+KOSPI
      4. 日经225ETF (sh513880, hq.sinajs.cn) — A股ETF近似日经涨跌幅
      5. 东方财富实时API (push2.eastmoney.com) — 同域名最后兜底
    """
    result = {
        "nikkei": None, "nikkei_change": None,
        "kospi": None, "kospi_change": None,
        "fetched": False,
        "sources_used": [],  # v4.5.9: 记录实际使用的数据源
    }
    
    # ── Tier 1: akshare 东方财富 ──
    try:
        from common.akshare_utils import safe_index_global_spot_em
        
        df = safe_index_global_spot_em()
        if df is not None and not df.empty:
            # 日经225
            if "N225" in df["代码"].values:
                row = df[df["代码"] == "N225"].iloc[0]
                result["nikkei"] = float(row["最新价"])
                result["nikkei_change"] = float(row["涨跌幅"])
                result["sources_used"].append("nikkei→akshare_em")
            # 韩国KOSPI
            if "KS11" in df["代码"].values:
                row = df[df["代码"] == "KS11"].iloc[0]
                result["kospi"] = float(row["最新价"])
                result["kospi_change"] = float(row["涨跌幅"])
                result["sources_used"].append("kospi→akshare_em")
    except Exception as e:
        print(f"  ⚠️ akshare东财亚洲指数失败: {e}")
    
    # ── Tier 2: 新浪财经 (hq.sinajs.cn, 异构域名) ──
    # 注意: 新浪不支持日经225 (所有N225代码均返回空), 但支持KOSPI
    if result["kospi"] is None:
        sina_ks = _fetch_asian_from_sina("b_KOSPI")  # ✅ 已验证可用
        if sina_ks["price"] and sina_ks["price"] > 0:
            result["kospi"] = sina_ks["price"]
            result["kospi_change"] = sina_ks["change_pct"]
            result["sources_used"].append("kospi→sina")
            print(f"  ✅ KOSPI降级→新浪: {sina_ks['price']:.0f}")
    
    # ── Tier 3: 东方财富历史K线API (push2his.eastmoney.com, 不同子域名) ──
    # push2his 通常在 push2 RemoteDisconnected 时仍可用
    if result["nikkei"] is None:
        emh_nk = _fetch_asian_from_em_history("100.N225")
        if emh_nk["price"] and emh_nk["price"] > 0:
            result["nikkei"] = emh_nk["price"]
            result["nikkei_change"] = emh_nk["change_pct"]
            result["sources_used"].append("nikkei→em_history")
            print(f"  ✅ 日经225降级→东财历史K线: {emh_nk['price']:.0f}")
    
    if result["kospi"] is None:
        emh_ks = _fetch_asian_from_em_history("100.KS11")
        if emh_ks["price"] and emh_ks["price"] > 0:
            result["kospi"] = emh_ks["price"]
            result["kospi_change"] = emh_ks["change_pct"]
            result["sources_used"].append("kospi→em_history")
            print(f"  ✅ KOSPI降级→东财历史K线: {emh_ks['price']:.0f}")
    
    # ── Tier 4: 日经225ETF (A股上市, 数据可靠性极高) ──
    # 当海外指数API全挂时, 用A股日经ETF(513880)涨跌幅近似日经225
    if result["nikkei"] is None:
        try:
            import requests
            # 通过新浪获取513880日经225ETF行情
            headers = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
            resp = requests.get("https://hq.sinajs.cn/list=sh513880", headers=headers, timeout=8)
            if resp.status_code == 200 and '="' in resp.text:
                data_str = resp.text.split('="')[1].rstrip('";')
                if data_str and len(data_str) > 10:
                    fields = data_str.split(',')
                    if len(fields) >= 4:
                        etf_price = float(fields[3]) if fields[3] else 0
                        etf_prev = float(fields[2]) if fields[2] else 0
                        if etf_price > 0 and etf_prev > 0:
                            etf_change_pct = round((etf_price / etf_prev - 1) * 100, 2)
                            # 用ETF涨跌幅反推日经225大约点位 (昨日收盘从缓存获取)
                            result["nikkei"] = None  # 无法精确推算点位
                            result["nikkei_change"] = etf_change_pct  # 涨跌幅可用
                            result["sources_used"].append("nikkei_change→etf513880")
                            print(f"  ✅ 日经225涨跌幅降级→ETF(513880): {etf_change_pct:+.2f}%")
        except Exception as e:
            print(f"    ⚠️ 日经ETF降级失败: {e}")
    
    # ── Tier 5: 东方财富实时API (最后兜底, 同域名但独立请求) ──
    if result["nikkei"] is None:
        try:
            import requests
            resp = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get?secid=100.N225&fields=f43,f170",
                timeout=5
            )
            if resp.status_code == 200:
                em = resp.json()
                if em and em.get("data"):
                    result["nikkei"] = float(em["data"].get("f43", 0)) / 100
                    result["nikkei_change"] = float(em["data"].get("f170", 0)) / 100
                    result["sources_used"].append("nikkei→em_direct")
                    print(f"  ✅ 日经225降级→东财直连: {result['nikkei']:.0f}")
        except Exception as e:
            print(f"    ⚠️ 东财直连日经失败: {e}")
    
    if result["kospi"] is None:
        try:
            import requests
            resp = requests.get(
                "https://push2.eastmoney.com/api/qt/stock/get?secid=100.KS11&fields=f43,f170",
                timeout=5
            )
            if resp.status_code == 200:
                em = resp.json()
                if em and em.get("data"):
                    result["kospi"] = float(em["data"].get("f43", 0)) / 100
                    result["kospi_change"] = float(em["data"].get("f170", 0)) / 100
                    result["sources_used"].append("kospi→em_direct")
                    print(f"  ✅ KOSPI降级→东财直连: {result['kospi']:.0f}")
        except Exception as e:
            print(f"    ⚠️ 东财直连KOSPI失败: {e}")
    
    result["fetched"] = any(v is not None for k, v in result.items() if k not in ("fetched", "sources_used"))
    if not result["fetched"]:
        print(f"  ❌ 亚洲市场全部5级降级失败!")
    return result


def fetch_black_swan_latest():
    """读取前夜黑天鹅分析最新结果
    
    v4.5.5 S6(black_swan修复): black_swan分析JSON的key名实际为events_severity_ge_3,
    而非events/findings/alerts, 导致bs_active永远=False, 盘前决策position_ratio=1.0.
    同时新增fallback: 若file-based events为空, 读取adaptive_params.yaml的risk状态
    """
    try:
        # v4.6.6 T2: 扫描 memory/black-swan 与 skill data 两处，按文件名日期取全局最新
        candidates = []
        for _dir in (BLACK_SWAN_DIR, BLACK_SWAN_DIR_SKILL):
            if _dir and os.path.exists(_dir):
                for _f in os.listdir(_dir):
                    if _f.startswith("analysis-") and _f.endswith(".json"):
                        candidates.append((_f, os.path.join(_dir, _f)))
        if not candidates:
            return []
        # 按文件名(analysis-YYYY-MM-DD)降序，取最新日期
        candidates.sort(key=lambda x: x[0], reverse=True)
        latest_path = candidates[0][1]
        with open(latest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # 提取严重等级≥3的事件
        events = []
        if isinstance(data, dict):
            # v4.6.6 T2: active_events 为当前schema实际key(旧版为events_severity_ge_3)，置首优先
            for key in ["active_events", "events_severity_ge_3", "events", "findings", "alerts"]:
                items = data.get(key, [])
                if isinstance(items, list):
                    for item in items:
                        severity = item.get("severity", item.get("level", 0))
                        if isinstance(severity, (int, float)) and severity >= 3:
                            events.append({
                                "title": item.get("title", item.get("name", "未知")),
                                "severity": severity,
                                "category": item.get("category", item.get("type", "其他")),
                                "summary": item.get("summary", item.get("description", ""))[:200],
                                "source": "black_swan",
                                "date": os.path.basename(latest_path).replace("analysis-", "").replace(".json", ""),
                            })
                    if events:
                        break  # 找到数据后不再搜索其他key
        return events
    except Exception as e:
        print(f"⚠️ 黑天鹅数据读取失败: {e}")
        return []


def fetch_predictions():
    """读取每日预测高信度信号"""
    try:
        pred_path = os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")
        if not os.path.exists(pred_path):
            return {"high_signals": [], "total_signals": 0}
        
        with open(pred_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        predictions = data.get("predictions", [])
        high = [p for p in predictions if p.get("confidence_level") == "high"]
        
        return {
            "high_signals": [
                {
                    "symbol": p["symbol"],
                    "name": p.get("name", ""),
                    "signal": p.get("signal", "hold"),
                    "predicted_return": p.get("predicted_return", 0),
                    "confidence": p.get("confidence", 0),
                    "horizon": p.get("horizon", "5d"),
                }
                for p in high
            ],
            "total_signals": len(predictions),
            "high_count": len(high),
        }
    except Exception as e:
        print(f"⚠️ 预测数据读取失败: {e}")
        return {"high_signals": [], "total_signals": 0}


def fetch_sector_scores():
    """v4.5.3b: 实时获取行业评分数据
    
    v4.5.5 S6: 改用麦蕊API(非东财), 避免代理故障.
    麦蕊Hszg/list返回行业/概念树, 用stock_zh_a_spot(Sina)计算涨跌幅评分.
    """
    result = {"sectors": [], "fetched": False}
    try:
        import requests
        mairui_licence = os.getenv("MAIRUI_LICENCE", "")
        sectors = []
        
        # 方法1: 麦蕊行业/概念树 — 仅返回结构(名称/代码), 无涨跌幅数据
        _mairui_only_structure = False
        if mairui_licence:
            try:
                resp = requests.get(
                    f"https://api.mairuiapi.com/hszg/list/{mairui_licence}",
                    timeout=8
                )
                if resp.status_code == 200:
                    m = resp.json()
                    if isinstance(m, list):
                        for item in m[:20]:
                            name = item.get("mc", item.get("name", ""))
                            code = item.get("dm", item.get("code", ""))
                            if name:
                                sectors.append({
                                    "name": name, "code": code,
                                    "change_pct": 0,
                                    "total_score": 6.0,
                                    "recommend_level": "中性",
                                })
                        # P0-FIX: 麦蕊接口只返回行业结构, change_pct/score全是硬编码默认值
                        # 标记后继续尝试akshare获取真实涨跌幅数据
                        if len(sectors) > 0:
                            _mairui_only_structure = True
                            print(f"  ℹ️ 麦蕊行业树返回{len(sectors)}个行业(仅结构), 继续获取涨跌幅...")
            except Exception as e:
                print(f"  ⚠️ 麦蕊行业树获取失败: {e}")
        else:
            print("  ⚠️ MAIRUI_LICENCE未配置，跳过麦蕊行业树，继续降级数据源")
        
        # 方法2: akshare stock_board_industry_spot_em (东财push2, 含真实涨跌幅)
        # P0-FIX: 即使麦蕊返回了行业结构(默认值), 也尝试akshare获取真实数据
        if not sectors or _mairui_only_structure:
            try:
                import akshare as ak
                df = ak.stock_board_industry_spot_em()
                if df is not None and not df.empty:
                    # P0-FIX: akshare有真实数据, 替换麦蕊的硬编码默认值
                    if _mairui_only_structure:
                        sectors.clear()
                    for _, row in df.head(20).iterrows():
                        name = row.get("板块名称", row.get("name", ""))
                        change = float(row.get("涨跌幅", row.get("change", 0)) or 0)
                        score = max(1, min(10, 5 + change * 0.5))
                        recommend = "增持" if score >= 7 else ("中性" if score >= 5 else "减持")
                        sectors.append({
                            "name": name, "code": "",
                            "change_pct": round(change, 2),
                            "total_score": round(score, 1),
                            "recommend_level": recommend,
                        })
                    _mairui_only_structure = False  # akshare已填充真实数据
            except Exception as e:
                print(f"  ⚠️ akshare行业板块备用失败: {e}")
        
        # 方法3(Tier-3): 同花顺网页抓取 (东财push2被反爬时的最后降级)
        if not sectors:
            print("  🟡 东财push2不可用, 降级同花顺...")
            ths = _fetch_thshy_sectors()
            if ths:
                sectors = ths[:20]
        
        result["sectors"] = sectors
        result["fetched"] = len(sectors) > 0
        if result["fetched"]:
            src = '麦蕊' if len(sectors) > 12 else ('akshare' if len(sectors) > 5 else '同花顺')
            print(f"  ✅ 行业评分: {len(sectors)}个板块(源: {src})")
    except Exception as e:
        print(f"⚠️ 行业评分获取失败: {e}")
    return result


def fetch_stock_scores():
    """v4.5.3b: 实时获取股票池个股评分 (融合行情+预测信号)
    
    v4.5.5 S6: 改用akshare stock_zh_a_spot(Sina后端) + 麦蕊API,
    避免东方财富代理故障导致全中性5.0分。
    """
    result = {"stocks": [], "fetched": False}
    try:
        import yaml
        pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
        if not os.path.exists(pool_path):
            return result
        
        with open(pool_path, "r", encoding="utf-8") as f:
            pool_data = yaml.safe_load(f)
        stocks_in_pool = pool_data.get("master_pool", [])
        if not stocks_in_pool:
            return result
        
        # 读取预测缓存
        pred_data = fetch_predictions()
        pred_map = {p["symbol"]: p for p in pred_data.get("high_signals", [])}
        
        # P0-2: 盘前(<09:25)stock_zh_a_spot返回全0涨跌幅 → 优先用前夜晚间缓存
        now = datetime.now()
        pre_market_cutoff = now.replace(hour=9, minute=25, second=0, microsecond=0)
        if now < pre_market_cutoff:
            # 查找最新晚间缓存文件
            for lookback in range(1, 5):  # 回溯最多4天
                _prev = now - timedelta(days=lookback)
                _evening_path = os.path.join(CACHE_ROOT, f"{_prev.strftime('%Y%m%d')}_stocks_evening.json")
                if os.path.exists(_evening_path):
                    try:
                        with open(_evening_path, 'r', encoding='utf-8') as _ef:
                            _evening_data = json.load(_ef)
                        if _evening_data and len(_evening_data) > 0:
                            # 检查是否有非中性信号
                            non_neutral = [s for s in _evening_data
                                          if s.get('action_signal', '') not in ('中性', '', None)
                                          or s.get('pred_signal', 'hold') != 'hold']
                            if non_neutral:
                                scores = _evening_data
                                result["stocks"] = scores
                                result["fetched"] = True
                                print(f"  ✅ 盘前(<09:25)使用晚间缓存({_prev.strftime('%m%d')}, {len(scores)}只, {len(non_neutral)}只有效信号)")
                                return result
                    except Exception as _e:
                        continue
            print(f"  ⚠️ 盘前无可用晚间缓存, 降级至stock_zh_a_spot(可能全中性)")
        
        # 从akshare stock_zh_a_spot (Sina/Tianan后端, 非东财) 获取实时行情
        import akshare as ak
        import requests
        scores = []
        
        spot_df = ak.stock_zh_a_spot()
        if spot_df is not None and not spot_df.empty:
            spot_map = {}
            for _, row in spot_df.iterrows():
                code = str(row.get("代码", ""))[:6]
                spot_map[code] = {
                    "price": float(row.get("最新价", 0) or 0),
                    "change": float(row.get("涨跌幅", 0) or 0),
                }
            for stock in stocks_in_pool[:25]:
                symbol = stock.get("symbol", "")
                name = stock.get("name", symbol)
                code_6d = symbol[:6]
                spot = spot_map.get(code_6d, {})
                change_pct = spot.get("change", 0)
                score = max(1, min(10, 5 + change_pct * 2))
                pred = pred_map.get(symbol, {})
                pred_signal = pred.get("signal", "hold")
                pred_ret = pred.get("predicted_return", 0)
                if pred_signal == "buy" and pred_ret > 0.005:
                    score = min(10, score + 1.5)
                elif pred_signal == "sell" and pred_ret < -0.005:
                    score = max(1, score - 1.5)
                action = "增持" if score >= 7 else ("中性" if score >= 5 else "减持")
                scores.append({
                    "symbol": symbol, "name": name,
                    "total_score": round(score, 1),
                    "action_signal": action,
                    "change_pct": round(change_pct, 2),
                    "predicted_return": round(pred_ret, 4),
                    "pred_signal": pred_signal,
                })
        if scores:
            scores.sort(key=lambda x: x["total_score"], reverse=True)
            result["stocks"] = scores
            result["fetched"] = True
            print(f"  ✅ 实时个股评分: {len(scores)}只")
    except Exception as e:
        print(f"⚠️ 个股评分获取失败: {e}")
    return result


def merge_with_previous_cache(date_str, new_data, cache_type):
    """v4.5.3b: 尝试与前夜21:00的缓存合并 (09:00刷新可能拿不到完整数据时降级)"""
    cache_path = os.path.join(CACHE_ROOT, f"{date_str}_{cache_type}.json")
    if os.path.exists(cache_path):
        # 前夜已有缓存，用09:00实时数据覆盖
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                prev_data = json.load(f)
            # 如果实时数据获取成功，用实时数据；否则保留前夜数据
            if new_data.get("fetched"):
                return new_data.get("sectors" if cache_type == "sectors" else "stocks", prev_data)
            else:
                print(f"  ⚠️ {cache_type} 实时数据不可用，保留前夜缓存")
                return prev_data
        except Exception:
            pass
    # 无前夜缓存，用实时数据
    if new_data.get("fetched"):
        return new_data.get("sectors" if cache_type == "sectors" else "stocks", [])
    return []


def main():
    date_str = datetime.now().strftime("%Y%m%d")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 进度追踪
    from common.progress_tracker import ProgressTracker
    tracker = ProgressTracker("pre_market_refresh", total_steps=6)
    
    print("=" * 60)
    print(f"🌅 DSL v4.5.3 盘前数据刷新 {now}")
    print("=" * 60)
    
    # 1. 隔夜外盘
    tracker.step(1, "获取隔夜外盘")
    print("\n📡 1/4 获取隔夜外盘...")
    overnight = fetch_overnight_data()
    if overnight["fetched"]:
        sp_str = f'{overnight.get("sp500",0):.0f}' if overnight.get("sp500") else "-"
        sp_chg = f'{overnight.get("sp500_change",0):+.1f}%' if overnight.get("sp500_change") is not None else "-"
        print(f"  ✅ S&P500 {sp_str} ({sp_chg}) | USDCNY {overnight.get('usdcny','-')}")
    
    tracker.step(2, "获取亚洲市场数据")
    # 2. 亚洲市场
    print("\n📡 2/4 获取亚洲市场...")
    asian = fetch_asian_markets()
    if asian["fetched"]:
        nk_str = f'{asian.get("nikkei",0):.0f}' if asian.get("nikkei") else "-"
        ks_str = f'{asian.get("kospi",0):.0f}' if asian.get("kospi") else "-"
        print(f"  ✅ 日经 {nk_str} | KOSPI {ks_str}")
    else:
        print("  ⚠️ 亚洲市场数据不可用（可能网络问题）")
    
    tracker.step(3, "读取黑天鹅分析")
    # 3. 黑天鹅
    print("\n📡 3/4 读取黑天鹅分析...")
    bs_events = fetch_black_swan_latest()
    print(f"  ✅ 严重事件: {len(bs_events)}个" + 
          (f" (最高等级{max(e['severity'] for e in bs_events)})" if bs_events else ""))
    
    tracker.step(4, "读取个股预测")
    # 4. 个股预测
    print("\n📡 4/6 读取个股预测...")
    pred_data = fetch_predictions()
    print(f"  ✅ 高信度信号: {pred_data['high_count']}/{pred_data['total_signals']}")
    
    # 5. v4.5.3b: 实时行业评分
    tracker.step(5, "获取实时行业评分")
    print("\n📡 5/6 获取实时行业评分...")
    sector_data = fetch_sector_scores()
    
    # 6. v4.5.3b: 实时个股评分
    tracker.step(6, "获取实时个股评分")
    print("\n📡 6/6 获取实时个股评分...")
    stock_data = fetch_stock_scores()
    
    # 合并写入 events.json
    events = {
        "refresh_time": now,
        "refresh_date": date_str,
        "version": "4.5.3b",
        "overnight": overnight,
        "asian_markets": asian,
        "black_swan": {
            "event_count": len(bs_events),
            "max_severity": max([e["severity"] for e in bs_events]) if bs_events else 0,
            "events": bs_events,
        },
        "predictions": pred_data,
    }
    
    events_path = os.path.join(CACHE_ROOT, f"{date_str}_events.json")
    with open(events_path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    print(f"\n✅ events.json → {events_path}")
    
    # 写入 risk.json
    bs_active = any(e["severity"] >= 4 for e in bs_events)
    
    # P0-2: 从 adaptive_params.yaml 读取黑天鹅仓位比率
    # v4.5.5 S6: 也读取black_swan_active作为fallback，当file-based events为空时使用
    black_swan_ratio = 0.8  # 默认值 (读不到config时不强制限制仓位)
    bs_active_from_config = False
    us_bubble = {}
    try:
        ap_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        if os.path.exists(ap_path):
            with open(ap_path, "r", encoding="utf-8") as f:
                ap = yaml.safe_load(f)
        if ap and "risk" in ap and "black_swan_position_ratio" in ap["risk"]:
            black_swan_ratio = float(ap["risk"]["black_swan_position_ratio"])
            print(f"  ✅ 读取 black_swan_position_ratio = {black_swan_ratio} (adaptive_params)")
        if ap and "risk" in ap and ap["risk"].get("black_swan_active", False):
            bs_active_from_config = True
            print(f"  ✅ adaptive_params.yaml 标记黑天鹅活跃 (black_swan_active=True)")
        # v4.5.3b: 读取美股泡沫评分
        us_bubble = ap.get("risk", {}).get("us_bubble", {}) if ap else {}
        if us_bubble:
            print(f"  ✅ 读取 us_bubble_score = {us_bubble.get('total_score',0)} ({us_bubble.get('level','?')})")
    except Exception as e:
        print(f"  ⚠️ 无法读取 adaptive_params: {e}, 使用默认 {black_swan_ratio}")
    
    # v4.5.5 S6: 如果file-based events为空但是adaptive_params标记active, 以config为准
    effective_bs_active = bs_active or bs_active_from_config
    if bs_active_from_config and not bs_active:
        print(f"  ⚠️ file-based events为空, 但adaptive_params标记黑天鹅活跃, 取config为准")
    
    risk = {
        "refresh_time": now,
        "refresh_date": date_str,
        "black_swan_active": effective_bs_active,
        "black_swan_max_severity": max([e["severity"] for e in bs_events]) if bs_events else 0,
        "black_swan_events": len(bs_events),
        "position_ratio": black_swan_ratio if effective_bs_active else 1.0,
        "overnight_risk": "high" if (overnight.get("sp500_change") or 0) < -1.5 else "normal",
        "asian_risk": "high" if (asian.get("nikkei_change") or 0) < -1.5 else "normal",
        "us_bubble": us_bubble if us_bubble else None,
    }
    
    risk_path = os.path.join(CACHE_ROOT, f"{date_str}_risk.json")
    with open(risk_path, "w", encoding="utf-8") as f:
        json.dump(risk, f, ensure_ascii=False, indent=2)
    print(f"✅ risk.json → {risk_path}")
    
    # v4.5.3b: 写入 sectors.json (09:20 盘前决策读取, 格式与pre_market_preparation一致: 直接列表)
    sector_scores = merge_with_previous_cache(date_str, sector_data, "sectors")
    sectors_path = os.path.join(CACHE_ROOT, f"{date_str}_sectors.json")
    with open(sectors_path, "w", encoding="utf-8") as f:
        json.dump(sector_scores, f, ensure_ascii=False, indent=2)
    print(f"✅ sectors.json → {sectors_path} ({len(sector_scores)}个板块)")
    
    # v4.5.3b: 写入 stocks.json (09:20 盘前决策读取, 格式与pre_market_preparation一致: 直接列表)
    stock_scores = merge_with_previous_cache(date_str, stock_data, "stocks")
    stocks_path = os.path.join(CACHE_ROOT, f"{date_str}_stocks.json")
    with open(stocks_path, "w", encoding="utf-8") as f:
        json.dump(stock_scores, f, ensure_ascii=False, indent=2)
    print(f"✅ stocks.json → {stocks_path} ({len(stock_scores)}只)")
    
    # 汇总
    print(f"\n{'='*60}")
    print(f"📊 刷新完成:")
    print(f"  外盘: {'✅' if overnight['fetched'] else '❌'}")
    print(f"  亚洲: {'✅' if asian['fetched'] else '⚠️'}")
    print(f"  黑天鹅: {len(bs_events)}个严重事件")
    print(f"  预测: {pred_data['high_count']}个高信度信号")
    print(f"  行业: {len(sector_scores)}个板块评分")
    print(f"  个股: {len(stock_scores)}只评分")
    print(f"  风险: {'🔴 黑天鹅活跃' if bs_active else '🟢 正常'}")
    print(f"  → 09:20 盘前决策就绪 (4文件齐全)")
    tracker.complete("盘前数据刷新完成", a_shares=len(stock_data.get("scores",[])), high_signals=pred_data['high_count'])
    
    # v4.5.13: 飞书自报告
    try:
        from common.feishu_utils import send_markdown
        report = f"""**📡 盘前数据刷新 | {datetime.now().strftime('%Y-%m-%d %H:%M')}**
外盘: {'✅' if overnight['fetched'] else '❌'} | 亚洲: {'✅' if asian['fetched'] else '⚠️'}
黑天鹅: {len(bs_events)}个 | 高信度信号: {pred_data['high_count']}个
行业板块: {len(sector_scores)}个 | 个股评分: {len(stock_scores)}只
风险: {'🔴 黑天鹅活跃' if bs_active else '🟢 正常'}"""
        send_markdown(title=f"📡 盘前刷新 | {datetime.now().strftime('%Y-%m-%d')}", content=report)
    except Exception:
        pass
    
    return 0


def _fetch_thshy_sectors() -> list:
    """同花顺行业板块涨跌幅 (东财push2降级备用)
    
    东财push2.eastmoney.com被反爬/IP封禁时, 用同花顺网页替代.
    返回: [{name, change_pct, total_score, recommend_level}]
    """
    sectors = []
    try:
        import requests
        from bs4 import BeautifulSoup
        s = requests.Session()
        s.trust_env = False
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        }
        # 获取行业板块列表(第一页)
        resp = s.get("http://q.10jqka.com.cn/thshy/", timeout=10, headers=headers)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            for table in soup.find_all('table'):
                rows = table.find_all('tr')[1:]  # 跳过表头
                for tr in rows:
                    cells = [td.get_text(strip=True) for td in tr.find_all(['td'])]
                    if len(cells) >= 6:
                        name = cells[1] if len(cells) > 1 else ''
                        chg_str = cells[2] if len(cells) > 2 else '0'
                        inflow_str = cells[5] if len(cells) > 5 else '0'
                        try:
                            change = float(chg_str.replace('%', ''))
                        except ValueError:
                            change = 0.0
                        score = 5.0 + (change / 2)
                        score = max(1, min(10, score))
                        recommend = "推荐" if change > 2 else ("关注" if change > 0.5 else ("中性" if change > -1 else "回避"))
                        sectors.append({
                            "name": f"行业-{name}",
                            "code": cells[0] if len(cells) > 0 else '',
                            "change_pct": change,
                            "net_inflow": inflow_str,
                            "total_score": round(score, 1),
                            "recommend_level": recommend,
                        })
            if sectors:
                print(f"  📊 行业板块(同花顺): {len(sectors)}个")
    except Exception as e:
        print(f"  ⚠️ 同花顺行业板块获取失败: {e}")
    return sectors


def _fetch_thshy_concepts() -> list:
    """同花顺概念板块涨跌幅 (东财push2降级备用)"""
    concepts = []
    try:
        import requests
        from bs4 import BeautifulSoup
        s = requests.Session()
        s.trust_env = False
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        }
        # 概念板块页面
        resp = s.get("http://q.10jqka.com.cn/gn/", timeout=10, headers=headers)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            for table in soup.find_all('table'):
                rows = table.find_all('tr')[1:]
                for tr in rows[:10]:
                    cells = [td.get_text(strip=True) for td in tr.find_all(['td'])]
                    if len(cells) >= 3:
                        name = cells[1] if len(cells) > 1 else ''
                        concepts.append({
                            "name": f"概念-{name}",
                            "change_pct": 0,
                            "total_score": 6.0,
                            "recommend_level": "中性",
                        })
            if concepts:
                print(f"  💡 概念板块(同花顺): {len(concepts)}个")
    except Exception as e:
        print(f"  ⚠️ 同花顺概念板块获取失败: {e}")
    return concepts


def _fetch_thshy_capital_flow() -> dict:
    """同花顺行业资金流向 (东财push2降级备用)
    
    从同花顺行业板块表头提取净流入数据
    返回: {top_inflow_sectors: [...], top_outflow_sectors: [...]}
    """
    result = {"top_inflow": [], "top_outflow": []}
    try:
        import requests
        from bs4 import BeautifulSoup
        s = requests.Session()
        s.trust_env = False
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        }
        resp = s.get("http://q.10jqka.com.cn/thshy/", timeout=10, headers=headers)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            flows = []
            for table in soup.find_all('table'):
                rows = table.find_all('tr')[1:]
                for tr in rows:
                    cells = [td.get_text(strip=True) for td in tr.find_all(['td'])]
                    if len(cells) >= 6:
                        try:
                            inflow = float(cells[5].replace('−', '-').replace('亿', '').replace('--', '0'))
                        except ValueError:
                            inflow = 0.0
                        name = cells[1] if len(cells) > 1 else cells[0]
                        flows.append({"name": name, "inflow": inflow})
            flows.sort(key=lambda x: x["inflow"], reverse=True)
            result["top_inflow"] = flows[:5]
            result["top_outflow"] = flows[-5:] if len(flows) >= 5 else []
            if flows:
                print(f"  💰 行业资金流(同花顺): {len(flows)}个板块")
    except Exception as e:
        print(f"  ⚠️ 同花顺资金流获取失败: {e}")
    return result


if __name__ == "__main__":
    if not _IS_TRADING_DAY:
        print(f"⏸️ {datetime.now().strftime('%Y-%m-%d')} 非A股交易日，跳过盘前刷新")
        sys.exit(0)
    sys.exit(main())
