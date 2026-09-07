#!/usr/bin/env python3
"""v4.7.3 P0: 生产 Rank IC/ICIR 监控（移植 QuantMind inference_quality_backfill + get_model_quality）

背景:
  DSL 此前只有二元方向精度, 无截面 IC 概念。本脚本:
  1. 补全 realized_return: daily_records 中所有有 predicted_return 的 stock-day
     (含 hold), 用麦蕊前复权K线计算 horizon 日真实收益。K线按 symbol 缓存。
  2. 按交易日截面计算 Rank IC (Spearman) / IC (Pearson) / coverage。
  3. 漂移判定 (移植 QuantMind get_model_quality 规则):
     - 近20日 rank_ic 均值 < 0            -> degraded (信号失效)
     - 历史均值>0 且近20日 < 50%历史       -> drifted  (衰减超50%)
     - 近10日覆盖率 < 60%                 -> data_issue
     - 30日滚动 ICIR = mean / pstdev
  4. 输出 confidence_data/rank_ic_series.json (series + summary + thresholds)

用法:
  .venv/bin/python3 scripts/rank_ic_monitor.py            # 增量日常运行 (cron 15:40)
  .venv/bin/python3 scripts/rank_ic_monitor.py --backfill # 全量回填K线+realized (首次)
  .venv/bin/python3 scripts/rank_ic_monitor.py --dry-run
"""
import os, sys, json, argparse, math, time, re
from datetime import datetime, timedelta
from collections import Counter
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

CALIB_PATH = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
IC_PATH = os.path.join(PROJECT_ROOT, "confidence_data", "rank_ic_series.json")
KLINES_DIR = os.path.join(PROJECT_ROOT, "data", "cache", "ic_kline")

# IC 计算参数
MIN_N = 10          # 单日截面最少样本数 (QuantMind 同款)
RECENT_WINDOW = 20  # 近期窗口 (漂移判定)
ICIR_WINDOW = 30    # ICIR 滚动窗口
DRIFT_DECAY = 0.5   # 近期均值衰减超50% -> drifted
COVERAGE_MIN = 0.6  # 覆盖率下限
# 阈值支持外部校准覆盖 (adaptive_params.rank_ic)
DEFAULT_THRESHOLDS = {
    "degraded_mean": 0.0,      # 近20日均值 < 0 -> degraded
    "critical_mean": -0.10,    # 近20日均值 < -0.10 -> critical (暂停新开仓)
    "drift_decay": DRIFT_DECAY,
    "coverage_min": COVERAGE_MIN,
    "min_n": MIN_N,
    "min_status_days": 10,     # v4.7.5: 当前模型族最少成熟日 (影子阈值, 非交易阈值)
    "max_lag_sessions": 2,     # v4.7.5: 最后成熟观测落后成熟截止日最多交易日
}


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_thresholds():
    """从 adaptive_params 读取覆盖, 缺省用 DEFAULT_THRESHOLDS。"""
    th = dict(DEFAULT_THRESHOLDS)
    try:
        import yaml
        ap_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        with open(ap_path, "r", encoding="utf-8") as f:
            ap = yaml.safe_load(f) or {}
        rk = ap.get("rank_ic") or {}
        if isinstance(rk, dict):
            for k in th:
                if k in rk and rk[k] is not None:
                    th[k] = rk[k]
    except Exception:
        pass
    return th


# ── K线缓存 ─────────────────────────────────────────────────────────────────
def load_kline_cached(symbol: str):
    """读缓存 {symbol}.json -> {date: close}; 不存在返回 None"""
    p = os.path.join(KLINES_DIR, f"{symbol}.json")
    return _load_json(p, None)


def save_kline_cached(symbol: str, m: dict):
    os.makedirs(KLINES_DIR, exist_ok=True)
    _save_json(os.path.join(KLINES_DIR, f"{symbol}.json"), m)


def fetch_kline(symbol: str):
    """麦蕊全量前复权K线 -> {date: close}"""
    from config.mairui_api_config import get_kline_history
    rows = get_kline_history(symbol, period="d", adjust="f")
    if not rows:
        return None
    m = {}
    for r in rows:
        try:
            m[r["t"]] = float(r["c"])
        except (KeyError, TypeError, ValueError):
            continue
    return m or None


