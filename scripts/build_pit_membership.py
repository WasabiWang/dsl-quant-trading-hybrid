#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_pit_membership.py — 选项 B · 步骤 1：构造逐日/逐季可交易股票集合（时点成员）

只读现有文件；仅新建 data/universe_pit_membership.json。

数据源（均实测可达，详见 audits/phase1-B-步骤1-成员判定.md）：
  - 当前股票列表：麦蕊 get_all_stock_list()（5,220 只，字段 dm/mc/jys）
  - 当前股上市日：akshare stock_info_sh_name_code(主板A股/科创板) + stock_info_sz_name_code(A股列表)
  - 沪退市名单：akshare stock_info_sh_delist（159 条，字段 公司代码/公司简称/上市日期/暂停上市日期）
  - 深退市名单：akshare stock_info_sz_delist（208 条，字段 证券代码/证券简称/上市日期/终止上市日期）

输出：data/universe_pit_membership.json
  - members: [{code, name, start, end, src}]  start=上市日, end=退市/终止日(退市股)或 "present"(当前股)
  - quarterly_tradable_series: 每个季度末可交易只数时间序列
"""
import os
import sys
import json
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

# ── 代理绕过（必须在 akshare/requests 使用前生效，规避 macOS 系统代理 127.0.0.1:1082）──
from common.proxy_bypass import clean_env_proxies
clean_env_proxies()

import akshare as ak

TODAY = "2026-09-12"
PRESENT = "present"  # 当前仍在市的股票 end 哨兵值


def _d(x):
    """归一化为 ISO 日期字符串 yyyy-mm-dd"""
    if x is None:
        return None
    if isinstance(x, (datetime.datetime, datetime.date)):
        return x.strftime("%Y-%m-%d")
    s = str(x).strip()
    return s[:10] if s else None


def main():
    # 1) 当前股票列表（麦蕊）
    from config.mairui_api_config import get_all_stock_list
    cur = get_all_stock_list()
    print(f"[1] mairui current list: {len(cur)}")

    # 2) 当前股上市日期（沪深交易所官网，可达）
    listing = {}
    try:
        df = ak.stock_info_sh_name_code(symbol="主板A股")
        for _, r in df.iterrows():
            listing[str(r['证券代码']).zfill(6)] = _d(r['上市日期'])
        print(f"[2a] SH 主板 listing dates: {len(df)}")
    except Exception as e:
        print(f"[2a] WARN SH主板: {type(e).__name__}: {str(e)[:120]}")
    try:
        df = ak.stock_info_sh_name_code(symbol="科创板")
        for _, r in df.iterrows():
            listing[str(r['证券代码']).zfill(6)] = _d(r['上市日期'])
        print(f"[2b] SH 科创板 listing dates: {len(df)}")
    except Exception as e:
        print(f"[2b] WARN SH科创板: {type(e).__name__}: {str(e)[:120]}")
    try:
        df = ak.stock_info_sz_name_code(symbol="A股列表")
        for _, r in df.iterrows():
            listing[str(r['A股代码']).zfill(6)] = _d(r['A股上市日期'])
        print(f"[2c] SZ A股 listing dates: {len(df)}")
    except Exception as e:
        print(f"[2c] WARN SZ: {type(e).__name__}: {str(e)[:120]}")
    print(f"[2] total listing dates: {len(listing)}")

    # 3) 退市名单
    delisted = []
    try:
        df = ak.stock_info_sh_delist()
        for _, r in df.iterrows():
            delisted.append((str(r['公司代码']).zfill(6), r['公司简称'],
                             _d(r['上市日期']), _d(r['暂停上市日期']), "delisted_sh"))
        print(f"[3a] SH delist: {len(df)} cols={list(df.columns)}")
    except Exception as e:
        print(f"[3a] WARN SH delist: {type(e).__name__}: {str(e)[:120]}")
    try:
        df = ak.stock_info_sz_delist()
        for _, r in df.iterrows():
            delisted.append((str(r['证券代码']).zfill(6), r['证券简称'],
                             _d(r['上市日期']), _d(r['终止上市日期']), "delisted_sz"))
        print(f"[3b] SZ delist: {len(df)} cols={list(df.columns)}")
    except Exception as e:
        print(f"[3b] WARN SZ delist: {type(e).__name__}: {str(e)[:120]}")

    # 4) 构造 members
    members = []
    seen = set()
    for code, name, start, end, src in delisted:
        members.append({"code": code, "name": name, "start": start, "end": end, "src": src})
        seen.add(code)
    for s in cur:
        code = str(s['dm']).split('.')[0]
        if code in seen:
            continue
        members.append({"code": code, "name": s.get('mc'),
                        "start": listing.get(code), "end": PRESENT, "src": "current"})
        seen.add(code)

    n_cur = sum(1 for m in members if m['src'] == 'current')
    n_sh = sum(1 for m in members if m['src'] == 'delisted_sh')
    n_sz = sum(1 for m in members if m['src'] == 'delisted_sz')
    n_cur_dated = sum(1 for m in members if m['src'] == 'current' and m['start'])

    # 5) 季度末可交易集合规模时间序列
    qends = []
    for y in range(1990, 2027):
        for mo, dd in [(3, 31), (6, 30), (9, 30), (12, 31)]:
            qends.append(datetime.date(y, mo, dd).isoformat())

    series = []
    for ts in qends:
        cnt = 0
        cnt_del = 0
        for m in members:
            s, e = m['start'], m['end']
            if s and s <= ts and (e == PRESENT or e is None or e >= ts):
                cnt += 1
                if m['src'].startswith('delisted'):
                    cnt_del += 1
        series.append({"quarter_end": ts, "tradable": cnt, "delisted_tradable": cnt_del})

    out = {
        "generated_at": datetime.datetime.now().isoformat(),
        "sources": [
            "mairui get_all_stock_list",
            "akshare stock_info_sh_delist (SSE官网)",
            "akshare stock_info_sz_delist (SZSE官网)",
            "akshare stock_info_sh_name_code (SSE官网)",
            "akshare stock_info_sz_name_code (SZSE官网)",
        ],
        "members": members,
        "stats": {
            "current": n_cur,
            "delisted_sh": n_sh,
            "delisted_sz": n_sz,
            "total": len(members),
            "current_with_listing_date": n_cur_dated,
        },
        "quarterly_tradable_series": series,
    }
    outpath = os.path.join(BASE, "data", "universe_pit_membership.json")
    with open(outpath, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"[DONE] wrote {outpath}")
    print(f"[stats] {out['stats']}")
    print(f"[series head] {series[:3]}")
    print(f"[series tail] {series[-3:]}")


if __name__ == "__main__":
    main()
