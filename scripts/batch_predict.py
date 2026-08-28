#!/usr/bin/env python3
"""
batch_predict.py — DSL v4.5.5 每日分批预测（增强版）
调度时间: 工作日 04:10, 09:45, 11:00, 14:00

v4.5.5 变更 (2026-05-05):
  S1: 低精度噪声信号分层压制 — acc<45%强制hold, 45~50%置信度上限50%+权重0.6
  S2: 双预测周期矛盾强裁决 — 符号级方向检测(不再依赖阈值), 冲突时强制hold+权重×0.5
  S3: 动态阈值正式切换 — enabled=true, calibration硬底线联动生效
  S4: accuracy数组去重 — 基于训练版本hash, 避免重复记录
  S5: correct_predictions兑现闭环 — daily_records正确计数同步

输出: cache/daily_predict.json (含 h5d_enhanced 和 pool_fallback 双通道, 新增signal_weight字段)
"""
import os, sys, json, math, time, gc, glob
from datetime import datetime, timedelta

# v4.5.20: 加载 .env.local（统一密钥文件），确保 MAIRUI_LICENCE 等API密钥可用
try:
    from dotenv import load_dotenv
    _pr = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _ep = os.path.join(_pr, ".env")
    _elp = os.path.join(_pr, ".env.local")
    if os.path.exists(_ep):
        load_dotenv(_ep, override=False)
    if os.path.exists(_elp):
        load_dotenv(_elp, override=True)
except Exception:
    pass

# 强制代理绕过（akshare/东方财富/麦蕊API等）
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import numpy as np
import yaml
import pandas as pd
import joblib

from scripts.fundamentals_loader import fetch_fundamentals_mairui, fetch_fundamentals_batch

POOL_PATH = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
CACHE_DIR = os.path.join(PROJECT_ROOT, "cache")
ENHANCED_DIR = os.path.join(PROJECT_ROOT, "reports", "predictor")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── v4.5.5: h5d信号阈值（增强噪声抑制）──
H5D_SIGNAL_THRESHOLD = 0.005    # ±0.5% → buy/sell
H5D_MIN_ACCURACY = 0.50         # test精度≥50% → high confidence
H5D_CONFIDENCE_THRESHOLD = 0.50 # h5d信心阈值

# ── v4.6.x: 平滑信号权重（sigmoid连续函数，消除硬门槛）──
SIGMOID_ACC_MIDPOINT = 0.40     # sigmoid中点: acc=40% → weight≈0.5
SIGMOID_ACC_STEEPNESS = 0.06    # sigmoid陡峭度: 越小越陡（过渡区≈±0.12）
SIGNAL_SCORE_SUPPRESS = 0.25    # joint_score < 0.25 → 强制hold（无有效边沿）
RETURN_MAG_STEEPNESS = 0.01     # 收益幅度陡峭度: |ret|≈1% → mag≈0.5

# ── v4.5.2: pool降级阈值 (A: 提升至0.5%) ──
POOL_SIGNAL_THRESHOLD = 0.005   # 与h5d统一为±0.5%

CONFIDENCE_THRESHOLD = 0.58     # pool模型信心阈值保持不变

# ── v4.6.x: 增强预测报告合并窗口 ──
ENHANCED_PARTIAL_WINDOW_HOURS = 48
ENHANCED_FULL_WINDOW_DAYS = 7
ENHANCED_FULL_MIN_COUNT = 10
ENHANCED_FULL_POOL_RATIO = 0.5
ENHANCED_COVERAGE_WARN_RATIO = 0.5

# ── v4.5.3: 动态阈值集成（从adaptive_params.yaml读取）──
def load_adaptive_threshold_config():
    """从 adaptive_params.yaml 读取动态阈值配置"""
    try:
        import yaml
        adaptive_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        if os.path.exists(adaptive_path):
            with open(adaptive_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            dt_cfg = cfg.get("dynamic_threshold", {})
            return {
                "enabled": dt_cfg.get("enabled", False),
                "hard_floor": dt_cfg.get("hard_floor", 0.35),
                "breakeven_accuracy": dt_cfg.get("breakeven_accuracy", 0.304),
            }
    except Exception as e:
        print(f"  ⚠️ 读取动态阈值配置失败: {e}")
    return {"enabled": False, "hard_floor": 0.35, "breakeven_accuracy": 0.304}

_dt_cfg = load_adaptive_threshold_config()
USE_DYNAMIC_THRESHOLD = _dt_cfg["enabled"]

# ── v4.5.3: h20d 信号阈值 ──
H20D_SIGNAL_THRESHOLD = 0.05    # ±5% for 20day horizon
H20D_BUY_MIN_ACCURACY = 0.40   # v4.7.2 P0-2: h20d自身方向精度<40% → 硬禁h20d买入
H20D_MIN_ACCURACY = 0.50        # 最小方向精度

# ── v4.5.3c: pool_fallback 信号权重抑制 ──
POOL_FALLBACK_SIGNAL_WEIGHT = 0.3  # 低精度pool模型信号权重(高噪声标的)

# ── v4.5.5 S2: h5d-h20d 方向矛盾裁决 ──
H5D_H20D_SIGN_FLIP_WEIGHT = 0.5   # 方向相反时信号权重
# v4.6.x: 交叉冲突不再强制hold, 仅减权（让更强方向决定信号）

# ── v4.6.x P0: calibration BUY gate ──
# Horizon-specific submodels may look optimistic while realized/training
# calibration for the same symbol is weak.  Low calibration accuracy must not
# open new BUY exposure; SELL/reduce-risk signals remain allowed.
CALIBRATION_BUY_MIN_ACCURACY = 0.50
CALIBRATION_CRITICAL_ACCURACY = 0.45
MODEL_BUY_MIN_ACCURACY = 0.50
SECTOR_BUY_MIN_AVG_ACCURACY = 0.50


def _calibration_last_accuracy(calibration_stock_accuracy: dict, code: str):
    """Return latest calibration accuracy for code, or None when unavailable."""
    if not calibration_stock_accuracy:
        return None
    for key in (str(code), str(code).zfill(6)):
        info = calibration_stock_accuracy.get(key)
        if not isinstance(info, dict):
            continue
        acc = info.get("last_accuracy")
        if isinstance(acc, (int, float)):
            return float(acc)
        accs = info.get("accuracies", [])
        if accs:
            try:
                return float(accs[-1])
            except Exception:
                return None
    return None


def _apply_calibration_buy_gate(record: dict, calibration_stock_accuracy: dict,
                                threshold: float = CALIBRATION_BUY_MIN_ACCURACY) -> dict:
    """Block BUY when calibration accuracy is below threshold; keep SELL allowed."""
    cal_acc = _calibration_last_accuracy(calibration_stock_accuracy, record.get("symbol", ""))
    if cal_acc is None:
        return record
    record["calibration_last_accuracy"] = round(cal_acc, 4)
    if str(record.get("signal", "")).lower() == "buy" and cal_acc < threshold:
        original_signal = record.get("signal", "buy")
        original_weight = float(record.get("signal_weight", 1.0) or 1.0)
        cap = 0.19 if cal_acc < CALIBRATION_CRITICAL_ACCURACY else 0.39
        record.update({
            "raw_signal": original_signal,
            "signal": "hold",
            "confidence_level": "low",
            "signal_weight": round(min(original_weight, cap), 4),
            "calibration_buy_blocked": True,
            "calibration_gate_reason": f"calibration_last_accuracy={cal_acc:.2%}<50%, BUY blocked",
        })
    return record


def _calibration_allows_buy(calibration_stock_accuracy: dict, code: str,
                            threshold: float = CALIBRATION_BUY_MIN_ACCURACY) -> bool:
    cal_acc = _calibration_last_accuracy(calibration_stock_accuracy, code)
    return cal_acc is None or cal_acc >= threshold


def _apply_model_quality_buy_gate(record: dict,
                                  threshold: float = MODEL_BUY_MIN_ACCURACY) -> dict:
    """Block BUY when the model's own direction accuracy is below threshold."""
    acc = record.get("direction_accuracy", record.get("accuracy"))
    if not isinstance(acc, (int, float)):
        return record
    if str(record.get("signal", "")).lower() == "buy" and float(acc) < threshold:
        original_weight = float(record.get("signal_weight", 1.0) or 1.0)
        record.update({
            "raw_signal": record.get("raw_signal", record.get("signal", "buy")),
            "signal": "hold",
            "confidence_level": "low",
            "signal_weight": round(min(original_weight, 0.39), 4),
            "model_quality_buy_blocked": True,
            "model_quality_gate_reason": f"direction_accuracy={float(acc):.2%}<50%, BUY blocked",
        })
    return record


def _apply_sector_quality_buy_gate(results: list,
                                   threshold: float = SECTOR_BUY_MIN_AVG_ACCURACY) -> set:
    """Block BUY in sectors whose active average direction accuracy is below threshold."""
    active_rows = [
        r for r in results
        if r.get("tier") not in OBSERVATION_TIERS
        and isinstance(r.get("direction_accuracy"), (int, float))
    ]
    sector_acc = {}
    for sector in sorted({r.get("sector", "") for r in active_rows}):
        rows = [r for r in active_rows if r.get("sector", "") == sector]
        if rows:
            sector_acc[sector] = sum(float(r["direction_accuracy"]) for r in rows) / len(rows)

    blocked = set()
    for record in results:
        sector = record.get("sector", "")
        avg_acc = sector_acc.get(sector)
        if avg_acc is None or avg_acc >= threshold:
            continue
        if record.get("tier") in OBSERVATION_TIERS:
            continue
        if str(record.get("signal", "")).lower() != "buy":
            continue
        original_weight = float(record.get("signal_weight", 1.0) or 1.0)
        record.update({
            "raw_signal": record.get("raw_signal", record.get("signal", "buy")),
            "signal": "hold",
            "confidence_level": "low",
            "signal_weight": round(min(original_weight, 0.39), 4),
            "sector_quality_buy_blocked": True,
            "sector_avg_direction_accuracy": round(avg_acc, 4),
            "sector_quality_gate_reason": f"{sector} avg_direction_accuracy={avg_acc:.2%}<50%, BUY blocked",
        })
        blocked.add(record.get("symbol"))
    return blocked

# v4.5.17 P1-4: 并行预测配置 (扩展用: 设为>1时启用ThreadPool)
PREDICT_MAX_WORKERS = 1  # =1串行(默认), >1启用ThreadPoolExecutor

LOW_ACCURACY_SUSPENDED = []  # v4.5.3: 废弃硬编码黑名单，改用 tier 自动分层

# ── v4.5.3: tier → 交易层级映射 ──
OBSERVATION_TIERS = {"cyclical", "flex"}  # 观察池（周期/灵活仓，仅监控不发信号）
TIER_LAYER_MAP = {
    # v4.7.0 P2-2: 对齐现行tier体系(alpha/core/bench), 旧名保留兼容
    "alpha": "蓝筹池",
    "bluechip": "蓝筹池",
    "core": "成长池",
    "growth": "成长池",
    "bench": "观察池",
    "cyclical": "观察池",
    "flex": "观察池",
}

# v4.5.9: 纸交易持仓代码缓存（在 main() 中初始化，预测时永不被挂起）
_PORTFOLIO_CODES = set()


def _load_portfolio_codes() -> set:
    """v4.5.9: 读取纸交易持仓代码 — 持仓股永不被挂起"""
    try:
        import sqlite3
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "paper_trading.db")
        if not os.path.exists(db_path):
            return set()
        conn = sqlite3.connect(db_path)
        codes = {r[0] for r in conn.execute("SELECT stock_code FROM positions WHERE quantity > 0")}
        conn.close()
        return codes
    except Exception:
        return set()


