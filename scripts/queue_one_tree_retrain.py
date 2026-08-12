#!/usr/bin/env python3
"""
queue_one_tree_retrain.py — DSL v4.5.12 P2-13
将仅含1棵树的模型(无pool fallback)加入重训队列
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml
from core.retrain_queue_manager import get_retrain_queue_manager

# 加载股票池
with open("config/master_stock_pool.yaml") as f:
    pool = yaml.safe_load(f)

stock_map = {s["symbol"]: s.get("name", s["symbol"]) for s in pool["master_pool"]}

# 识别1-tree无fallback的标的
one_tree = []
for sym, name in stock_map.items():
    model_path = f"models/{sym}/lightgbm.pkl"
    if not os.path.exists(model_path):
        continue
    sz = os.path.getsize(model_path)
    if sz > 20000:
        continue  # 正常模型
    # 检查有无pool fallback
    pool_has = any(f.startswith(sym) for f in os.listdir("models/pool/") if os.path.isfile(f"models/pool/{f}"))
    if not pool_has:
        one_tree.append((sym, name))

print(f"将{len(one_tree)}只1-tree模型加入重训队列...")

mgr = get_retrain_queue_manager()
queued = 0
skipped = 0
for sym, name in one_tree:
    ok = mgr.queue_for_retrain(sym, name, priority="high",
                                reason=f"1-tree模型({os.path.getsize(f'models/{sym}/lightgbm.pkl')}B), 无pool fallback")
    if ok:
        print(f"  ✅ {sym} {name}")
        queued += 1
    else:
        print(f"  ⏭️ {sym} {name} (已在队列中)")
        skipped += 1

print(f"\n完成: 新入队{queued}只, 已存在{skipped}只")
print("batch_train.py将在下次调度(工作日02:40)消费队列。")
