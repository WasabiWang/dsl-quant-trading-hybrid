#!/usr/bin/env python3
"""
DSL v4.5.1 预测模型训练脚本 - 三阶段全面升级版

Phase 1: 5年训练数据 + 5日预测窗口 → 提升信噪比
Phase 2: 50+维特征工程(技术面+资金流+北向+行业+基本面) → 丰富信息维度
Phase 3: Optuna超参搜索 + 5-fold时序交叉验证 + 集成投票 → 精度+稳定性

输出:
  models/<code>/lightgbm.pkl     LightGBM主模型
  models/<code>/xgboost.pkl      XGBoost辅助模型
  models/<code>/scaler.pkl       特征标准化器
  reports/predictor/prediction_*.json  预测结果
"""
import os, sys, json, warnings, gc
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import joblib
import yaml

warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dsl_data_sdk_original import get_kline, normalize_symbol
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb
import xgboost as xgb

# v4.5.9: CatBoost - 处理类别特征+噪声更鲁棒
try:
    import catboost as cb
    HAS_CATBOOST = True
except ImportError:
    HAS_CATBOOST = False

# ====== 可调参数 ======
TRAIN_YEARS = 5           # 训练数据年限
LOOKBACK = 60              # 特征回看窗口(天)
PREDICT_HORIZON = 5        # 预测未来N天后的涨跌幅(5日信号更稳定)
MIN_TRAIN_SAMPLES = 250    # 最少训练样本数
TEST_SPLIT = 0.15          # 测试集比例 (P0: 降低到15%,新增独立验证集)
VAL_SPLIT = 0.15            # 验证集比例 (P0: 独立验证集,用于集成权重和early stopping)
N_FOLDS = 5                # 交叉验证折数
USE_OPTUNA = True          # 是否用Optuna超参搜索
OPTUNA_TRIALS = 50         # Optuna试验次数
DIRECTION_THRESHOLD = 0.52  # 方向精度阈值 (v4.5.3c: 统一为0.52)

# P2: 分级交易成本表(A股实盘校验)
TIER_SLIPPAGE = {
    "bluechip": 0.0005,  # 大蓝筹: 5bp
    "core": 0.0010,       # 核心池: 10bp
    "growth": 0.0015,     # 成长池: 15bp
    "flex": 0.0030,       # 弹性池: 30bp
}

# v4.5.9: 分级特征策略 - A股专项特征仅对高波动tier启用
AGGRESSIVE_TIERS = {"growth", "flex"}  # 成长/灵活层启用8维新特征+CatBoost
# bluechip/core保持原有特征集,避免大蓝筹精度下降

# v4.5.5 S6: 被batch_train调用时,adaptive_overrides已设置
SKIP_OPTUNA = False

# ====== 自适应参数默认值 (可被 batch_train.py 的 adaptive_overrides 覆盖) ======
N_ESTIMATORS_DEFAULT = 200       # LGB默认估计器数
MAX_DEPTH_DEFAULT = 5            # LGB默认树深度
LEARNING_RATE_DEFAULT = 0.05     # 学习率 (v4.5.6: 补导出,供batch_train引用)
REG_ALPHA_DEFAULT = 0.5          # L1正则化
REG_LAMBDA_DEFAULT = 0.5         # L2正则化
MIN_CHILD_SAMPLES_DEFAULT = 20   # 叶子最小样本数
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
PRED_DIR = os.path.join(PROJECT_ROOT, "reports", "predictor")
TRAIN_LOG_DIR = os.path.join(PROJECT_ROOT, "reports", "predictor")
os.makedirs(PRED_DIR, exist_ok=True)
os.makedirs(TRAIN_LOG_DIR, exist_ok=True)

# Optuna 按需加载
if USE_OPTUNA:
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        HAS_OPTUNA = True
    except ImportError:
        print("⚠️ Optuna未安装,回退到固定参数。安装命令: pip install optuna")
        HAS_OPTUNA = False
        USE_OPTUNA = False