def get_stock_pool():
    """读取去重后的主股票池，返回 [{symbol, name, tier, score}]"""
    with open(POOL_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    seen = set()
    stocks = []
    for s in data.get("master_pool", []):
        sym = s.get("symbol", "")
        if sym and sym not in seen:
            seen.add(sym)
            stocks.append({
                "symbol": sym,
                "name": s.get("name", sym),
                "tier": s.get("tier", "core"),
                "sector": s.get("sector", ""),
                "score": s.get("score", 0),
            })
    return stocks


def _report_symbols(data: dict) -> list:
    """Return valid six-digit stock keys from an enhanced prediction report."""
    if not isinstance(data, dict):
        return []
    return [
        symbol for symbol in data.keys()
        if isinstance(symbol, str) and symbol.isdigit() and len(symbol) >= 6
    ]


def _enhanced_full_threshold(pool_size: int) -> int:
    return max(ENHANCED_FULL_MIN_COUNT, int(pool_size * ENHANCED_FULL_POOL_RATIO))


def _report_training_hash(filepath: str, data: dict) -> str:
    import hashlib
    symbols = _report_symbols(data)
    mean_acc = sum(
        float(data.get(symbol, {}).get("h5d", {}).get("direction_accuracy", 0) or 0)
        for symbol in symbols
    ) / max(len(symbols), 1)
    mtime_str = str(os.path.getmtime(filepath))
    return hashlib.md5(f"{mtime_str}_{mean_acc:.4f}".encode()).hexdigest()[:12]


def _load_enhanced_report(filepath: str):
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return None
    stock_count = len(_report_symbols(data))
    if stock_count == 0:
        return None
    return {
        "path": filepath,
        "name": os.path.basename(filepath),
        "mtime": os.path.getmtime(filepath),
        "data": data,
        "stock_count": stock_count,
        "training_hash": _report_training_hash(filepath, data),
    }


def _select_enhanced_reports(all_files: list, pool_size: int, now_ts: float = None) -> list:
    """Select recent partial reports first, then the latest full report as base."""
    now_ts = now_ts or time.time()
    full_threshold = _enhanced_full_threshold(pool_size)
    partial_cutoff = now_ts - ENHANCED_PARTIAL_WINDOW_HOURS * 3600
    full_cutoff = now_ts - ENHANCED_FULL_WINDOW_DAYS * 24 * 3600
    partial_reports = []
    full_reports = []
    skipped_old = 0

    for filepath in all_files:
        try:
            report = _load_enhanced_report(filepath)
        except Exception as e:
            print(f"  ⚠️ 跳过报告 {os.path.basename(filepath)}: {e}")
            continue
        if not report:
            continue

        is_full = report["stock_count"] >= full_threshold
        if is_full and report["mtime"] >= full_cutoff:
            full_reports.append(report)
        elif (not is_full) and report["mtime"] >= partial_cutoff:
            partial_reports.append(report)
        else:
            skipped_old += 1

    partial_reports.sort(key=lambda r: r["mtime"], reverse=True)
    full_reports.sort(key=lambda r: r["mtime"], reverse=True)
    selected = partial_reports[:]
    if full_reports:
        selected.append(full_reports[0])

    if skipped_old:
        print(f"📁 过滤陈旧增强报告: {skipped_old}个 (小批量>{ENHANCED_PARTIAL_WINDOW_HOURS}h 或全量>{ENHANCED_FULL_WINDOW_DAYS}d)")
    print(
        f"📁 增强报告候选: 小批量{len(partial_reports)}个, "
        f"全量{len(full_reports)}个, 全量阈值≥{full_threshold}只"
    )
    return selected


def load_enhanced_predictions():
    """加载增强预测报告 (h5d), 用近期小批量覆盖、近7天全量补齐。

    最近48h小批量报告优先；最近7天内最新全量报告作为基底。
    按新到旧合并，同一symbol只接受第一条记录，避免旧报告覆盖新报告。
    """
    all_files = sorted(glob.glob(os.path.join(ENHANCED_DIR, "prediction_enhanced_*.json")),
                       key=os.path.getmtime, reverse=True)
    if not all_files:
        print("⚠️ 无增强预测报告，回退到pool模型")
        return {}

    try:
        pool_size = len(get_stock_pool())
    except Exception:
        pool_size = 0

    reports = _select_enhanced_reports(all_files, pool_size)
    if not reports:
        print("⚠️ 最近窗口内无可用增强报告，回退到pool模型")
        return {}

    merged_enhanced = {}
    base_file_name = None
    
    for report in reports:
        filepath = report["path"]
        data = report["data"]
        if base_file_name is None or report["stock_count"] > len(merged_enhanced):
            base_file_name = report["name"]
        try:
            for symbol, sdata in data.items():
                if not (isinstance(symbol, str) and symbol.isdigit() and len(symbol) >= 6):
                    continue
                if symbol in merged_enhanced:
                    continue
                h5d = sdata.get("h5d", {})
                if not h5d or not h5d.get("direction_accuracy", 0):
                    continue
                test_acc = h5d.get("direction_accuracy", 0)
                lgb_acc = h5d.get("lgb_accuracy", 0)
                xgb_acc = h5d.get("xgb_accuracy", 0)
                clf_acc = h5d.get("clf_accuracy", 0)
                if clf_acc > test_acc + 0.03:
                    best_acc = clf_acc
                    acc_source = "classifier_best"
                elif lgb_acc > test_acc + 0.10:
                    best_acc = lgb_acc
                    acc_source = "lgbm_best"
                else:
                    best_acc = test_acc
                    acc_source = "ensemble"
                merged_enhanced[symbol] = {
                    "predicted_return": h5d.get("predicted_return", 0),
                    "direction_accuracy": best_acc,
                    "cv_accuracy": h5d.get("cv_accuracy", 0),
                    "cv_clf_accuracy": h5d.get("cv_clf_accuracy", 0),
                    "horizon": "5d",
                    "training_hash": report["training_hash"],
                    "lgb_accuracy": lgb_acc,
                    "xgb_accuracy": xgb_acc,
                    "clf_accuracy": clf_acc,
                    "acc_source": acc_source,
                }
        except Exception as e:
            print(f"  ⚠️ 跳过报告 {os.path.basename(filepath)}: {e}")
            continue
    
    if not merged_enhanced:
        print("⚠️ 所有增强报告加载失败")
        return {}
    
    acc_ok = sum(1 for v in merged_enhanced.values() if v["direction_accuracy"] >= H5D_MIN_ACCURACY)
    print(f"📁 合并增强报告: {len(merged_enhanced)} 只 (acc≥{H5D_MIN_ACCURACY:.0%}: {acc_ok}, 低于: {len(merged_enhanced)-acc_ok}) [基文件: {base_file_name}]")
    coverage = len(merged_enhanced) / max(pool_size, 1)
    if pool_size and coverage < ENHANCED_COVERAGE_WARN_RATIO:
        print(
            f"🚨 增强预测覆盖率过低: {len(merged_enhanced)}/{pool_size} "
            f"({coverage:.1%})，缺口将回退到pool_fallback"
        )
    return merged_enhanced
def fetch_kline(code: str) -> pd.DataFrame:
    """获取股票K线数据 (pool fallback用)"""
    try:
        from dsl_data_sdk_original import get_kline
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        data = get_kline(code, start, end)
        if data and len(data) >= 60:
            df = pd.DataFrame(data)
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = df[col].astype(float)
            return df
    except Exception as e:
        print(f"  ⚠️ K线获取失败 {code}: {e}")
    return None


def compute_h5d_signal(pred_return: float, accuracy: float, vol_scale: float = 1.0) -> dict:
    """v4.6.x: 平滑信号权重 — sigmoid联合精度×收益幅度判信号
    v4.6.9f P1-2: 双门槛 — 方向精度≥55% 且 |预测收益|≥1% 才允许buy/sell
    (55%精度+2%盈亏-0.2%费用=0边际, 需更严门槛才有正期望)
    v4.7.0 P2-1: 波动率自适应 — min_ret和±阈值均×vol_scale
    (高波标的抬高触发线避免噪音交易, 低波标的降低触发线捕捉弱信号)

    权重计算: w_acc = sigmoid((acc - 0.40) / 0.06)
    幅度评分: mag = sigmoid(|ret| / 0.01)
    综合分数: signal_score = w_acc × mag

    signal_score < 0.25 → 强制hold（精度与收益均不足以提供边沿）
    否则 → 按阈值正常生成buy/sell，加权signal_score
    置信度 = accuracy（无人工上限）"""

    w_acc = 1.0 / (1.0 + math.exp(-(accuracy - SIGMOID_ACC_MIDPOINT) / SIGMOID_ACC_STEEPNESS))
    mag_score = 1.0 / (1.0 + math.exp(-abs(pred_return) / RETURN_MAG_STEEPNESS))
    signal_score = max(0.0, min(1.0, w_acc * mag_score))

    # v4.6.9f P1-2: 双门槛 — 精度≥55% 且 |收益|≥1% (个人投资者正期望门槛)
    # 从 adaptive_params.yaml 的 signal 段读取(可调), 默认: MIN_SIGNAL_ACC=0.55, MIN_SIGNAL_RET=0.01
    _min_acc = 0.55
    _min_ret = 0.01
    try:
        import yaml as _yaml
        with open(os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml"), "r", encoding="utf-8") as _f:
            _ap = _yaml.safe_load(_f) or {}
        _h = (_ap.get("signal", {}) or {})
        _min_acc = float(_h.get("min_accuracy", 0.55))
        _min_ret = float(_h.get("min_return", 0.01))
    except Exception:
        pass

    if signal_score < SIGNAL_SCORE_SUPPRESS:
        signal = "hold"
        signal_weight = signal_score
    else:
        # v4.7.0 P2-1: 波动率自适应 — 有效门槛 = 基础门槛 × vol_scale
        _min_ret_eff = _min_ret * vol_scale
        _h5d_thr_eff = H5D_SIGNAL_THRESHOLD * vol_scale
        if accuracy >= _min_acc and abs(pred_return) >= _min_ret_eff:
            if pred_return > _h5d_thr_eff:
                signal = "buy"
            elif pred_return < -_h5d_thr_eff:
                signal = "sell"
            else:
                signal = "hold"
            signal_weight = signal_score
        else:
            # 精度或幅度不足 → 降级hold (保留signal_weight供观察)
            signal = "hold"
            signal_weight = signal_score * 0.5

    return {
        "signal": signal,
        "confidence": round(accuracy, 4),
        "predicted_return": round(pred_return, 4),
        "accuracy": accuracy,
        "signal_weight": round(signal_weight, 4),
    }


# ═══════════════════════════════════════════════════════════════
# v4.5.4: P0-2 截面排序特征 (Cross-sectional Rank)
# ═══════════════════════════════════════════════════════════════

CROSS_SECTIONAL_RANK_FEATURES = [
    "mom_5d", "mom_10d", "mom_20d",
    "vol_ratio_20", "vol_change",
    "atr14", "rsi14", "amplitude", "turnover",
]


def compute_cross_sectional_ranks(all_stock_features: dict, stock_codes: list) -> dict:
    """
    对所有活跃标的的最新一行计算 cross-sectional rank (0-1 分位)。
    
    Args:
        all_stock_features: {code: features_df} 每个标的的特征DataFrame
        stock_codes: 需要计算 rank 的标的列表
    
    Returns:
        {code: {rank_feat_name: rank_value}} 每个标的的 rank 特征
    """
    # 汇总所有标的的最新行
    latest_rows = {}
    for code in stock_codes:
        df = all_stock_features.get(code)
        if df is not None and len(df) > 0:
            latest_rows[code] = df.iloc[-1]
    
    if len(latest_rows) < 2:
        # 不足2只标的无法计算截面rank, 返回0
        result = {}
        for code in stock_codes:
            result[code] = {f"rank_{feat}": 0.5 for feat in CROSS_SECTIONAL_RANK_FEATURES}
        return result
    
    n_stocks = len(latest_rows)
    
    # 对每个特征计算 rank
    all_ranks = {}
    for feat in CROSS_SECTIONAL_RANK_FEATURES:
        # 收集该特征所有值
        values = {}
        for code in latest_rows:
            row = latest_rows[code]
            if feat in row.index:
                v = row[feat]
                if not np.isnan(v) and not np.isinf(v):
                    values[code] = v
        
        if len(values) < 2:
            # 数据不足, 所有标的给0.5
            for code in stock_codes:
                if code not in all_ranks:
                    all_ranks[code] = {}
                all_ranks[code][f"rank_{feat}"] = 0.5
            continue
        
        # v4.5.17 P1-8: pandas向量化rank替代O(n²)手动排序
        import pandas as _pd
        _values_series = _pd.Series(values)
        _ranks_pct = _values_series.rank(pct=True)
        for code in _ranks_pct.index:
            if code not in all_ranks:
                all_ranks[code] = {}
            all_ranks[code][f"rank_{feat}"] = round(float(_ranks_pct[code]), 6)
        for code in stock_codes:
            if code not in all_ranks:
                all_ranks[code] = {}
                all_ranks[code][f"rank_{feat}"] = 0.5
    
    return all_ranks


# ═══════════════════════════════════════════════════════════════
# v4.5.3: h20d (20日预测) 通道 — 实时特征工程 + 模型推理
# ═══════════════════════════════════════════════════════════════

def build_h20d_features(df: pd.DataFrame) -> pd.DataFrame:
    """复刻 train_predictor_enhanced.py 的 build_features() 产生 ~84 维原始特征。
    输出行的顺序与训练时一致，不含 target 列。
    v4.6.2: 补全 v4.5.7 新增的截面百分位(12) + 情绪因子(8) + 市场状态(10)。"""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_p = df["open"].astype(float)
    prev = df.get("prev_close", close.shift(1)).astype(float)

    feats = pd.DataFrame(index=df.index)

    # — 价格特征 (12+3=15维) —
    for w in [5, 10, 20, 30, 60, 120]:
        ma = close.rolling(w).mean()
        if w < len(close):
            feats[f"ma{w}_ratio"] = close / (ma + 1e-8)
            feats[f"vol{w}d"] = close.rolling(w).std() / close
    feats["above_ma20"] = (close > close.rolling(20).mean()).astype(int)
    feats["above_ma60"] = (close > close.rolling(60).mean()).astype(int)
    feats["ma20_slope"] = close.rolling(20).mean().diff(5) / (close.rolling(20).mean() + 1e-8)

    # — 动量 (8维) —
    for w in [1, 3, 5, 10, 20, 40, 60, 120]:
        if w < len(close):
            feats[f"mom_{w}d"] = close.pct_change(w)

    # — 量价 (6维) —
    feats["vol_ratio_5"] = volume / (volume.rolling(5).mean() + 1e-8)
    feats["vol_ratio_10"] = volume / (volume.rolling(10).mean() + 1e-8)
    feats["vol_ratio_20"] = volume / (volume.rolling(20).mean() + 1e-8)
    feats["vol_change"] = volume.pct_change()
    feats["up_days_10"] = (close > close.shift(1)).rolling(10).mean()
    feats["up_days_20"] = (close > close.shift(1)).rolling(20).mean()

    # — 波动/价差 (5维) —
    feats["atr14"] = (high - low).rolling(14).mean() / close
    feats["hl_ratio"] = (high - low) / close
    feats["close_pos"] = (close - low) / (high - low + 1e-8)
    feats["open_gap"] = (open_p - prev) / (prev + 1e-8)
    feats["amplitude"] = (high - low) / (prev + 1e-8)

    # — RSI/MACD/布林 (6维) —
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    feats["rsi14"] = 100 - (100 / (1 + gain / (loss + 1e-8)))
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    feats["macd"] = ema12 - ema26
    feats["macd_sig"] = feats["macd"].ewm(span=9).mean()
    feats["macd_hist"] = feats["macd"] - feats["macd_sig"]
    bb = close.rolling(20).mean()
    bs = close.rolling(20).std()
    feats["bb_pos"] = (close - bb) / (2 * bs + 1e-8)
    feats["bb_width"] = (2 * bs) / (bb + 1e-8)

    # — 趋势/反转 (5维) —
    feats["new_high_20"] = (close == close.rolling(20).max()).astype(int)
    feats["new_low_20"] = (close == close.rolling(20).min()).astype(int)
    feats["drawdown_20"] = close / close.rolling(20).max() - 1
    feats["drawdown_60"] = close / close.rolling(60).max() - 1
    feats["price_52w_pos"] = close / close.rolling(250).max()

    # — 换手率 (2维) —
    feats["turnover"] = volume / (volume.rolling(250).mean() + 1e-8)
    feats["amount_ratio"] = (volume * close).rolling(5).mean() / ((volume * close).rolling(20).mean() + 1e-8)

    # — 基本面 (7维，将在外部通过 add_fundamentals_to_features() 填充) —
    # 这里留空位, batch_predict 的 main() 会通过 cross-sectional pipeline 填充
    for k in ["roe", "eps", "bps", "cfps", "revenue_growth", "gross_margin", "capex_ratio"]:
        feats[f"fund_{k}"] = 0.0

    # — v4.5.7: 截面特征 — 历史百分位（票内rank）(12维) —
    for n, period in [("5", 5), ("10", 10), ("20", 20), ("60", 60)]:
        if f"mom_{period}d" in feats.columns:
            feats[f"mom_{n}d_pct"] = feats[f"mom_{period}d"].rank(pct=True)
        if f"vol{period}d" in feats.columns:
            feats[f"vol_{n}d_pct"] = feats[f"vol{period}d"].rank(pct=True)
        feats[f"vol_ratio_{n}_pct"] = feats.get(f"vol_ratio_{period}", pd.Series(0, index=feats.index)).rank(pct=True)

    # — v4.5.7: 情绪因子 (8维) —
    feats["vp_divergence_5"] = feats["mom_5d"] * (1 - feats["vol_ratio_5"])
    feats["vp_divergence_10"] = feats["mom_10d"] * (1 - feats["vol_ratio_10"])
    vol_ma20 = volume.rolling(20).mean()
    feats["volume_spike"] = (volume > vol_ma20 * 2).astype(int)
    feats["volume_abnormal"] = volume / (vol_ma20 + 1e-8) - 1
    feats["down_vol_5"] = ((feats["mom_5d"] < -0.03) & (feats["vol_ratio_5"] > 1.5)).astype(int)
    ma5_v = close.rolling(5).mean()
    feats["intraday_strength"] = (close / open_p - 1) / (high / low - 1 + 1e-8)
    feats["close_to_high"] = (close - low) / (high - low + 1e-8)
    feats["contrarian_strength"] = ((feats["mom_5d"] < 0) & (close > ma5_v)).astype(int)

    # — v4.5.7: 市场状态因子 (10维) —
    vol_30 = close.pct_change().rolling(30).std()
    vol_median = vol_30.rolling(250).median()
    feats["vol_regime"] = (vol_30 / (vol_median + 1e-8) - 1).clip(-0.5, 2)
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    feats["bull_arrange"] = ((ma5_v > ma20) & (ma20 > ma60) & (ma60 > ma120)).astype(int)
    feats["bear_arrange"] = ((ma5_v < ma20) & (ma20 < ma60) & (ma60 < ma120)).astype(int)
    peak_60 = close.rolling(60).max()
    drawdown = close / peak_60 - 1
    feats["drawdown_regime"] = (drawdown < -0.1).astype(int)
    feats["recovery_stage"] = (close.rolling(20).mean() > close.rolling(60).mean()).astype(int)
    feats["mom_decay_5x20"] = feats["mom_5d"] - feats["mom_20d"]
    feats["mom_decay_10x60"] = feats["mom_10d"] - feats["mom_60d"]
    feats["volume_trend"] = feats["vol_ratio_20"].rolling(10).mean() > 1
    feats["price_volume_harmony"] = (feats["mom_20d"] > 0) & feats["volume_trend"]

    # — v4.6.2: 截面rank特征列位 (9维空位，由 compute_cross_sectional_ranks 填充) —
    # 重训后模型期望92维，此处预留列名以对齐
    for rank_col in ["rank_mom_5d", "rank_mom_10d", "rank_mom_20d",
                     "rank_vol_ratio_20", "rank_vol_change", "rank_atr14",
                     "rank_rsi14", "rank_amplitude", "rank_turnover"]:
        feats[rank_col] = 0.0  # 占位，推理时由 cross_ranks 填充

    feats = feats.replace([np.inf, -np.inf], np.nan)
    return feats


def fetch_kline_h20d(code: str) -> pd.DataFrame:
    """获取更多K线数据用于h20d推理（至少600天，覆盖250日滚动窗口）"""
    try:
        from dsl_data_sdk_original import get_kline
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=600)).strftime("%Y-%m-%d")
        data = get_kline(code, start, end)
        if data and len(data) >= 300:
            df = pd.DataFrame(data)
            for col in ["open", "high", "low", "close", "volume"]:
                if col in df.columns:
                    df[col] = df[col].astype(float)
            return df
    except Exception as e:
        print(f"  ⚠️ h20d K线获取失败 {code}: {e}")
    return None


