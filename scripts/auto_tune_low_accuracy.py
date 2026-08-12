#!/usr/bin/env python3
"""
低精度标的自动参数寻优 — v4.5.5 S6
针对 calibration critical (acc<45%) 标的，快速搜索最优参数组合
"""
import os, sys, json, yaml, copy
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

# 低精度标的(2026-05-07)
TUNE_TARGETS = ["603799","002415","603259","300390","002466","300014","002709","600487"]

# 搜索空间: 常用参数组合(避免组合爆炸)
PARAM_SETS = [
    {"n_estimators": 300, "learning_rate": 0.03, "max_depth": 5,  "lookback": 60,  "reg_alpha": 0.5,  "reg_lambda": 0.5,  "min_child_samples": 20},  # default
    {"n_estimators": 500, "learning_rate": 0.05, "max_depth": 7,  "lookback": 90,  "reg_alpha": 0.1,  "reg_lambda": 0.1,  "min_child_samples": 10},  # more trees, deeper
    {"n_estimators": 200, "learning_rate": 0.01, "max_depth": 3,  "lookback": 120, "reg_alpha": 1.0,  "reg_lambda": 1.0,  "min_child_samples": 30},  # conservative
    {"n_estimators": 800, "learning_rate": 0.1,  "max_depth": 10, "lookback": 45,  "reg_alpha": 0.0,  "reg_lambda": 0.0,  "min_child_samples": 10},  # aggressive
    {"n_estimators": 300, "learning_rate": 0.03, "max_depth": 5,  "lookback": 60,  "reg_alpha": 0.5,  "reg_lambda": 0.5,  "min_child_samples": 20},  # re-run default
]

def load_stock_info(code):
    pool_path = PROJECT_ROOT / "config" / "master_stock_pool.yaml"
    with open(pool_path) as f:
        pool = yaml.safe_load(f)
    for s in pool.get("master_pool", []):
        if s["symbol"] == code:
            return s
    return {"symbol": code, "name": code, "tier": "core"}

def train_stock(code, name, params):
    """用指定参数训练单只股票"""
    from scripts.batch_train import train_one
    # 临时修改全局参数(模块级全局变量在train_one中读取)
    import scripts.train_predictor_v3 as tp
    orig = {}
    overrides = {}
    for k, v in params.items():
        attr = f"{k.upper()}_DEFAULT"
        if hasattr(tp, attr):
            orig[k] = getattr(tp, attr)
            setattr(tp, attr, v)
            overrides[k] = v
        elif hasattr(tp, k.upper()):
            attr2 = k.upper()
            orig[k] = getattr(tp, attr2)
            setattr(tp, attr2, v)
            overrides[k] = v
    
    try:
        result = train_one(code, name, overrides)
        return result.get("direction_accuracy", 0)
    finally:
        # restore
        for k, v in orig.items():
            attr = f"{k.upper()}_DEFAULT"
            if hasattr(tp, attr):
                setattr(tp, attr, v)

def main():
    print("="*70)
    print(f"⚡ 低精度标的自动参数寻优 — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"   标的: {', '.join(TUNE_TARGETS)}")
    print(f"   参数集: {len(PARAM_SETS)}种组合")
    print("="*70)

    results = {}
    for code in TUNE_TARGETS:
        info = load_stock_info(code)
        name = info.get("name", code)
        print(f"\n{'─'*50}")
        print(f"📊 {code} {name}")

        best_acc = 0
        best_params = None
        
        for i, params in enumerate(PARAM_SETS):
            print(f"  ▶ 参数集{i+1}: n_est={params['n_estimators']} lr={params['learning_rate']} "
                  f"depth={params['max_depth']} look={params['lookback']}", end=" ")
            
            try:
                acc = train_stock(code, name, params)
                print(f"→ acc={acc:.1%}")
                if acc > best_acc:
                    best_acc = acc
                    best_params = params
            except Exception as e:
                print(f"→ ❌ {str(e)[:50]}")
                continue
        
        improved = best_acc >= 0.45
        status = "✅ 达标" if improved else "❌ 仍低"
        print(f"  {'='*40}")
        print(f"  {status} 最佳acc={best_acc:.1%}")
        if best_params:
            print(f"  最佳参数: {best_params}")
        results[code] = {
            "symbol": code, "name": name,
            "best_accuracy": round(best_acc, 4),
            "improved": improved,
            "best_params": best_params,
        }

    print(f"\n{'='*70}")
    improved_count = sum(1 for r in results.values() if r["improved"])
    print(f"📊 调优完成: {improved_count}/{len(results)} 达标(>45%)")
    for r in results.values():
        emoji = "✅" if r["improved"] else "❌"
        print(f"  {emoji} {r['symbol']} {r.get('name','')}: {r['best_accuracy']:.1%}")

    # 保存结果
    out = PROJECT_ROOT / "cache" / "auto_tune_results.json"
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n📄 结果已保存: {out}")

if __name__ == "__main__":
    main()
