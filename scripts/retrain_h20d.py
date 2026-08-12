#!/usr/bin/env python3
"""
v4.6.2: h20d 模型重训脚本 — 对齐 92 维特征（含截面rank特征）

用途:  将 h20d 模型从 83 维重训到 92 维，修复维度不匹配
步骤:
  1. 批量获取K线(含原始close用于target) + 基本面
  2. 构建特征 (83维) + 添加rank列位 (9维) = 92维
  3. 计算截面rank值并填充
  4. 训练 StandardScaler + SelectFromModel + LightGBM
  5. 保存模型到 models/{code}/
"""

import os, sys, json, joblib, warnings, yaml
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import SelectFromModel
from lightgbm import LGBMRegressor

from batch_predict import (
    build_h20d_features, fetch_kline_h20d, fetch_fundamentals_mairui,
    compute_cross_sectional_ranks, CROSS_SECTIONAL_RANK_FEATURES,
)

# === v4.6.2: 新增的rank特征列名 ===
RANK_COLS = [
    "rank_mom_5d", "rank_mom_10d", "rank_mom_20d",
    "rank_vol_ratio_20", "rank_vol_change", "rank_atr14",
    "rank_rsi14", "rank_amplitude", "rank_turnover",
]
TARGET_HORIZON = 20  # 日

print("=" * 60)
print("🔧 h20d 模型重训 (83维 → 92维, 含截面rank)")
print("=" * 60)

# === 加载股票池 ===
pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
with open(pool_path) as f:
    pool = yaml.safe_load(f)
stocks = pool["master_pool"]
print(f"📋 股票池: {len(stocks)} 只")

# === 1. 获取K线原始数据 (保留close用于target) ===
all_close = {}
all_raw_features = {}
fund_data = {}

print(f"📡 获取K线+基本面...")
for s in stocks:
    code = s["symbol"]
    try:
        df = fetch_kline_h20d(code)
        if df is None or len(df) < 300:
            print(f"  ⚠️ {code}: K线不足({len(df) if df is not None else 0}), 跳过")
            continue
        all_close[code] = df["close"].astype(float)  # Keep as Series with index
        features = build_h20d_features(df)
        features = features.dropna()
        if len(features) < 100:
            print(f"  ⚠️ {code}: 有效特征行不足({len(features)}), 跳过")
            continue
        all_raw_features[code] = features
        fund_data[code] = fetch_fundamentals_mairui(code)
    except Exception as e:
        print(f"  ⚠️ {code}: 数据获取失败 - {e}")

valid_codes = list(all_raw_features.keys())
print(f"  ✅ 有效标的: {len(valid_codes)}/{len(stocks)}")
print(f"  📐 基础特征维度: {all_raw_features[valid_codes[0]].shape[1] if valid_codes else 'N/A'}")

# === 2. 添加rank列位到特征中 (v4.6.2: build_h20d_features 已包含，此处做安全补齐) ===
print(f"📊 检查截面rank列位 (9维)...")
for code in valid_codes:
    df = all_raw_features[code]
    for col in RANK_COLS:
        if col not in df.columns:
            df[col] = 0.0

print(f"  📐 最终维度: {all_raw_features[valid_codes[0]].shape[1] if valid_codes else 'N/A'}")

# === 3. 计算截面rank并填充 ===
print(f"📊 计算截面rank值...")
cross_ranks = compute_cross_sectional_ranks(all_raw_features, valid_codes)

# 填充rank + 基本面到每只票
for code in valid_codes:
    df = all_raw_features[code]
    
    # 基本面
    fund = fund_data.get(code, {})
    for k in ["roe", "eps", "bps", "cfps", "revenue_growth", "gross_margin", "capex_ratio"]:
        col = f"fund_{k}"
        if col in df.columns:
            df[col] = fund.get(k, 0.0)
    
    # 截面rank
    ranks = cross_ranks.get(code, {})
    for col in RANK_COLS:
        if col in df.columns:
            df[col] = ranks.get(col, 0.0)

# === 4. 训练模型 ===
print(f"\n🤖 训练 LightGBM 模型 ({TARGET_HORIZON}日 horizon)...")
models_dir = os.path.join(PROJECT_ROOT, "models")
success = fail = 0