def load_h20d_models(stock_pool: list) -> dict:
    """遍历股票池，加载 h20d 模型（lightgbm + scaler + selector）"""
    models_dir = os.path.join(PROJECT_ROOT, "models")
    loaded = {}
    for s in stock_pool:
        code = s["symbol"]
        model_path = os.path.join(models_dir, code, "lightgbm_20d.pkl")
        scaler_path = os.path.join(models_dir, code, "scaler_20d.pkl")
        selector_path = os.path.join(models_dir, code, "selector_20d.pkl")
        if all(os.path.exists(p) for p in [model_path, scaler_path, selector_path]):
            try:
                loaded[code] = {
                    "model": joblib.load(model_path),
                    "scaler": joblib.load(scaler_path),
                    "selector": joblib.load(selector_path),
                }
            except Exception as e:
                print(f"  ⚠️ 加载h20d模型失败 {code}: {e}")
        else:
            print(f"  ⚠️ 无h20d模型 {code}")
    print(f"  🤖 h20d模型: {len(loaded)}/{len(stock_pool)} 只")
    return loaded


def compute_h20d_predictions(h20d_models: dict, stock_pool: list) -> dict:
    """
    为每只股票计算 h20d 预测。
    
    v4.5.4 P0改进:
    - P0-1: 接入真实基本面特征
    - P0-2: 截面排序特征 (Cross-sectional Rank)
    
    Args:
        h20d_models: {code: {model, scaler, selector}} 模型字典
        stock_pool: [{symbol, name}] 股票池
    
    Returns:
        {code: {predicted_return, direction_accuracy, horizon}}
    """
    # 1. 为所有股票批量获取K线和基本面
    stock_codes = [s["symbol"] for s in stock_pool]
    all_features = {}
    fund_data = {}
    
    print(f"  📡 获取K线数据和基本面...")
    for code in stock_codes:
        df = fetch_kline_h20d(code)
        if df is None or len(df) < 300:
            continue
        features = build_h20d_features(df)
        features = features.dropna()
        if len(features) == 0:
            continue
        all_features[code] = features
        
        # P0-1: 获取真实基本面
        fund = fetch_fundamentals_mairui(code)
        fund_data[code] = fund
    
    print(f"  ✅ 有效K线: {len(all_features)}/{len(stock_codes)} 只")
    
    # 统计基本面覆盖
    fund_covered = sum(1 for f in fund_data.values() if sum(1 for v in f.values() if v != 0.0) >= 3)
    print(f"  📊 基本面有效覆盖: {fund_covered}/{len(stock_codes)} 只")
    
    # 2. 计算截面排序特征 (P0-2)
    cross_ranks = compute_cross_sectional_ranks(all_features, list(all_features.keys()))
    print(f"  📊 截面rank特征: {len(CROSS_SECTIONAL_RANK_FEATURES)}维")
    
    # 3. 将基本面+rank特征填入每只标的的最新特征向量
    enhanced_features = {}  # {code: enhanced_df}
    for code, df in all_features.items():
        # 复制以避免修改原df
        enhanced_df = df.copy()
        
        # 填充基本面 (P0-1)
        fund = fund_data.get(code, {})
        for k in ["roe", "eps", "bps", "cfps", "revenue_growth", "gross_margin", "capex_ratio"]:
            v = fund.get(k, 0.0)
            col = f"fund_{k}"
            if col in enhanced_df.columns:
                enhanced_df[col] = v
        
        # 填充截面rank特征 (P0-2)
        # v4.6.2 fix: 只填充训练时已存在的rank列，避免新增列导致维度不匹配
        # 模型重训后(build_h20d_features包含rank列)会自动生效
        ranks = cross_ranks.get(code, {})
        for rank_name, rank_val in ranks.items():
            if rank_name in enhanced_df.columns:
                enhanced_df[rank_name] = rank_val
        
        enhanced_features[code] = enhanced_df
    
    # 4. 推理
    print(f"  🤖 加载模型进行推理...")
    results = {}
    success = fail = 0

    for code, bundle in h20d_models.items():
        try:
            enhanced_df = enhanced_features.get(code)
            if enhanced_df is None or len(enhanced_df) == 0:
                fail += 1
                continue

            latest = enhanced_df.iloc[[-1]]
            raw = latest.values

            # ⚠️ 每只标的分辨初始化期望特征数 (不同模型可能不同维度)
            expected_feat_count = bundle["scaler"].mean_.shape[0]

            # v4.6.2: 特征维度校验
            if raw.shape[1] != expected_feat_count:
                if raw.shape[1] > expected_feat_count:
                    # 推理特征数>模型特征数: 旧模型未包含rank_*特征, 裁剪冗余特征
                    print(f"    ⚠️ {code} 特征维度: 推理{raw.shape[1]} > 模型{expected_feat_count}, 裁剪{raw.shape[1]-expected_feat_count}个冗余特征(rank_*)")
                    raw = latest.iloc[:, :expected_feat_count].values
                else:
                    # 推理特征数<模型特征数: 异常情况
                    print(f"    🚨 {code} 特征维度不匹配: 推理{raw.shape[1]} vs 模型{expected_feat_count}")
                    raise ValueError(f"特征维度不匹配: 推理{raw.shape[1]} vs 模型{expected_feat_count}")

            X_scaled = bundle["scaler"].transform(raw)
            X_selected = bundle["selector"].transform(X_scaled)
            pred_log = bundle["model"].predict(X_selected)[0]
            
            # v4.6.2: 分位数回归(中位数) + log目标 → expm1还原
            # 比均值回归鲁棒: 极端偏斜标的(如光迅76%↑)中位数更合理
            pred = float(np.sign(pred_log) * np.expm1(np.abs(pred_log)))
            # 截断 ±25% — 20日收益超此边界为异常事件, 模型不应拟合
            pred = float(np.clip(pred, -0.25, 0.25))

            direction_acc = 0.50  # 默认中性

            results[code] = {
                "predicted_return": round(float(pred), 6),
                "direction_accuracy": direction_acc,
                "horizon": "20d",
            }
            success += 1
        except Exception as e:
            fail += 1
            print(f"  ❌ h20d推理失败 {code}: {e}")

    print(f"  🎯 h20d: {success}成功/{fail}失败")
    return results


