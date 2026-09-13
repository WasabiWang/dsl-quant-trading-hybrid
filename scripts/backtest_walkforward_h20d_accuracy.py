#!/usr/bin/env python3
"""
DSL H20D Walk-Forward OOS Direction Accuracy (评估路径 · 阶段1时点一致性修复版)
严格样本外方向精度统计 — 无交易模拟，无未来信息泄露

阶段1修复 (FX-1/FX-2/FX-3, 仅评估路径, 不碰生产路径):
- FX-2 复权统一: --adjust {hfq(新默认)/qfq(旧行为)} → 统一走 common/adjust.py 入口
- FX-1 时点宇宙: --universe {point_in_time(新默认)/snapshot(旧行为)/both}
- FX-3 基本面:   --fund {drop(新默认)/keep(旧行为)} — 剔除无 ann_date 的 fund_* 特征
所有旧行为均可通过开关复现, 不静默替换。

方法:
- 滚动窗口: 每90天一个窗口，用窗口前数据训练 LightGBM
- 对每个窗口的测试期，每天用模型预测未来20日收益方向
- 跨窗口汇总每只股票的 OOS direction_accuracy
"""
import os, sys, json, yaml, gc, time, argparse, threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
import numpy as np
import pandas as pd

os.environ["PYTHONWARNINGS"] = "ignore"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from dsl_data_sdk_original import normalize_symbol
from train_predictor_enhanced import build_features, fetch_fundamentals
from common.adjust import get_kline_adjusted, ADJUST_STANDARD
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb
import warnings
warnings.filterwarnings("ignore")

# ====== 参数 ======
HORIZON = 20
WINDOW_DAYS = 90          # 每季度切换窗口
MIN_TRAIN_SAMPLES = 200   # 每只至少 200 训练样本才训练
MIN_OOS_PREDICTIONS = 50  # 每只至少 50 次 OOS 预测才计入
TIMEOUT_GLOBAL = 1200     # 全局超时秒数
MAX_WORKERS = 3           # 并行线程数

# ====== FX-1b (阶段1-B 步骤4): 规则化时点宇宙 ======
PIT_PATH = os.path.join(PROJECT_ROOT, "data", "universe_pit.json")
PIT_MEMBERSHIP = None     # {code: [(start,end),...]}; point_in_time 模式下启用
# 显著性检验用: 逐笔预测落盘 (--dump-predictions 时启用)
PRED_DUMP = None          # {code: [[date, correct01], ...]}
PRED_LOCK = threading.Lock()

# ====== 行情取数源: 麦蕊拿不到退市股 ⇒ 回落到本地 PIT 缓存 ======
KLINES_DIR = os.path.join(PROJECT_ROOT, "data", "universe_klines")
KLINE_SOURCE = "auto"     # mairui | local | auto(默认: 麦蕊优先, 不足则用本地)
KLINE_SKIP_ADJUST_MISMATCH = False   # True = 跳过与 --adjust 口径不符的本地文件


def load_kline_local(code: str, start: str, end: str):
    """从 PIT 行情缓存 data/universe_klines/{code}.parquet 读 K 线(含退市股)。
    列结构与 get_kline_adjusted 一致, 可直接替换。"""
    p = os.path.join(KLINES_DIR, f"{code}.parquet")
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
    except Exception:
        return None
    if df is None or len(df) == 0 or "date" not in df.columns:
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
    return df.reset_index(drop=True) if len(df) else None


def _load_kline(code: str, start: str, end: str, adjust: str):
    """返回 (kdf, source_tag)。麦蕊不足 350 行 → 回落本地 PIT 缓存。"""
    if KLINE_SOURCE in ("mairui", "auto"):
        try:
            k = get_kline_adjusted(code, start, end, adjust=adjust)
            if k is not None and len(k) >= 350:
                return k, "mairui"
        except Exception:
            pass
        if KLINE_SOURCE == "mairui":
            return None, "mairui"
    k = load_kline_local(code, start, end)
    if k is None or len(k) < 350:
        return None, "local"
    tag = str(k["adjust"].iloc[0]) if "adjust" in k.columns else "unknown"
    if KLINE_SKIP_ADJUST_MISMATCH and adjust == "hfq" and tag.startswith("raw"):
        return None, f"local:{tag}(skip:mismatch)"
    return k, f"local:{tag}"
# ================================

