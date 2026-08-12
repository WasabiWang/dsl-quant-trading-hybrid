#!/usr/bin/env python3
"""
DSL v4.5.1 增强预测模型 — 量化机构级优化

新增:
1. 基本面特征（PE/ROE/营收增速/每股收益等，来自麦蕊API）
2. 多时间框架集成（1d+5d+20d三档预测）
3. 特征重要性剪枝（去除噪声特征）
4. 质量过滤（数据不足/极端波动标记）
5. 样本外Walk-Forward验证（更真实的精度估计）

输出: models/<code>/lightgbm.pkl + reports/predictor/
"""
import os, sys, json, gc, time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import joblib, yaml

# v4.5.20: 加载 .env.local（统一密钥文件），确保 MAIRUI_LICENCE 等API密钥可用
try:
    from dotenv import load_dotenv
    _pr = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(_ep := os.path.join(_pr, ".env")):
        load_dotenv(_ep, override=False)
    if os.path.exists(_elp := os.path.join(_pr, ".env.local")):
        load_dotenv(_elp, override=True)
except Exception:
    pass

warnings = __import__('warnings')
warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from dsl_data_sdk_original import get_kline, normalize_symbol
from config.mairui_api_config import get_financial_indicators
from scripts.fundamentals_loader import fetch_fundamentals_mairui
from sklearn.preprocessing import StandardScaler, RobustScaler
from sklearn.model_selection import TimeSeriesSplit
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb
import xgboost as xgb

# ═════════════ 参数 ═════════════
TRAIN_YEARS = 5
LOOKBACK = 60
HORIZONS = [1, 5, 20]  # 多时间框架
N_FOLDS = 5
TEST_SPLIT = 0.2
DIR_THRESHOLD = 0.52

# v4.5.3c: 废弃硬编码黑名单 → 改用 tier 自动分层
# 仅观察层(cyclical/flex)跳过训练, 蓝筹/核心/成长全量训练
# 训练后模型性能不足的, 由 dynamic_threshold+signal_weight 在后端抑制

MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
PRED_DIR = os.path.join(PROJECT_ROOT, "reports", "predictor")
OBSERVATION_TIERS = {"cyclical", "flex"}
os.makedirs(PRED_DIR, exist_ok=True)


def get_stock_pool():
    """v4.5.3c: tier 自动分层 — 观察层跳过训练, 其余全量
    v4.6.9f P2-1b: degraded停训机制 — 精度<50%标记的标的跳过训练(省资源)
    """
    pool = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    with open(pool) as f:
        data = yaml.safe_load(f)
    all_stocks = [{"symbol": s["symbol"], "name": s["name"], "tier": s.get("tier", "core"),
                   "degraded": s.get("degraded", False)}
                  for s in data["master_pool"] if "." not in s["symbol"]]
    # 仅观察层跳过训练
    filtered = [s for s in all_stocks if s["tier"] not in OBSERVATION_TIERS]
    excluded = [s for s in all_stocks if s["tier"] in OBSERVATION_TIERS]
    if excluded:
        names = ", ".join(f'{s["symbol"]}({s["name"]}[{s["tier"]}])' for s in excluded)
        print(f"⏸️ 观察层跳过训练: {len(excluded)}只 → {names}")
    # v4.6.9f: degraded停训 — 精度<50%的标的跳过训练(资源浪费+信号噪声)
    _deg = [s for s in filtered if s.get("degraded")]
    if _deg:
        filtered = [s for s in filtered if not s.get("degraded")]
        names = ", ".join(f'{s["symbol"]}({s["name"]})' for s in _deg)
        print(f"⏸️ degraded停训({len(_deg)}只): {names}")
    print(f"✅ h5d训练池: {len(filtered)}/{len(all_stocks)} 只")
    return filtered


def fetch_fundamentals(code: str) -> dict:
    """从麦蕊获取最新基本面数据 (P0-1: 使用统一fundamentals_loader)"""
    return fetch_fundamentals_mairui(code)


