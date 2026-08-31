#!/usr/bin/env python3
"""v4.7.3 P2c: 集成权重滚动快照 (移植 QuantMind refresh_ensemble_weights 思路)

背景:
  train_predictor_v3 的集成权重来自"当日验证集精度"单一样本 → 单日噪声大,
  权重天天跳变。本脚本聚合最近 N 份增强报告 (reports/predictor/) 中每只标的的
  子模型精度 (lgb/xgb/clf/cb accuracy), 指数衰减加权 (exp(-d/10), 最近权重最大),
  产出滚动权重快照 → train_predictor_v3 训练时优先消费 (14天内+≥5份样本)。

与 QuantMind 差异 (透明标注):
  QuantMind 用"生产 Rank IC"回填动态权重; DSL 逐股时间序列无生产子模型
  预测记录 → 用滚动验证精度代理 (模型每日重训, 验证窗口每日滑动, 已包含
  最新市场数据)。生产 realized 级别的子模型跟踪为后续升级项。

用法:
  .venv/bin/python3 scripts/refresh_ensemble_weights.py [--days 30] [--dry-run]
输出: reports/predictor/weight_snapshots/{code}.json
"""
import os, sys, json, argparse, glob, math
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports", "predictor")
SNAP_DIR = os.path.join(REPORTS_DIR, "weight_snapshots")
DECAY = 10.0            # 指数衰减半衰期 (天): w = exp(-d/10)
MIN_REPORTS = 5         # 最少报告数, 不足则不生成快照
SUB_MODELS = ("lgb", "xgb", "clf", "cb")
ACC_KEYS = {"lgb": "lgb_accuracy", "xgb": "xgb_accuracy",
            "clf": "clf_accuracy", "cb": "cb_accuracy"}


def _load_report(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def collect_report_series(days: int):
    """返回 {symbol: [(mtime, acc_map)]}, 按时间升序。"""
    files = glob.glob(os.path.join(REPORTS_DIR, "*.json"))
    cutoff = datetime.now().timestamp() - days * 86400
    series = {}
    for fp in files:
        try:
            mtime = os.path.getmtime(fp)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        data = _load_report(fp)
        if not isinstance(data, dict):
            continue
        for sym, v in data.items():
            if not isinstance(v, dict):
                continue
            h5d = v.get("h5d") or {}
            accs = {}
            ok = False
            for k, key in ACC_KEYS.items():
                a = h5d.get(key)
                if isinstance(a, (int, float)) and 0.0 <= a <= 1.0:
                    accs[k] = float(a)
                    ok = True
            if ok:
                series.setdefault(sym, []).append((mtime, accs))
    for sym in series:
        series[sym].sort(key=lambda x: x[0])
    return series


def exp_decay_weights(series: list, now: float):
    """指数衰减加权均值 → 归一化权重。{lgb:.., xgb:.., clf:.., cb:.., n_reports}"""
    acc_sum = {k: 0.0 for k in SUB_MODELS}
    w_sum = {k: 0.0 for k in SUB_MODELS}
    for mtime, accs in series:
        days_ago = max(0.0, (now - mtime) / 86400)
        w = math.exp(-days_ago / DECAY)
        for k in SUB_MODELS:
            if k in accs:
                acc_sum[k] += accs[k] * w
                w_sum[k] += w
    raw = {}
    for k in SUB_MODELS:
        if w_sum[k] > 0:
            # 有效精度: <0.5 → 反转仍有信息量 (与训练端 eff_acc 口径一致)
            a = acc_sum[k] / w_sum[k]
            raw[k] = max(a, 1 - a)
    total = sum(raw.values())
    if total <= 0:
        return None
    return {k: raw.get(k, 0.0) / total for k in SUB_MODELS}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    series_map = collect_report_series(args.days)
    print(f"报告窗口 {args.days} 天: 覆盖 {len(series_map)} 只标的")
    now = datetime.now().timestamp()

    written = 0
    for sym, series in sorted(series_map.items()):
        if len(series) < MIN_REPORTS:
            continue
        weights = exp_decay_weights(series, now)
        if weights is None:
            continue
        snap = {
            "updated_at": datetime.now().isoformat(),
            "weights": {k: round(v, 4) for k, v in weights.items()},
            "n_reports": len(series),
            "decay_days": DECAY,
            "source": "rolling_val_accuracy",
        }
        if not args.dry_run:
            os.makedirs(SNAP_DIR, exist_ok=True)
            tmp = os.path.join(SNAP_DIR, f"{sym}.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snap, f, indent=2, ensure_ascii=False)
            os.replace(tmp, os.path.join(SNAP_DIR, f"{sym}.json"))
        written += 1
        w = snap["weights"]
        print(f"  {sym}: lgb={w['lgb']:.3f} xgb={w['xgb']:.3f} clf={w['clf']:.3f} cb={w['cb']:.3f} (n={len(series)})")

    print(f"\n快照: {written} 只标的" + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