def get_kline_map(symbol: str, force: bool = False):
    """缓存优先; 缓存超过2天自动刷新(保证新交易日收盘价及时入库)。"""
    p = os.path.join(KLINES_DIR, f"{symbol}.json")
    if not force and os.path.exists(p):
        age_h = (time.time() - os.path.getmtime(p)) / 3600
        if age_h < 48:
            cached = load_kline_cached(symbol)
            if cached:
                return cached
    m = fetch_kline(symbol)
    if m:
        save_kline_cached(symbol, m)
    return m


# ── 标的集合 ───────────────────────────────────────────────────────────────
def collect_symbols(cal: dict) -> list:
    """主池 + 观察池 + 影子池 + daily_records 中出现过的所有 symbol。"""
    syms = set()
    for dr in cal.get("daily_records", []):
        for s in dr.get("stocks", []):
            sym = s.get("symbol")
            if sym:
                syms.add(str(sym).zfill(6))
    # 当前主池
    try:
        from scripts.batch_predict import get_stock_pool
        for code, _name in get_stock_pool():
            syms.add(str(code).zfill(6))
    except Exception:
        pass
    # 观察池 / 影子池
    for rel in ("config/observation_pool.yaml", "config/shadow_pool.yaml"):
        p = os.path.join(PROJECT_ROOT, rel)
        if not os.path.exists(p):
            continue
        try:
            import yaml
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for item in (data.get("stocks") or data.get("pool") or []):
                if isinstance(item, dict):
                    c = item.get("code") or item.get("symbol")
                else:
                    c = item
                if c:
                    syms.add(str(c).zfill(6))
        except Exception:
            pass
    return sorted(syms)


# ── realized 回填 ───────────────────────────────────────────────────────────
def fill_realized(cal: dict, kline_maps: dict, trading_days: set, dry_run: bool) -> int:
    """为所有有 predicted_return 的 stock-day 回填 realized_return (幂等)。"""
    filled = 0
    today = datetime.now().date()
    for dr in cal.get("daily_records", []):
        try:
            pred_date = datetime.strptime(dr["date"], "%Y-%m-%d").date()
        except Exception:
            continue
        for s in dr.get("stocks", []):
            if s.get("realized_return") is not None:
                continue
            pred_ret = s.get("predicted_return")
            if pred_ret is None:
                continue
            horizon = s.get("horizon", "5d") or "5d"
            try:
                hd = int("".join(c for c in str(horizon) if c.isdigit())) or 5
            except Exception:
                hd = 5
            sym = s.get("symbol")
            km = kline_maps.get(sym)
            if not km or dr["date"] not in km:
                continue
            # 第 hd 个交易日后收盘
            sorted_days = sorted(d for d in trading_days
                                 if dr["date"] <= d <= (pred_date + timedelta(days=hd * 5)).strftime("%Y-%m-%d"))
            if len(sorted_days) <= hd:
                continue  # 未到期
            target_day = sorted_days[hd]
            if target_day > today.strftime("%Y-%m-%d"):
                continue  # 未来日期不可用
            pred_close = km[dr["date"]]
            future_close = km[target_day]
            actual = (future_close - pred_close) / pred_close
            s["realized_return"] = round(actual, 4)
            s["realized_check_date"] = target_day
            filled += 1
    if not dry_run:
        cal["last_updated"] = datetime.now().isoformat()
    return filled


# ── IC 计算 ─────────────────────────────────────────────────────────────────
def _spearman(x, y):
    n = len(x)
    rx = pd.Series(x).rank(method="average").to_numpy()
    ry = pd.Series(y).rank(method="average").to_numpy()
    if np.std(rx) == 0 or np.std(ry) == 0:
        return float("nan")
    rxc = rx - rx.mean()
    ryc = ry - ry.mean()
    denom = math.sqrt(float((rxc ** 2).sum()) * float((ryc ** 2).sum()))
    if denom == 0:
        return float("nan")
    return float((rxc * ryc).sum() / denom)


import numpy as np
import pandas as pd


