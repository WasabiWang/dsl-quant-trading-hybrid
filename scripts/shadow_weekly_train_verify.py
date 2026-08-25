#!/usr/bin/env python3
"""
v4.7.0 P1-5: 影子池+观察池 周日先训后验

背景: 影子候选/观察标的缺乏模型或模型过期 → 精度记录全0 → 入池/回池通道断裂
方案: 每周日 先定向训练候选模型(train_predictor_enhanced) → 再记录预测精度(shadow_pool_verify)

用法:
  python3 scripts/shadow_weekly_train_verify.py [--dry-run]

输出:
  - models/<code>/ 更新模型
  - reports/predictor/prediction_enhanced_*.json (观察覆盖专用报告)
  - cache/shadow_pool_state.json 精度样本
"""
import os, sys, time, yaml, json
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SHADOW_PATH = PROJECT_ROOT / "config" / "shadow_pool.yaml"
OBS_PATH = PROJECT_ROOT / "config" / "observation_pool.yaml"
MODEL_ROOT = PROJECT_ROOT / "models"
STALE_DAYS = 14  # 模型超过14天视为过期需重训


def _load_targets() -> list:
    """读取影子候选+观察池标的, 返回 [{symbol, name, sector, pool}]"""
    out = []
    if SHADOW_PATH.exists():
        cfg = yaml.safe_load(open(SHADOW_PATH, encoding="utf-8")) or {}
        for s in cfg.get("candidates", []):
            sym = str(s.get("symbol", ""))
            if sym:
                out.append({"symbol": sym, "name": s.get("name", sym),
                            "sector": s.get("sector", ""), "pool": "shadow"})
    if OBS_PATH.exists():
        cfg = yaml.safe_load(open(OBS_PATH, encoding="utf-8")) or {}
        for s in cfg.get("observation_pool", []):
            sym = str(s.get("symbol", ""))
            if sym:
                out.append({"symbol": sym, "name": s.get("name", sym),
                            "sector": s.get("sector", ""), "pool": "observation"})
    # 去重
    seen, uniq = set(), []
    for s in out:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            uniq.append(s)
    return uniq


def _model_stale(code: str) -> bool:
    """模型缺失或超过STALE_DAYS未更新"""
    model_file = MODEL_ROOT / code / "lightgbm_5d.pkl"
    if not model_file.exists():
        return True
    age = time.time() - model_file.stat().st_mtime
    return age > STALE_DAYS * 24 * 3600


def main(dry_run: bool = False):
    targets = _load_targets()
    print(f"🔭 周日观察训练: {len(targets)}只 (影子{sum(1 for t in targets if t['pool']=='shadow')} + 观察{sum(1 for t in targets if t['pool']=='observation')})")
    to_train = [t for t in targets if _model_stale(t["symbol"])]
    fresh = [t for t in targets if not _model_stale(t["symbol"])]
    print(f"   需训练: {len(to_train)}只 | 模型新鲜跳过: {len(fresh)}只")
    if dry_run:
        print(f"   [DRY-RUN] 将训练: {[t['symbol'] for t in to_train]}")
        return 0

    if to_train:
        from scripts.train_predictor_enhanced import train_single_stock
        ok = fail = 0
        for t in to_train:
            try:
                r = train_single_stock(t["symbol"], t["name"])
                if r and "error" not in r:
                    h5d = r.get("h5d", {})
                    acc = h5d.get("direction_accuracy", 0) if isinstance(h5d, dict) else 0
                    print(f"  ✅ {t['symbol']} {t['name']}: 训练完成 5d精度={acc:.1%}")
                    ok += 1
                else:
                    print(f"  ❌ {t['symbol']} {t['name']}: {r.get('error', '未知错误') if isinstance(r, dict) else r}")
                    fail += 1
            except Exception as e:
                print(f"  ❌ {t['symbol']} {t['name']}: {e}")
                fail += 1
            time.sleep(0.3)
        print(f"\n📊 训练汇总: {ok}成功/{fail}失败")
    else:
        print("   全部模型新鲜, 跳过训练")

    # 第二步: 记录精度 (shadow_pool_verify predict模式)
    print("\n🔎 记录预测精度:")
    try:
        from scripts.shadow_pool_verify import main as shadow_main
        rc = shadow_main(evaluate=False)
        if rc != 0:
            print(f"⚠️ shadow_pool_verify 返回码 {rc}")
    except Exception as e:
        print(f"⚠️ shadow_pool_verify 调用失败: {e}")

    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="影子池+观察池周日先训后验")
    parser.add_argument("--dry-run", action="store_true", help="只预览不训练")
    args = parser.parse_args()
    sys.exit(main(dry_run=args.dry_run))
