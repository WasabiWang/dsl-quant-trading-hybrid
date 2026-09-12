#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/fetch_all_klines.py — 全 A 股日线抓取管道 (阶段1-B 步骤2 · pilot)

设计要点:
1. 复权口径 **统一走 common/adjust.get_kline_adjusted** (不得硬编码 / 不得直连迈蕊)。
2. 分批 + 限流退避 (复用 config.mairui_api_config 内建 _rate_limit + 指数退避重试)。
3. 断点续传: 已存在的 `data/universe_klines/{code}.parquet` 直接跳过。
4. 失败清单落盘 (JSONL)。
5. pilot 模式: `--limit N` 只抓前 N 只。

用法:
    python3 scripts/fetch_all_klines.py --limit 500                 # pilot 500 只
    python3 scripts/fetch_all_klines.py --limit 0                   # 全量 5220 只
    python3 scripts/fetch_all_klines.py --limit 500 --start 1990-01-01 --end 2026-09-12

预检 (proxy 绕过): macOS 系统代理 (127.0.0.1:1082) 会被 requests 自动读取并导致
ProxyError/RemoteDisconnected。本脚本导入阶段强制 trust_env=False + no_proxy=*。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ─── 代理绕过 (导入即生效, 必须在 import requests/迈蕊配置之前) ────────────────
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402


def _patch_session():
    """双保险: 让迈蕊内部 session 也 trust_env=False。"""
    try:
        from config import mairui_api_config as m
        sess = getattr(m, "_MAIRUI_SESSION", None)
        if sess is not None:
            sess.trust_env = False
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] patch session failed: {exc}", file=sys.stderr)