def get_stock_pool():
    """从master_stock_pool.yaml读取股票池"""
    pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    with open(pool_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [{"symbol": s["symbol"], "name": s["name"]}
            for s in data.get("master_pool", [])
            if "." not in s["symbol"]]

# Phase 3: 截面排序特征
_global_rank_cache = None

def _get_rank_features(end_date=None):
    """预计算所有标的的截面排序特征(支持时间窗口限制,消除前视偏差)

    Args:
        end_date: 排名计算的截止日期(格式YYYY-MM-DD)。
                  为None时使用今天(仅预测模式使用)。
                  训练时传入训练集最后一天,避免测试集信息泄露。
    """
    global _global_rank_cache
    cache_current_snapshot = end_date is None
    if _global_rank_cache is not None and cache_current_snapshot:
        return _global_rank_cache

    from dsl_data_sdk_original import get_kline, normalize_symbol

    pool = get_stock_pool()
    if end_date is None:
        end_dt = datetime.now()
    else:
        end_dt = pd.to_datetime(end_date).to_pydatetime()
    end = end_dt.strftime("%Y-%m-%d")
    start = (end_dt - timedelta(days=TRAIN_YEARS * 365)).strftime("%Y-%m-%d")

    all_features = {}
    feat_names = ["mom_5d","mom_10d","mom_20d","vol_ratio_20","vol_change","atr14","rsi14","amplitude","turnover"]

    for s in pool:
        code = s["symbol"]
        try:
            k = get_kline(normalize_symbol(code), start, end)
            if not k or len(k) < 250:
                continue
            df = pd.DataFrame(k)
            for c in ["close","volume","high","low","open"]:
                df[c] = df[c].astype(float)
            close = df["close"]; volume = df["volume"]; high = df["high"]; low = df["low"]
            prev = close.shift(1)

            feats = pd.DataFrame(index=df.index)
            for w in [5,10,20]:
                feats[f"mom_{w}d"] = close.pct_change(w)
            feats["vol_ratio_20"] = volume / (volume.rolling(20).mean() + 1e-8)
            feats["vol_change"] = volume.pct_change()
            feats["atr14"] = (high-low).rolling(14).mean() / close
            delta = close.diff(); gain = delta.clip(lower=0).rolling(14).mean(); loss = (-delta.clip(upper=0)).rolling(14).mean()
            feats["rsi14"] = 100 - (100 / (1 + gain / (loss + 1e-8)))
            feats["amplitude"] = (high-low) / (prev + 1e-8)
            feats["turnover"] = volume / (volume.rolling(250).mean() + 1e-8)

            all_features[code] = feats.dropna()
        except Exception:
            continue

    if len(all_features) < 2:
        result = {}
        if cache_current_snapshot:
            _global_rank_cache = result
        return result

    # 对每只股票计算各特征的截面排名
    result = {}
    for code in all_features:
        result[code] = {}
        for feat in feat_names:
            result[code][f"rank_{feat}"] = 0.5

    codes = list(all_features.keys())
    last_idx = -1
    common_dates = set(all_features[codes[0]].index)
    for c in codes[1:]:
        common_dates &= set(all_features[c].index)
    common_dates = sorted(common_dates)

    if not common_dates:
        if cache_current_snapshot:
            _global_rank_cache = result
        return result

    for dt in [common_dates[-1]]:  # 只算最新一天
        for feat in feat_names:
            vals = {}
            for code in codes:
                v = all_features[code].loc[dt, feat] if dt in all_features[code].index else None
                if v is not None and not np.isnan(v) and not np.isinf(v):
                    vals[code] = v
            if len(vals) >= 2:
                sorted_codes = sorted(vals, key=lambda c: vals[c])
                for rank_idx, code in enumerate(sorted_codes):
                    result[code][f"rank_{feat}"] = round(rank_idx / (len(sorted_codes)-1), 4)

    if cache_current_snapshot:
        _global_rank_cache = result
    print(f"  📊 截面rank特征: {len(feat_names)}维 (全池{len(codes)}只)")
    return result


def _split_end_date(split: pd.DataFrame) -> str:
    """Return YYYY-MM-DD for a time-split frame with a real date index."""
    idx = split.index[-1]
    if hasattr(idx, "strftime"):
        return idx.strftime("%Y-%m-%d")
    return str(idx)[:10]


def _add_cross_sectional_features(split: pd.DataFrame, code: str, split_name: str):
    """Add leakage-safe cross-sectional rank features to one time split."""
    if split is None or len(split) == 0:
        return split, 0, 0, None

    split = split.copy()
    end_date = _split_end_date(split)
    rank_cache = _get_rank_features(end_date=end_date)
    ranks = rank_cache.get(code, {})
    for rank_name, rank_val in ranks.items():
        if not rank_name.startswith("rank_"):
            continue
        split[rank_name] = rank_val
        feat_base = rank_name.replace("rank_", "", 1)
        split[f"rel_{feat_base}"] = rank_val - 0.5

    n_rank = sum(1 for c in split.columns if c.startswith("rank_"))
    n_rel = sum(1 for c in split.columns if c.startswith("rel_"))
    if n_rank > 0:
        print(f"  📊 截面rank特征({split_name}): +{n_rank}维 (截止{end_date})")
    return split, n_rank, n_rel, end_date


def _align_cross_sectional_columns(*splits: pd.DataFrame):
    """Make train/val/test share the same rank/rel columns with neutral fallback values."""
    rank_cols = sorted({
        c for split in splits if split is not None
        for c in split.columns if c.startswith("rank_")
    })
    rel_cols = sorted({
        c for split in splits if split is not None
        for c in split.columns if c.startswith("rel_")
    })
    aligned = []
    for split in splits:
        split = split.copy()
        for col in rank_cols:
            if col not in split.columns:
                split[col] = 0.5
        for col in rel_cols:
            if col not in split.columns:
                split[col] = 0.0
        aligned.append(split)
    return (*aligned, rank_cols, rel_cols)


# ============================================================
# Phase 2: 50+维特征工程
# ============================================================
def build_features(df, use_aggressive: bool = False):
    """从OHLCV构建50+维特征

    Args:
        df: OHLCV DataFrame
        use_aggressive: 是否启用A股专项特征(growth/flex层)
    """
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_p = df["open"].astype(float)
    amount = df.get("amount", pd.Series(0, index=df.index)).astype(float)
    prev_close = df.get("prev_close", close.shift(1)).astype(float)

    feats = pd.DataFrame(index=df.index)

    # ---- 价格特征 (12维) ----
    for w in [5, 10, 20, 30, 60, 120]:
        ma = close.rolling(w).mean()
        feats[f"ma{w}_ratio"] = ma / close
        feats[f"vol_w{w}"] = close.rolling(w).std() / close
    # ---- 价格特征 - 同部分 -

    # ---- 动量特征 (8维) ----
    for w in [1, 3, 5, 10, 20, 40, 60, 120]:
        col = f"mom_{w}d"
        feats[col] = close.pct_change(w)

    # ---- 成交量/资金特征 (8维) ----
    feats["volume_change_1d"] = volume.pct_change()
    feats["volume_ratio_5"] = volume / volume.rolling(5).mean()
    feats["volume_ratio_10"] = volume / volume.rolling(10).mean()
    feats["volume_ratio_20"] = volume / volume.rolling(20).mean()
    feats["volume_trend_5d"] = volume.rolling(5).mean().pct_change(5)
    feats["amount_ratio_5"] = amount / amount.rolling(5).mean()
    feats["amount_ma_ratio"] = amount.rolling(5).mean() / amount.rolling(20).mean()
    feats["up_down_ratio"] = (close > close.shift(1)).rolling(10).mean()  # 近10日上涨天数比例

    # ---- 波动/价差特征 (6维) ----
    feats["atr_14"] = (high - low).rolling(14).mean() / close
    feats["atr_ratio_5_20"] = (high - low).rolling(5).mean() / ((high - low).rolling(20).mean() + 1e-8)
    feats["hl_ratio"] = (high - low) / close
    feats["close_position"] = (close - low) / (high - low + 1e-6)  # 收盘价在高低价中的位置
    feats["open_gap"] = (open_p - prev_close) / (prev_close + 1e-6)
    feats["gap_retention"] = (close > open_p).astype(int)  # 日内是否收阳

    # ---- RSI / MACD / 布林带 (7维) ----
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    feats["rsi_14"] = 100 - (100 / (1 + gain / (loss + 1e-8)))
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    feats["macd"] = ema12 - ema26
    feats["macd_signal"] = feats["macd"].ewm(span=9).mean()
    feats["macd_hist"] = feats["macd"] - feats["macd_signal"]
    bb_mid = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    feats["bb_position"] = (close - bb_mid) / (2 * bb_std + 1e-6)
    feats["bb_width"] = (2 * bb_std) / (bb_mid + 1e-6)
    feats["bollinger_squeeze"] = (feats["bb_width"] == feats["bb_width"].rolling(20).min()).astype(int)

    # ---- 基本面特征 (5维) ----
    feats["turnover_rate"] = volume / (volume.rolling(250).mean() + 1e-8)  # 换手率代理
    feats["amplitude"] = (high - low) / (prev_close + 1e-6)
    feats["price_position_52w"] = close / close.rolling(250).max()  # 距52周高点
    feats["drawdown_20d"] = close / close.rolling(20).max() - 1  # 近20日回撤
    feats["drawdown_60d"] = close / close.rolling(60).max() - 1  # 近60日回撤

    # ---- 趋势/反转特征 (5维) ----
    feats["above_ma20"] = (close > close.rolling(20).mean()).astype(int)
    feats["above_ma60"] = (close > close.rolling(60).mean()).astype(int)
    feats["ma20_slope"] = close.rolling(20).mean().diff(5) / (close.rolling(20).mean() + 1e-6)
    feats["new_high_20d"] = (close == close.rolling(20).max()).astype(int)
    feats["new_low_20d"] = (close == close.rolling(20).min()).astype(int)

    # ══════════ v4.5.9: A股专项特征 (8维, 仅growth/flex层) ══════════
    if use_aggressive:
        # 量价背离: 价格涨但量缩, 或价格跌但量增 → 反转信号
        price_dir_5 = (close.pct_change(5) > 0).astype(int)
        vol_dir_5 = (volume.pct_change(5) > 0).astype(int)
        feats["vp_divergence"] = (price_dir_5 != vol_dir_5).astype(int)

        # 换手率加速度
        turnover_proxy = volume / (volume.rolling(250).mean() + 1e-8)
        feats["turnover_accel"] = turnover_proxy.diff(5) - turnover_proxy.diff(10)

        # 跳空缺口
        gap = (open_p - prev_close) / (prev_close + 1e-6)
        feats["gap_up"] = (gap > 0.02).astype(int)
        feats["gap_down"] = (gap < -0.02).astype(int)
        feats["gap_retention_5d"] = gap.rolling(5).mean()

        # 距涨跌停距离
        feats["limit_up_dist"] = (close * 1.1 - close) / close
        feats["limit_down_dist"] = (close - close * 0.9) / close

        # 连续涨跌方向
        up_days = (close > close.shift(1)).rolling(10).sum()
        down_days = (close < close.shift(1)).rolling(10).sum()
        feats["consecutive_direction"] = (up_days - down_days) / 10

    # ══════════ 目标 ══════════
    feats["target"] = close.shift(-PREDICT_HORIZON) / close - 1

    # Phase 4: 波动率调整目标 - 降低高波动期噪声
    ret = close.pct_change()
    future_vol = ret.rolling(20).std().shift(-PREDICT_HORIZON)
    feats["target_voladj"] = feats["target"] / (future_vol + 1e-8)

    return feats


# ============================================================
# Phase 3: Optuna超参搜索
# ============================================================
def optimize_lgb(X_train, y_train, X_val, y_val):
    """用Optuna搜索LightGBM最优超参"""
    if not HAS_OPTUNA:
        return None

    def objective(trial):
        # P1: 收紧搜索范围防过拟合 → max_depth 3-8, n_est 50-250
        params = {
            "n_estimators": trial.suggest_int("n_estimators", 50, 250),
            "max_depth": trial.suggest_int("max_depth", 3, 8),
            "num_leaves": trial.suggest_int("num_leaves", 15, 63),
            "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
            "subsample": trial.suggest_float("subsample", 0.5, 0.9),
            "colsample_bytree": trial.suggest_float("colsample_bytree", 0.5, 0.9),
            "reg_alpha": trial.suggest_float("reg_alpha", 0.01, 1.0, log=True),
            "reg_lambda": trial.suggest_float("reg_lambda", 0.01, 1.0, log=True),
            "min_child_samples": trial.suggest_int("min_child_samples", 10, 50),
            "random_state": 42,
            "verbose": -1,
            "n_jobs": -1,
        }
        model = lgb.LGBMRegressor(**params)
        model.fit(X_train, y_train, eval_set=[(X_val, y_val)],
                  callbacks=[lgb.early_stopping(20, verbose=False)])
        y_pred = model.predict(X_val)
        return np.mean((y_pred > 0) == (y_val > 0))

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=OPTUNA_TRIALS, show_progress_bar=False)
    return study.best_params if study.best_trial else None


