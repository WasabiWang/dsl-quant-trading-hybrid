#!/usr/bin/env python3
"""
v4.6.9h P3: 候选池 Shadow 验证 — 低相关行业候选标的精度验证

用途: 为股票池引入低相关行业标的(降低电子集中度), 先shadow预测2周(不交易),
      精度≥55% 才允许正式入池

候选来源: 配置 config/shadow_pool.yaml
验证流程:
  1. 每周对候选标的跑预测(只记录, 不交易)
  2. 2周后评估: 精度≥55% → 标记 eligible_for_pool
  3. 与 remove_candidate 标的交换

用法:
  python3 scripts/shadow_pool_verify.py [--evaluate]
"""
import os, sys, json, yaml
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SHADOW_CFG = PROJECT_ROOT / "config" / "shadow_pool.yaml"
SHADOW_STATE = PROJECT_ROOT / "cache" / "shadow_pool_state.json"

MIN_ACC_TO_ENTER = 0.55   # 2周精度≥55%才可入池
SHADOW_WEEKS = 2          # shadow周期


def load_shadow_config() -> dict:
    if not SHADOW_CFG.exists():
        print(f"❌ 候选池配置不存在: {SHADOW_CFG}")
        return {}
    with open(SHADOW_CFG, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> dict:
    if SHADOW_STATE.exists():
        with open(SHADOW_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"candidates": {}}


def save_state(state: dict):
    SHADOW_STATE.parent.mkdir(parents=True, exist_ok=True)
    with open(SHADOW_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main(evaluate: bool = False):
    cfg = load_shadow_config()
    candidates = cfg.get("candidates", [])
    if not candidates:
        print("ℹ️ 候选池为空 — 在 config/shadow_pool.yaml 添加候选标的")
        return 0

    state = load_state()
    now = datetime.now()

    if not evaluate:
        print(f"🔍 Shadow 预测模式: {len(candidates)}只候选 (仅记录, 不交易)")
        # 每次运行: 记录候选标的的当前精度
        # 来源优先级: 1) prediction_enhanced 最新报告(定向训练产出) 2) calibration
        calib = {}
        calib_path = PROJECT_ROOT / "confidence_data" / "prediction_calibration.json"
        if calib_path.exists():
            with open(calib_path, "r", encoding="utf-8") as f:
                _c = json.load(f)
            calib = {k: v.get("last_accuracy", 0) for k, v in _c.get("stock_accuracy", {}).items()
                     if isinstance(v, dict)}
        # v4.6.9h: 从 prediction_enhanced 报告补充(候选不在calibration, 训练产出在报告)
        pred_dir = PROJECT_ROOT / "reports" / "predictor"
        if pred_dir.exists():
            _files = sorted(pred_dir.glob("prediction_enhanced_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
            for _f in _files[:3]:  # 最近3份报告
                try:
                    with open(_f, "r", encoding="utf-8") as _rf:
                        _rep = json.load(_rf)
                    for _code, _info in _rep.items():
                        _da = _info.get("direction_accuracy") if isinstance(_info, dict) else None
                        if _da:
                            calib[_code] = float(_da)
                except Exception:
                    pass
                # 3份报告取最新即可
                if all(c in calib for c in [x.get("symbol", "") for x in candidates]):
                    break
        for cand in candidates:
            code = cand.get("symbol", "")
            acc = calib.get(code, 0)
            entry = state["candidates"].setdefault(code, {
                "symbol": code, "name": cand.get("name", code),
                "started_at": now.isoformat(), "samples": [],
            })
            entry["samples"].append({
                "date": now.strftime("%Y-%m-%d"),
                "accuracy": round(acc, 4),
            })
            # 只保留最近 samples
            entry["samples"] = entry["samples"][-30:]
            print(f"  📊 {code} {cand.get('name', code)}: acc={acc:.1%} 样本{len(entry['samples'])}")
        save_state(state)
        print(f"\n✅ Shadow状态已记录 → {SHADOW_STATE}")
        print(f"   2周后运行: python3 scripts/shadow_pool_verify.py --evaluate")
        return 0

    # evaluate 模式: 评估是否达准入精度
    print(f"🔎 Shadow 评估模式 (周期{SHADOW_WEEKS}周, 需精度≥{MIN_ACC_TO_ENTER:.0%})")
    eligible = []
    for code, entry in state.get("candidates", {}).items():
        samples = entry.get("samples", [])
        if len(samples) < 2:
            print(f"  ⏳ {code} {entry.get('name', code)}: 样本不足({len(samples)})")
            continue
        accs = [s["accuracy"] for s in samples if s["accuracy"] > 0]
        if not accs:
            continue
        avg = sum(accs) / len(accs)
        last = accs[-1]
        status = "✅可入池" if last >= MIN_ACC_TO_ENTER else "⏳继续观察"
        print(f"  {status} {code} {entry.get('name', code)}: 均值{avg:.1%} 最新{last:.1%} (样本{len(accs)})")
        if last >= MIN_ACC_TO_ENTER:
            eligible.append({"symbol": code, "name": entry.get("name", code), "accuracy": round(last, 4)})

    if eligible:
        print(f"\n🎯 可入池 {len(eligible)}只:")
        for e in eligible:
            print(f"  → {e['symbol']} {e['name']} acc={e['accuracy']:.1%}")
        print("  需手动将标的加入 master_stock_pool.yaml, 并评估是否替换 remove_candidate 标的")
    else:
        print(f"\nℹ️ 暂无达标候选 — 继续shadow观察")
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="候选池Shadow验证")
    parser.add_argument("--evaluate", action="store_true", help="评估模式(2周后)")
    args = parser.parse_args()
    sys.exit(main(evaluate=args.evaluate))