# ====== FX-1: 池成分时间线 (从可得快照重建) ======
# 快照来源 (文件 mtime) + 每股 pool_updated_at 字段。
# 关键事实: master_stock_pool 系列快照最早只到 2026-08-25 (及 .bak-20260825 内
# 每股 pool_updated_at 最早 2026-08-01), 而回测起点 ≈ 730 天前(2024-09 前后)。
# → 回测起点当日宇宙**不可考**, 只能重建 2026-08 以来的约 5 周 churn。
POOL_SNAPSHOTS = [
    # (快照日期, 文件路径, 说明)
    ("2026-08-25", "config/master_stock_pool.yaml.bak-20260825", "master_pool 最早可得快照"),
    ("2026-08-30", "config/master_stock_pool.yaml.bak.poolacc", "poolacc 备份"),
    ("2026-09-06", "config/master_stock_pool.yaml", "当前 master_pool"),
]

UNIVERSE_UNKNOWABLE_BEFORE = "2026-08-25"  # 早于此时点的宇宙成分不可考


def _read_pool_symbols(path: str) -> set:
    with open(path) as f:
        data = yaml.safe_load(f)
    raw = data.get("master_pool", []) if isinstance(data, dict) else []
    return {str(s["symbol"]) for s in raw if "." not in str(s["symbol"])}


def build_universe_timeline() -> dict:
    """重建带时间戳的 master_pool 成分表; 返回 {date: {symbols}}。"""
    timeline = {}
    for date, path, note in POOL_SNAPSHOTS:
        if os.path.exists(path):
            timeline[date] = _read_pool_symbols(path)
    return timeline


def load_pit_universe(path: str = None) -> dict:
    """读 data/universe_pit.json → {rebalance_date: [codes]}。"""
    p = path or PIT_PATH
    if not os.path.exists(p):
        raise RuntimeError(f"时点宇宙文件不存在: {p} — 先跑 scripts/build_pointintime_universe.py")
    with open(p, encoding="utf-8") as f:
        return (json.load(f).get("rebalance_dates") or {})


