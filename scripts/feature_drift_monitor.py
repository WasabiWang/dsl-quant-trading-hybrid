#!/usr/bin/env python3
"""v4.7.3 P1: 特征漂移检测 (移植 QuantMind compute_psi_drift 双通道逻辑, 简化版)

背景:
  DSL 的 degraded 机制是输出侧检测(精度掉了才发现), 本模块补输入侧检测:
  特征分布在"基线窗口" vs "最近窗口"之间漂移 → 提前预警模型输入的世界变了。

双通道 (QuantMind 同款设计):
  - level_psi: 原始水平值分箱 PSI (量纲敏感)。成交额/换手等水平特征在牛市中
    整体抬升时必然高, 但树模型只走阈值分叉+标签是截面rank, 单纯水平平移
    对预测力几乎无影响 → "良性量纲膨胀"。
  - rank_disp: 每只股票在基线窗 vs 最近窗的**截面 rank 位移均值**(0~1)。
    只反映"个股相对位置是否重排", 对整体水平平移免疫, 能抓住真实的风格切换/
    板块轮动等结构漂移。判级以 rank_disp 为主。

简化点 (vs QuantMind, 有历史数据后可升级):
  - 噪声本底校准/显著性检验 → 固定阈值 0.10(medium)/0.25(severe)
  - 基线窗 = 紧邻最近窗的等长窗口 (等长相邻, 消除采样噪声偏差)
  - 截面 = 主池+观察池+影子池 (~42只)

特征源: 每只标的全量前复权 OHLCV K线(麦蕊, 缓存 data/cache/feat_kline/),
        用 batch_predict.build_h20d_features 复刻训练特征 (~84维, 剔除常数列)。

用法:
  .venv/bin/python3 scripts/feature_drift_monitor.py            # 增量(cache>2天才刷新K线)
  .venv/bin/python3 scripts/feature_drift_monitor.py --backfill # 强制刷新全部K线
  .venv/bin/python3 scripts/feature_drift_monitor.py --dry-run
输出: data/monitoring/feature_drift.json
"""
import os, sys, json, argparse, time
from datetime import datetime

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

OUT_PATH = os.path.join(PROJECT_ROOT, "data", "monitoring", "feature_drift.json")
FEAT_KLINE_DIR = os.path.join(PROJECT_ROOT, "data", "cache", "feat_kline")

RECENT_DAYS = 20        # 最近窗口(交易日)
MIN_OBS = 5             # 一只股票在一段窗口至少出现的交易日数
MIN_COMMON = 10         # 两窗交集股票数下限 (42只池 → 阈值比全市场版低)
TOP_N = 20              # 输出 top 漂移特征数
# 判级阈值 (噪声本底校准后使用, 见 compute_drift)
# 实测校准(2026-08-31): 35只池20日窗的 rank_disp 噪声本底中位≈0.136 →
# 固定阈值0.10会把纯噪声误报为medium(47/75假阳性)。故引入本底倍数法。
TH_MEDIUM_ABS = 0.12    # medium 绝对下限 (本底不可估计时退化阈值: 0.10)
TH_SEVERE_ABS = 0.20    # severe 绝对下限 (本底不可估计时退化阈值: 0.25)
TH_MEDIUM_MULT = 1.8    # medium = max(abs, mult×噪声本底)
TH_SEVERE_MULT = 2.5    # severe = max(abs, mult×噪声本底)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def collect_symbols() -> list:
    """主池 + 观察池 + 影子池 (与 rank_ic_monitor 一致)"""
    syms = set()
    try:
        from scripts.batch_predict import get_stock_pool
        for item in get_stock_pool():
            code = item.get("symbol") if isinstance(item, dict) else item
            if code:
                syms.add(str(code).zfill(6))
    except Exception:
        pass
    for rel in ("config/observation_pool.yaml", "config/shadow_pool.yaml"):
        p = os.path.join(PROJECT_ROOT, rel)
        if not os.path.exists(p):
            continue
        try:
            import yaml
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for key in ("observation_pool", "candidates", "stocks", "pool"):
                for item in (data.get(key) or []):
                    c = (item.get("code") or item.get("symbol")) if isinstance(item, dict) else item
                    if c:
                        syms.add(str(c).zfill(6))
        except Exception:
            pass
    return sorted(syms)


