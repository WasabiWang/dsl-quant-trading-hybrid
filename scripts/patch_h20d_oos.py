#!/usr/bin/env python3
"""
h20d OOS 缺评估标的补丁脚本
仅对17只缺失股票运行 walk-forward OOS 方向精度评估，合并到现有评估文件
"""
import os, sys, json, yaml, gc, time, shutil
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import numpy as np

os.environ["PYTHONWARNINGS"] = "ignore"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from backtest_walkforward_h20d_accuracy import evaluate_stock

# 需要补充的17只
MISSING_CODES = sorted([
    '000725', '002460', '002475', '300014', '300223', '300274',
    '300458', '300613', '301308', '600549', '600584', '603259',
    '603599', '603893', '603986', '688525', '688608'
])

# 加载名称映射
with open("config/master_stock_pool.yaml") as f:
    raw_pool = yaml.safe_load(f)["master_pool"]
NAMES = {s["symbol"]: s.get("name", s["symbol"]) for s in raw_pool}

# 读取现有评估文件
EVAL_DIR = os.path.join(PROJECT_ROOT, "reports", "h20d_evaluation")
existing_files = sorted([f for f in os.listdir(EVAL_DIR) if f.startswith("evaluation_h20d_oos_") and f.endswith(".json")])
if not existing_files:
    print("❌ 找不到现有评估文件")
    sys.exit(1)

existing = existing_files[-1]
existing_path = os.path.join(EVAL_DIR, existing)
with open(existing_path, "r") as f:
    base_data = json.load(f)

existing_stocks = set(base_data.get("per_stock", {}).keys())
print(f"📋 现有评估: {len(existing_stocks)}只 · 文件: {existing}")
print(f"🔧 需补充: {len(MISSING_CODES)}只")

# 并行评估缺失股票
MAX_WORKERS = 3
results_patch = []
errors_patch = []

t0 = time.time()
with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(evaluate_stock, code, NAMES.get(code, code)): code for code in MISSING_CODES}
    for fut in as_completed(futures):
        code = futures[fut]
        try:
            result = fut.result(timeout=900)  # 15分钟超时每只
            if result and result.get("direction_accuracy") is not None:
                results_patch.append({"code": code, **result})
                n_preds = result.get('total_predictions', 0)
                print(f"  ✅ {code} {NAMES.get(code,'')}: acc={result['direction_accuracy']:.4f} ({n_preds}预测)")
            else:
                errors_patch.append({"code": code, "error": "空结果"})
                print(f"  ❌ {code}: 空结果")
        except Exception as e:
            errors_patch.append({"code": code, "error": str(e)})
            print(f"  ❌ {code}: {str(e)[:60]}")

elapsed = time.time() - t0
print(f"\n⏱️ 耗时: {elapsed:.0f}s | 成功: {len(results_patch)} | 失败: {len(errors_patch)}")

# 合并到现有评估数据
base_per_stock = base_data.get("per_stock", {})
per_stock = dict(base_per_stock)
all_accs = list(base_data.get("all_accuracies", []))
h5d_accs = list(base_data.get("h5d_accuracies_for_comparison", []))

for r in results_patch:
    code = r["code"]
    per_stock[code] = {
        "direction_accuracy": r["direction_accuracy"],
        "oos_predictions": r.get("total_predictions", 0),
        "windows": r.get("windows", 0),
        "true_positive_rate": r.get("true_positive_rate", 0),
        "true_negative_rate": r.get("true_negative_rate", 0),
        "evaluated_at": datetime.now().isoformat()
    }
    all_accs.append(r["direction_accuracy"])

# 更新统计
accs = [v["direction_accuracy"] for v in per_stock.values() if isinstance(v, dict)]
summary = {
    "stocks_evaluated": len(per_stock),
    "stocks_failed": len(errors_patch),
    "total_oos_predictions": sum(v.get("oos_predictions", v.get("total_predictions", 0)) for v in per_stock.values() if isinstance(v, dict)),
    "mean_accuracy": round(float(np.mean(accs)), 4) if accs else 0,
    "median_accuracy": round(float(np.median(accs)), 4) if accs else 0,
    "std_accuracy": round(float(np.std(accs)), 4) if accs else 0,
    "min_accuracy": round(float(np.min(accs)), 4) if accs else 0,
    "max_accuracy": round(float(np.max(accs)), 4) if accs else 0,
    "stocks_above_50pct": sum(1 for a in accs if a > 0.5),
    "stocks_above_55pct": sum(1 for a in accs if a > 0.55),
    "stocks_above_60pct": sum(1 for a in accs if a > 0.6),
}

# 保存新文件
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
output = {
    "timestamp": timestamp,
    "horizon": 20,
    "method": "walk_forward_oos_patch",
    "per_stock": per_stock,
    "all_accuracies": sorted(all_accs, reverse=True),
    "summary": summary,
    "errors": errors_patch,
    "patched_stocks": [r["code"] for r in results_patch],
}
out_path = os.path.join(EVAL_DIR, f"evaluation_h20d_oos_{timestamp}.json")
with open(out_path, "w") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print(f"\n📂 已保存: {os.path.basename(out_path)}")
print(f"📊 合并后: {len(per_stock)}只")
print(f"   均值精度: {summary['mean_accuracy']:.2%}")
print(f"   >50%: {summary['stocks_above_50pct']}只")
print(f"   >55%: {summary['stocks_above_55pct']}只")
print(f"   >60%: {summary['stocks_above_60pct']}只")

# 输出补丁结果详情
print("\n=== 补丁结果 ===")
for code in MISSING_CODES:
    if code in per_stock:
        acc = per_stock[code]["direction_accuracy"]
        print(f"  {code} {NAMES.get(code,'')}: {acc:.2%}")
    else:
        err = next((e["error"] for e in errors_patch if e["code"]==code), "未找到")
        print(f"  {code} {NAMES.get(code,'')}: ❌ {err}")
