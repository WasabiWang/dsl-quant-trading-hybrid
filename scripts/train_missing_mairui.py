#!/usr/bin/env python3
"""DSL v4.5.1 补充训练 — 5只标的 (akshare OHLCV)"""
import sys, os, time, warnings
warnings.filterwarnings('ignore')
import pandas as pd, numpy as np
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
MODEL_DIR = os.path.join(PROJECT_ROOT, 'models', 'pool')
os.makedirs(MODEL_DIR, exist_ok=True)

import akshare as ak
from sklearn.metrics import r2_score
import lightgbm as lgb
import joblib

MISSING = [
    ("688525", "佰维存储", "core"),
    ("688347", "华虹公司", "core"),
    ("688141", "杰华特", "growth"),
    ("600726", "华电能源", "growth"),
    ("301510", "固高科技", "growth"),
]

def build_features(df):
    df = df.copy()
    df = df.sort_values('date')
    df['ret_1d'] = df['close'].pct_change()
    df['ret_5d'] = df['close'].pct_change(5)
    df['ret_20d'] = df['close'].pct_change(20)
    for p in [5, 10, 20, 60]:
        df[f'ma_{p}'] = df['close'].rolling(p).mean()
        df[f'ma_ratio_{p}'] = df['close'] / df[f'ma_{p}'].replace(0,np.nan)
    df['vol_20d'] = df['ret_1d'].rolling(20).std()
    df['vol_ma_5'] = df['volume'].rolling(5).mean()
    df['vol_ratio_5'] = df['volume'] / df['vol_ma_5'].replace(0,np.nan)
    df['amplitude'] = (df['high']-df['low'])/df['close']
    df['hl_ratio'] = df['high']/df['low']
    e12 = df['close'].ewm(span=12).mean()
    e26 = df['close'].ewm(span=26).mean()
    df['macd'] = e12 - e26
    df['macd_ratio'] = df['macd'] / df['close']
    delta = df['close'].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    df['rsi'] = 100 - 100/(1 + gain/loss.replace(0,np.nan))
    bb_mid = df['close'].rolling(20).mean()
    bb_std = df['close'].rolling(20).std()
    df['bb_position'] = (df['close']-bb_mid)/(2*bb_std+1e-9)
    df['vol_ratio_20'] = df['volume'] / df['volume'].rolling(20).mean().replace(0,np.nan)
    df['target'] = df['close'].shift(-1)/df['close'] - 1
    return df.dropna(subset=['close','volume','target'])

print(f"{'='*50}")
print(f"akshare补充训练 — {len(MISSING)}只")
print(f"{'='*50}")

for code, name, tier in MISSING:
    print(f"\n📡 {code} {name} [{tier}]")
    try:
        sym = f"sh{code}" if code.startswith('6') else f"sz{code}"
        df = ak.stock_zh_a_daily(symbol=sym, start_date="20210101", end_date="20260427", adjust="qfq")
        df = df.rename(columns={'日期':'date','开盘':'open','最高':'high','最低':'low','收盘':'close','成交量':'volume','成交额':'amount'})
        df['date'] = pd.to_datetime(df['date'])
        print(f"  ✅ raw: {len(df)}行 ({df['date'].min().date()}~{df['date'].max().date()})")
    except Exception as e:
        print(f"  ❌ {e}")
        continue

    for col in ['open','high','low','close','volume']:
        if col not in df.columns: df[col] = df.get('close',50)

    df = build_features(df)
    if len(df) < 100:
        print(f"  ❌ 特征后仅{len(df)}行")
        continue

    skip = ['date','target','open','high','low','close','volume','amount']
    feats = [c for c in df.columns if c not in skip and df[c].dtype in ('float64','float32','int64')]
    X = df[feats].fillna(df[feats].median()).values.astype(np.float32)
    y = df['target'].values.astype(np.float32)

    split = int(len(X)*0.8)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    t0 = time.time()
    model = lgb.LGBMRegressor(n_estimators=200, max_depth=10, learning_rate=0.03,
                               subsample=0.8, colsample_bytree=0.7, reg_alpha=0.1, reg_lambda=1.0,
                               verbose=-1, n_jobs=-1, random_state=42)
    model.fit(X_tr, y_tr)
    y_pred = model.predict(X_te)

    r2 = float(r2_score(y_te, y_pred))
    ad = np.sign(np.diff(y_te, prepend=y_te[0]))
    pd_ = np.sign(np.diff(y_pred, prepend=y_pred[0]))
    acc = float((ad == pd_).sum() / len(y_te))
    use_reg = r2 > 0

    path = os.path.join(MODEL_DIR, f"{code}_{name}_lgb_v2.pkl")
    joblib.dump({'model':model,'feature_cols':feats,'use_regression':use_reg,
                 'r2':r2,'dir_accuracy':acc,'data_source':'akshare',
                 'trained_at':datetime.now().isoformat()}, path)

    rf = '🟢' if r2>0.05 else ('🟡' if r2>-0.1 else '🔴')
    af = '🟢' if acc>0.55 else ('🟡' if acc>0.50 else '🔴')
    print(f"  → {rf} R²={r2:.4f} {af} acc={acc:.2%} | "
          f"样本={len(X_tr)}/{len(X_te)} | {'📈回归' if use_reg else '🧭方向'} | {time.time()-t0:.1f}s")

print(f"\n🎉 完成 — 模型: {MODEL_DIR}")
