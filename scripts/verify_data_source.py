#!/usr/bin/env python3
"""
DSL v4 数据源健康检查 — 快速验证所有数据源可用性
优先测试麦蕊API → 降级akshare(Sina后端) → 输出JSON状态

用法:
  python3 scripts/verify_data_source.py               # 检查所有数据源
  python3 scripts/verify_data_source.py --json         # JSON输出
  python3 scripts/verify_data_source.py --quick        # 仅检查麦蕊
  python3 scripts/verify_data_source.py --deep         # 包含全市场akshare实时行情等重型探针
"""
import os, sys, json, time

# 强制代理绕过
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

MAIRUI_BASE = 'https://api.mairuiapi.com'

QUICK_CHECK = '--quick' in sys.argv
JSON_OUTPUT = '--json' in sys.argv
DEEP_CHECK = '--deep' in sys.argv

results = {
    "timestamp": time.strftime('%Y-%m-%dT%H:%M:%S+08:00'),
    "datasources": {},
    "overall": "unknown"
}


def log(msg: str, status: str = "info"):
    if not JSON_OUTPUT:
        icon = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}.get(status, "ℹ️")
        print(f"{icon} {msg}")


def _row_count(df) -> int:
    """Return 0 for empty/None probe results without raising in health checks."""
    if df is None:
        return 0
    try:
        return len(df)
    except Exception:
        return 0


def check_mairui():
    """优先级1: 麦蕊API — 主数据源"""
    log("正在测试麦蕊API (A股列表)...", "info")
    sources = {}
    licence = os.environ.get("MAIRUI_LICENCE", "")
    if not licence:
        sources["licence"] = {"status": "fail", "error": "MAIRUI_LICENCE not set"}
        results['datasources']['mairui_api'] = {"status": "fail", "details": sources}
        log("MAIRUI_LICENCE未设置，跳过麦蕊API测试", "fail")
        return False
    try:
        import requests
        # 1) A股列表
        t0 = time.time()
        r = requests.get(
            f'{MAIRUI_BASE}/hslt/list/{licence}',
            timeout=8,
            proxies={"http": "", "https": ""},
        )
        t = time.time() - t0
        if r.status_code == 200:
            data = r.json()
            count = len(data) if isinstance(data, list) else len(data.get('data', [])) if isinstance(data, dict) and 'data' in data else 0
            sources['a_stock_list'] = {"status": "ok", "count": count, "latency_ms": round(t * 1000)}
            log(f"麦蕊A股列表: {count}只股票, {t*1000:.0f}ms", "ok")
        else:
            sources['a_stock_list'] = {"status": "fail", "error": f"HTTP {r.status_code}: {r.text[:100]}"}
            log(f"麦蕊A股列表失败: HTTP {r.status_code}", "fail")
        
        # 2) 实时行情抽样 (平安银行 000001)
        t0 = time.time()
        from config.mairui_api_config import get_stock_real_fast, get_kline_history
        quote = get_stock_real_fast("000001")
        t = time.time() - t0
        if quote and quote.get("current_price"):
            sources['realtime_quote'] = {
                "status": "ok",
                "price": quote.get("current_price"),
                "latency_ms": round(t * 1000),
            }
            log(f"麦蕊实时行情: {t*1000:.0f}ms", "ok")
        else:
            sources['realtime_quote'] = {
                "status": "fail",
                "error": "empty quote from get_stock_real_fast",
            }
            log("麦蕊实时行情失败: 空数据", "fail")

        # 3) K线抽样 (招商银行 600036)
        t0 = time.time()
        kline = get_kline_history("600036", period="d", adjust="f", limit=5)
        t = time.time() - t0
        rows = len(kline) if isinstance(kline, list) else 0
        if rows > 0:
            sources['kline'] = {"status": "ok", "rows": rows, "latency_ms": round(t * 1000)}
            log(f"麦蕊K线: {rows}行, {t*1000:.0f}ms", "ok")
        else:
            sources['kline'] = {"status": "fail", "error": "empty kline"}
            log("麦蕊K线失败: 空数据", "fail")

        mairui_ok = all(s.get('status') == 'ok' for s in sources.values())
        results['datasources']['mairui_api'] = {
            "status": "ok" if mairui_ok else "degraded",
            "details": sources
        }
        return mairui_ok
    except ImportError:
        log("requests库未安装, 跳过麦蕊API测试", "skip")
        results['datasources']['mairui_api'] = {"status": "skip", "error": "requests not installed"}
        return False
    except Exception as e:
        log(f"麦蕊API异常: {e}", "fail")
        results['datasources']['mairui_api'] = {"status": "fail", "error": str(e)[:200]}
        return False