def compute_ic_series(cal: dict, thresholds: dict) -> list:
    """按日期截面计算 (5d horizon) IC 序列。每行带 model_version/model_family。"""
    rows = []
    for dr in cal.get("daily_records", []):
        date = dr["date"]
        model_version = dr.get("version", "unknown")
        model_family = normalize_model_family(model_version)
        preds, reals = [], []
        total = 0
        for s in dr.get("stocks", []):
            if s.get("horizon") != "5d":
                continue
            pr = s.get("predicted_return")
            if pr is None:
                continue
            total += 1
            rr = s.get("realized_return")
            if rr is not None and np.isfinite(rr):
                preds.append(float(pr))
                reals.append(float(rr))
        if total == 0 or len(preds) < thresholds["min_n"]:
            continue
        rank_ic = _spearman(preds, reals)
        ic = float(np.corrcoef(preds, reals)[0, 1]) if len(preds) >= 3 else float("nan")
        if not np.isfinite(rank_ic):
            continue
        rows.append({
            "date": date,
            "model_version": model_version,
            "model_family": model_family,
            "n": len(preds),
            "total_5d": total,
            "coverage": round(len(preds) / total, 4),
            "rank_ic": round(rank_ic, 4),
            "ic": round(ic, 4) if np.isfinite(ic) else None,
        })
    rows.sort(key=lambda r: r["date"])
    return rows


# ── v4.7.5: 模型族与交易日成熟度纯函数 ───────────────────────────────────

def normalize_model_family(version: str) -> str:
    """稳定模型族归一化: v4.7.3.1 与 v4.7.4 均归入 v4.7; 不按 training_hash 分群。"""
    match = re.match(r"^v?(\d+)\.(\d+)", str(version or ""))
    return f"v{match.group(1)}.{match.group(2)}" if match else "unknown"


def mature_cutoff(trading_days, horizon: int = 5, as_of_date=None):
    """可兑现截止日: horizon 日兑现窗口下, 该日及之前的截面收益均已到期。
    禁止依赖真实当前时间(测试可传 as_of_date)。"""
    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y-%m-%d")
    ordered = sorted(d for d in trading_days if d <= as_of_date)
    return ordered[-(horizon + 1)] if len(ordered) > horizon else None


def trading_session_lag(as_of_date, cutoff_date, trading_days):
    """用交易日(非自然日)计算滞后: 落在 (as_of_date, cutoff_date] 的交易日数。"""
    if not as_of_date or not cutoff_date:
        return None
    eligible = [d for d in sorted(trading_days) if as_of_date < d <= cutoff_date]
    return len(eligible)


def compute_quality_summary(series: list, thresholds: dict) -> dict:
    """移植 QuantMind get_model_quality 质量判定 (仅质量层, 不含新鲜度/成熟度)。
    显式接收已筛选的 series, 不再隐式用全历史代表当前模型。"""
    rics = [r["rank_ic"] for r in series if r.get("rank_ic") is not None]
    covs = [r["coverage"] for r in series if r.get("coverage") is not None]
    status, reasons = "healthy", []
    recent_mean = None
    if rics:
        recent = rics[-RECENT_WINDOW:]
        recent_mean = float(sum(recent) / len(recent))
        if recent_mean < thresholds.get("critical_mean", -0.10):
            status = "critical"
            reasons.append(f"近{len(recent)}日 Rank IC 均值 {recent_mean:.4f} < {thresholds.get('critical_mean', -0.10)}，信号严重失效，建议暂停新开仓")
        elif recent_mean < thresholds["degraded_mean"]:
            status = "degraded"
            reasons.append(f"近{len(recent)}日 Rank IC 均值 {recent_mean:.4f} < {thresholds['degraded_mean']}，信号可能失效")
        elif len(rics) >= 30:
            hist_mean = float(sum(rics) / len(rics))
            if hist_mean > 0 and recent_mean < hist_mean * thresholds["drift_decay"]:
                status = "drifted"
                reasons.append(f"近{RECENT_WINDOW}日 Rank IC {recent_mean:.4f} 较历史均值 {hist_mean:.4f} 衰减超{int((1-thresholds['drift_decay'])*100)}%")
    if covs and min(covs[-10:]) < thresholds["coverage_min"]:
        if status == "healthy":
            status = "data_issue"
        reasons.append(f"近10日覆盖率低于 {thresholds['coverage_min']*100:.0f}%，疑似数据问题")
    icir30 = None
    if len(rics) >= 5:
        s = rics[-ICIR_WINDOW:]
        mean_s = sum(s) / len(s)
        std_s = (sum((x - mean_s) ** 2 for x in s) / len(s)) ** 0.5
        icir30 = round(mean_s / std_s, 4) if std_s > 0 else None
    return {
        "quality_status": status,
        "quality_reasons": reasons,
        "recent_mean": round(recent_mean, 4) if recent_mean is not None else None,
        "rank_ic_mean": round(sum(rics) / len(rics), 4) if rics else None,
        "rank_icir_30d": icir30,
        "n_days": len(rics),
    }


