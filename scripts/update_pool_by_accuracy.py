#!/usr/bin/env python3
"""
v4.6.9f P2-1: 股票池精度联动 — 基于校准精度动态调整 tier

分层逻辑 (与 master_pool.yaml 头部注释一致):
  alpha ≥ 60%  (高置信度交易标的)
  core  ≥ 55%  (正常交易标的)
  bench ≥ 50%  (观察标的)
  <50%  → 标记 degraded (降级观察, 不物理删除)

用法:
  python3 scripts/update_pool_by_accuracy.py [--dry-run]

输出:
  - 更新 master_pool.yaml 的 tier 字段
  - 打印升降级明细
"""
import os, sys, json, yaml
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

POOL_PATH = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
CALIB_PATH = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"

# 分层阈值
TIER_THRESHOLDS = [("alpha", 0.60), ("core", 0.55), ("bench", 0.50)]

# v4.6.9h P1: 行业配额 — 单行业上限25% (29只→≤7只)
# 电子行业当前9只(31%)超限, 池刷新时超额行业的最低精度标的中, degraded优先移出观察
MAX_SECTOR_RATIO = 0.25


def sector_quota_check(stocks: list) -> list:
    """行业配额检查: 单行业占比>25%时, 超额标的中degraded/低精度优先移出
    返回需要标记的symbol集合
    """
    from collections import Counter
    sec_count = Counter(s.get("sector", "?") for s in stocks)
    max_allowed = max(1, int(len(stocks) * MAX_SECTOR_RATIO))
    out = set()
    for sec, cnt in sec_count.items():
        if cnt <= max_allowed:
            continue
        # 该行业超额 → 移出低精度标的(degraded优先移出, 精度升序)
        # 排序key: degraded的排末尾(优先被移出), 非degraded精度高的排前面(优先保留)
        sec_stocks = [s for s in stocks if s.get("sector") == sec]
        sec_stocks.sort(key=lambda x: (1 if x.get("degraded") else 0, -x.get("accuracy", 0)))
        # 保留精度最高的 max_allowed 只, 其余移出
        for s in sec_stocks[max_allowed:]:
            out.add(s["symbol"])
    return out


def load_calibration_accuracy() -> dict:
    """从校准文件读取每只股票的最新精度"""
    if not CALIB_PATH.exists():
        print(f"❌ 校准文件不存在: {CALIB_PATH}")
        return {}
    with open(CALIB_PATH, "r", encoding="utf-8") as f:
        calib = json.load(f)
    stock_acc = calib.get("stock_accuracy", {})
    result = {}
    for sym, data in stock_acc.items():
        if isinstance(data, dict):
            acc = data.get("last_accuracy", 0) or 0
            # 默认精度0.5视为未训练, 不参与降级
            result[sym] = {"accuracy": acc, "name": data.get("name", sym)}
    return result


def tier_for_accuracy(acc: float) -> str:
    """按精度确定目标tier"""
    for tier, threshold in TIER_THRESHOLDS:
        if acc >= threshold:
            return tier
    return "bench"  # <50% 保留bench但标记degraded


def main(dry_run: bool = False):
    if not POOL_PATH.exists():
        print(f"❌ 股票池不存在: {POOL_PATH}")
        return 1

    with open(POOL_PATH, "r", encoding="utf-8") as f:
        pool = yaml.safe_load(f)

    acc_map = load_calibration_accuracy()
    if not acc_map:
        print("❌ 无校准数据, 跳过")
        return 1

    stocks = pool.get("master_pool", [])
    changes = []
    for s in stocks:
        sym = s.get("symbol", "")
        info = acc_map.get(sym)
        if not info:
            continue
        acc = info["accuracy"]
        old_tier = s.get("tier", "bench")
        new_tier = tier_for_accuracy(acc)

        # <50% → 标记 degraded; <48% 且已 degraded 过的 → remove_candidate (池外观察)
        degraded = acc < 0.50
        old_degraded = s.get("degraded", False)
        # v4.6.9h P2: bench瘦身 — 精度<48%且已degraded ≥1轮 → 标记remove_candidate
        # 说明: 该标的历史精度持续低位, 保留在池内只占资源; 移出池但保留观察
        remove_candidate = degraded and acc < 0.48 and (old_degraded or s.get("pool_updated_at"))
        old_remove = s.get("remove_candidate", False)

        if new_tier != old_tier or degraded != old_degraded or remove_candidate != old_remove:
            changes.append({
                "symbol": sym, "name": info["name"],
                "old_tier": old_tier, "new_tier": new_tier,
                "accuracy": round(acc, 4),
                "degraded": degraded,
                "remove_candidate": remove_candidate,
            })
            if not dry_run:
                s["tier"] = new_tier
                s["degraded"] = degraded
                s["remove_candidate"] = remove_candidate
                s["accuracy"] = round(acc, 4)
                s["pool_updated_at"] = datetime.now().strftime("%Y-%m-%d")

    if dry_run:
        print(f"🔍 [DRY-RUN] 检测到 {len(changes)} 处变化:")
    else:
        # v4.6.9h P1: 行业配额检查 — 电子>25%时超额标的中degraded优先标记
        try:
            _quota_out = sector_quota_check(stocks)
            if _quota_out:
                _qnames = ", ".join(
                    f'{s["symbol"]}({s.get("name","")})' for s in stocks if s["symbol"] in _quota_out)
                print(f"🏷️ 行业超配额({MAX_SECTOR_RATIO:.0%}): {len(_quota_out)}只标记观察 {_qnames}")
                for s in stocks:
                    if s["symbol"] in _quota_out:
                        s["out_of_quota"] = True
        except Exception as _qe:
            print(f"⚠️ 行业配额检查失败: {_qe}")
        # 备份 + 写回
        backup = POOL_PATH.with_suffix(".yaml.bak.poolacc")
        import shutil
        shutil.copy2(POOL_PATH, backup)
        with open(POOL_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(pool, f, allow_unicode=True, sort_keys=False)
        print(f"✅ 已更新 {len(changes)} 处 (备份: {backup.name})")

    for c in changes:
        arrow = "⬆️" if c["new_tier"] != c["old_tier"] and c["new_tier"] in ("alpha", "core") else ("⬇️" if c["new_tier"] == "bench" and c["old_tier"] in ("alpha", "core") else "➡️")
        deg = " [⚠️精度<50%]" if c["degraded"] else ""
        print(f"  {arrow} {c['symbol']} {c['name']}: {c['old_tier']}→{c['new_tier']} (acc={c['accuracy']:.1%}){deg}")

    # 汇总
    if not dry_run:
        tiers = {}
        for s in stocks:
            t = s.get("tier", "?")
            tiers[t] = tiers.get(t, 0) + 1
        print(f"\n📊 池分布: {tiers}")
        degraded_count = sum(1 for s in stocks if s.get("degraded"))
        print(f"⚠️ degraded标的: {degraded_count}只")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="股票池精度联动")
    parser.add_argument("--dry-run", action="store_true", help="只预览不写回")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
