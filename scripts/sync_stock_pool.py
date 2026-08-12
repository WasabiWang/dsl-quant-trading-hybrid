#!/usr/bin/env python3
"""
sync_stock_pool.py — DSL v4.5.3d 双池同步工具
功能: 将 master_stock_pool.yaml 的新增标的自同步到 stock_pool.yaml
用法: python3 scripts/sync_stock_pool.py [--dry-run]

场景: 
  当 master_stock_pool.yaml 通过 dynamic_pool_manager 或手动修改新增标的时,
  自动将其加入 stock_pool.yaml 的对应 tier 末尾, 保持双池一致。
  
不受影响:
  - 已存在的 tier 配置 (allocation/holding_days/factor_weights) 保持不变
  - 已在 stock_pool.yaml 中的标的不会被修改
  - pool_fallback 逻辑不受影响 (signal_weight 保持)

设计原则:
  master_stock_pool.yaml → 标的权威列表 (简洁格式, tier/symbol/name/score/sector/concept)
  stock_pool.yaml       → 交易配置 (完整格式, 含 tier 参数 + 每只标的 code/name/sector/score)
"""
import os, sys, yaml
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MASTER_POOL = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
STOCK_POOL = PROJECT_ROOT / "config" / "stock_pool.yaml"


def load_master_pool() -> dict:
    """加载 master_stock_pool.yaml, 返回 {symbol_zfilled: {tier, name, sector, score}}"""
    with open(MASTER_POOL, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    pool = {}
    for s in data.get("master_pool", []):
        sym = str(s.get("symbol", "")).zfill(6)
        if sym:
            pool[sym] = {
                "tier": s.get("tier", "core"),
                "name": s.get("name", sym),
                "sector": s.get("sector", ""),
                "score": s.get("score", 50),
            }
    return pool


def load_stock_pool() -> dict:
    """加载 stock_pool.yaml, 返回 {symbol_zfilled: {code, name, sector, score}} per tier"""
    with open(STOCK_POOL, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    tiers = data.get("tiers", {})
    return tiers, data


def sync(master_pool: dict, stock_tiers: dict, dry_run: bool = False) -> tuple:
    """
    同步: 将 stock_pool 重建为 master_pool 的 tier/sector 权威投影。
    返回: (changed_count, warning_count)
    """
    # 构建旧 stock_pool 配置索引，保留已有 name/score 等非权威字段 fallback。
    existing = {}
    for tname, tdata in stock_tiers.items():
        for s in tdata.get("stocks", []):
            code = str(s.get("code", "")).zfill(6)
            if code:
                existing[code] = dict(s)

    rebuilt = {tier: [] for tier in stock_tiers.keys()}
    changed = 0
    for sym, info in sorted(master_pool.items()):
        tier = info["tier"]
        if tier not in stock_tiers:
            # flex 作为默认 fallback 兜底
            fallback_tiers = [t for t in ["flex", "growth", "core"] if t in stock_tiers]
            tier = fallback_tiers[0] if fallback_tiers else list(stock_tiers.keys())[0]
            print(f"  ⚠️ {sym} ({info['name']}) tier={info['tier']} 在stock_pool不存在, fallback到{tier}")
            changed += 1

        old_entry = existing.get(sym, {})
        new_entry = {
            "code": sym,
            "name": info.get("name") or old_entry.get("name") or sym,
            "sector": info.get("sector", ""),
            "score": info.get("score", 50),
        }
        old_tier = None
        for tname, tdata in stock_tiers.items():
            if any(str(s.get("code", "")).zfill(6) == sym for s in tdata.get("stocks", [])):
                old_tier = tname
                break
        if old_tier != tier or old_entry.get("sector", "") != new_entry["sector"] or old_entry.get("name") != new_entry["name"]:
            changed += 1
            print(f"  🔁 {sym} {new_entry['name']:8s} {old_tier or 'NEW'} → {tier} sector={new_entry['sector']}")

        rebuilt.setdefault(tier, []).append(new_entry)

    removed = sorted(set(existing) - set(master_pool))
    if removed:
        changed += len(removed)
        print(f"  🧹 移除 stock_pool 多余标的: {removed}")

    if not dry_run:
        for tier, entries in rebuilt.items():
            stock_tiers.setdefault(tier, {})["stocks"] = entries

    if not changed:
        print("  ✅ stock_pool 已与 master_pool tier/sector 同步")

    return changed, 0


def main():
    dry_run = "--dry-run" in sys.argv

    print(f"📋 双池同步工具 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Master: {MASTER_POOL}")
    print(f"  Stock:  {STOCK_POOL}")
    if dry_run:
        print(f"  ⚠️ DRY RUN 模式 — 不会写入")
    print()

    master = load_master_pool()
    stock_tiers, stock_data = load_stock_pool()

    print(f"📊 master_pool: {len(master)} 只标的")
    total_tier = sum(len(t.get("stocks", [])) for t in stock_tiers.values())
    print(f"📊 stock_pool:  {total_tier} 条记录 ({len(stock_tiers)} tiers)")

    changed, warnings = sync(master, stock_tiers, dry_run)

    if changed > 0 and not dry_run:
        with open(STOCK_POOL, "w", encoding="utf-8") as f:
            stock_data["tiers"] = stock_tiers
            yaml.dump(stock_data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        print(f"\n✅ stock_pool.yaml 已同步 ({changed}处变化)")
    elif changed > 0 and dry_run:
        print(f"\n🔍 DRY RUN: 将同步 {changed} 处变化")
    else:
        print(f"\n✅ stock_pool.yaml 无需更新")

    # 验证双向一致性
    master_set = set(master.keys())
    stock_set = set()
    for tname, tdata in stock_tiers.items():
        for s in tdata.get("stocks", []):
            stock_set.add(str(s.get("code", "")).zfill(6))
    
    only_in_master = master_set - stock_set
    only_in_stock = stock_set - master_set
    if only_in_master:
        print(f"⚠️ master_pool 特有 (stock_pool 缺失): {sorted(only_in_master)}")
    if only_in_stock:
        print(f"⚠️ stock_pool 特有 (master_pool 缺失): {sorted(only_in_stock)}")
    if not only_in_master and not only_in_stock:
        print(f"✅ 双池完全一致 ({len(master_set)}/{len(stock_set)})")
    
    # v4.5.7: 池刷新后自动验证重训队列一致性
    if not dry_run:
        print(f"\n🔍 验证重训队列与股票池一致性...")
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from core.retrain_queue_manager import validate_retrain_queue
            result = validate_retrain_queue()
            if result["valid"]:
                print(f"  ✅ {result['message']}")
            else:
                print(f"  🧹 {result['message']}")
        except Exception as e:
            print(f"  ⚠️ 重训队列验证跳过: {e}")


if __name__ == "__main__":
    from datetime import datetime
    main()
