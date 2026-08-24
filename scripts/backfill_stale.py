#!/usr/bin/env python3
"""补缺同步 — 为本地数据缺失最近交易日的股票回填 (不写 sync_state, 不干扰晚间重试)
用法: backfill_stale.py [codes_file]  (每行一个代码, 默认 /tmp/stale_codes.txt)
"""
import sys, os, time
from pathlib import Path
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

codes_file = sys.argv[1] if len(sys.argv) > 1 else "/tmp/stale_codes.txt"
codes = [l.strip() for l in open(codes_file) if l.strip()]
print(f"📥 补缺 {len(codes)} 只股票 (仅写 parquet + manifest, 不更新 sync_state)")

from data_db.sync import _sync_single_stock
ok = skip = err = 0
t0 = time.time()
for i, c in enumerate(codes, 1):
    r = _sync_single_stock(c)
    if r["status"] == "ok":
        ok += 1
        print(f"  [{i}/{len(codes)}] ✅ {c}: +{r['rows']}行")
    elif r["status"] == "skip":
        skip += 1
    else:
        err += 1
        print(f"  [{i}/{len(codes)}] ❌ {c}: {r['msg']}")
    if i % 50 == 0:
        print(f"  ...进度 {i}/{len(codes)} ({time.time()-t0:.0f}s, ok={ok} err={err})")

print(f"\n📊 补缺完成: 成功{ok} / 跳过{skip} / 失败{err} / 共{len(codes)} ({time.time()-t0:.0f}s)")