def main():
    print("=" * 60)
    print(f"🔮 DSL {_read_version()} 每日分批预测 (h5d+h20d双通道 + 进度追踪)")
    print("=" * 60)
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"h5d阈值: ±{H5D_SIGNAL_THRESHOLD:.1%} | h20d阈值: ±{H20D_SIGNAL_THRESHOLD:.0%} | pool降级阈值: ±{POOL_SIGNAL_THRESHOLD:.1%}")

    # v4.5.9: 加载持仓代码 — 持仓股永不被挂起
    global _PORTFOLIO_CODES
    _PORTFOLIO_CODES = _load_portfolio_codes()
    if _PORTFOLIO_CODES:
        print(f"📦 持仓保护: {len(_PORTFOLIO_CODES)}只 — 永不被挂起 ({', '.join(sorted(_PORTFOLIO_CODES))})")

    # v4.5.3d: 进度追踪
    try:
        from common.progress_tracker import ProgressTracker
        # v4.6: 通过环境变量区分09:00盘前/17:00收盘后两个cron的进度文件
        _progress_task = os.environ.get("BATCH_PREDICT_SLOT", "batch_predict")
        tracker = ProgressTracker(_progress_task, total_steps=1)
    except ImportError:
        tracker = None

    # v4.5.17 P1-5: train→predict 依赖检查 — batch_train是否已成功完成
    try:
        _train_progress_files = sorted(
            __import__("glob").glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "cache", "progress", "batch_train_*.json")),
            key=os.path.getmtime, reverse=True
        )
        if _train_progress_files:
            with open(_train_progress_files[0]) as _tf:
                _train_data = __import__("json").load(_tf)
            _train_status = _train_data.get("status", "unknown")
            _train_msg = _train_data.get("message", "")
            if _train_status == "completed":
                print(f"✅ 依赖检查通过: batch_train 已成功完成 ({_train_msg})")
            else:
                print(f"⚠️ 依赖检查: batch_train 状态={_train_status} — 可能使用旧模型权重")
        else:
            print("⚠️ 依赖检查: 无batch_train进度文件 — 可能使用旧模型权重")
    except Exception as _e:
        print(f"ℹ️ 依赖检查跳过: {_e}")
    
    # B: 加载增强h5d预测
    enhanced = load_enhanced_predictions()
    
    # Pool fallback (仅对enhanced中不存在的标的)
    from core.pool_predictor import get_predictor
    predictor = get_predictor()
    pool_codes = predictor.get_all_codes()
    print(f"🤖 PoolPredictor: {len(pool_codes)} 个模型 (降级备用)")

    # v4.5.3: 加载 h20d 模型
    stocks = get_stock_pool()
    print(f"📋 股票池: {len(stocks)} 只标的")
    h20d_models = load_h20d_models(stocks)
    # v4.5.4: P0改进的h20d预测 (基本面+截面rank特征)
    h20d_preds = compute_h20d_predictions(h20d_models, stocks)

    # ── v4.7.0 P2-1: 波动率自适应阈值 ──
    _vol_scales = {}
    try:
        _vol_map = _compute_vol20_map([s["symbol"] for s in stocks])
        _vol_scales = _vol_adaptive_scales(_vol_map, [s["symbol"] for s in stocks])
        if _vol_scales:
            _scaled = {c: round(v, 2) for c, v in _vol_scales.items() if abs(v - 1.0) > 0.01}
            print(f"🌊 波动率自适应阈值: ✅ 启用 ({len(_scaled)}只缩放: {_scaled})")
        else:
            print(f"🌊 波动率自适应阈值: ❌ 未启用 (静态阈值)")
    except Exception as _ve:
        print(f"🌊 波动率自适应阈值: 计算失败({_ve}), 回退静态")
        _vol_scales = {}

    # ── v4.5.3: 动态阈值引擎初始化 ──
    dt_engine = None
    if USE_DYNAMIC_THRESHOLD:
        from core.dynamic_threshold import DynamicThresholdEngine
        dt_engine = DynamicThresholdEngine(enabled=True)
        print(f"🧠 动态阈值: ✅ 已启用 (hard_floor={_dt_cfg['hard_floor']:.0%}, 盈亏平衡={_dt_cfg['breakeven_accuracy']:.1%})")
    else:
        print(f"🧠 动态阈值: ❌ 未启用 (使用固定阈值: h5d≥{H5D_CONFIDENCE_THRESHOLD:.0%}, pool≥{CONFIDENCE_THRESHOLD:.0%})")

    # v4.5.9: 加载校准数据(flex标的accuracy fallback)
    _calibration_stock_accuracy = {}
    try:
        cal_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "confidence_data", "prediction_calibration.json")

        if os.path.exists(cal_path):
            with open(cal_path) as _cf:
                _cal_data = json.load(_cf)
            _calibration_stock_accuracy = _cal_data.get("stock_accuracy", {})
            if _calibration_stock_accuracy:
                print(f"📊 校准数据: {len(_calibration_stock_accuracy)} 只标的 (flex精度fallback)")
    except Exception as _e:
        print(f"⚠️ 校准数据加载失败: {_e}")
    # v4.6.5: flex精度恢复 — 自动提升top-3高精度flex标的至growth层
    _flex_promoted = set()
    try:
        _flex_accuracy_candidates = []
        for _sym, _v in _calibration_stock_accuracy.items():
            _accs = _v.get('accuracies', [])
            if _accs:
                _mean10 = sum(_accs[-10:]) / min(10, len(_accs))
                _flex_accuracy_candidates.append((_sym, _v.get('name', _sym), _mean10))
        _flex_accuracy_candidates.sort(key=lambda x: x[2], reverse=True)
        # 取top-3但精度必须>55%(低于此阈值提升无意义)
        _flex_promoted_count = 0
        for _sym, _name, _acc in _flex_accuracy_candidates[:3]:
            if _acc >= 0.55:
                _flex_promoted.add(_sym)
                _flex_promoted_count += 1
        if _flex_promoted:
            print(f"🚀 flex精度恢复: {_flex_promoted_count}只提升至growth层 ({', '.join(sorted(_flex_promoted))})")
    except Exception:
        pass
    # v4.5.17 P2-12: 检查重训队列积压告警
    try:
        from core.retrain_queue_manager import get_retrain_queue_manager
        _rq_summary = get_retrain_queue_manager().get_summary()
        _rq_count = _rq_summary.get("queued", 0) + _rq_summary.get("training", 0)
        if _rq_count > 10:
            print(f"  ⚠️ 重训队列积压: {_rq_count}条待处理 — batch_train可能未及时消费")
        elif _rq_count > 0:
            print(f"  📋 重训队列: {_rq_count}条待处理")
    except Exception:
        pass
    
    results = []
    h5d_hit, h5d_miss = 0, 0
    pool_hit, pool_miss = 0, 0
    h20d_hit, h20d_miss = 0, 0
    cross_count = 0  # v4.5.3d
    suspended = 0
    fail = 0

    for stock in stocks:
        code = stock["symbol"]
        name = stock["name"]
        tier = stock.get("tier", "core")
        sector = stock.get("sector", "")
        
        # v4.5.3: tier自动分层 — 观察池（cyclical/flex）仅监控，不发交易信号
        # v4.5.9: 持仓股永不被挂起 — 风控要求必须持续监控
        # v4.6.5: flex精度恢复 — _flex_promoted 中的标的自动升为growth层
        if tier in OBSERVATION_TIERS and code not in _PORTFOLIO_CODES and code not in _flex_promoted:
            layer = TIER_LAYER_MAP.get(tier, "观察池")
            # 保留真实精度（从h5d增强/h20d/校准获取），不置0，避免校准脱节误报
            # v4.5.9b: 修复三级fallback——enhanced有但acc=0时也要继续往下走
            h20d_r = h20d_preds.get(code, {})
            real_acc = 0.0
            _enhanced_acc = enhanced.get(code, {}).get("direction_accuracy", 0)
            if _enhanced_acc > 0:
                real_acc = _enhanced_acc
            elif h20d_r.get("direction_accuracy", 0) > 0:
                real_acc = h20d_r.get("direction_accuracy", 0)
            elif code in _calibration_stock_accuracy:
                # Fallback: 从校准数据读取最新精度
                accs_list = _calibration_stock_accuracy[code].get("accuracies", [])
                if accs_list:
                    real_acc = accs_list[-1]
            print(f"  🔍 {code:6s} {name:8s} [{layer}, 仅监控] acc={real_acc:.1%}")
            results.append({
                "symbol": code, "name": name, "signal": "hold",
                "score": 5.0, "confidence": 0, "confidence_level": "suspended",
                "predicted_return": 0, "horizon": "suspended",
                "source": "suspended", "tier": tier, "layer": layer,
                "sector": sector,
                "accuracy": real_acc, "direction_accuracy": real_acc,
                "h20d": h20d_r if h20d_r else {"predicted_return": 0, "direction_accuracy": 0.50, "horizon": "20d"},
                "predict_time": datetime.now().isoformat(),
            })
            suspended += 1
            continue
        
        # ═══════════════ B: 优先使用h5d增强预测 ═══════════════
        if code in enhanced:
            e = enhanced[code]
            sig = compute_h5d_signal(e["predicted_return"], e["direction_accuracy"],
                                      vol_scale=_vol_scales.get(code, 1.0))
            
            # v4.5.5 S2: 多Horizon交叉确认（强化方向矛盾裁决）
            h20d_r = h20d_preds.get(code, {})
            h20d_ret = h20d_r.get("predicted_return", 0)
            # 使用符号方向（不要求超过阈值），±0即可判定方向
            h20d_dir = 1 if h20d_ret > 0 else (-1 if h20d_ret < 0 else 0)
            h5d_dir = 1 if sig["predicted_return"] > 0 else (-1 if sig["predicted_return"] < 0 else 0)
            
            cross_confirmed = False
            cross_conflict = False  # v4.5.6: 显式标记h5d×h20d方向冲突
            cross_boost = 0.0
            
            if h5d_dir != 0 and h20d_dir != 0:
                if h5d_dir == h20d_dir:
                    # 方向一致 → 置信度提升
                    cross_confirmed = True
                    cross_boost = 0.08
                    # signal_weight 在一致时取二者平均（保持原权重上限）
                    if "signal_weight" in sig:
                        sig["signal_weight"] = min(1.0, sig["signal_weight"] + 0.1)
                else:
                    # v4.6.x: 方向相反 → 权重减半但不再强制hold（让更强的方向决定信号）
                    cross_conflict = True
                    sig["confidence"] = round(sig["confidence"] * H5D_H20D_SIGN_FLIP_WEIGHT, 4)
                    sig["signal_weight"] = round(sig.get("signal_weight", 1.0) * H5D_H20D_SIGN_FLIP_WEIGHT, 4)
            elif h5d_dir != 0 and h20d_dir == 0:
                # h20d方向中性（接近0%收益）→ 保持h5d, 微降信心
                sig["confidence"] = round(sig["confidence"] * 0.95, 4)
            
            if cross_confirmed:
                sig["confidence"] = round(min(sig["confidence"] + cross_boost, 0.95), 4)
            
            conf = sig["confidence"]
            # P0-3: 动态阈值或固定阈值判定高信度
            if USE_DYNAMIC_THRESHOLD and dt_engine:
                dt_result = dt_engine.compute(code=code, accuracy=e["direction_accuracy"])
                stock_threshold = dt_result["threshold"]
                is_high = (conf >= stock_threshold and sig["signal"] != "hold")
            else:
                stock_threshold = H5D_CONFIDENCE_THRESHOLD
                is_high = (conf >= H5D_CONFIDENCE_THRESHOLD 
                           and sig["signal"] != "hold"
                           and e["direction_accuracy"] >= H5D_MIN_ACCURACY)
            
            # v4.5.5 S1: 从sig提取signal_weight（已由compute_h5d_signal分层压制）
            sig_weight = sig.get("signal_weight", 1.0)
            
            # v4.6.5: Transformer预测集成
            # v4.6.9f P1-3: 动态权重 — 按Transformer自身方向精度调 0.15~0.35
            # 精度<50% → 0.15(降权); ≥58% → 0.35(升权); 线性插值
            tf_pred = {"predicted_return": 0, "confidence": 0, "signal": "hold"}
            try:
                from core.transformer_model import load_transformer, predict_transformer
                model_dir = os.path.join(PROJECT_ROOT, "models", code)
                tf_data = load_transformer(model_dir, code)
                if tf_data:
                    tf_pred = predict_transformer(tf_data, features_df_raw)
                    if tf_pred.get("signal") != "hold":
                        # v4.6.9f P1-3: 动态权重
                        _tf_acc = float(tf_data.get("direction_accuracy", 0.5) or 0.5)
                        if _tf_acc >= 0.58:
                            tf_weight = 0.35
                        elif _tf_acc <= 0.50:
                            tf_weight = 0.15
                        else:
                            tf_weight = round(0.15 + (_tf_acc - 0.50) / 0.08 * 0.20, 3)
                        sig["predicted_return"] = round(
                            sig["predicted_return"] * (1 - tf_weight) + tf_pred["predicted_return"] * tf_weight, 6)
                        sig["confidence"] = round(
                            max(sig["confidence"], tf_pred["confidence"] * tf_weight + sig["confidence"] * (1 - tf_weight)), 4)
            except Exception:
                pass
            
            record = {
                "symbol": code, "name": name,
                "signal": sig["signal"],
                "score": round(5.0 + sig["predicted_return"] * 100, 2),
                "confidence": conf,
                "confidence_level": "high" if is_high else "low",
                "threshold_used": round(stock_threshold, 4),
                "tier": tier,
                "sector": sector,
                "layer": TIER_LAYER_MAP.get(tier, "成长池"),
                "predicted_return": sig["predicted_return"],
                "accuracy": e["direction_accuracy"],
                "direction_accuracy": e["direction_accuracy"],
                "cv_accuracy": e["cv_accuracy"],
                "horizon": "5d", "source": "h5d_enhanced",
                "h20d": h20d_preds.get(code, {"predicted_return": 0, "direction_accuracy": 0.50, "horizon": "20d"}),
                "signal_weight": sig_weight,
                "cross_confirmed": cross_confirmed,
                "cross_conflict": cross_conflict,
                "cross_boost": round(cross_boost, 2),
                "predict_time": datetime.now().isoformat(),
                # v4.5.5 S4: 训练版本hash (用于accuracy去重)
                "training_hash": e.get("training_hash", ""),
            }
            record = _apply_calibration_buy_gate(record, _calibration_stock_accuracy)
            record = _apply_model_quality_buy_gate(record)
            is_high = is_high and not (
                record.get("calibration_buy_blocked", False)
                or record.get("model_quality_buy_blocked", False)
            )
            
            if is_high:
                h5d_hit += 1
                if cross_confirmed: cross_count += 1
                xmark = "🟢" if cross_confirmed else ("🔴" if h5d_dir != 0 and h20d_dir != 0 else "🟡")
                print(f"  {xmark} {code:6s} {name:8s} h5d={sig['signal']:4s} 收益={sig['predicted_return']:+.2%} 精度={e['direction_accuracy']:.0%} [HIGH]", end="")
                if cross_confirmed: print(" ✨conf", end="")
                elif h5d_dir != 0 and h20d_dir != 0: print(" ⚡conflict", end="")
            else:
                h5d_miss += 1
                gate_note = " gate" if (
                    record.get("calibration_buy_blocked")
                    or record.get("model_quality_buy_blocked")
                ) else ""
                print(f"  ⚪ {code:6s} {name:8s} h5d={record['signal']:4s} 收益={sig['predicted_return']:+.2%} 精度={e['direction_accuracy']:.0%} [LOW{gate_note}]", end="")
            
            # h20d信号 (v4.5.3d: h20d_r 已在交叉确认段获取)
            if h20d_r.get("predicted_return", 0) > H20D_SIGNAL_THRESHOLD:
                print(f" | h20d=买 🟢 {h20d_r['predicted_return']:+.2%}")
                h20d_hit += 1
            elif h20d_r.get("predicted_return", 0) < -H20D_SIGNAL_THRESHOLD:
                print(f" | h20d=卖 🔴 {h20d_r['predicted_return']:+.2%}")
                h20d_hit += 1
            else:
                print(f" | h20d=持 ⚪ {h20d_r.get('predicted_return',0):+.2%}")
                h20d_miss += 1
            
            results.append(record)
            continue
        
        # ═══════════════ Pool降级 ═══════════════
        if code not in pool_codes:
            fail += 1
            continue

        try:
            df = fetch_kline(code)
            if df is None or len(df) < 30:
                fail += 1
                continue

            pred = predictor.predict(code, df, name)
            if not pred:
                fail += 1
                continue

            # v4.5.20 FIX: pool_fallback路径中h20d_r未定义导致UnboundLocalError
            h20d_r = h20d_preds.get(code, {})

            actual_signal = pred.get("signal", "hold")
            conf = pred.get("confidence", 0)
            model_accuracy = pred.get("accuracy", 0.5)
            
            if USE_DYNAMIC_THRESHOLD and dt_engine:
                dt_result = dt_engine.compute(code=code, accuracy=model_accuracy, r2=pred.get("r2",-1))
                stock_threshold = dt_result["threshold"]
                is_high = conf >= stock_threshold and actual_signal != "hold"
            else:
                stock_threshold = CONFIDENCE_THRESHOLD
                is_high = conf >= CONFIDENCE_THRESHOLD and actual_signal != "hold"
            
            pool_acc = pred.get("accuracy", 0)
            record = {
                "symbol": code, "name": name,
                "signal": actual_signal,
                "score": pred.get("score", 5.0),
                "confidence": round(conf, 4),
                "confidence_level": "high" if is_high else "low",
                "threshold_used": round(stock_threshold, 4),
                "tier": tier,
                "sector": sector,
                "layer": TIER_LAYER_MAP.get(tier, "成长池"),
                "predicted_return": round(pred.get("predicted_return", 0), 4),
                "r2": pred.get("r2", 0), "accuracy": pool_acc,
                "direction_accuracy": pool_acc,  # P0-1: 统一输出方向精度
                "horizon": "1d", "source": "pool_fallback",
                "h20d": h20d_preds.get(code, {"predicted_return": 0, "direction_accuracy": 0.50, "horizon": "20d"}),
                # v4.5.17 P1-6: 基于精度分层的动态信号权重 (替代固定0.3)
                "signal_weight": max(POOL_FALLBACK_SIGNAL_WEIGHT,
                                      pool_acc if pool_acc > 0.5 else POOL_FALLBACK_SIGNAL_WEIGHT),
                "predict_time": datetime.now().isoformat(),
            }
            record = _apply_calibration_buy_gate(record, _calibration_stock_accuracy)
            record = _apply_model_quality_buy_gate(record)
            is_high = is_high and not (
                record.get("calibration_buy_blocked", False)
                or record.get("model_quality_buy_blocked", False)
            )
            
            if is_high:
                pool_hit += 1
                print(f"  🔴 {code:6s} {name:8s} pool={actual_signal:4s} 收益={pred.get('predicted_return',0):+.2%} [HIGH-pool]", end="")
            else:
                pool_miss += 1
                gate_note = " gate" if (
                    record.get("calibration_buy_blocked")
                    or record.get("model_quality_buy_blocked")
                ) else ""
                print(f"  ⚪ {code:6s} {name:8s} pool={record['signal']:4s} [LOW-pool{gate_note}]", end="")
            
            # h20d信号 (v4.5.3d: h20d_r 已在交叉确认段获取)
            if h20d_r.get("predicted_return", 0) > H20D_SIGNAL_THRESHOLD:
                print(f" | h20d=买 🟢 {h20d_r['predicted_return']:+.2%}")
                h20d_hit += 1
            elif h20d_r.get("predicted_return", 0) < -H20D_SIGNAL_THRESHOLD:
                print(f" | h20d=卖 🔴 {h20d_r['predicted_return']:+.2%}")
                h20d_hit += 1
            else:
                print(f" | h20d=持 ⚪ {h20d_r.get('predicted_return',0):+.2%}")
                h20d_miss += 1
            
            results.append(record)
        except Exception as e:
            fail += 1
            print(f"  ❌ {code:6s} 预测异常: {e}")
        
        time.sleep(0.05)
        gc.collect()
    
    total_hit = h5d_hit + pool_hit
    total_miss = h5d_miss + pool_miss
    
    # ── v4.7.2 h20d方向精度补丁 — 三层数据源 ──
    # ① walk-forward OOS评估(权威, 严格样本外, 7天内新鲜优先)
    # ② 每日增强训练报告(训练时计算, 新鲜fallback)
    # ③ 保守默认0.50 + missing标记 (废除pool_mean_fallback假值 — 缺数据宁可保守拦截, 不伪造精度放行)
    h20d_accuracy_source = "default_0.5"

    # ② 增强训练报告 (reports/predictor/prediction_enhanced_*.json, 每日16:30产出)
    enhanced_h20d = {}
    try:
        enh_pattern = os.path.join(PROJECT_ROOT, "reports", "predictor", "prediction_enhanced_*.json")
        enh_files = sorted(glob.glob(enh_pattern))
        if enh_files:
            with open(enh_files[-1], "r", encoding="utf-8") as f:
                enh_data = json.load(f)
            if isinstance(enh_data, dict):
                for _code, _v in enh_data.items():
                    if isinstance(_v, dict) and isinstance(_v.get("h20d"), dict):
                        _da = _v["h20d"].get("direction_accuracy")
                        if isinstance(_da, (int, float)) and _da > 0:
                            enhanced_h20d[str(_code).zfill(6)] = round(float(_da), 4)
    except Exception as e:
        print(f"  ⚠️ 增强训练h20d读取失败: {e}")

    # ① OOS评估 (7天内新鲜才优先; 否则降级为enhanced)
    oos_per_stock = {}
    oos_meta = {}
    oos_pattern = os.path.join(PROJECT_ROOT, "reports", "h20d_evaluation", "evaluation_h20d_oos_*.json")
    oos_files = sorted(glob.glob(oos_pattern))
    if oos_files:
        try:
            _oos_fresh = (time.time() - os.path.getmtime(oos_files[-1])) < 7 * 86400
            if _oos_fresh:
                with open(oos_files[-1], "r", encoding="utf-8") as f:
                    oos_data = json.load(f)
                oos_per_stock = {k: v.get("direction_accuracy")
                                 for k, v in (oos_data.get("per_stock") or {}).items()
                                 if isinstance(v, dict)}
                oos_meta = {"file": os.path.basename(oos_files[-1]),
                            "eval_date": oos_data.get("evaluation_date", "")}
            else:
                print(f"  ⚠️ OOS评估过期({os.path.basename(oos_files[-1])}, {int((time.time()-os.path.getmtime(oos_files[-1]))/86400)}天前) → 降级用增强训练h20d")
        except Exception as e:
            print(f"  ⚠️ OOS评估文件读取失败: {e}")

    patched_oos = patched_enh = missing = 0
    for code, h20d in h20d_preds.items():
        if not isinstance(h20d, dict):
            continue
        if code in oos_per_stock and isinstance(oos_per_stock[code], (int, float)):
            h20d["direction_accuracy"] = round(float(oos_per_stock[code]), 4)
            h20d["accuracy_source"] = "walk_forward_oos"
            patched_oos += 1
        elif code in enhanced_h20d:
            h20d["direction_accuracy"] = enhanced_h20d[code]
            h20d["accuracy_source"] = "enhanced_train_h20d"
            patched_enh += 1
        else:
            h20d["direction_accuracy"] = 0.50
            h20d["accuracy_source"] = "missing_conservative"
            missing += 1
    # records中的h20d与h20d_preds为同一对象引用(多数路径), 非引用路径同步一次
    for record in results:
        _h = record.get("h20d") if isinstance(record.get("h20d"), dict) else None
        if _h is None:
            continue
        _src = _h.get("accuracy_source")
        if _src in ("walk_forward_oos", "enhanced_train_h20d", "missing_conservative"):
            continue
        _code = record.get("symbol", "")
        if _code in oos_per_stock and isinstance(oos_per_stock[_code], (int, float)):
            _h["direction_accuracy"] = round(float(oos_per_stock[_code]), 4)
            _h["accuracy_source"] = "walk_forward_oos"
        elif _code in enhanced_h20d:
            _h["direction_accuracy"] = enhanced_h20d[_code]
            _h["accuracy_source"] = "enhanced_train_h20d"
        else:
            _h["direction_accuracy"] = 0.50
            _h["accuracy_source"] = "missing_conservative"
    h20d_accuracy_source = "walk_forward_oos" if patched_oos else ("enhanced_train_h20d" if patched_enh else "default_0.5")
    _oos_label = f"({oos_meta.get('file','')})" if oos_meta.get("file") else ""
    print(f"  🏷️ h20d精度补丁: OOS={patched_oos}只{_oos_label} | 增强训练={patched_enh}只 | 保守0.50={missing}只")

    # h20d信号统计
    h20d_returns = [r["predicted_return"] for r in h20d_preds.values() if r.get("predicted_return", 0) != 0]
    h20d_buy_blocked_by_calibration = [
        code for code, r in h20d_preds.items()
        if r.get("predicted_return", 0) > H20D_SIGNAL_THRESHOLD
        and not _calibration_allows_buy(_calibration_stock_accuracy, code)
    ]
    # v4.7.2 P0-2: h20d自身方向精度<40% → 硬禁h20d买入 (真实h20d低于红线时保守拦截, 废除53%假值放行)
    h20d_buy_blocked_by_own_accuracy = [
        code for code, r in h20d_preds.items()
        if r.get("predicted_return", 0) > H20D_SIGNAL_THRESHOLD
        and r.get("direction_accuracy", 0) < H20D_BUY_MIN_ACCURACY
    ]
    h20d_buy_symbols = [
        code for code, r in h20d_preds.items()
        if r.get("predicted_return", 0) > H20D_SIGNAL_THRESHOLD
        and _calibration_allows_buy(_calibration_stock_accuracy, code)
        and r.get("direction_accuracy", 0) >= H20D_BUY_MIN_ACCURACY
    ]
    h20d_buy_signals = len(h20d_buy_symbols)
    h20d_sell_signals = sum(1 for r in h20d_preds.values() if r.get("predicted_return", 0) < -H20D_SIGNAL_THRESHOLD)
    h20d_hold_signals = sum(1 for r in h20d_preds.values() if -H20D_SIGNAL_THRESHOLD <= r.get("predicted_return", 0) <= H20D_SIGNAL_THRESHOLD)
    
    # v4.6.x P0: 陈旧模型检测 → 24h时间阈值代替日历日期比较
    _training_status = {}
    _stale_codes = set()
    try:
        _ts_path = os.path.join(CACHE_DIR, "training_status.json")
        if os.path.exists(_ts_path):
            with open(_ts_path, "r") as _tsf:
                _training_status = json.load(_tsf)
            _last_batch_time = _training_status.get("_last_batch_train_time", "")
            if _last_batch_time:
                try:
                    _last_dt = datetime.fromisoformat(_last_batch_time)
                    _elapsed_h = (datetime.now() - _last_dt).total_seconds() / 3600
                    if _elapsed_h > 24:
                        _stale_codes = {r["symbol"] for r in results}
                        print(f"🏷️ 模型陈旧>24h ({_elapsed_h:.0f}h) → signal_weight×0.7")
                    else:
                        for r in results:
                            code = r["symbol"]
                            code_ts = _training_status.get(code, {}).get("last_train_time", "")
                            if code_ts:
                                code_dt = datetime.fromisoformat(code_ts)
                                if (datetime.now() - code_dt).total_seconds() / 3600 > 24:
                                    _stale_codes.add(code)
                        if _stale_codes:
                            print(f"🏷️ 部分模型陈旧>24h: {len(_stale_codes)}只 → signal_weight×0.7")
                        else:
                            print(f"✅ 全部模型fresh (最后训练: {_elapsed_h:.0f}h前)")
                except Exception:
                    # 旧格式兼容：fallback到日历日期比较
                    _last_batch = _training_status.get("_last_batch_train_date", "")
                    _today = datetime.now().strftime("%Y-%m-%d")
                    if _last_batch != _today:
                        _stale_codes = {r["symbol"] for r in results}
            else:
                # 旧格式兼容
                _last_batch = _training_status.get("_last_batch_train_date", "")
                _today = datetime.now().strftime("%Y-%m-%d")
                if _last_batch != _today:
                    _stale_codes = {r["symbol"] for r in results}
    except Exception as _e:
        print(f"⚠️ 训练状态加载失败: {_e}")
    
    # 对stale模型降权
    if _stale_codes:
        for r in results:
            if r["symbol"] in _stale_codes:
                old_sw = r.get("signal_weight", 1.0)
                r["signal_weight"] = round(old_sw * 0.7, 2)
                r["model_stale"] = True
        print(f"🏷️ Stale模型: {len(_stale_codes)}只 signal_weight×0.7 ({', '.join(sorted(_stale_codes)[:5])}{'...' if len(_stale_codes)>5 else ''})")
    
    # 保存
    output_path = os.path.join(CACHE_DIR, "daily_predict.json")
    sector_quality_blocked = _apply_sector_quality_buy_gate(results)
    if sector_quality_blocked:
        print(f"🏷️ 弱行业BUY阻断: {len(sector_quality_blocked)}只 ({', '.join(sorted(sector_quality_blocked))})")
    # ── v4.7.0 P1: 观察池+影子池 predict-only 覆盖 ──
    # 为移出/候选标的持续产出预测, 喂 feedback_controller→calibration, 支撑回池/入池评估
    _observe_count = 0
    try:
        _obs_stocks = _load_observation_symbols()
        if _obs_stocks:
            _results_before = len(results)
            results = _append_predict_only(results, _obs_stocks, enhanced, h20d_preds,
                                           _calibration_stock_accuracy, predictor)
            _observe_count = len(results) - _results_before
            if _observe_count:
                print(f"🔭 观察覆盖: {_observe_count}只 predict-only (观察池+影子池, 不进信号层)")
    except Exception as _oe:
        print(f"⚠️ 观察覆盖失败: {_oe}")
    final_high_confidence = sum(
        1 for r in results
        if r.get("confidence_level") == "high" and r.get("signal") != "hold"
    )
    final_low_confidence = sum(
        1 for r in results
        if r.get("confidence_level") != "high" and r.get("confidence_level") != "suspended"
    )
    # 🔧 附加最近收盘价到每只标的
    _attach_latest_prices(results, stocks)
    def _target_predict_date():
        today = datetime.now().date()
        try:
            from config.holiday_calendar import is_trading_day
            cur = today
            if is_trading_day(check_date=cur, market="A_SHARE") and datetime.now().hour < 15:
                return cur.strftime("%Y-%m-%d")
            cur = cur + timedelta(days=1)
            for _ in range(14):
                if is_trading_day(check_date=cur, market="A_SHARE"):
                    return cur.strftime("%Y-%m-%d")
                cur += timedelta(days=1)
        except Exception:
            cur = today
            if cur.weekday() < 5 and datetime.now().hour < 15:
                return cur.strftime("%Y-%m-%d")
            cur += timedelta(days=1)
            while cur.weekday() >= 5:
                cur += timedelta(days=1)
            return cur.strftime("%Y-%m-%d")

    target_predict_date = _target_predict_date()
    meta = {
        "predict_time": datetime.now().isoformat(),
        "generated_at": datetime.now().isoformat(),
        "predict_date": target_predict_date,
        "version": _read_version(),
        "h5d_threshold": H5D_SIGNAL_THRESHOLD,
        "h5d_min_accuracy": H5D_MIN_ACCURACY,
        "h5d_confidence_threshold": H5D_CONFIDENCE_THRESHOLD,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
        "h20d_threshold": H20D_SIGNAL_THRESHOLD,
        "enhanced_stocks": len(enhanced),
        "total_stocks": len(stocks),
        "observation_stocks": _observe_count,
        "high_confidence_h5d": h5d_hit,
        "high_confidence_pool": pool_hit,
        "high_confidence": final_high_confidence,
        "cross_confirmed": cross_count,  # v4.5.3d
        "low_confidence": final_low_confidence,
        "suspended": suspended,
        "failed": fail,
        "h20d_stocks": len(h20d_preds),
        "h20d_buy": h20d_buy_signals,
        "h20d_buy_symbols": h20d_buy_symbols,
        "h20d_buy_blocked_by_calibration": h20d_buy_blocked_by_calibration,
        "h20d_buy_blocked_by_own_accuracy": h20d_buy_blocked_by_own_accuracy,
        "h20d_buy_min_accuracy": H20D_BUY_MIN_ACCURACY,
        "h20d_sell": h20d_sell_signals,
        "h20d_sell_symbols": [code for code, r in h20d_preds.items() if r.get("predicted_return", 0) < -H20D_SIGNAL_THRESHOLD],
        "h20d_hold": h20d_hold_signals,
        "h20d_mean_return": round(float(np.mean(h20d_returns)), 6) if h20d_returns else 0,
        "h20d_accuracy_source": h20d_accuracy_source,
        "model_buy_min_accuracy": MODEL_BUY_MIN_ACCURACY,
        "sector_buy_min_avg_accuracy": SECTOR_BUY_MIN_AVG_ACCURACY,
        "sector_quality_buy_blocked": sorted(sector_quality_blocked),
        "predictions": results,
    }
    from common.file_lock import locked_json_write
    locked_json_write(output_path, meta)
    
    # v4.5.3b: 信号变更追踪 (与上一次预测对比)
    _track_signal_changes(results, CACHE_DIR)

    # v4.6.9f P1-1: 信号2日确认 — 连续2个交易日同向信号才标记confirmed_2d
    # (消除单日噪声; 消费方 execute_scheduled_trades 可据此过滤)
    try:
        _prev_path = os.path.join(CACHE_DIR, "daily_predict.json.prev")
        if os.path.exists(_prev_path):
            with open(_prev_path, "r", encoding="utf-8") as _pf:
                _prev_data = json.load(_pf)
            _prev_map = {p.get("symbol", ""): p.get("signal", "hold") for p in _prev_data.get("predictions", [])}
            for _r in results:
                _sym = _r.get("symbol", "")
                _cur_sig = _r.get("signal", "hold")
                _prev_sig = _prev_map.get(_sym, None)
                _r["confirmed_2d"] = bool(
                    _prev_sig is not None
                    and _cur_sig in ("buy", "sell")
                    and _cur_sig == _prev_sig
                )
            # 写回缓存(供消费方读取)
            if os.path.exists(output_path):
                with open(output_path, "r", encoding="utf-8") as _cf:
                    _cdata = json.load(_cf)
                _cmap = {p.get("symbol", ""): p for p in _cdata.get("predictions", [])}
                for _r in results:
                    if _r.get("symbol") in _cmap:
                        _cmap[_r["symbol"]]["confirmed_2d"] = _r.get("confirmed_2d", False)
                _cdata["predictions"] = list(_cmap.values())
                locked_json_write(output_path, _cdata)
            print(f"  ✅ 2日确认标记: {sum(1 for r in results if r.get('confirmed_2d'))}只连续同向")
    except Exception as _ce:
        print(f"  ⚠️ 2日确认标记失败: {_ce}")

    print(f"\n{'='*60}")
    print(f"📊 预测完成:")
    print(f"   h5d增强: {h5d_hit}高/{h5d_miss}低 (✨{cross_count}交叉确认)")
    print(f"   pool降级: {pool_hit}高/{pool_miss}低")
    print(f"   h20d: 买{h20d_buy_signals} 卖{h20d_sell_signals} 持{h20d_hold_signals}")
    print(f"   🔍 观察池(仅监控): {suspended} | 失败: {fail}")
    print(f"   高信度总计: {total_hit}")
    print(f"📁 缓存: {output_path}")

    # v4.5.3d: 进度追踪完成
    result_code = 0 if len(results) >= 10 else 1
    if tracker:
        tracker.complete(f"预测完成: {len(results)}只", total_stocks=len(stocks), high_confidence=final_high_confidence)
    
    # v4.5.13: 飞书自报告
    try:
        from common.feishu_utils import send_markdown
        from datetime import datetime as _dt
        
        # 提取信号详情 (results是list[dict])
        buy_signals = []
        sell_signals = []
        high_sigs = []
        for r in results:
            sig = r.get('signal', '?')
            conf = r.get('confidence', 0)
            name = r.get('name', r.get('symbol', '?'))
            acc = r.get('direction_accuracy', 0)
            ret = r.get('predicted_return', 0)
            if sig == 'buy' and r.get('confidence_level') == 'high':
                buy_signals.append(f"  🟢 {name} ({r['symbol']}) score={r.get('score','?'):.1f} ret={ret*100:+.2f}%")
            elif sig == 'sell' and r.get('confidence_level') == 'high':
                sell_signals.append(f"  🔴 {name} ({r['symbol']}) score={r.get('score','?'):.1f} ret={ret*100:+.2f}%")
            if r.get('confidence_level') == 'high':
                high_sigs.append(f"  ✨ {name} ({r['symbol']}) {sig} ret={ret*100:+.2f}% acc={acc:.0%}")
        
        h20d_buy_list = meta.get('h20d_buy_symbols', [])
        h20d_sell_list = meta.get('h20d_sell_symbols', [])
        
        buy_str = '\n'.join(buy_signals[:10]) if buy_signals else '  无'
        sell_str = '\n'.join(sell_signals[:5]) if sell_signals else '  无'
        high_str = '\n'.join(high_sigs[:10]) if high_sigs else '  无'
        
        report = f"""**🔮 个股分批预测 | {_dt.now().strftime('%Y-%m-%d %H:%M')}**
**标的**: {len(results)}只 | **高信度**: {total_hit}
h5d增强: {h5d_hit}/{len(results)}高信度 (✨{cross_count}交叉确认)
h20d: 买{h20d_buy_signals} 卖{h20d_sell_signals} 持{h20d_hold_signals}
观察池: {suspended} | 失败: {fail}

**🟢 买入建议**
{buy_str}

**🔴 卖出建议**
{sell_str}

**✨ 高信度信号**
{high_str}

**h20d**: 买{len(h20d_buy_list)}只: {', '.join(h20d_buy_list[:5]) if h20d_buy_list else '无'} | 卖{len(h20d_sell_list)}只: {', '.join(h20d_sell_list[:5]) if h20d_sell_list else '无'}"""
        send_markdown(title=f"🔮 个股预测 | {_dt.now().strftime('%Y-%m-%d')}", content=report)
    except Exception:
        pass
    
    return result_code