def build_features(df: pd.DataFrame, fundamentals: dict = None) -> pd.DataFrame:
    """60+维特征：技术面+量价+基本面"""
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_p = df["open"].astype(float)
    prev = df.get("prev_close", close.shift(1)).astype(float)

    feats = pd.DataFrame(index=df.index)

    # — 价格特征 (12维) —
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

    # — RSI/MACD/布林 (7维) —
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

    # v4.6.9f P0-2: 修复基本面前视偏差 — 仅在最近60个交易日注入,
    # 早期样本填NaN(模型天然忽略), 避免2019年训练样本看到2026年ROE
    # (旧代码对整个序列赋值, 回测精度虚高)
    if fundamentals:
        # 基本面为最新快照, 只能用于最新数据点 — 只对最后MIN_FUND_ROWS行注入
        MIN_FUND_ROWS = 60
        n = len(feats)
        for k, v in fundamentals.items():
            if np.isnan(v):
                feats[f"fund_{k}"] = 0.0
            else:
                col = pd.Series(np.nan, index=feats.index, dtype=float)
                start_idx = max(0, n - MIN_FUND_ROWS)
                col.iloc[start_idx:] = v
                feats[f"fund_{k}"] = col

    # — 多时间框架目标 (v4.5.7: 3种标签) —
    # 统一的波动率分母（20日rolling vol，避免短窗口全NaN）
    vol_20d_est = close.pct_change().rolling(20, min_periods=10).std()
    for h in HORIZONS:
        if h < len(close):
            # ① 原始收益率（回归标签）
            target = close.shift(-h) / close - 1
            feats[f"target_{h}d"] = target
            # ② 波动率调整收益率（稳定标签，抑制高波动噪声）
            feats[f"target_{h}d_voladj"] = target / (vol_20d_est + 1e-6)
            # ③ 方向标签（二分类: 涨幅>0.5%为上涨, 跌幅>0.5%为下跌）
            feats[f"target_{h}d_dir"] = (target > 0.005).astype(int)

    # — v4.5.7: 截面特征 — 历史百分位（票内rank）(12维) —
    for n, period in [("5", 5), ("10", 10), ("20", 20), ("60", 60)]:
        # 动量百分位
        if f"mom_{period}d" in feats.columns:
            feats[f"mom_{n}d_pct"] = feats[f"mom_{period}d"].rank(pct=True)
        # 波动百分位
        if f"vol{period}d" in feats.columns:
            feats[f"vol_{n}d_pct"] = feats[f"vol{period}d"].rank(pct=True)
        # 成交量百分位
        feats[f"vol_ratio_{n}_pct"] = feats.get(f"vol_ratio_{period}", pd.Series(0, index=feats.index)).rank(pct=True)

    # — v4.5.7: 情绪因子 (8维) —
    # 量价背离: 价涨量缩/价跌量放 = 情绪反转信号
    feats["vp_divergence_5"] = feats["mom_5d"] * (1 - feats["vol_ratio_5"])
    feats["vp_divergence_10"] = feats["mom_10d"] * (1 - feats["vol_ratio_10"])
    # 异常放量: 成交量突发暴增
    vol_ma20 = volume.rolling(20).mean()
    feats["volume_spike"] = (volume > vol_ma20 * 2).astype(int)
    feats["volume_abnormal"] = volume / (vol_ma20 + 1e-8) - 1
    # 反转信号: 连续阴跌后放量
    feats["down_vol_5"] = ((feats["mom_5d"] < -0.03) & (feats["vol_ratio_5"] > 1.5)).astype(int)
    # 资金意向: 尾盘/盘中价格强度
    feats["intraday_strength"] = (close / open_p - 1) / (high / low - 1 + 1e-8)
    feats["close_to_high"] = (close - low) / (high - low + 1e-8)
    # 逆势表现: 当日涨跌 vs 自身均线
    ma5_v = close.rolling(5).mean()
    feats["contrarian_strength"] = ((feats["mom_5d"] < 0) & (close > ma5_v)).astype(int)

    # — v4.5.7: 市场状态因子 (10维) —
    # ① 波动率状态: 当前30日波动 vs 历史中位数
    vol_30 = close.pct_change().rolling(30).std()
    vol_median = vol_30.rolling(250).median()
    feats["vol_regime"] = (vol_30 / (vol_median + 1e-8) - 1).clip(-0.5, 2)  # 高>0=高波动
    # ② 趋势状态: 均线多头/空头排列
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    ma120 = close.rolling(120).mean()
    feats["bull_arrange"] = ((ma5_v > ma20) & (ma20 > ma60) & (ma60 > ma120)).astype(int)
    feats["bear_arrange"] = ((ma5_v < ma20) & (ma20 < ma60) & (ma60 < ma120)).astype(int)
    # ③ 风险状态: 回撤幅度 + 回撤恢复阶段
    peak_60 = close.rolling(60).max()
    drawdown = close / peak_60 - 1
    feats["drawdown_regime"] = (drawdown < -0.1).astype(int)  # 深度回撤
    feats["recovery_stage"] = (close.rolling(20).mean() > close.rolling(60).mean()).astype(int)
    # ④ 动量衰减: 短期 vs 长期动量差异 = 趋势衰竭信号
    feats["mom_decay_5x20"] = feats["mom_5d"] - feats["mom_20d"]
    feats["mom_decay_10x60"] = feats["mom_10d"] - feats["mom_60d"]
    # ⑤ 量价趋势一致性
    feats["volume_trend"] = feats["vol_ratio_20"].rolling(10).mean() > 1
    feats["price_volume_harmony"] = (feats["mom_20d"] > 0) & feats["volume_trend"]
    
    return feats