def _prev_day(d: str) -> str:
    return (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def build_pit_intervals(rd: dict, window_start: str) -> tuple:
    """由时点宇宙序列构造 {code: [(start,end), ...]} 隶属区间 + 需评估的代码集合。

    规则: 某码在再平衡日 t 入选 → 自 t 起生效, 至其下一次**未**入选的再平衡日为止。
    窗口所需期 = 窗口起点前的最后一期(定义窗口起点隶属) + 窗口内的全部期。
    """
    ts = sorted(rd)
    before = [t for t in ts if t < window_start]
    inside = [t for t in ts if t >= window_start]
    periods = ([before[-1]] if before else []) + inside
    if not periods:
        return {}, []
    intervals: dict = {}
    for i, t in enumerate(periods):
        nxt = periods[i + 1] if i + 1 < len(periods) else None
        end = "9999-12-31" if nxt is None else _prev_day(nxt)
        for c in rd.get(t, []):
            intervals.setdefault(c, []).append((t, end))
    return intervals, sorted(intervals)


def _in_pit(code: str, ts) -> bool:
    """预测日是否落在该码的时点宇宙隶属区间内 (point_in_time 模式启用)。"""
    if PIT_MEMBERSHIP is None:
        return True
    ivs = PIT_MEMBERSHIP.get(code)
    if not ivs:
        return False
    d = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
    return any(a <= d <= b for a, b in ivs)


def resolve_universe(mode: str, backtest_days: int = 730, pit_file: str = None) -> tuple:
    """
    返回 (universe_symbols, meta_dict)。
    - snapshot:       旧行为 → 当前 master_stock_pool.yaml (前视选池, 仅作对照)
    - point_in_time:  阶段1-B → 读 data/universe_pit.json (规则化时点宇宙 R1–R6, 无前视)
    """
    if mode == "snapshot":
        timeline = build_universe_timeline()
        cur = timeline.get("2026-09-06", set())
        meta = {
            "mode": "snapshot",
            "snapshot_date": "2026-09-06",
            "universe": sorted(cur),
            "note": "旧行为: 用当前池回测 730 天历史(前视选池)",
        }
        return sorted(cur), meta

    rd = load_pit_universe(pit_file)
    window_start = (datetime.now() - timedelta(days=backtest_days)).strftime("%Y-%m-%d")
    intervals, codes = build_pit_intervals(rd, window_start)
    meta = {
        "mode": "point_in_time",
        "source": pit_file or PIT_PATH,
        "window_start": window_start,
        "periods_in_window": len([t for t in sorted(rd) if t >= window_start]),
        "universe_size": len(codes),
        "note": ("阶段1-B: 宇宙来自规则化时点宇宙(R1–R6 预注册, 无前视选池); "
                 "每笔预测按预测日与隶属区间过滤——非成员日的预测不计入。"),
        "previous_behavior": ("旧实现用的是「最早可得快照(2026-08-25)」作代理, "
                             "新实现直接读 data/universe_pit.json"),
    }
    return codes, meta


def evaluate_stock(code: str, name: str, backtest_days: int, adjust: str, drop_fund: bool) -> dict:
    """对单只股票执行 walk-forward OOS 方向精度评估"""
    try:
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=int(backtest_days * 2.2))).strftime("%Y-%m-%d")

        # FX-2: 统一复权入口 (新默认 hfq; --adjust qfq 复现旧行为)
        # 2026-09-13: 麦蕊结构性无退市股(返回 0 行) ⇒ 不足 350 行时回落本地 PIT 缓存,
        #   否则时点宇宙里的退市股永远进不了回测(幸存者偏差消不掉)。
        kdf, ksrc = _load_kline(code, start_date, end_date, adjust)
        if kdf is None or len(kdf) < 350:
            return None

        df = kdf.reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        for c in ["close", "volume", "high", "low", "open"]:
            df[c] = df[c].astype(float)

        # FX-3: 默认剔除基本面 (无 ann_date, 无法保证时点一致); --fund keep 复现旧行为
        fundamentals = None if drop_fund else fetch_fundamentals(code)

        feats = build_features(df, fundamentals).replace([np.inf, -np.inf], np.nan)
        tcol = f"target_{HORIZON}d"

        # FX-3: drop 模式下把可能残留的 fund_* 列一并剔除 (双保险)
        if drop_fund:
            feats = feats[[c for c in feats.columns if not c.startswith("fund_")]]

        fcols = [c for c in feats.columns if not c.startswith("target_")]

        if tcol not in feats.columns or feats[tcol].notna().sum() < MIN_TRAIN_SAMPLES:
            return None

        feats[fcols] = feats[fcols].fillna(0)

        all_idx = feats.index.tolist()
        if len(all_idx) < MIN_TRAIN_SAMPLES + WINDOW_DAYS:
            return None

        # 2026-09-13 修复: 行数少于 backtest_days 时 bt_start 会变负 → all_idx[-9:] 静默变成
        # “最后 9 行”, 测试窗口被压成几天(退市股/次新股必踩)。必须钳到 0。
        bt_start = max(0, len(all_idx) - backtest_days)
        bt_indices = all_idx[bt_start:]

        all_predictions = []

        for w in range(0, len(bt_indices), WINDOW_DAYS):
            w_end = min(w + WINDOW_DAYS, len(bt_indices))
            test_dates = bt_indices[w:w_end]
            if len(test_dates) < 5:
                continue

            train_end_pos = all_idx.index(test_dates[0])
            train_idx = all_idx[:train_end_pos]
            if len(train_idx) < MIN_TRAIN_SAMPLES:
                continue

            train_data = feats.loc[train_idx]
            target_valid = train_data[tcol].notna()
            if target_valid.sum() < MIN_TRAIN_SAMPLES:
                continue

            X_tr = train_data.loc[target_valid, fcols].values
            y_tr = train_data.loc[target_valid, tcol].values

            split = int(len(X_tr) * 0.8)
            X_tr_80, y_tr_80 = X_tr[:split], y_tr[:split]

            try:
                scaler = StandardScaler()
                X_scaled = scaler.fit_transform(X_tr_80)
                selector = SelectFromModel(
                    lgb.LGBMRegressor(n_estimators=50, random_state=42, verbose=-1),
                    threshold="median", max_features=40
                )
                X_sel = selector.fit_transform(X_scaled, y_tr_80)
                model = lgb.LGBMRegressor(
                    n_estimators=200, max_depth=6, learning_rate=0.03,
                    random_state=42, verbose=-1, n_jobs=1,
                )
                model.fit(X_sel, y_tr_80)

                for test_idx in test_dates:
                    if test_idx not in feats.index:
                        continue
                    row = feats.loc[test_idx]
                    if pd.isna(row[tcol]):
                        continue
                    if not _in_pit(code, test_idx):   # 阶段1-B: 非成员日的预测不计入
                        continue
                    X_test = scaler.transform([row[fcols].values])
                    X_test_sel = selector.transform(X_test)
                    pred = float(model.predict(X_test_sel)[0])
                    actual = float(row[tcol])
                    pdir = 1 if pred > 0 else -1
                    adir = 1 if actual > 0 else -1
                    all_predictions.append((pdir, adir))
                    if PRED_DUMP is not None:
                        with PRED_LOCK:
                            PRED_DUMP.setdefault(code, []).append(
                                [str(test_idx)[:10], 1 if pdir == adir else 0])
            except Exception:
                continue

        if len(all_predictions) < MIN_OOS_PREDICTIONS:
            return None

        correct = sum(1 for pd_, ad_ in all_predictions if pd_ == ad_)
        total = len(all_predictions)
        tp = sum(1 for pd_, ad_ in all_predictions if pd_ == 1 and ad_ == 1)
        tn = sum(1 for pd_, ad_ in all_predictions if pd_ == -1 and ad_ == -1)
        fp = sum(1 for pd_, ad_ in all_predictions if pd_ == 1 and ad_ == -1)
        fn = sum(1 for pd_, ad_ in all_predictions if pd_ == -1 and ad_ == 1)
        tpr = tp / (tp + fn) if (tp + fn) > 0 else 0
        tnr = tn / (tn + fp) if (tn + fp) > 0 else 0

        return {
            "direction_accuracy": round(correct / total, 4),
            "total_predictions": total,
            "correct": correct,
            "wrong": total - correct,
            "true_positive_rate": round(tpr, 4),
            "true_negative_rate": round(tnr, 4),
            "kline_source": ksrc,
        }
    except Exception:
        return None