for code in valid_codes:
    try:
        df = all_raw_features[code]
        close_series = all_close[code]
        
        # 对齐 close 到特征 DataFrame 的索引 (build_h20d_features 会去掉NaN行)
        aligned_close = close_series[close_series.index.isin(df.index)]
        if len(aligned_close) != len(df):
            # Reindex: use df's index to select close values
            aligned_close = pd.Series(close_series.values, index=close_series.index)
            aligned_close = aligned_close.reindex(df.index).dropna()
            df = df.loc[aligned_close.index]
        
        close_arr = aligned_close.values
        
        # 目标: 20日未来收益率
        future_return = np.zeros(len(close_arr))
        for i in range(len(close_arr) - TARGET_HORIZON):
            future_return[i] = close_arr[i + TARGET_HORIZON] / close_arr[i] - 1
        
        # 对齐: 只保留有target的行
        valid_idx = (np.arange(len(future_return)) < len(future_return) - TARGET_HORIZON) & (~np.isnan(future_return))
        X = df.iloc[valid_idx].values
        y = future_return[valid_idx]
        
        if len(y) < 100:
            fail += 1
            print(f"  ⚠️ {code}: 训练样本不足({len(y)}), 跳过")
            continue
        
        # v4.6.2: 目标预处理 — log变换压缩极端值 + 3σ截断
        # log变换: 70% → log1p(0.70)=0.53, 压缩正偏斜
        y_log = np.sign(y) * np.log1p(np.abs(y))
        log_std = np.std(y_log)
        y_clipped = np.clip(y_log, -3*log_std, 3*log_std)  # 3σ截断去极值
        
        # 诊断：记录极端目标比例
        extreme_pct = np.mean(np.abs(y) > 0.30)
        if extreme_pct > 0.10:
            print(f"    ⚠️ {code}: {extreme_pct:.0%} 目标 >30% | max={np.max(y):.1%}")
        
        # 时间序列切分 (80/20)
        split = int(len(y_clipped) * 0.8)
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y_clipped[:split], y_clipped[split:]
        
        # StandardScaler
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        
        # 特征选择 (保留 top 40%, 减少过拟合)
        selector = SelectFromModel(
            LGBMRegressor(n_estimators=30, max_depth=2, random_state=42, verbose=-1),
            threshold="0.4*mean"
        )
        X_train_selected = selector.fit_transform(X_train_scaled, y_train)
        
        # LightGBM — v4.6.2: 分位数回归(中位数) + log目标, 抗极端偏斜
        n_selected = X_train_selected.shape[1]
        model = LGBMRegressor(
            objective='quantile',
            alpha=0.5,  # 预测中位数, 对偏斜分布鲁棒
            n_estimators=min(50, n_selected * 2),
            max_depth=min(3, int(np.log2(len(y_train)))),
            learning_rate=0.03,
            num_leaves=min(15, max(n_selected, 5)),
            subsample=0.7,
            colsample_bytree=0.7,
            reg_alpha=0.1,
            reg_lambda=0.1,
            min_child_samples=10,
            random_state=42,
            verbose=-1,
        )
        model.fit(X_train_selected, y_train)
        
        # 验证 — pred在log空间, expm1还原后判断方向
        X_val_scaled = scaler.transform(X_val)
        X_val_selected = selector.transform(X_val_scaled)
        val_pred_log = model.predict(X_val_selected)
        val_pred = np.sign(val_pred_log) * np.expm1(np.abs(val_pred_log))
        val_acc = np.mean((val_pred > 0) == (y_val > 0))
        
        # 备份旧模型
        stock_dir = os.path.join(models_dir, code)
        os.makedirs(stock_dir, exist_ok=True)
        for fname in ["scaler_20d.pkl", "selector_20d.pkl", "lightgbm_20d.pkl"]:
            src = os.path.join(stock_dir, fname)
            if os.path.exists(src):
                bak = src + ".bak.20260616"
                if not os.path.exists(bak):
                    os.rename(src, bak)
        
        # 保存
        joblib.dump(scaler, os.path.join(stock_dir, "scaler_20d.pkl"))
        joblib.dump(selector, os.path.join(stock_dir, "selector_20d.pkl"))
        joblib.dump(model, os.path.join(stock_dir, "lightgbm_20d.pkl"))
        
        success += 1
        if success <= 5 or success % 10 == 0:
            print(f"  ✅ {code}: {X.shape[1]}维, sel={X_train_selected.shape[1]}维, val_acc={val_acc:.3f}")
    except Exception as e:
        fail += 1
        import traceback
        print(f"  ❌ {code}: {e}")
        traceback.print_exc()

# === 5. 把rank列位加入 build_h20d_features 供推理使用 ===
print(f"\n📝 更新 build_h20d_features 加入rank列位...")

# Read batch_predict.py
bp_path = os.path.join(SCRIPTS_DIR, "batch_predict.py")
with open(bp_path) as f:
    bp_code = f.read()

# Check if rank cols already added
if "v4.6.2: 截面rank特征列位" not in bp_code:
    # Insert rank column slots before the "feats = feats.replace" line
    rank_block = '\n    # — v4.6.2: 截面rank特征列位 (9维空位，由 compute_cross_sectional_ranks 填充) —\n    # 重训后模型期望92维，此处预留列名以对齐\n    for rank_col in ["rank_mom_5d", "rank_mom_10d", "rank_mom_20d",\n                     "rank_vol_ratio_20", "rank_vol_change", "rank_atr14",\n                     "rank_rsi14", "rank_amplitude", "rank_turnover"]:\n        feats[rank_col] = 0.0  # 占位，推理时由 cross_ranks 填充\n'
    bp_code = bp_code.replace(
        'feats["price_volume_harmony"] = (feats["mom_20d"] > 0) & feats["volume_trend"]\n\n    feats = feats.replace([np.inf, -np.inf], np.nan)',
        'feats["price_volume_harmony"] = (feats["mom_20d"] > 0) & feats["volume_trend"]\n' + rank_block + '\n    feats = feats.replace([np.inf, -np.inf], np.nan)'
    )
    with open(bp_path, 'w') as f:
        f.write(bp_code)
    print("  ✅ rank列位已加入 build_h20d_features")
else:
    print("  ⏭️  rank列位已存在，跳过")

print(f"\n{'='*60}")
print(f"🎯 重训完成: {success}成功 / {fail}失败 / {len(valid_codes)}目标")
if success > 0:
    print(f"📐 特征维度: 83 → {X.shape[1]}")
    print(f"💡 旧模型已备份为 *_20d.pkl.bak.20260616")
    print(f"📁 模型目录: {models_dir}/")
    print(f"🗑️  删除旧备份: find models/ -name '*.bak.20260616' -delete")
else:
    print(f"⚠️  所有训练失败，请检查错误日志")
print(f"{'='*60}")