def _write_cache(df: pd.DataFrame, path: Path) -> str:
    """写 parquet 缓存; 无 pyarrow/fastparquet 时降级为 csv 同名前缀并返回实际格式。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        df.to_parquet(path, index=False)
        return "parquet"
    except Exception:  # noqa: BLE001
        csv_path = path.with_suffix(".csv")
        df.to_csv(csv_path, index=False)
        return "csv"


def _cache_exists(path: Path) -> bool:
    return path.exists() or path.with_suffix(".csv").exists()


def load_universe(limit: int, codes_file: str | None = None) -> list[dict]:
    """取股票列表 (麦蕊 get_all_stock_list, 全量约 5220 只)。

    codes_file: 可选 —— 显式代码列表 JSON（`{"codes":[...]}` 或 `["..."]`）。
    用于补抓**非当前上市**标的（如退市股）—— 这类标的不在 get_all_stock_list() 里。
    """
    if codes_file:
        with open(codes_file, encoding="utf-8") as f:
            payload = json.load(f)
        codes = payload.get("codes") if isinstance(payload, dict) else payload
        codes = [str(c).strip() for c in (codes or []) if str(c).strip()]
        if not codes:
            raise RuntimeError(f"codes_file {codes_file} 未解析出任何代码")
        if limit and limit > 0:
            codes = codes[:limit]
        return [{"dm": c} for c in codes]

    from config.mairui_api_config import get_all_stock_list

    lst = get_all_stock_list()
    if not lst:
        raise RuntimeError("get_all_stock_list() 返回空")
    if limit and limit > 0:
        lst = lst[:limit]
    return lst


def _fetch_tencent(code6: str, start: str, end: str, adjust: str = "hfq") -> pd.DataFrame:
    """腾讯兜底 —— 用于**麦蕊没有的标的**（退市股等）。

    腾讯 fqkline 忽略 start、只按 end 返回最近 n 根 → 用首页首日作为下一页 end 向前翻页。
    字段顺序: [date, open, close, high, low, volume, ...]
    """
    import requests
    from common.adjust import _KLINE_FIELDS

    fq = {"hfq": "hfq", "qfq": "qfq", "raw": ""}.get(adjust, "hfq")
    pre = "sh" if code6.startswith(("6", "9", "5")) else "sz"
    sess = requests.Session()
    sess.trust_env = False          # 绕过 macOS 系统代理注入

    seen: set[str] = set()
    rows: list[dict] = []
    cur_end = end
    import time as _time
    consec_fail = 0
    for _ in range(80):             # 上限 80 页 ≈ 25600 交易日
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={pre}{code6},day,{start},{cur_end},320,{fq}")
        try:
            js = sess.get(url, timeout=15).json()
        except Exception:
            # 典型: WAF 501 返回 HTML → json() 抛错。指数退避后放弃(交上层走新浪)
            consec_fail += 1
            if consec_fail >= 3:
                break
            _time.sleep(1.5 * consec_fail)
            continue
        consec_fail = 0
        _time.sleep(1.2)            # 页间限速, 避免触发腾讯 WAF
        d = (js.get("data") or {}).get(pre + code6) or {}
        k = d.get(f"{fq}day") or d.get("day") or []
        if not k:
            break
        added = 0
        for r in k:
            try:
                t = str(r[0])[:10]
                if t in seen or len(t) < 10:
                    continue
                seen.add(t)
                added += 1
                rows.append({
                    "date": t,
                    "open": float(r[1]),
                    "high": float(r[3]),
                    "low": float(r[4]),
                    "close": float(r[2]),
                    "volume": float(r[5]) if len(r) > 5 else 0.0,
                    "amount": 0.0,
                    "prev_close": 0.0,
                    "adjust": adjust,
                })
            except (ValueError, TypeError, IndexError):
                continue
        first = str(k[0][0])[:10]
        if added == 0 or first <= start:
            break
        cur_end = first

    if not rows:
        return pd.DataFrame(columns=_KLINE_FIELDS)
    return (pd.DataFrame(rows)
              .drop_duplicates(subset=["date"])
              .sort_values("date")
              .reset_index(drop=True))


def _fetch_sina(code6: str, start: str, end: str, adjust: str = "hfq") -> pd.DataFrame:
    """新浪兜底 —— 用于腾讯被 WAF 封禁(HTTP 501)时。

    仅返回最近 datalen 根(1023 ≈ 4 年), **无法分页到全历史** —— 这是已知限制。
    新浪端点不复权, 故只产出 raw 口径; 需要 hfq 时应优先腾讯。
    """
    import json as _json
    import re as _re
    import requests
    from common.adjust import _KLINE_FIELDS

    pre = "sh" if code6.startswith(("6", "9", "5")) else "sz"
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/x/CN_MarketDataService.getKLineData"
           f"?symbol={pre}{code6}&scale=240&ma=no&datalen=1023")
    sess = requests.Session()
    sess.trust_env = False
    try:
        txt = sess.get(url, timeout=15).text
    except Exception:
        return pd.DataFrame(columns=_KLINE_FIELDS)
    m = _re.search(r"\((\[.*\])\)", txt, _re.S)
    if not m:
        return pd.DataFrame(columns=_KLINE_FIELDS)
    try:
        arr = _json.loads(m.group(1))
    except Exception:
        return pd.DataFrame(columns=_KLINE_FIELDS)

    rows = []
    for it in arr:
        try:
            d = str(it.get("day", ""))[:10]
            if len(d) < 10 or d < start or d > end:
                continue
            rows.append({
                "date": d,
                "open": float(it["open"]),
                "high": float(it["high"]),
                "low": float(it["low"]),
                "close": float(it["close"]),
                "volume": float(it.get("volume", 0) or 0),
                "amount": 0.0,
                "prev_close": 0.0,
                "adjust": "raw_sina",
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return pd.DataFrame(columns=_KLINE_FIELDS)
    return (pd.DataFrame(rows)
              .drop_duplicates(subset=["date"])
              .sort_values("date")
              .reset_index(drop=True))


def fetch_one(code6: str, start: str, end: str) -> pd.DataFrame:
    """三级取数: 麦蕊 → 腾讯 → 新浪。

    麦蕊无退市股; 腾讯有全历史但会被 WAF 限流(HTTP 501); 新浪仅≈1023 根且不复权。
    """
    from common.adjust import get_kline_adjusted

    df = get_kline_adjusted(code6, start, end)
    if df is not None and len(df) > 0:
        return df
    df = _fetch_tencent(code6, start, end)
    if df is not None and len(df) > 0:
        return df
    return _fetch_sina(code6, start, end)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500, help="抓取前 N 只 (0=全量)")
    ap.add_argument("--codes-file", default=None,
                    help="显式代码列表 JSON (用于补抓退市股等非当前上市标的); 与 --limit 并用时 limit 作用在列表上")
    ap.add_argument("--start", default="1990-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "universe_klines"))
    ap.add_argument("--fail-log", default=str(ROOT / "data" / "universe_klines" / "_failures.jsonl"))
    ap.add_argument("--sleep", type=float, default=0.0, help="每只额外 sleep 秒 (0=仅靠内建限流)")
    args = ap.parse_args()

    _patch_session()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fail_log = Path(args.fail_log)

    t0 = time.time()
    universe = load_universe(args.limit, args.codes_file)
    print(f"[universe] {len(universe)} 只 (limit={args.limit}, codes_file={args.codes_file}) 区间 {args.start} ~ {args.end}")

    ok = skipped = failed = 0
    failed_list: list[dict] = []
    times: list[float] = []

    for i, item in enumerate(universe, 1):
        dm = str(item.get("dm") or item.get("code") or "").strip()
        code6 = dm.split(".")[0].lower().replace("sh", "").replace("sz", "").strip() or dm
        cache_path = out_dir / f"{code6}.parquet"

        if _cache_exists(cache_path):
            skipped += 1
            continue

        ts = time.time()
        try:
            df = fetch_one(code6, args.start, args.end)
            if df is None or len(df) == 0:
                raise ValueError("empty kline")
            fmt = _write_cache(df, cache_path)
            ok += 1
            times.append(time.time() - ts)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            rec = {"code": code6, "dm": dm, "error": f"{type(exc).__name__}: {exc}"[:300],
                   "ts": datetime.now().isoformat(timespec="seconds")}
            failed_list.append(rec)
            with fail_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        if args.sleep:
            time.sleep(args.sleep)
        if i % 50 == 0 or i == len(universe):
            el = time.time() - t0
            print(f"[{i}/{len(universe)}] ok={ok} skip={skipped} fail={failed} "
                  f"elapsed={el:.1f}s avg={el/i:.3f}s/只", flush=True)

    total = time.time() - t0
    avg = (sum(times) / len(times)) if times else 0.0
    summary = {
        "universe": len(universe),
        "ok": ok, "skipped": skipped, "failed": failed,
        "elapsed_sec": round(total, 2),
        "avg_sec_per_stock": round(avg, 4),
        "throughput_per_sec": round(ok / total, 3) if total else 0,
        "range": [args.start, args.end],
    }
    print("[DONE] " + json.dumps(summary, ensure_ascii=False))

    # 外推 (基于本次实测)
    if ok > 0:
        for label, n in (("pilot", len(universe)), ("full 5220", 5220)):
            est = avg * n
            print(f"[EXTRAPOLATE] {label}: {n} 只 × {avg:.3f}s ≈ {est/60:.1f} 分钟 ({est/3600:.2f} 小时)")
    if failed_list:
        print(f"[FAILURES] {len(failed_list)} 条 → {fail_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