def train_horizon(X_train, y_train, X_val=None, y_val=None, horizon_label=""):
    """训练单个时间框架的集成模型 + 方向分类器"""
    # P1: 降低模型复杂度 → max_depth 7→5, n_estimators 300→200, num_leaves 50→31
    #      增强正则化 → reg_alpha/reg_lambda 0.1→0.5, min_child_samples=20
    lgb_model = lgb.LGBMRegressor(
        n_estimators=200, max_depth=5, num_leaves=31, learning_rate=0.025,
        subsample=0.7, colsample_bytree=0.7, reg_alpha=0.5, reg_lambda=0.5,
        min_child_samples=20, random_state=42, verbose=-1, n_jobs=-1,
    )
    eval_set = [(X_val, y_val)] if X_val is not None else None
    lgb_model.fit(X_train, y_train,
            eval_set=eval_set,
            callbacks=[lgb.early_stopping(50, verbose=False)] if eval_set else None)

    # P1: XGBoost同步降低复杂度 → max_depth 6→5, n_estimators 200→150
    #      增强正则化 → reg_alpha/reg_lambda 0.1→0.5, min_child_weight=3
    xgb_model = xgb.XGBRegressor(
        n_estimators=150, max_depth=5, learning_rate=0.025, subsample=0.8,
        colsample_bytree=0.8, reg_alpha=0.5, reg_lambda=0.5,
        min_child_weight=3, random_state=42, verbosity=0, n_jobs=-1,
    )
    xgb_model.fit(X_train, y_train, eval_set=eval_set, verbose=False)

    # v4.5.7: 方向分类器 — 专门预测涨跌方向(不预测幅度)
    y_dir = (y_train > 0.005).astype(int)
    y_val_dir = (y_val > 0.005).astype(int) if y_val is not None else None
    # 分类器的eval_set需要用方向标签，不是回归标签
    clf_eval_set = [(X_val, y_val_dir)] if (y_val_dir is not None and len(set(y_val_dir)) > 1) else None
    # 检查类别平衡
    n_pos = int(y_dir.sum())
    n_neg = len(y_dir) - n_pos
    has_both_classes = min(n_pos, n_neg) > len(y_dir) * 0.05  # 至少5%的少数类
    
    if has_both_classes:
        class_weight = "balanced" if min(n_pos, n_neg) > len(y_dir) * 0.1 else "auto"
        lgb_clf = lgb.LGBMClassifier(
            n_estimators=150, max_depth=4, num_leaves=21, learning_rate=0.03,
            subsample=0.75, colsample_bytree=0.75, reg_alpha=0.3, reg_lambda=0.3,
            min_child_samples=15, class_weight=class_weight,
            random_state=42, verbose=-1, n_jobs=-1,
        )
        lgb_clf.fit(X_train, y_dir, eval_set=clf_eval_set if len(set(y_dir)) > 1 else None,
                    callbacks=[lgb.early_stopping(30, verbose=False)])

        xgb_clf = xgb.XGBClassifier(
            n_estimators=120, max_depth=4, learning_rate=0.03, subsample=0.75,
            colsample_bytree=0.75, reg_alpha=0.3, reg_lambda=0.3,
            min_child_weight=3, random_state=42, verbosity=0, n_jobs=-1,
        )
        scale_pos = n_neg / n_pos if n_pos > 0 else 1.0
        xgb_clf.set_params(scale_pos_weight=scale_pos)
        xgb_clf.fit(X_train, y_dir, eval_set=clf_eval_set, verbose=False)
    else:
        # 类别严重失衡：用回归模型降级为伪分类器（基于符号）
        # 创建一个wrapper对象，用回归器方向加权
        class ProxyClassifier:
            def predict(self, X):
                return (self._reg.predict(X) > 0).astype(int)
            def predict_proba(self, X):
                p = self._reg.predict(X)
                return np.column_stack([1 - (p > 0).astype(float), (p > 0).astype(float)])
        lgb_clf = ProxyClassifier()
        lgb_clf._reg = lgb_model
        xgb_clf = ProxyClassifier()
        xgb_clf._reg = xgb_model
        # 使用回归器的方向精度作为分类器精度
        print(f"    ⚠️ 分类器跳过（类别失衡: {n_pos}/{n_neg}），使用回归器方向代理")

    return {
        "lgb": lgb_model, "xgb": xgb_model,
        "lgb_clf": lgb_clf, "xgb_clf": xgb_clf
    }