# ============================================================
# 核心训练函数
# ============================================================
def train_single_stock(code, name, tier: str = "core"):
    """训练单只股票: LGBM + XGBoost + CatBoost + 分类器 集成

    Args:
        code: 股票代码
        name: 股票名称
        tier: tier级别 (bluechip/core保持原特征, growth/flex启用A股专项特征+CatBoost)
    """
    use_aggressive = tier in AGGRESSIVE_TIERS

    print(f"\n{'='*50}")
    tag = "⚡A股特征+CatBoost" if use_aggressive else "标准特征"
    print(f"🔧 训练 {code} {name} [{tier}] {tag}")

    try:
        # 1. 拉取K线数据 (v4.5.9: 自动调整窗口防过拟合)
        normal = normalize_symbol(code)
        end = datetime.now().strftime("%Y-%m-%d")
        # 先用5年拉数据,后续如果样本过多再裁剪
        start = (datetime.now() - timedelta(days=TRAIN_YEARS * 365)).strftime("%Y-%m-%d")
        kline = get_kline(normal, start, end)

        if not kline or len(kline) < MIN_TRAIN_SAMPLES:
            print(f"  ❌ 数据不足 ({len(kline) if kline else 0}条 < {MIN_TRAIN_SAMPLES})")
            return None

        df = pd.DataFrame(kline)
        for col in ["close", "volume", "high", "low", "open"]:
            df[col] = df[col].astype(float)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
            df = df.dropna(subset=["date"]).sort_values("date").set_index("date", drop=False)

        # v4.5.9: 自动缩窗 - 样本>800时截取最近3年, 避免2021-2022极端行情污染
        if len(df) > 800:
            cutoff = datetime.now() - timedelta(days=365 * 3)
            df["_date_tmp"] = pd.to_datetime(df.get("date", df.index), errors="coerce")
            df = df[df["_date_tmp"] >= cutoff].drop(columns=["_date_tmp"])
            print(f"  ⏱️ 自动缩窗: {len(kline)}→{len(df)}天 (3年窗口, 防过拟合)")

        # v4.5.18 P1: ST股过滤 - 从akshare获取实时ST列表,ST股跳过训练
        try:
            import akshare as _ak
            _st_df = _ak.stock_zh_a_st_em()
            if _st_df is not None and len(_st_df) > 0:
                if code in _st_df['代码'].values:
                    print(f"  🚫 {code} {name} 当前为ST股,跳过训练")
                    return None
        except Exception:
            pass

        # 2. 构建特征(按tier分级)
        features = build_features(df, use_aggressive=use_aggressive)
        # v4.6.5: 注入情绪/资金流特征(北向/融资/VIX/行业动量)
        try:
            from core.sentiment_features import inject_sentiment_to_df
            features = inject_sentiment_to_df(features, code, name, "")
        except Exception as _se:
            pass  # 情绪特征注入失败不阻断训练
        features = features.replace([np.inf, -np.inf], np.nan).dropna()
        if len(features) < MIN_TRAIN_SAMPLES:
            print(f"  ❌ 有效特征行数不足: {len(features)}")
            return None

        print(f"  📊 {len(df)}天原始数据 → {len(features)}天有效样本 → {len([c for c in features.columns if c != 'target'])}维特征")

        # Phase 3: 时间权重(近期数据权重大)
        _has_time_weights = False
        features = features.replace([np.inf, -np.inf], np.nan).dropna()
        if len(features) < MIN_TRAIN_SAMPLES:
            print(f"  ❌ 有效特征行数不足: {len(features)}")
            return None

        # P0 FIX: 先做时序切分,再分别计算截面排名(消除前视偏差)
                # P0: 训练/验证/测试三分 (70:15:15)
        val_split_idx = int(len(features) * (1 - TEST_SPLIT - VAL_SPLIT))
        split_idx = int(len(features) * (1 - TEST_SPLIT))
        train = features.iloc[:val_split_idx]
        val = features.iloc[val_split_idx:split_idx]
        test = features.iloc[split_idx:]
        print(f"  📊 样本划分: 训练{len(train)}/验证{len(val)}/测试{len(test)} (消除集成权重循环)")

        # P0: train/val/test 各自按自身截止日计算截面特征, 再对齐列。
        train, _, _, _ = _add_cross_sectional_features(train, code, "训练集")
        val, _, _, _ = _add_cross_sectional_features(val, code, "验证集")
        test, _, _, _ = _add_cross_sectional_features(test, code, "测试集")
        train, val, test, rank_cols, rel_cols = _align_cross_sectional_columns(train, val, test)
        if rank_cols:
            print(f"  📊 截面特征(独立计算): rank{len(rank_cols)}维 + rel{len(rel_cols)}维 [P0:消除前视偏差]")

        feature_cols = [c for c in train.columns if c != "target" and c != "target_voladj"]
        X_train_raw = train[feature_cols].values
        y_train_raw = train["target"].values
        X_val_raw = val[feature_cols].values
        y_val_raw = val["target"].values
        X_test_raw = test[feature_cols].values
        y_test_raw = test["target"].values

        # Phase 4: 特征选择 - 用LightGBM筛选Top特征,减少过拟合 (Gap 6)
        try:
            selector = SelectFromModel(
                lgb.LGBMRegressor(n_estimators=100, max_depth=4, random_state=42, verbose=-1),
                threshold="median", max_features=min(45, len(feature_cols))
            )
            selector.fit(X_train_raw, y_train_raw)
            mask = selector.get_support()
            n_selected = mask.sum()
            if n_selected >= 10:  # 至少保留10个特征
                X_train_raw = X_train_raw[:, mask]
                X_val_raw = X_val_raw[:, mask]
                X_test_raw = X_test_raw[:, mask]
                feature_cols_selected = [feature_cols[i] for i in range(len(feature_cols)) if mask[i]]
                print(f"  🔍 特征选择: {len(feature_cols)}→{n_selected}维 (Top 5: {feature_cols_selected[:5]})")
                feature_cols = feature_cols_selected
        except Exception as e:
            print(f"  ⚠️ 特征选择跳过: {e}")

        # Phase 3: 时间加权采样(近期数据权重大)
        n_train = len(X_train_raw)
        sample_weight = np.exp(np.linspace(-2.5, 0, n_train))  # oldest~0.08, newest~1.0
        sample_weight /= sample_weight.sum()  # 归一化

        # 4. 标准化
        scaler = StandardScaler()
        X_train = scaler.fit_transform(X_train_raw)
        X_val = scaler.transform(X_val_raw)
        X_test = scaler.transform(X_test_raw)

        # 5. 交叉验证评估
        tscv = TimeSeriesSplit(n_splits=min(N_FOLDS, 5))
        cv_scores = []
        cv_clf_scores = []
        for tr_idx, vl_idx in tscv.split(X_train):
            X_tr, X_vl = X_train[tr_idx], X_train[vl_idx]
            y_tr, y_vl = y_train_raw[tr_idx], y_train_raw[vl_idx]
            # v4.5.5 S6: CV使用与最终模型一致的参数
            m = lgb.LGBMRegressor(
                n_estimators=min(N_ESTIMATORS_DEFAULT, 150),
                max_depth=MAX_DEPTH_DEFAULT,
                num_leaves=min(MAX_DEPTH_DEFAULT * 2 + 1, 63),
                learning_rate=0.025,
                subsample=0.8, colsample_bytree=0.8,
                reg_alpha=REG_ALPHA_DEFAULT,
                reg_lambda=REG_LAMBDA_DEFAULT,
                random_state=42, verbose=-1, n_jobs=-1
            )
            # CV-fold时间权重
            sw_cv = np.exp(np.linspace(-2, 0, len(y_tr)))
            m.fit(X_tr, y_tr, sample_weight=sw_cv, eval_set=[(X_vl, y_vl)],
                  callbacks=[lgb.early_stopping(25, verbose=False)])
            y_pred = m.predict(X_vl)
            cv_scores.append(np.mean((y_pred > 0) == (y_vl > 0)))

            # Phase 2: 分类器CV
            clf = lgb.LGBMClassifier(
                n_estimators=min(N_ESTIMATORS_DEFAULT, 150),
                max_depth=MAX_DEPTH_DEFAULT, num_leaves=31,
                learning_rate=0.025, subsample=0.8, colsample_bytree=0.8,
                reg_alpha=REG_ALPHA_DEFAULT, reg_lambda=REG_LAMBDA_DEFAULT,
                random_state=42, verbose=-1, n_jobs=-1,
            )
            y_tr_bin = (y_tr > 0).astype(int)
            y_vl_bin = (y_vl > 0).astype(int)
            clf.fit(X_tr, y_tr_bin, sample_weight=sw_cv, eval_set=[(X_vl, y_vl_bin)],
                    callbacks=[lgb.early_stopping(25, verbose=False)])
            clf_pred = clf.predict(X_vl)
            cv_clf_scores.append(np.mean(clf_pred == y_vl_bin))

        cv_accuracy = np.mean(cv_scores) if cv_scores else 0
        cv_clf_accuracy = np.mean(cv_clf_scores) if cv_clf_scores else 0
        print(f"  📈 5-fold CV: 回归={cv_accuracy:.2%}(σ={np.std(cv_scores):.3f}) 分类器={cv_clf_accuracy:.2%}(σ={np.std(cv_clf_scores):.3f})")

        # 6. Optuna超参搜索
        best_params = None
        if USE_OPTUNA and HAS_OPTUNA and not SKIP_OPTUNA:
            print(f"  🔍 Optuna超参搜索中 ({OPTUNA_TRIALS} trials)...")
            # P0: Optuna使用独立验证集
            best_params = optimize_lgb(X_train, y_train_raw, X_val, y_val_raw)
            if best_params:
                print(f"  ✅ 最优参数: n_est={best_params.get('n_estimators','?')} depth={best_params.get('max_depth','?')} lr={best_params.get('learning_rate','?'):.4f}")

        # 7. 训练最终LGBM (P1: 降低复杂度防过拟合, 支持adaptive覆盖)
        if best_params:
            # 用Optuna搜索的参数,但限制上限
            best_params["max_depth"] = min(best_params.get("max_depth", MAX_DEPTH_DEFAULT), 6)
            best_params["n_estimators"] = min(best_params.get("n_estimators", N_ESTIMATORS_DEFAULT), 250)
            best_params["num_leaves"] = min(best_params.get("num_leaves", 31), 40)
            best_params["reg_alpha"] = max(best_params.get("reg_alpha", REG_ALPHA_DEFAULT), 0.3)
            best_params["reg_lambda"] = max(best_params.get("reg_lambda", REG_LAMBDA_DEFAULT), 0.3)
            lgb_model = lgb.LGBMRegressor(**best_params)
        else:
            lgb_model = lgb.LGBMRegressor(
                n_estimators=N_ESTIMATORS_DEFAULT, max_depth=MAX_DEPTH_DEFAULT, num_leaves=31, learning_rate=0.025,
                subsample=0.7, colsample_bytree=0.7, reg_alpha=REG_ALPHA_DEFAULT, reg_lambda=REG_LAMBDA_DEFAULT,
                min_child_samples=MIN_CHILD_SAMPLES_DEFAULT, random_state=42, verbose=-1, n_jobs=-1,
            )
        lgb_model.fit(X_train, y_train_raw, sample_weight=sample_weight, eval_set=[(X_val, y_val_raw)],
                      callbacks=[lgb.early_stopping(50, verbose=False)])

        # 8. 训练XGBoost (互补模型, 支持adaptive覆盖)
        xgb_model = xgb.XGBRegressor(
            n_estimators=max(100, N_ESTIMATORS_DEFAULT - 50), max_depth=MAX_DEPTH_DEFAULT, learning_rate=0.025, subsample=0.8,
            colsample_bytree=0.8, reg_alpha=REG_ALPHA_DEFAULT, reg_lambda=REG_LAMBDA_DEFAULT,
            min_child_weight=max(1, MIN_CHILD_SAMPLES_DEFAULT // 7), random_state=42, verbosity=0, n_jobs=-1,
        )
        xgb_model.fit(X_train, y_train_raw, sample_weight=sample_weight, eval_set=[(X_val, y_val_raw)], verbose=False)

        # v4.5.9: CatBoost (仅growth/flex层, 高波动标的更受益)
        cb_model = None
        cb_acc = 0.0
        if HAS_CATBOOST and use_aggressive:
            try:
                cb_model = cb.CatBoostRegressor(
                    iterations=min(N_ESTIMATORS_DEFAULT, 200),
                    depth=MAX_DEPTH_DEFAULT,
                    learning_rate=0.025,
                    l2_leaf_reg=REG_LAMBDA_DEFAULT,
                    random_seed=42,
                    verbose=False,
                    thread_count=-1,
                )
                cb_model.fit(X_train, y_train_raw, sample_weight=sample_weight,
                            eval_set=(X_val, y_val_raw), silent=True)
                cb_preds_val = cb_model.predict(X_val)
                cb_acc = np.mean((cb_preds_val > 0) == (y_val_raw > 0))
            except Exception:
                cb_model = None

        # Phase 2: 训练LGBM分类器(直接预测方向)
        clf_model = lgb.LGBMClassifier(
            n_estimators=N_ESTIMATORS_DEFAULT, max_depth=min(MAX_DEPTH_DEFAULT, 6), num_leaves=31,
            learning_rate=0.025, subsample=0.7, colsample_bytree=0.7,
            reg_alpha=REG_ALPHA_DEFAULT, reg_lambda=REG_LAMBDA_DEFAULT,
            min_child_samples=MIN_CHILD_SAMPLES_DEFAULT, random_state=42, verbose=-1, n_jobs=-1,
        )
        y_train_bin = (y_train_raw > 0).astype(int)
        y_test_bin = (y_test_raw > 0).astype(int)
        y_val_bin = (y_val_raw > 0).astype(int)
        clf_model.fit(X_train, y_train_bin, sample_weight=sample_weight, eval_set=[(X_val, y_val_bin)],
                      callbacks=[lgb.early_stopping(50, verbose=False)])

        # 9. 多模型集成评估(P0: 验证集计算权重,测试集独立评估)
        # 验证集预测 → 计算集成权重
        lgb_preds_val = lgb_model.predict(X_val)
        xgb_preds_val = xgb_model.predict(X_val)
        clf_probas_val = clf_model.predict_proba(X_val)[:, 1]
        clf_direction_val = clf_model.predict(X_val)

        lgb_acc = np.mean((lgb_preds_val > 0) == (y_val_raw > 0))
        xgb_acc = np.mean((xgb_preds_val > 0) == (y_val_raw > 0))
        clf_acc = np.mean(clf_direction_val == y_val_bin)
        lgb_polarity = -1 if lgb_acc < 0.5 else 1
        xgb_polarity = -1 if xgb_acc < 0.5 else 1
        clf_polarity = -1 if clf_acc < 0.5 else 1
        cb_polarity = -1 if cb_model and cb_acc < 0.5 else 1
        lgb_eff_acc = max(lgb_acc, 1 - lgb_acc)
        xgb_eff_acc = max(xgb_acc, 1 - xgb_acc)
        clf_eff_acc = max(clf_acc, 1 - clf_acc)
        cb_eff_acc = max(cb_acc, 1 - cb_acc) if cb_model else 0.0

        # 自适应权重: 精度归一化 (含CatBoost)
        total_acc = lgb_eff_acc + xgb_eff_acc + clf_eff_acc + cb_eff_acc + 1e-6
        w_lgb = lgb_eff_acc / total_acc
        w_xgb = xgb_eff_acc / total_acc
        w_clf = clf_eff_acc / total_acc
        w_cb = cb_eff_acc / total_acc if cb_model else 0.0

        # 信号融合:验证集预测值 + 验证集权重
        lgb_signal_val = ((lgb_preds_val > 0).astype(float) * 2 - 1) * lgb_polarity
        xgb_signal_val = ((xgb_preds_val > 0).astype(float) * 2 - 1) * xgb_polarity
        clf_score_val = (clf_probas_val - 0.5) * 2 * clf_polarity
        cb_signal = (((cb_preds_val > 0).astype(float) * 2 - 1) * cb_polarity
                     if cb_model else np.zeros(len(lgb_preds_val)))
        ensemble_signal = (w_lgb * lgb_signal_val + w_xgb * xgb_signal_val +
                          w_clf * clf_score_val + w_cb * cb_signal)
        ens_acc = np.mean((ensemble_signal > 0) == (y_val_raw > 0))

        # 10. 测试集独立评估(held-out,权重来自验证集,不参与任何训练决策)
        lgb_test_preds = lgb_model.predict(X_test)
        xgb_test_preds = xgb_model.predict(X_test)
        clf_test_probas = clf_model.predict_proba(X_test)[:, 1]
        test_lgb_sig = ((lgb_test_preds > 0).astype(float) * 2 - 1) * lgb_polarity
        test_xgb_sig = ((xgb_test_preds > 0).astype(float) * 2 - 1) * xgb_polarity
        test_clf_score = (clf_test_probas - 0.5) * 2 * clf_polarity
        test_cb_sig = (((cb_model.predict(X_test) > 0).astype(float) * 2 - 1) * cb_polarity
                       if cb_model else np.zeros_like(test_lgb_sig))
        test_ensemble = (w_lgb * test_lgb_sig + w_xgb * test_xgb_sig +
                        w_clf * test_clf_score + w_cb * test_cb_sig)
        test_bin = (y_test_raw > 0).astype(int)
        test_ens_acc = np.mean((test_ensemble > 0) == test_bin)
        print(f"  🧪 测试集(Held-out): 集成方向精度={test_ens_acc:.2%} (从未参与训练/权重/特征选择)")

        # 11. 最新预测 (P1fix: 保留回归幅度,不再硬裁剪为±3%)
        latest_lgb = lgb_model.predict(X_test[-1:])
        latest_xgb = xgb_model.predict(X_test[-1:])
        latest_clf_p = clf_model.predict_proba(X_test[-1:])[:, 1][0]
        latest_cb = cb_model.predict(X_test[-1:])[0] if cb_model else 0
        latest_price = float(df["close"].iloc[-1])

        # 加权回归预测(保留真实幅度)
        reg_w = w_lgb + w_xgb + w_cb + 1e-6
        latest_lgb_adj = latest_lgb[0] * lgb_polarity
        latest_xgb_adj = latest_xgb[0] * xgb_polarity
        latest_cb_adj = latest_cb * cb_polarity
        latest_clf_score = (latest_clf_p - 0.5) * clf_polarity
        reg_pred = (w_lgb * latest_lgb_adj + w_xgb * latest_xgb_adj) / max(w_lgb + w_xgb, 1e-6)
        if cb_model and cb_acc > 0:
            reg_pred = (reg_pred * (w_lgb + w_xgb) + w_cb * latest_cb_adj) / reg_w

        # 分类器信号加权融合
        clf_adjustment = latest_clf_score * w_clf * 0.04
        predicted_return_raw = reg_pred + clf_adjustment
        predicted_return_raw = max(-0.15, min(0.15, float(predicted_return_raw)))

        # 方向信号(兼容旧接口)
        lgb_s = np.sign(latest_lgb_adj)
        xgb_s = np.sign(latest_xgb_adj)
        cb_s = np.sign(latest_cb_adj)
        latest_signal = (w_lgb * lgb_s + w_xgb * xgb_s + w_clf * latest_clf_score * 2 + w_cb * cb_s)

        # 11. 保存模型
        model_dir = os.path.join(MODELS_DIR, code)
        os.makedirs(model_dir, exist_ok=True)
        joblib.dump(lgb_model, os.path.join(model_dir, "lightgbm.pkl"))
        joblib.dump(lgb_model, os.path.join(model_dir, "lightgbm_5d.pkl"))
        joblib.dump(xgb_model, os.path.join(model_dir, "xgboost.pkl"))
        joblib.dump(xgb_model, os.path.join(model_dir, "xgboost_5d.pkl"))
        joblib.dump(clf_model, os.path.join(model_dir, "lightgbm_clf.pkl"))
        if cb_model:
            joblib.dump(cb_model, os.path.join(model_dir, "catboost.pkl"))
        joblib.dump(scaler, os.path.join(model_dir, "scaler.pkl"))
        joblib.dump(scaler, os.path.join(model_dir, "scaler_5d.pkl"))
        # P1fix: 保存metadata含test+val准确率防过拟合置信度
        train_metadata = {
            'feature_cols': feature_cols,
            'use_regression': True,
            'dir_accuracy': round(float(test_ens_acc), 4),  # held-out test
            'val_dir_accuracy': round(float(ens_acc), 4),   # validation
            'lgb_polarity': int(lgb_polarity),
            'clf_polarity': int(clf_polarity),
            'r2': -1,
            'generated_at': datetime.now().isoformat(),
        }
        joblib.dump(train_metadata, os.path.join(model_dir, "model_metadata.pkl"))
        
        # v4.6.5: Transformer深度学习模型（并行训练）
        try:
            from core.transformer_model import train_transformer
            tf_result = train_transformer(features_train_only, target_col="target",
                                          model_dir=model_dir, code=code, verbose=False)
            if tf_result:
                tf_acc = tf_result.get("direction_accuracy", 0)
                print(f"  🧠 Transformer: dir_acc={tf_acc:.2%}")
                # 集成权重更新：Transformer参与最终预测加权
                train_metadata["tf_dir_accuracy"] = tf_acc
                train_metadata["use_transformer"] = True
                joblib.dump(train_metadata, os.path.join(model_dir, "model_metadata.pkl"))
        except Exception as _tfe:
            print(f"  ⚠️ Transformer训练跳过({code}): {_tfe}")
        
        cb_str = f" CatBoost={cb_acc:.2%}" if cb_model else ""
        print(f"  📊 验证集: LGBM={lgb_acc:.2%} XGB={xgb_acc:.2%} Clf={clf_acc:.2%}{cb_str} 集成={ens_acc:.2%}")
        print(f"  🧭 Polarity: LGBM={lgb_polarity:+d} XGB={xgb_polarity:+d} Clf={clf_polarity:+d}" +
              (f" CatBoost={cb_polarity:+d}" if cb_model else ""))
        w_str = f" CatBoost={w_cb:.2f}" if cb_model else ""
        print(f"  🎯 权重: LGBM={w_lgb:.2f} XGB={w_xgb:.2f} Clf={w_clf:.2f}{w_str} 方向={'看涨' if latest_signal > 0 else '看跌'}")
        print(f"  🎯 预测{PREDICT_HORIZON}日收益: {predicted_return_raw:+.3%} (价格{latest_price*(1+predicted_return_raw):.2f}) (CV=回归{cv_accuracy:.2%} 分类器{cv_clf_accuracy:.2%})")

        # v4.5.12 P2-11: 保存训练特征快照用于PSI漂移监控
        try:
            _psi_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", code)
            os.makedirs(_psi_dir, exist_ok=True)
            _psi_path = os.path.join(_psi_dir, "training_features.npy")
            _aligned_feature_frame = pd.concat(
                [train[feature_cols], val[feature_cols], test[feature_cols]],
                axis=0,
            )
            _feature_data = _aligned_feature_frame.values
            np.save(_psi_path, _feature_data)
        except Exception:
            pass

        return {
            "symbol": code,
            "model": "lgb_xgb_ensemble",
            "model_source": "lgb_enhanced_v430",
            "latest_price": latest_price,
            "predicted_return": round(predicted_return_raw, 6),
            "predicted_price": round(float(latest_price * (1 + predicted_return_raw)), 2),
            "prediction_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_points": len(features),
            "features_used": len(feature_cols),
            "direction_accuracy": round(float(test_ens_acc), 4),   # P0: 使用held-out测试集精度
            "val_direction_accuracy": round(float(ens_acc), 4),    # P0: 验证集精度(权重计算用)
            "test_direction_accuracy": round(float(test_ens_acc), 4),  # P0: 测试集精度(最终评估)
            "cv_accuracy": round(float(cv_accuracy), 4),
            "lgb_accuracy": round(float(lgb_acc), 4),
            "xgb_accuracy": round(float(xgb_acc), 4),
            "clf_accuracy": round(float(clf_acc), 4),  # Phase 2
            "cv_clf_accuracy": round(float(cv_clf_accuracy), 4),  # Phase 2
            "ensemble_weights": {"lgb": round(float(w_lgb), 3), "xgb": round(float(w_xgb), 3), "clf": round(float(w_clf), 3)},
            "train_years": TRAIN_YEARS,
            "predict_horizon": PREDICT_HORIZON,
        }

    except Exception as e:
        print(f"  ❌ {code} 训练失败: {e}")
        import traceback
        traceback.print_exc()
        return {"symbol": code, "error": str(e), "prediction_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ============================================================
# 主流程
# ============================================================
def main():
    stock_pool = get_stock_pool()
    print(f"🚀 DSL v4.5.1 预测模型训练 - Phase 1+2+3 全面升级")
    print(f"   股票池: {len(stock_pool)}只")
    print(f"   训练周期: {TRAIN_YEARS}年 | 特征窗口: {LOOKBACK}天 | 预测: {PREDICT_HORIZON}天后")
    print(f"   特征维度: 50+ | 交叉验证: {N_FOLDS}-fold时序 | 超参搜索: {'Optuna' if USE_OPTUNA else '固定参数'}")
    print(f"   开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}
    success = fail = 0

    for stock in stock_pool:
        r = train_single_stock(stock["symbol"], stock["name"])
        if r:
            results[stock["symbol"]] = r
            if "error" in r:
                fail += 1
            else:
                success += 1
        gc.collect()

    # 保存预测结果
    pred_file = os.path.join(PRED_DIR, f"prediction_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(pred_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 保存训练报告
    train_log = os.path.join(TRAIN_LOG_DIR, f"training_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    accuracies = [r["direction_accuracy"] for r in results.values() if "error" not in r]
    cv_accuracies = [r["cv_accuracy"] for r in results.values() if "error" not in r and "cv_accuracy" in r]
    report = {
        "train_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "v4.5.1",
        "phases": ["5年数据+5日预测", "50+维特征", "Optuna+5fold+集成"],
        "params": {
            "train_years": TRAIN_YEARS, "lookback": LOOKBACK,
            "predict_horizon": PREDICT_HORIZON, "optuna_trials": OPTUNA_TRIALS,
        },
        "summary": {
            "total": len(stock_pool), "success": success, "failed": fail,
            "mean_accuracy": round(float(np.mean(accuracies)) if accuracies else 0, 4),
            "median_accuracy": round(float(np.median(accuracies)) if accuracies else 0, 4),
            "mean_cv": round(float(np.mean(cv_accuracies)) if cv_accuracies else 0, 4),
            "accuracy_above_50": sum(1 for a in accuracies if a > 0.5),
            "accuracy_above_55": sum(1 for a in accuracies if a > 0.55),
        },
        "results": [r for r in results.values() if "error" not in r],
        "errors": [r for r in results.values() if "error" in r],
    }
    with open(train_log, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 终端汇总
    print(f"\n{'='*60}")
    print(f"📊 训练完成: {success}成功 / {fail}失败")
    if accuracies:
        print(f"   方向精度: 均值{np.mean(accuracies):.2%} 中位数{np.median(accuracies):.2%}")
        print(f"   >50%: {sum(1 for a in accuracies if a > 0.5)}/{len(accuracies)}")
        print(f"   >55%: {sum(1 for a in accuracies if a > 0.55)}/{len(accuracies)}")
    if cv_accuracies:
        print(f"   CV平均: {np.mean(cv_accuracies):.2%}")
    print(f"   预测: {pred_file}")
    print(f"   训练报告: {train_log}")

    return results


if __name__ == "__main__":
    results = main()
    # 训练完成后自动同步股票池到飞书
    print("\n🔄 训练完成,同步股票池到飞书...")
    try:
        from scripts.sync_bitable import sync_stock_pool_from_config
        count = sync_stock_pool_from_config()
        print(f"✅ 飞书股票池同步完成: {count} 条")
    except Exception as e:
        print(f"⚠️ 飞书同步失败: {e}")