def _read_version() -> str:
    """从VERSION文件读取版本号, 失败返回unknown"""
    try:
        vpath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
        if os.path.exists(vpath):
            with open(vpath) as f:
                return f.readline().strip()
    except Exception:
        pass
    return "unknown"


def _compute_vol20_map(codes: list) -> dict:
    """v4.7.0 P2-1: 20日年化波动率 {code: vol20}
    用于波动率自适应信号阈值 (高波标的抬高触发线, 低波标的降低)
    数据源: 麦蕊日K (免费版~120条足够); 失败标的回退1.0(不缩放)
    """
    out = {}
    try:
        from config.mairui_api_config import get_kline_history
        for c in codes:
            try:
                kl = get_kline_history(c, period="d", adjust="f", limit=45)
                if not kl or len(kl) < 25:
                    out[c] = 1.0
                    continue
                closes = [float(x["c"]) for x in kl]
                rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
                # 20日窗口
                window = rets[-20:]
                if not window:
                    out[c] = 1.0
                    continue
                mean = sum(window) / len(window)
                var = sum((r - mean) ** 2 for r in window) / max(len(window) - 1, 1)
                out[c] = math.sqrt(var) * math.sqrt(252) if var > 0 else 1.0
            except Exception:
                out[c] = 1.0
    except Exception:
        pass
    return out