def compute_drift(series: list, thresholds: dict) -> dict:
    """向后兼容薄包装: 旧消费者仍读 drift_status/drift_reasons/recent20_mean。"""
    q = compute_quality_summary(series, thresholds)
    return {
        "drift_status": q["quality_status"],
        "drift_reasons": q["quality_reasons"],
        "recent20_mean": q["recent_mean"],
        "rank_ic_mean": q["rank_ic_mean"],
        "rank_icir_30d": q["rank_icir_30d"],
        "n_days": q["n_days"],
    }


# ── v4.7.5: 状态判定 (固定优先级) ──────────────────────────────────────────

def classify_current_status(recent_mean, n_days, lag_trading_days, coverage,
                            thresholds=None) -> dict:
    """当前模型族评估状态判定, 优先级固定 (不依赖文件/时间):
      1. coverage 异常            -> data_issue
      2. 无成熟行                 -> insufficient_data
      3. 落后成熟截止日过多        -> stale
      4. 成熟行不足最低样本        -> insufficient_data
      5. 其余才进入质量判定        -> critical / degraded / healthy
    stale 与 insufficient_data 均不得翻译成"当前模型失效"。"""
    th = dict(thresholds or {})
    coverage_min = float(th.get("coverage_min", COVERAGE_MIN))
    min_status_days = int(th.get("min_status_days", 10))
    max_lag_sessions = int(th.get("max_lag_sessions", 2))
    critical_mean = float(th.get("critical_mean", -0.10))
    degraded_mean = float(th.get("degraded_mean", 0.0))

    if coverage is not None and coverage < coverage_min:
        return {"evaluation_status": "data_issue", "actionable": False}
    if not n_days:
        return {"evaluation_status": "insufficient_data", "actionable": False}
    if lag_trading_days is not None and lag_trading_days > max_lag_sessions:
        return {"evaluation_status": "stale", "actionable": False}
    if n_days < min_status_days:
        return {"evaluation_status": "insufficient_data", "actionable": False}
    if recent_mean is not None and recent_mean < critical_mean:
        return {"evaluation_status": "critical", "actionable": True}
    if recent_mean is not None and recent_mean < degraded_mean:
        return {"evaluation_status": "degraded", "actionable": True}
    return {"evaluation_status": "healthy", "actionable": True}


def build_legacy_conservative_gate(historical: dict, current: dict) -> dict:
    """风控 gate 保持不变: 继承历史 degraded 约束, 新开仓上限仍为1。
    critical=0, degraded=1, 其他不设上限(None)。当前族样本不足时 basis=historical_summary。"""
    hist_status = historical.get("quality_status", "healthy")
    if hist_status == "critical":
        return {"policy": "legacy_conservative", "effective_status": "critical",
                "max_new_positions": 0, "basis": "historical_summary",
                "reason": "历史截面IC critical，暂停新开仓"}
    if hist_status == "degraded":
        reason = ("当前模型族样本不足，暂继承最后一个有效历史告警"
                  if current.get("evaluation_status") == "insufficient_data"
                  else "历史截面IC degraded，新开仓上限1只")
        return {"policy": "legacy_conservative", "effective_status": "degraded",
                "max_new_positions": 1, "basis": "historical_summary", "reason": reason}
    return {"policy": "legacy_conservative", "effective_status": hist_status,
            "max_new_positions": None, "basis": "historical_summary",
            "reason": "历史截面IC未触发 degraded/critical，不额外限新开仓"}


def resolve_rank_ic_new_position_cap(gate, available):
    """从 risk_gate 解析新开仓上限; max_new_positions=None 表示不设额外上限。"""
    cap = gate.get("max_new_positions") if isinstance(gate, dict) else None
    if cap is None:
        return available
    return min(available, max(0, int(cap)))