# ── K线 (OHLCV) 缓存 ────────────────────────────────────────────────────────
def fetch_ohlcv(symbol: str):
    from config.mairui_api_config import get_kline_history
    rows = get_kline_history(symbol, period="d", adjust="f")
    if not rows:
        return None
    out = []
    for r in rows:
        try:
            out.append({"t": r["t"], "o": float(r["o"]), "h": float(r["h"]),
                        "l": float(r["l"]), "c": float(r["c"]),
                        "v": float(r.get("v", 0)), "pc": float(r.get("pc", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    return out or None


def get_ohlcv(symbol: str, force: bool = False):
    p = os.path.join(FEAT_KLINE_DIR, f"{symbol}.json")
    if not force and os.path.exists(p):
        age_h = (time.time() - os.path.getmtime(p)) / 3600
        if age_h < 48:
            cached = _load_json(p, None)
            if cached:
                return cached
    rows = fetch_ohlcv(symbol)
    if rows:
        _save_json(p, rows)
    return rows


# ── PSI / rank位移 (移植 QuantMind) ─────────────────────────────────────────
def psi_single(a: np.ndarray, b: np.ndarray, n_bins: int = 10) -> float:
    """单个特征的水平 PSI (a=基线基准, b=待检)。<0.05 无漂移; 0.05~0.2 中等; >0.2 显著。"""
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    a = a[np.isfinite(a)]
    b = b[np.isfinite(b)]
    if len(a) < 20 or len(b) < 20:
        return float("nan")
    edges = np.quantile(a, np.linspace(0, 1, n_bins + 1))
    edges[0], edges[-1] = -np.inf, np.inf
    unique_edges = []
    for e in edges:
        if not unique_edges or e != unique_edges[-1]:
            unique_edges.append(e)
    if len(unique_edges) < 3:
        return 0.0
    bin_a = np.histogram(a, bins=unique_edges)[0].astype(np.float64)
    bin_b = np.histogram(b, bins=unique_edges)[0].astype(np.float64)
    pct_a = np.clip(bin_a / max(len(a), 1), 1e-6, None)
    pct_b = np.clip(bin_b / max(len(b), 1), 1e-6, None)
    return float(np.sum((pct_b - pct_a) * np.log(pct_b / pct_a)))


def _per_symbol_mean_rank(df: pd.DataFrame, features: list, min_obs: int = MIN_OBS) -> pd.DataFrame:
    """每只股票在每个特征上的窗口内截面 rank 均值。"""
    keep = df.groupby("symbol")[features[0]].transform("size") >= min_obs
    sub = df.loc[keep]
    if sub.empty:
        return pd.DataFrame()
    rank_df = sub.groupby("trade_date")[features].rank(pct=True)
    rank_df["symbol"] = sub["symbol"].to_numpy()
    return rank_df.groupby("symbol")[features].mean()


def rank_disp_all(base_df: pd.DataFrame, recent_df: pd.DataFrame, features: list) -> dict:
    """批量计算全部特征的截面 rank 位移 (0~1)。"""
    avail = [f for f in features if f in base_df.columns and f in recent_df.columns]
    if not avail:
        return {}
    tr_mean = _per_symbol_mean_rank(base_df, avail)
    rc_mean = _per_symbol_mean_rank(recent_df, avail)
    common = tr_mean.index.intersection(rc_mean.index)
    if len(common) < MIN_COMMON:
        return {}
    disp = (rc_mean.loc[common] - tr_mean.loc[common]).abs()
    return {f: float(disp[f].mean()) for f in avail}


# ── 特征矩阵构建 ────────────────────────────────────────────────────────────
def build_feature_panel(ohlcv_rows: list) -> pd.DataFrame:
    """单只标的: OHLCV rows → 特征矩阵 (日期 × 特征, 复刻训练特征)。"""
    df = pd.DataFrame(ohlcv_rows)
    df = df.rename(columns={"t": "trade_date", "o": "open", "h": "high",
                            "l": "low", "c": "close", "v": "volume",
                            "pc": "prev_close"})
    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date")
    if len(df) < 120:
        return pd.DataFrame()
    try:
        from scripts.batch_predict import build_h20d_features
        feats = build_h20d_features(df)
    except Exception:
        return pd.DataFrame()
    # 剔除常数列(fund_ 占位 / rank_ 占位 / 全NaN)
    cols = []
    for c in feats.columns:
        s = feats[c]
        if s.nunique(dropna=True) <= 1:
            continue
        cols.append(c)
    feats = feats[cols]
    feats["symbol"] = None
    feats["trade_date"] = df["trade_date"].to_numpy()
    return feats


def judge_results(disp_map: dict, floor_map: dict, features: list,
                  baseline_df: pd.DataFrame, recent_df: pd.DataFrame) -> list:
    """逐特征判级: severe/medium/stable + benign_scale (可单测)。"""
    results = []
    for f in features:
        if f not in disp_map:
            continue
        a = baseline_df[f].to_numpy()
        b = recent_df[f].to_numpy()
        level_psi = psi_single(a, b)
        if not np.isfinite(level_psi):
            continue
        rank_disp = disp_map[f]
        feat_floor = floor_map.get(f)
        if feat_floor is not None and feat_floor > 1e-4:
            th_m = max(TH_MEDIUM_ABS, TH_MEDIUM_MULT * feat_floor)
            th_s = max(TH_SEVERE_ABS, TH_SEVERE_MULT * feat_floor)
        else:
            th_m, th_s = 0.10, 0.25  # 退化固定阈值 (QuantMind fallback 同款)
        if rank_disp >= th_s:
            level = "severe"
        elif rank_disp >= th_m:
            level = "medium"
        else:
            level = "stable"
        benign_scale = (level_psi >= 0.05 and level == "stable")
        results.append({
            "feature": f,
            "psi": round(level_psi, 4),
            "rank_disp": round(rank_disp, 4),
            "noise_floor": round(feat_floor, 4) if feat_floor is not None else None,
            "level": level,
            "benign_scale": benign_scale,
        })
    return results


# ── 主流程 ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="强制刷新全部OHLCV K线")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    symbols = collect_symbols()
    print(f"标的集合: {len(symbols)} 只")

    panels = {}
    failed = []
    for i, sym in enumerate(symbols):
        rows = get_ohlcv(sym, force=args.backfill)
        if not rows:
            failed.append(sym)
            continue
        feats = build_feature_panel(rows)
        if not feats.empty:
            feats["symbol"] = sym
            panels[sym] = feats
        if (i + 1) % 10 == 0:
            print(f"  K线+特征 {i+1}/{len(symbols)}")
    print(f"特征面板: {len(panels)}/{len(symbols)}")
    if failed:
        print(f"  ⚠️ K线失败: {failed}")

    if len(panels) < MIN_COMMON:
        print("❌ 面板不足, 无法计算")
        sys.exit(1)

    # 合并全池面板, 取公共日期轴
    all_panel = pd.concat(panels.values(), ignore_index=True)
    dates = sorted(all_panel["trade_date"].unique())
    if len(dates) < 2 * RECENT_DAYS:
        print(f"❌ 公共交易日不足 {2*RECENT_DAYS} 天 (实际 {len(dates)})")
        sys.exit(1)
    recent_dates = dates[-RECENT_DAYS:]
    baseline_dates = dates[-2 * RECENT_DAYS:-RECENT_DAYS]
    recent_df = all_panel[all_panel["trade_date"].isin(recent_dates)]
    baseline_df = all_panel[all_panel["trade_date"].isin(baseline_dates)]
    print(f"窗口: 基线 {baseline_dates[0].date()}~{baseline_dates[-1].date()} ({len(baseline_dates)}日) "
          f"vs 最近 {recent_dates[0].date()}~{recent_dates[-1].date()} ({len(recent_dates)}日)")

    features = [c for c in all_panel.columns if c not in ("symbol", "trade_date")]
    print(f"特征数: {len(features)}")

    # 批量 rank 位移
    disp_map = rank_disp_all(baseline_df, recent_df, features)
    print(f"rank_disp 可估计特征: {len(disp_map)}/{len(features)}")

    # 噪声本底校准: 历史相邻等长窗对的 rank_disp 中位数 (每特征)
    # 依据: 短窗截面rank均值有强时间自相关, 固定阈值必随市场状态误报;
    # 真实漂移需显著超过历史正常波动 (QuantMind 2026-08 同款教训)。
    floor_map = {}
    pool_dates = dates[:-RECENT_DAYS]
    ntd = len(pool_dates)
    if ntd >= 3 * RECENT_DAYS:
        n_pairs = min(4, (ntd - RECENT_DAYS) // RECENT_DAYS)
        per_pair_maps = []
        for i in range(n_pairs):
            win_a = pool_dates[-(i + 2) * RECENT_DAYS:-(i + 1) * RECENT_DAYS]
            win_b = pool_dates[-(i + 1) * RECENT_DAYS:]
            if not win_a or not win_b:
                continue
            per_pair_maps.append(rank_disp_all(
                all_panel[all_panel["trade_date"].isin(win_a)],
                all_panel[all_panel["trade_date"].isin(win_b)],
                features,
            ))
        for f in features:
            vals = [m[f] for m in per_pair_maps if m.get(f) is not None and np.isfinite(m[f])]
            if vals:
                floor_map[f] = float(np.median(vals))
        if floor_map:
            _floor_vals = sorted(floor_map.values())
            print(f"噪声本底: {len(floor_map)}特征, 中位={np.median(_floor_vals):.4f} p90={np.percentile(_floor_vals, 90):.4f}")
        else:
            print("⚠️ 噪声本底不可估计 → 退化固定阈值")
    else:
        print(f"⚠️ 历史不足({ntd}日) → 噪声本底不可估计, 退化固定阈值")

    # 逐特征 level PSI + 判级
    results = judge_results(disp_map, floor_map, features, baseline_df, recent_df)

    if not results:
        print("❌ 无可计算特征")
        sys.exit(1)

    results.sort(key=lambda r: (r["rank_disp"], r["psi"]), reverse=True)
    counts = {"stable": 0, "medium": 0, "severe": 0}
    for r in results:
        counts[r["level"]] += 1

    severe_count, medium_count = counts["severe"], counts["medium"]
    severe_ratio = severe_count / max(1, len(results))
    if severe_count >= 3 or severe_ratio >= 0.3 or (severe_count + medium_count) >= max(5, len(results) * 0.3):
        overall = "severe"
    elif severe_count >= 1 or medium_count >= 3:
        overall = "warning"
    else:
        overall = "stable"

    out = {
        "updated_at": datetime.now().isoformat(),
        "overall": overall,
        "drift": counts,
        "max_rank_disp": round(max(r["rank_disp"] for r in results), 4),
        "baseline_window": {"start": str(baseline_dates[0].date()), "end": str(baseline_dates[-1].date())},
        "recent_window": {"start": str(recent_dates[0].date()), "end": str(recent_dates[-1].date())},
        "n_symbols": len(panels),
        "n_features": len(results),
        "top_drift_features": results[:TOP_N],
        "calibration": {
            "noise_floor_median": round(float(np.median(list(floor_map.values()))), 4) if floor_map else None,
            "n_floor_features": len(floor_map),
            "note": "噪声本底=历史相邻等长窗对rank_disp中位数; severe=max(0.20, 2.5×本底), medium=max(0.12, 1.8×本底)",
        },
    }
    print(f"\n漂移判定: {overall} | severe={severe_count} medium={medium_count} stable={counts['stable']} | max_rank_disp={out['max_rank_disp']}")
    for r in results[:5]:
        mark = "良性量纲" if r["benign_scale"] else r["level"]
        print(f"  {r['feature']:22s} rank_disp={r['rank_disp']:.4f} psi={r['psi']:.4f} [{mark}]")
    if not args.dry_run:
        _save_json(OUT_PATH, out)
        print(f"✅ {OUT_PATH}")
    else:
        print("(dry-run, 未写回)")


if __name__ == "__main__":
    main()