def _vol_adaptive_scales(vol_map: dict, codes: list) -> dict:
    """v4.7.0 P2-1: 波动率缩放系数 = clamp(vol20/中位数, 0.5, 2.0)
    未启用时全部返回1.0(行为不变)
    """
    try:
        import yaml as _yaml
        with open(os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml"), "r", encoding="utf-8") as _f:
            _ap = _yaml.safe_load(_f) or {}
        _sig = _ap.get("signal", {}) or {}
        _enabled = bool(_sig.get("use_vol_adaptive", False))
        if not _enabled:
            return {}
        _floor = float(_sig.get("vol_scale_floor", 0.5))
        _cap = float(_sig.get("vol_scale_cap", 2.0))
    except Exception:
        return {}
    vals = [v for v in vol_map.values() if v > 0]
    if not vals:
        return {}
    med = sorted(vals)[len(vals) // 2]
    if med <= 0:
        return {}
    scales = {}
    for c in codes:
        v = vol_map.get(c, med)
        scales[c] = max(_floor, min(_cap, v / med))
    return scales


def _load_observation_symbols() -> list:
    """v4.7.0 P1: 读取观察池+影子池候选 → predict-only覆盖
    返回 [{symbol, name, sector, pool}] (排除主池已有标的)
    """
    out = []
    try:
        main_codes = {s["symbol"] for s in get_stock_pool()}
        # 观察池 (移出标的, 30天评估回池)
        obs_path = os.path.join(PROJECT_ROOT, "config", "observation_pool.yaml")
        if os.path.exists(obs_path):
            with open(obs_path, encoding="utf-8") as f:
                _obs = yaml.safe_load(f) or {}
            for s in _obs.get("observation_pool", []):
                sym = str(s.get("symbol", ""))
                if sym and sym not in main_codes:
                    out.append({"symbol": sym, "name": s.get("name", sym),
                                "sector": s.get("sector", ""), "pool": "observation"})
        # 影子池 (候选标的, 2周观察后评估入池)
        sh_path = os.path.join(PROJECT_ROOT, "config", "shadow_pool.yaml")
        if os.path.exists(sh_path):
            with open(sh_path, encoding="utf-8") as f:
                _sh = yaml.safe_load(f) or {}
            for s in _sh.get("candidates", []):
                sym = str(s.get("symbol", ""))
                if sym and sym not in main_codes:
                    out.append({"symbol": sym, "name": s.get("name", sym),
                                "sector": s.get("sector", ""), "pool": "shadow"})
    except Exception as e:
        print(f"  ⚠️ 观察池读取失败: {e}")
    return out


def _load_enhanced_for_symbols(symbols: set, max_age_days: int = 7) -> dict:
    """v4.7.0 P1: 观察/影子标的专用增强报告加载
    独立于主池报告选择逻辑(主链只保留1份最新全量报告, 会挤出周日观察训练报告)
    按新到旧扫描, 每个symbol取最新一份含 direction_accuracy>0 的报告
    """
    out = {}
    try:
        all_files = sorted(glob.glob(os.path.join(ENHANCED_DIR, "prediction_enhanced_*.json")),
                           key=os.path.getmtime, reverse=True)
        cutoff = time.time() - max_age_days * 24 * 3600
        for fp in all_files:
            if os.path.getmtime(fp) < cutoff:
                break
            if all(s in out for s in symbols):
                break
            try:
                with open(fp, encoding="utf-8") as f:
                    data = json.load(f)
                for sym in symbols:
                    if sym in out:
                        continue
                    sdata = data.get(sym, {})
                    h5d = sdata.get("h5d", {}) if isinstance(sdata, dict) else {}
                    if h5d and h5d.get("direction_accuracy", 0) > 0:
                        out[sym] = {
                            "predicted_return": h5d.get("predicted_return", 0),
                            "direction_accuracy": h5d["direction_accuracy"],
                        }
            except Exception:
                continue
    except Exception:
        pass
    return out


def _append_predict_only(results: list, obs_stocks: list, enhanced: dict,
                         h20d_preds: dict, calibration_stock_accuracy: dict,
                         predictor=None) -> list:
    """v4.7.0 P1: 观察/影子标的 predict-only — 不产生交易信号, 只为校准链喂精度
    精度来源优先级: enhanced h5d > pool_predictor模型 > h20d > 校准last_accuracy; 全无则跳过(无模型)
    signal强制hold + confidence_level=suspended (morning_decision已显式跳过suspended层)
    """
    existing = {r["symbol"] for r in results}
    _obs_enhanced = _load_enhanced_for_symbols({s["symbol"] for s in obs_stocks})
    _registered = set(predictor.get_all_codes()) if predictor is not None else set()
    for s in obs_stocks:
        code = s["symbol"]
        if code in existing:
            continue
        acc = 0.0
        pred_ret = 0.0
        horizon = "5d"
        h20d_r = h20d_preds.get(code, {})
        _e = enhanced.get(code, {})
        if _e.get("direction_accuracy", 0) > 0:
            acc = float(_e["direction_accuracy"])
            pred_ret = float(_e.get("predicted_return", 0))
        elif code in _obs_enhanced:
            _oe = _obs_enhanced[code]
            acc = float(_oe["direction_accuracy"])
            pred_ret = float(_oe.get("predicted_return", 0))
        elif predictor is not None and code in _registered:
            # pool_predictor 模型预测 (移出标的有既有模型, 立即可覆盖)
            try:
                _df = fetch_kline(code)
                if _df is not None and len(_df) >= 30:
                    _pred = predictor.predict(code, _df, s["name"])
                    if _pred:
                        acc = float(_pred.get("accuracy", 0) or 0)
                        pred_ret = float(_pred.get("predicted_return", 0) or 0)
                        horizon = "1d"
            except Exception:
                pass
        if acc <= 0 and h20d_r.get("direction_accuracy", 0) > 0:
            acc = float(h20d_r["direction_accuracy"])
            pred_ret = float(h20d_r.get("predicted_return", 0))
        if acc <= 0 and code in calibration_stock_accuracy:
            _accs = calibration_stock_accuracy[code].get("accuracies", [])
            if _accs:
                acc = float(_accs[-1])
        if acc <= 0:
            print(f"  ⏭️  {code} {s['name']}: 无模型/无精度, 跳过观察覆盖")
            continue
        results.append({
            "symbol": code, "name": s["name"], "signal": "hold",
            "score": 5.0, "confidence": 0, "confidence_level": "suspended",
            "predicted_return": pred_ret, "horizon": horizon,
            "source": "predict_only", "tier": "observe", "layer": "观察池",
            "sector": s.get("sector", ""),
            "accuracy": round(acc, 4), "direction_accuracy": round(acc, 4),
            "h20d": h20d_r if h20d_r else {"predicted_return": 0, "direction_accuracy": 0.50, "horizon": "20d"},
            "predict_time": datetime.now().isoformat(),
            "observe_pool": s.get("pool", "observation"),
        })
    return results

def _attach_latest_prices(results: list, stocks: list):
    """为每只标的附加最近收盘价（last交易日的close）"""
    for r in results:
        code = r.get("symbol", "")
        if not code: continue
        try:
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            kline = fetch_kline(code)  # 使用已有的fetch逻辑
            if kline is not None and len(kline) > 0:
                r["latest_price"] = round(float(kline.iloc[-1]["close"]), 2)
            else:
                # 回退: 用dsl_data_sdk
                from dsl_data_sdk_original import get_kline
                data = get_kline(code, start, end)
                if data and len(data) > 0:
                    r["latest_price"] = round(float(data[-1].get("close", data[-1].get("c", 0))), 2)
        except:
            pass
    priced = sum(1 for r in results if r.get("latest_price"))
    print(f"  💰 价格附加: {priced}/{len(results)}只")


def _track_signal_changes(current_results: list, cache_dir: str):
    """v4.5.3b: 追踪信号变化 (与上一次预测对比)，写入 signal_changes.json"""
    try:
        history_path = os.path.join(cache_dir, "signal_changes.json")
        
        # 读取上一次预测
        prev_path = os.path.join(cache_dir, "daily_predict.json.prev")
        # 先把当前预测备份为 .prev (下次对比用)
        curr_path = os.path.join(cache_dir, "daily_predict.json")
        if os.path.exists(curr_path):
            import shutil
            shutil.copy2(curr_path, prev_path)
        
        if not os.path.exists(prev_path):
            return  # 首次运行，无历史对比
        
        with open(prev_path, "r", encoding="utf-8") as f:
            prev_data = json.load(f)
        prev_predictions = {p["symbol"]: p for p in prev_data.get("predictions", [])}
        
        # 对比当前和上一次
        current_map = {p["symbol"]: p for p in current_results}
        changes = []
        
        for sym, curr in current_map.items():
            prev = prev_predictions.get(sym)
            if prev is None:
                changes.append({"symbol": sym, "name": curr.get("name", sym), "change": "new", "signal": curr.get("signal", "hold")})
                continue
            
            prev_signal = prev.get("signal", "hold")
            curr_signal = curr.get("signal", "hold")
            if prev_signal != curr_signal:
                changes.append({
                    "symbol": sym, "name": curr.get("name", sym),
                    "change": "signal_flip",
                    "from": prev_signal, "to": curr_signal,
                    "confidence": curr.get("confidence", 0),
                })
            
            # 置信度显著变化
            prev_conf = prev.get("confidence", 0)
            curr_conf = curr.get("confidence", 0)
            if abs(curr_conf - prev_conf) >= 0.15:
                changes.append({
                    "symbol": sym, "name": curr.get("name", sym),
                    "change": "confidence_shift",
                    "from": round(prev_conf, 3), "to": round(curr_conf, 3),
                    "signal": curr_signal,
                })
        
        # 删除的信号
        for sym, prev in prev_predictions.items():
            if sym not in current_map:
                changes.append({"symbol": sym, "name": prev.get("name", sym), "change": "removed", "signal": prev.get("signal", "hold")})
        
        if changes:
            entry = {
                "time": datetime.now().isoformat(),
                "changes": changes,
                "total_changes": len(changes),
            }
            
            # 追加写入历史
            history = []
            if os.path.exists(history_path):
                try:
                    with open(history_path, "r") as f:
                        history = json.load(f)
                    if not isinstance(history, list):
                        history = []
                except Exception:
                    history = []
            
            history.append(entry)
            history = history[-50:]  # 只保留最近50次
            
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
            
            print(f"  🔄 信号变更: {len(changes)}条 (翻转{sum(1 for c in changes if c['change']=='signal_flip')}条)")
    except Exception as e:
        print(f"  ⚠️ 信号变更追踪失败: {e}")


def _verify_progress_completed():
    """v4.5.17 P0-2: 进程退出后检查progress文件是否已标记完成，如未完成则自动补全"""
    import json, os
    from datetime import datetime
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache_dir = os.path.join(project_root, "cache", "progress")
    if not os.path.exists(cache_dir):
        return
    for fname in os.listdir(cache_dir):
        if not fname.endswith(".json"):
            continue
        if not (fname.startswith("batch_predict") or fname.startswith("intraday_monitor")):
            continue
        fpath = os.path.join(cache_dir, fname)
        try:
            with open(fpath) as f:
                data = json.load(f)
            if data.get("status") in ("running", "started"):
                data["status"] = "completed"
                data["message"] = data.get("message", "") + " (退出后自动补全)"
                data["updated_at"] = datetime.now().isoformat()
                with open(fpath, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"  ✅ 进度已补全: {fname}")
        except Exception:
            pass

if __name__ == "__main__":
    # v4.6.9: 支持 --force 手动强制预测(绕过非交易日跳过, 供Dashboard"立即重训"使用)
    import argparse
    _parser = argparse.ArgumentParser(description="批量预测")
    _parser.add_argument("--force", action="store_true",
                        help="强制预测: 即使明天非交易日也执行(手动重训场景)")
    _args, _unknown = _parser.parse_known_args()

    # ⏸️ 节假日检查：如果明天不是交易日则跳过预测 (今天是盘前准备日)
    # batch_predict为明天盘前决策提供INPUT, 需在交易日的前一天执行(Sun-Thu)
    # v4.6.9: --force 时跳过此检查(Dashboard"立即重训"在周末/节假日也能全链路刷新)
    if not _args.force:
        try:
            from config.holiday_calendar import is_trading_day
            tomorrow = datetime.now().date() + timedelta(days=1)
            if not is_trading_day(check_date=tomorrow, market="A_SHARE"):
                print(f"⏸️ {datetime.now().strftime('%Y-%m-%d')} 明天非A股交易日，跳过预测")
                # v4.6.3: 写skipped进度文件 → Dashboard显示"跳过"而非"missed"
                try:
                    _pd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "progress")
                    _slot = os.environ.get("BATCH_PREDICT_SLOT", "batch_predict")
                    _skip_id = f"{_slot}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    os.makedirs(_pd, exist_ok=True)
                    with open(os.path.join(_pd, f"{_skip_id}.json"), "w") as _sf:
                        json.dump({
                            "task_id": _skip_id,
                            "task_name": _slot,
                            "status": "skipped",
                            "progress": {"step": 0, "total": 1},
                            "message": f"⏸️ 明日({tomorrow})非交易日，跳过预测",
                            "started_at": datetime.now().isoformat(),
                            "updated_at": datetime.now().isoformat(),
                            "completed_at": datetime.now().isoformat(),
                        }, _sf, ensure_ascii=False, indent=2)
                    print(f"  ✅ 已写入跳过标记: {_skip_id}")
                except Exception as _se:
                    print(f"  ⚠️ 写跳过标记失败: {_se}")
                sys.exit(0)
        except ImportError:
            pass
    _exit_code = main()
    _verify_progress_completed()
    # ──────────── v4.6.9i(审计F1-5): 校准实现闭环每日激活 ────────────
    # 修复: check_realized_accuracy 从未被调用, realized_checked 74条全false(审计P0-5)
    try:
        from core.calibration_feedback import get_calibration_feedback
        _cf = get_calibration_feedback()
        _r = _cf.check_realized_accuracy(max_days=60)
        print(f"  📐 校准回验: 新增{_r.get('checked', 0)}条 | 正确{_r.get('correct', 0)}/{_r.get('total', 0)} "
              f"| 兑现精度{_r.get('accuracy', 0):.1%}")
    except Exception as _ce:
        print(f"⚠️ 校准回验失败: {_ce}")
    sys.exit(_exit_code)