def _summarize(results: dict, label: str, elapsed: float) -> dict:
    if not results:
        return {"label": label, "stocks_evaluated": 0, "note": "无有效结果"}
    accs = [v["direction_accuracy"] for v in results.values()]
    preds = [v["total_predictions"] for v in results.values()]
    return {
        "label": label,
        "stocks_evaluated": len(results),
        "mean_accuracy": round(float(np.mean(accs)), 4),
        "median_accuracy": round(float(np.median(accs)), 4),
        "std_accuracy": round(float(np.std(accs)), 4),
        "min_accuracy": round(float(np.min(accs)), 4),
        "max_accuracy": round(float(np.max(accs)), 4),
        "stocks_above_50pct": int(sum(1 for a in accs if a > 0.50)),
        "stocks_above_55pct": int(sum(1 for a in accs if a > 0.55)),
        "total_oos_predictions": int(np.sum(preds)),
        "elapsed_seconds": round(elapsed, 1),
    }


def run_universe(symbols: list, names: dict, backtest_days: int, adjust: str, drop_fund: bool) -> tuple:
    start_time = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(evaluate_stock, c, names.get(c, c), backtest_days, adjust, drop_fund): c
                   for c in symbols}
        try:
            for future in as_completed(futures, timeout=TIMEOUT_GLOBAL):
                code = futures[future]
                try:
                    r = future.result(timeout=5)
                    if r:
                        results[code] = r
                except Exception:
                    pass
        except TimeoutError:
            pass
    return results, time.time() - start_time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--universe", choices=["snapshot", "point_in_time", "both"], default="both",
                        help="snapshot=旧行为(当前池); point_in_time=新默认(最早可得快照); both=两者对照")
    parser.add_argument("--adjust", choices=["hfq", "qfq"], default=ADJUST_STANDARD,
                        help="hfq=后复权(新默认); qfq=前复权(旧行为)")
    parser.add_argument("--fund", choices=["drop", "keep"], default="drop",
                        help="drop=剔除fund_*(新默认, 无ann_date); keep=保留(旧行为)")
    parser.add_argument("--codes", nargs="*", default=None, help="仅评估指定代码(冒烟测试用)")
    parser.add_argument("--backtest-days", type=int, default=730, help="回测天数(默认730)")
    parser.add_argument("--pit-file", default=None,
                        help="时点宇宙 JSON (默认 data/universe_pit.json)")
    parser.add_argument("--dump-predictions", default=None,
                        help="将逐笔预测(date, correct)落盘供显著性检验使用")
    parser.add_argument("--kline-source", choices=["mairui", "local", "auto"], default="auto",
                        help="auto=麦蕊优先不足回落本地PIT缓存(含退市股); local=只用本地; mairui=旧行为")
    parser.add_argument("--skip-adjust-mismatch", action="store_true",
                        help="跳过与 --adjust 口径不符的本地文件")
    args = parser.parse_args()

    global KLINE_SOURCE, KLINE_SKIP_ADJUST_MISMATCH
    KLINE_SOURCE = args.kline_source
    KLINE_SKIP_ADJUST_MISMATCH = args.skip_adjust_mismatch

    drop_fund = (args.fund == "drop")
    backtest_days = args.backtest_days

    snapshot_symbols, snap_meta = resolve_universe("snapshot")
    pit_symbols, pit_meta = resolve_universe("point_in_time", backtest_days, args.pit_file)
    # 隶属区间: **只用于 point_in_time 宇宙**；snapshot 基线绝不能被它过滤。
    # (2026-09-13 修复 Astra 复核发现的 bug: both 模式下两组曾共用过滤,
    #  导致 snapshot 的 50.02% 实为「当前池代码 ∩ PIT 成员日」的混合样本)
    global PIT_MEMBERSHIP, PRED_DUMP
    pit_intervals = {}
    if args.universe in ("point_in_time", "both"):
        pit_intervals, _ = build_pit_intervals(
            load_pit_universe(args.pit_file), pit_meta["window_start"])
        if args.dump_predictions:
            PRED_DUMP = {}
        print(f"   时点隶属区间: {len(pit_intervals)} 只 (窗口起点 {pit_meta['window_start']})")

    names = {}
    for s in snapshot_symbols + pit_symbols:
        names[s] = s
    if os.path.exists("config/master_stock_pool.yaml"):
        with open("config/master_stock_pool.yaml") as f:
            for s in yaml.safe_load(f)["master_pool"]:
                if "." not in str(s["symbol"]):
                    names[str(s["symbol"])] = s.get("name", s["symbol"])

    if args.codes:
        snapshot_symbols = [c for c in snapshot_symbols if c in args.codes]
        pit_symbols = [c for c in pit_symbols if c in args.codes]

    print(f"🚀 H20D Walk-Forward OOS (评估路径·阶段1修复版)")
    print(f"   universe={args.universe} adjust={args.adjust} fund={args.fund} backtest={backtest_days}d")
    print(f"   snapshot池: {len(snapshot_symbols)}只 | point_in_time池: {len(pit_symbols)}只")

    universes = []
    if args.universe in ("snapshot", "both"):
        universes.append(("snapshot", snapshot_symbols, snap_meta))
    if args.universe in ("point_in_time", "both"):
        universes.append(("point_in_time", pit_symbols, pit_meta))

    output = {
        "evaluated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "horizon": HORIZON, "window_days": WINDOW_DAYS, "backtest_days": backtest_days,
            "adjust": args.adjust, "fund": args.fund, "universe": args.universe,
        },
        "universe_meta": {"snapshot": snap_meta, "point_in_time": pit_meta},
        "universe_delta": {
            "snapshot_count": len(snapshot_symbols),
            "point_in_time_count": len(pit_symbols),
            "only_in_snapshot": sorted(set(snapshot_symbols) - set(pit_symbols)),
            "only_in_point_in_time": sorted(set(pit_symbols) - set(snapshot_symbols)),
        },
        "per_universe": {},
    }

    for label, symbols, meta in universes:
        # 过滤开关: **仅 point_in_time 生效**（snapshot 为前视基线, 必须不过滤）
        PIT_MEMBERSHIP = pit_intervals if label == "point_in_time" else None
        print(f"\n▶ 运行 {label} 宇宙 ({len(symbols)}只, PIT过滤={'开' if PIT_MEMBERSHIP else '关'})...")
        results, elapsed = run_universe(symbols, names, backtest_days, args.adjust, drop_fund)
        summ = _summarize(results, label, elapsed)
        output["per_universe"][label] = {"summary": summ, "per_stock": results}
        print(f"   {label}: 评估{summ.get('stocks_evaluated',0)}只 "
              f"mean_acc={summ.get('mean_accuracy',0):.2%} "
              f"median={summ.get('median_accuracy',0):.2%}")

    # 差额 (前视选池贡献的上限代理)
    if "snapshot" in output["per_universe"] and "point_in_time" in output["per_universe"]:
        s = output["per_universe"]["snapshot"]["summary"]
        p = output["per_universe"]["point_in_time"]["summary"]
        if s.get("stocks_evaluated") and p.get("stocks_evaluated"):
            output["universe_delta"]["mean_accuracy_delta"] = round(
                s["mean_accuracy"] - p["mean_accuracy"], 4)

    os.makedirs("reports/h20d_evaluation", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/h20d_evaluation/eval_fix1_{args.universe}_{args.adjust}_fund{args.fund}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n📁 {out_path}")
    print(json.dumps(output["universe_delta"], ensure_ascii=False, indent=2))

    if args.dump_predictions and PRED_DUMP is not None:
        with open(args.dump_predictions, "w", encoding="utf-8") as f:
            json.dump({"universe": args.universe, "horizon": HORIZON,
                       "window_days": WINDOW_DAYS, "backtest_days": backtest_days,
                       "predictions": PRED_DUMP}, f, ensure_ascii=False)
        print(f"🧾 逐笔预测: {args.dump_predictions} "
              f"({sum(len(v) for v in PRED_DUMP.values())} 条 / {len(PRED_DUMP)} 只)")
    return output, out_path


if __name__ == "__main__":
    main()