def assess_evaluation_readiness(rows, daily_records, family, mature_cutoff_date,
                                trading_days, thresholds, current_version,
                                as_of_date=None) -> dict:
    """为当前模型族计算就绪度 + evaluation_status。新鲜度/成熟度先于质量判定。"""
    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y-%m-%d")
    min_status_days = int(thresholds.get("min_status_days", 10))
    max_lag_sessions = int(thresholds.get("max_lag_sessions", 2))

    n_mature_days = len(rows)
    last_obs_date = max(r["date"] for r in rows) if rows else None
    coverage = min(r.get("coverage", 1.0) for r in rows[-10:]) if rows else None
    lag_trading_days = trading_session_lag(last_obs_date, mature_cutoff_date, trading_days)

    quality = compute_quality_summary(rows, thresholds)
    recent_mean = quality["recent_mean"]
    classified = classify_current_status(
        recent_mean=recent_mean, n_days=n_mature_days,
        lag_trading_days=lag_trading_days, coverage=coverage, thresholds=thresholds,
    )
    evaluation_status = classified["evaluation_status"]
    actionable = classified["actionable"]

    if not rows:
        freshness_status = "unknown"
    elif lag_trading_days is not None and lag_trading_days > max_lag_sessions:
        freshness_status = "stale"
    else:
        freshness_status = "fresh"

    quality_status = quality["quality_status"] if (rows and actionable) else "unknown"

    family_dates = [dr["date"] for dr in daily_records
                    if normalize_model_family(dr.get("version", "")) == family and dr.get("date")]
    latest_prediction_date = max(family_dates) if family_dates else None

    return {
        "model_version": current_version,
        "model_family": family,
        "evaluation_status": evaluation_status,
        "quality_status": quality_status,
        "freshness_status": freshness_status,
        "actionable": actionable,
        "as_of_date": last_obs_date,
        "latest_prediction_date": latest_prediction_date,
        "mature_cutoff_date": mature_cutoff_date,
        "lag_trading_days": lag_trading_days,
        "n_mature_days": n_mature_days,
        "min_required_days": min_status_days,
        "recent_mean": recent_mean,
        "hac": None,
        "shadow_quality_status": None,
    }


def build_status_payload(series, daily_records, current_version, mature_cutoff_date,
                         trading_days, thresholds, as_of_date=None) -> dict:
    """三层契约: historical_summary / current_summary / risk_gate。"""
    if as_of_date is None:
        as_of_date = datetime.now().strftime("%Y-%m-%d")
    family = normalize_model_family(current_version)
    current_rows = [r for r in series if r.get("model_family") == family]

    hist_quality = compute_quality_summary(series, thresholds)
    hist_last = series[-1]["date"] if series else None
    hist_window = series[-RECENT_WINDOW:] if series else []
    historical = {
        "quality_status": hist_quality["quality_status"],
        "freshness_status": "stale" if (hist_last and mature_cutoff_date and hist_last < mature_cutoff_date) else "fresh",
        "as_of_date": hist_last,
        "window_start": hist_window[0]["date"] if hist_window else None,
        "window_end": hist_window[-1]["date"] if hist_window else None,
        "recent_mean": hist_quality["recent_mean"],
        "rank_ic_mean": hist_quality["rank_ic_mean"],
        "rank_icir_30d": hist_quality["rank_icir_30d"],
        "n_mature_days": len(hist_window),
        "quality_reasons": hist_quality["quality_reasons"],
    }

    current = assess_evaluation_readiness(
        rows=current_rows, daily_records=daily_records, family=family,
        mature_cutoff_date=mature_cutoff_date, trading_days=trading_days,
        thresholds=thresholds, current_version=current_version, as_of_date=as_of_date,
    )

    risk_gate = build_legacy_conservative_gate(historical, current)

    return {
        "historical_summary": historical,
        "current_summary": current,
        "risk_gate": risk_gate,
    }


def compute_data_gaps(daily_records, trading_days, min_missing_sessions=3):
    """从 daily_records 预测日期序列找不可恢复缺口(预测归档缺失), 不生成伪造 IC。"""
    dates = sorted({dr["date"] for dr in daily_records if dr.get("date")})
    td = sorted(trading_days)
    gaps = []
    for a, b in zip(dates, dates[1:]):
        missing = [d for d in td if a < d < b]
        if len(missing) >= min_missing_sessions:
            gaps.append({
                "start": missing[0],
                "end": missing[-1],
                "reason": "prediction archive unavailable",
                "recoverable": False,
            })
    return gaps