def train_single_stock(code, name):
    """训练单只股票——多时间框架 + 基本面 + Walk-Forward验证"""
    print(f"\n{'='*50}")
    print(f"🔧 {code} {name}")

    # v4.5.13: 单只股票超时保护（防止麦蕊坏数据卡死整个训练）
    import signal
    _timed_out = [False]
    def _handle_timeout(signum, frame):
        _timed_out[0] = True
        raise TimeoutError(f"单只股票训练超时 ({code} {name}, >5min)")
    old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
    signal.alarm(300)  # 5分钟单只超时

    try:
        # 1. 拉取数据
        normal = normalize_symbol(code)
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=TRAIN_YEARS * 365)).strftime("%Y-%m-%d")
        kline = get_kline(normal, start, end)

        if not kline or len(kline) < 250:
            print(f"  ❌ 数据不足 ({len(kline) if kline else 0}条)")
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            return None

        df = pd.DataFrame(kline)
        for col in ["close", "volume", "high", "low", "open"]:
            df[col] = df[col].astype(float)

        # 2. 获取基本面
        fundamentals = fetch_fundamentals(code)
        has_fund = bool(fundamentals and not all(np.isnan(v) for v in fundamentals.values()))

        # 3. 构建特征
        features = build_features(df, fundamentals)
        # v4.6.9h: dropna排除fund_列 — fund_仅最后60行有值(去前视偏差),
        # 若全列参与dropna会删掉1200+历史样本(只剩40行). fund_缺失由模型NaN处理
        _fund_cols = [c for c in features.columns if c.startswith("fund_")]
        _drop_cols = [c for c in features.columns if c not in _fund_cols]
        features = features.replace([np.inf, -np.inf], np.nan)
        features[_drop_cols] = features[_drop_cols].dropna()
        if len(features) < 200:
            print(f"  ❌ 有效样本不足: {len(features)}")
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
            return None

        print(f"  📊 {len(df)}天K线 → {len(features)}样本 → {len([c for c in features.columns if not c.startswith('target_')])}维特征 (基本面: {'✅' if has_fund else '❌'})")

        # 4. 多时间框架训练
        result = {"symbol": code, "model_source": "lgb_xgb_enhanced_v430",
                   "data_points": len(features),
                   "train_years": TRAIN_YEARS, "has_fundamentals": has_fund}

        # v4.5.7: 回归标签只用原始收益率 target_{h}d（不含voladj/dir）
        target_cols = [c for c in features.columns if c.startswith("target_")]
        reg_target_cols = [c for c in target_cols if not c.endswith("_voladj") and not c.endswith("_dir")]
        feature_cols = [c for c in features.columns if c not in target_cols]

        for target_col in reg_target_cols:
            horizon_days = int(target_col.split("_")[1][:-1])  # e.g. "target_5d" → 5

            # 划分
            split = int(len(features) * (1 - TEST_SPLIT))
            train_f = features.iloc[:split]
            test_f = features.iloc[split:]
            # v4.6.9h: 过滤y含NaN的行(序列尾部target_{h}d无未来数据) —
            # 之前全列dropna顺带删了, 现在fund_排除后需显式过滤
            train_f = train_f[train_f[target_col].notna()]
            test_f = test_f[test_f[target_col].notna()]
            X_train_raw = train_f[feature_cols].values
            y_train_raw = train_f[target_col].values
            X_test_raw = test_f[feature_cols].values
            y_test_raw = test_f[target_col].values

            # 标准化
            scaler = StandardScaler()
            X_train = scaler.fit_transform(X_train_raw)
            X_test = scaler.transform(X_test_raw)

            # 特征选择（去噪声）
            selector = SelectFromModel(
                lgb.LGBMRegressor(n_estimators=50, random_state=42, verbose=-1),
                threshold="median", max_features=40
            )
            X_train_sel = selector.fit_transform(X_train, y_train_raw)
            X_test_sel = selector.transform(X_test)

            # Walk-Forward CV (方向精度)
            tscv = TimeSeriesSplit(n_splits=min(N_FOLDS, 5))
            cv_scores_dir = []
            cv_scores_clf = []
            for tr_idx, vl_idx in tscv.split(X_train_sel):
                X_tr, X_vl = X_train_sel[tr_idx], X_train_sel[vl_idx]
                y_tr, y_vl = y_train_raw[tr_idx], y_train_raw[vl_idx]
                # v4.5.7: CV同时评估回归方向 + 分类器方向
                m = lgb.LGBMRegressor(n_estimators=80, max_depth=5, learning_rate=0.025,
                                       reg_alpha=0.3, reg_lambda=0.3,
                                       random_state=42, verbose=-1, n_jobs=-1)
                m.fit(X_tr, y_tr, eval_set=[(X_vl, y_vl)],
                      callbacks=[lgb.early_stopping(15, verbose=False)])
                p = m.predict(X_vl)
                cv_scores_dir.append(np.mean((p > 0) == (y_vl > 0.005)))
                # 分类器CV
                y_tr_dir = (y_tr > 0.005).astype(int)
                y_vl_dir = (y_vl > 0.005).astype(int)
                if len(set(y_tr_dir)) > 1:
                    mc = lgb.LGBMClassifier(n_estimators=60, max_depth=4,
                                             class_weight='balanced',
                                             random_state=42, verbose=-1, n_jobs=-1)
                    mc.fit(X_tr, y_tr_dir, eval_set=[(X_vl, y_vl_dir)],
                           callbacks=[lgb.early_stopping(10, verbose=False)])
                    pc = mc.predict(X_vl)
                    cv_scores_clf.append(np.mean(pc == y_vl_dir))
            cv_acc = np.mean(cv_scores_dir) if cv_scores_dir else 0
            cv_clf_acc = np.mean(cv_scores_clf) if cv_scores_clf else 0

            # 训练模型（回归+分类）
            # v4.6.x P1: 从训练集中抽取独立验证集用于早停（不改用测试集）
            val_split = int(len(X_train_sel) * 0.85)
            X_tr_val, X_val_set = X_train_sel[:val_split], X_train_sel[val_split:]
            y_tr_val, y_val_set = y_train_raw[:val_split], y_train_raw[val_split:]
            models = train_horizon(X_tr_val, y_tr_val, X_val_set, y_val_set,
                                   f"{horizon_days}d")

            # 评估回归器方向精度
            lgb_p = models["lgb"].predict(X_test_sel)
            xgb_p = models["xgb"].predict(X_test_sel)
            ens_p = lgb_p * 0.6 + xgb_p * 0.4
            lgb_acc = np.mean((lgb_p > 0) == (y_test_raw > 0.005))
            xgb_acc = np.mean((xgb_p > 0) == (y_test_raw > 0.005))
            ens_acc = np.mean((ens_p > 0) == (y_test_raw > 0.005))

            # v4.5.7: 评估分类器方向精度
            y_test_dir = (y_test_raw > 0.005).astype(int)
            lgb_clf_p = models["lgb_clf"].predict(X_test_sel)
            xgb_clf_p = models["xgb_clf"].predict(X_test_sel)
            ens_clf_p = ((lgb_clf_p.astype(float) + xgb_clf_p.astype(float)) > 1).astype(int)
            lgb_clf_acc = np.mean(lgb_clf_p == y_test_dir)
            xgb_clf_acc = np.mean(xgb_clf_p == y_test_dir)
            ens_clf_acc = np.mean(ens_clf_p == y_test_dir)

            # 加权融合：回归信号×分类置信度
            lgb_conf = np.abs(lgb_p[-1]) / (np.abs(lgb_p).std() + 1e-6)
            xgb_conf = np.abs(xgb_p[-1]) / (np.abs(xgb_p).std() + 1e-6)
            clf_pred = (lgb_clf_p[-1] + xgb_clf_p[-1]) / 2
            fused_direction = float(clf_pred > 0.5) * 2 - 1 if clf_pred != 0.5 else np.sign(ens_p[-1])
            fused_signal = fused_direction * max(abs(ens_p[-1]), 0.001)

            # 保存模型（回归+分类+预处理）
            model_dir = os.path.join(MODELS_DIR, code)
            os.makedirs(model_dir, exist_ok=True)
            joblib.dump(models["lgb"], os.path.join(model_dir, f"lightgbm_{horizon_days}d.pkl"))
            joblib.dump(models["xgb"], os.path.join(model_dir, f"xgboost_{horizon_days}d.pkl"))
            joblib.dump(models["lgb_clf"], os.path.join(model_dir, f"lightgbm_{horizon_days}d_clf.pkl"))
            joblib.dump(models["xgb_clf"], os.path.join(model_dir, f"xgboost_{horizon_days}d_clf.pkl"))
            joblib.dump(scaler, os.path.join(model_dir, f"scaler_{horizon_days}d.pkl"))
            joblib.dump(selector, os.path.join(model_dir, f"selector_{horizon_days}d.pkl"))

            # 最新预测（融合: 分类器方向 + 回归器幅度）
            latest_pred = fused_signal

            # 打印改进对比
            print(f"    {horizon_days}d | 回归acc={ens_acc:.1%} 分类acc={ens_clf_acc:.1%} "
                  f"CV_dir={cv_acc:.1%} CV_clf={cv_clf_acc:.1%}" if cv_clf_acc else "")
            latest_price = float(df["close"].iloc[-1])

            key = f"h{horizon_days}d"
            result[key] = {
                "predicted_return": round(float(latest_pred), 6),
                "direction_accuracy": round(float(ens_acc), 4),
                "clf_accuracy": round(float(ens_clf_acc), 4),
                "lgb_accuracy": round(float(lgb_acc), 4),
                "xgb_accuracy": round(float(xgb_acc), 4),
                "cv_accuracy": round(float(cv_acc), 4),
                "cv_clf_accuracy": round(float(cv_clf_acc), 4),
                "features_used": X_train_sel.shape[1],
            }

            # 保存主预测（5日）
            if horizon_days == 5:
                result["latest_price"] = latest_price
                result["predicted_return"] = round(float(latest_pred), 6)
                result["predicted_price"] = round(float(latest_price * (1 + latest_pred)), 2)
                result["prediction_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                result["direction_accuracy"] = round(float(ens_clf_acc), 4)
                result["cv_accuracy"] = round(float(cv_acc), 4)
                result["lgb_accuracy"] = round(float(lgb_acc), 4)
                result["xgb_accuracy"] = round(float(xgb_acc), 4)
                result["lgb_clf_accuracy"] = round(float(lgb_clf_acc), 4)
                result["xgb_clf_accuracy"] = round(float(xgb_clf_acc), 4)
                result["features_used"] = X_train_sel.shape[1]

            print(f"  🎯 {horizon_days}d: ens={ens_acc:.2%} cv={cv_acc:.2%} | 预测={latest_pred*100:+.3f}%")

        _timed_out[0] = False  # 成功，标记为未超时
        return result

    except TimeoutError as e:
        print(f"  ⏰ 超时: {e}")
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        return {"symbol": code, "error": str(e)}
    except Exception as e:
        print(f"  ❌ {e}")
        import traceback
        traceback.print_exc()
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        return {"symbol": code, "error": str(e)}


def main(codes=None):
    pool = get_stock_pool()
    if codes:
        # 仅训练指定股票
        code_set = set(codes) if isinstance(codes, list) else {codes}
        name_map = {s["symbol"]: s["name"] for s in pool}
        pool = [s for s in pool if s["symbol"] in code_set]
        if not pool:
            # 传入的codes不在池中，仍尝试训练
            pool = [{"symbol": c, "name": name_map.get(c, c)} for c in code_set]
        print(f"🎯 定向重训 — {len(pool)}只: {', '.join(s['symbol'] for s in pool)}")
    print(f"🚀 DSL v4.5.1 增强训练 — {len(pool)}只 × {len(HORIZONS)}时间框架")
    print(f"   数据: {TRAIN_YEARS}年 | 特征: 50+基本面 | Walk-Forward {N_FOLDS}折")
    print(f"   开始: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    results = {}
    success = fail = 0

    for s in pool:
        r = train_single_stock(s["symbol"], s["name"])
        if r:
            results[s["symbol"]] = r
            if "error" in r:
                fail += 1
            else:
                success += 1
        gc.collect()

    # 保存
    pred_file = os.path.join(PRED_DIR, f"prediction_enhanced_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(pred_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 汇总
    accs = [r.get("direction_accuracy", 0) for r in results.values() if "error" not in r]
    print(f"\n{'='*60}")
    print(f"📊 完成: {success}成功/{fail}失败")
    if accs:
        print(f"   5d精度: 均值{np.mean(accs):.2%} 中位数{np.median(accs):.2%}")
        print(f"   >50%: {sum(1 for a in accs if a>0.5)}/{len(accs)}")
        print(f"   >55%: {sum(1 for a in accs if a>0.55)}/{len(accs)}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", nargs="+", help="仅训练指定股票代码")
    args = parser.parse_args()
    results = main(codes=args.codes)

    # ===== 训练后自动回测 =====
    print(f"\n{'='*60}")
    print(f"📊 步骤B: 自动Walk-Forward回测...")
    try:
        from scripts.backtest_walkforward import *
        # backtest_walkforward.py 的 __main__ 会直接运行
        import subprocess
        bt = subprocess.run(
            [os.path.join(PROJECT_ROOT, ".venv/bin/python3"), "scripts/backtest_walkforward.py"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=1800
        )
        if bt.returncode == 0:
            # 提取回测摘要
            for line in bt.stdout.split("\n"):
                if any(kw in line for kw in ["总收益率", "夏普比率", "盈亏比", "胜率", "📁"]):
                    print(f"  {line.strip()}")
            print(f"  ✅ 回测完成")
        else:
            print(f"  ⚠️ 回测异常: {bt.stderr[-200:]}")
    except Exception as e:
        print(f"  ⚠️ 回测跳过: {e}")

    # ===== 训练后同步飞书股票池 =====
    print(f"\n{'='*60}")
    print(f"📊 步骤C: 同步飞书股票池...")
    try:
        from scripts.sync_bitable import sync_stock_pool_from_config
        count = sync_stock_pool_from_config()
        print(f"  ✅ 同步完成: {count} 条")
    except Exception as e:
        print(f"  ⚠️ 同步失败: {e}")

    # ===== 反馈闭环 =====
    print(f"\n📊 步骤D: 反馈闭环更新...")
    try:
        from scripts.feedback_controller import update_all
        fb = update_all()
        for k, v in fb.items():
            print(f"  {k}: {v.get('status', '?')}")
    except Exception as e:
        print(f"  ⚠️ 反馈更新跳过: {e}")

    print(f"\n✅ DSL v4.5.1 完整闭环管线 (训练→回测→同步→反馈)")