def check_akshare_sina():
    """优先级2: akshare (Sina后端) — 降级方案"""
    log("正在测试akshare(Sina后端)...", "info")
    sources = {}
    try:
        from common.akshare_utils import (
            safe_index_us_stock_sina,
            safe_stock_zh_a_hist,
            safe_stock_zh_a_spot_em,
            safe_stock_zh_index_daily_em,
        )
        # 1) 美股指数
        t0 = time.time()
        df = safe_index_us_stock_sina(symbol=".INX")
        t = time.time() - t0
        rows = _row_count(df)
        status = "ok" if rows > 0 else "warn"
        sources['us_index'] = {"status": status, "rows": rows, "latency_ms": round(t * 1000)}
        log(f"S&P500: {rows}行, {t*1000:.0f}ms", status)
        
        # 2) A股实时行情全市场接口较重，只在 --deep 中检查。
        if DEEP_CHECK and not QUICK_CHECK:
            t0 = time.time()
            df = safe_stock_zh_a_spot_em()
            t = time.time() - t0
            rows = _row_count(df)
            status = "ok" if rows > 0 else "warn"
            sources['a_spot'] = {"status": status, "rows": rows, "latency_ms": round(t * 1000)}
            log(f"A股实时(akshare): {rows}行, {t*1000:.0f}ms", status)

        # 3) 指数K线
        if not QUICK_CHECK:
            t0 = time.time()
            df = safe_stock_zh_index_daily_em(symbol="sh000001")
            t = time.time() - t0
            rows = _row_count(df)
            status = "ok" if rows > 0 else "warn"
            sources['index_kline'] = {"status": status, "rows": rows, "latency_ms": round(t * 1000)}
            log(f"上证指数K线: {rows}行, {t*1000:.0f}ms", status)

        # 4) 单股K线
        if not QUICK_CHECK:
            t0 = time.time()
            df = safe_stock_zh_a_hist(
                symbol="600036", period="daily",
                start_date="20260601", end_date="20260605",
                adjust="qfq",
            )
            t = time.time() - t0
            rows = _row_count(df)
            status = "ok" if rows > 0 else "warn"
            sources['a_kline'] = {"status": status, "rows": rows, "latency_ms": round(t * 1000)}
            log(f"A股K线(akshare): {rows}行, {t*1000:.0f}ms", status)

        akshare_ok = any(s.get('status') == 'ok' for s in sources.values())
        results['datasources']['akshare_sina'] = {
            "status": "ok" if akshare_ok else "fail",
            "details": sources
        }
        return akshare_ok
    except ImportError:
        log("akshare未安装, 跳过Sina后端测试", "skip")
        results['datasources']['akshare_sina'] = {"status": "skip", "error": "akshare not installed"}
        return False
    except Exception as e:
        log(f"akshare异常: {e}", "fail")
        results['datasources']['akshare_sina'] = {"status": "fail", "error": str(e)[:200]}
        return False


def check_trading_calendar():
    """交易日历 — 使用项目统一holiday_calendar，动态akshare失败时有静态fallback。"""
    log("正在测试A股交易日历...", "info")
    try:
        from config.holiday_calendar import get_holidays_for_year, is_trading_day
        holidays = get_holidays_for_year(2026)
        friday_ok = is_trading_day(__import__("datetime").date(2026, 6, 5), "A_SHARE")
        saturday_closed = not is_trading_day(__import__("datetime").date(2026, 6, 6), "A_SHARE")
        ok = len(holidays) > 0 and friday_ok and saturday_closed
        results['datasources']['trading_calendar'] = {
            "status": "ok" if ok else "fail",
            "details": {
                "holidays_2026": len(holidays),
                "2026-06-05_trading": friday_ok,
                "2026-06-06_closed": saturday_closed,
            },
        }
        log(f"A股交易日历: holidays={len(holidays)}, 工作日/周末校验={'通过' if ok else '失败'}", "ok" if ok else "fail")
        return ok
    except Exception as e:
        results['datasources']['trading_calendar'] = {"status": "fail", "error": str(e)[:200]}
        log(f"A股交易日历异常: {e}", "fail")
        return False


def main():
    log("=" * 50, "info")
    log(f"📡 数据源健康检查 — {results['timestamp']}", "info")
    log("=" * 50, "info")
    
    # 优先级1: 麦蕊
    mairui_ok = check_mairui()
    calendar_ok = check_trading_calendar()
    
    if not QUICK_CHECK:
        # 优先级2: akshare
        akshare_ok = check_akshare_sina()
    else:
        akshare_ok = False
    
    # 总体状态
    if mairui_ok and calendar_ok:
        results['overall'] = "ok"
        log("\n✅ 总体: 正常 (麦蕊API主数据源可用)", "ok")
    elif calendar_ok and (mairui_ok or akshare_ok):
        results['overall'] = "degraded"
        log("\n⚠️ 总体: 降级 (部分主源不可用, fallback可用)", "warn")
    else:
        results['overall'] = "fatal"
        log("\n❌ 总体: 致命 (所有数据源不可用)", "fail")
    
    if JSON_OUTPUT:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        log(f"\n结果概要:", "info")
        for ds, info in results['datasources'].items():
            icon = {"ok": "✅", "degraded": "⚠️", "skip": "⏭️", "fail": "❌"}.get(info.get('status', ''), "❓")
            log(f"  {icon} {ds}: {info.get('status', 'unknown')}", info.get('status', 'warn'))
    
    # 写入缓存供其他脚本读取
    cache_dir = os.path.join(PROJECT_ROOT, 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, 'datasource_status.json'), 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    return 0 if results['overall'] in ('ok', 'degraded') else 1


if __name__ == '__main__':
    sys.exit(main())