def _resolve_current_version(daily_records):
    """当前模型版本 = 最近一条 daily_record 的 version; 缺失时读 VERSION 文件。"""
    if daily_records:
        latest = max(daily_records, key=lambda r: r.get("date", ""))
        v = latest.get("version")
        if v:
            return str(v)
    try:
        with open(os.path.join(PROJECT_ROOT, "VERSION"), encoding="utf-8") as f:
            first = f.readline().strip()
            if first:
                return first
    except Exception:
        pass
    return "unknown"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", action="store_true", help="强制全量拉取K线")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    thresholds = load_thresholds()
    cal = _load_json(CALIB_PATH, {"daily_records": []})
    if not cal or not cal.get("daily_records"):
        print("❌ prediction_calibration.json 无 daily_records")
        sys.exit(1)

    symbols = collect_symbols(cal)
    print(f"标的集合: {len(symbols)} 只")

    # 拉取/缓存K线
    kline_maps, trading_days = {}, set()
    failed = []
    for i, sym in enumerate(symbols):
        km = get_kline_map(sym, force=args.backfill)
        if km:
            kline_maps[sym] = km
            trading_days.update(km.keys())
        else:
            failed.append(sym)
        if (i + 1) % 10 == 0:
            print(f"  K线 {i+1}/{len(symbols)}")
    print(f"K线就绪: {len(kline_maps)}/{len(symbols)} | 交易日池: {len(trading_days)} 天")
    if failed:
        print(f"  ⚠️ 失败: {failed}")

    # 回填 realized_return
    filled = fill_realized(cal, kline_maps, trading_days, args.dry_run)
    print(f"回填 realized_return: +{filled} 条")

    # 计算 IC 序列
    series = compute_ic_series(cal, thresholds)
    print(f"IC 序列: {len(series)} 天")
    if series:
        print(f"  范围: {series[0]['date']} -> {series[-1]['date']}")
        rics = [r["rank_ic"] for r in series]
        print(f"  rank_ic: mean={np.mean(rics):.4f} | median={np.median(rics):.4f} | std={np.std(rics):.4f} | min={min(rics):.4f} | max={max(rics):.4f}")
        n_per_day = [r["n"] for r in series]
        print(f"  n/day: mean={np.mean(n_per_day):.1f} | min={min(n_per_day)} | max={max(n_per_day)}")

    summary = compute_drift(series, thresholds)
    summary["deprecated"] = True
    summary["replacement"] = "current_summary"
    summary["updated_at"] = datetime.now().isoformat()
    summary["thresholds"] = thresholds
    print(f"历史漂移判定: {summary['drift_status']} | ICIR30={summary['rank_icir_30d']} | recent20={summary['recent20_mean']}")
    for reason in summary["drift_reasons"]:
        print(f"  ⚠️ {reason}")

    # v4.7.5: 三层状态契约
    daily_records = cal.get("daily_records", [])
    current_version = _resolve_current_version(daily_records)
    mature_cutoff_date = mature_cutoff(trading_days, horizon=5)
    payload = build_status_payload(
        series=series, daily_records=daily_records, current_version=current_version,
        mature_cutoff_date=mature_cutoff_date, trading_days=trading_days,
        thresholds=thresholds,
    )
    historical = payload["historical_summary"]
    current = payload["current_summary"]
    risk_gate = payload["risk_gate"]
    data_gaps = compute_data_gaps(daily_records, trading_days)

    print(f"current={normalize_model_family(current_version)}")
    print(f"evaluation_status={current['evaluation_status']}")
    print(f"historical_window={historical['window_start']}..{historical['window_end']}")
    print(f"risk_gate={risk_gate['policy']} max_new_positions={risk_gate['max_new_positions']}")
    if data_gaps:
        print(f"data_gaps={[(g['start'], g['end']) for g in data_gaps]}")

    # 落盘
    out = {"schema_version": 2, "updated_at": datetime.now().isoformat(), "thresholds": thresholds,
           "series": series, "summary": summary,
           "historical_summary": historical, "current_summary": current,
           "risk_gate": risk_gate, "data_gaps": data_gaps}
    if args.dry_run:
        print("(dry-run, 未写回)")
    else:
        _save_json(IC_PATH, out)
        _save_json(CALIB_PATH, cal)
        print(f"✅ {IC_PATH}")
        print(f"✅ realized 已写回 {CALIB_PATH}")


if __name__ == "__main__":
    main()
