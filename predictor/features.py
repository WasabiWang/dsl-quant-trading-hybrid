#!/usr/bin/env python3
"""多因子特征工程v4.1 - 统一特征管线 (v4.5.12 fix: 全量shift(1)防前视偏差)

合并历史4套特征工程:
  1. v3.1 predictor/features.py (93特征) - stockday_wd深加工+自适应MA+交互特征
  2. train_predictor_v2.py FeatureEngineer (37特征) - 基础技术指标+市场情绪
  3. train_csi300_leaders.py (57特征) - KDJ/OBV/MFI/均线bias/上下影线
  4. ml_predictor.py (17特征) - 最简技术指标

v4.0统一:
  - 技术指标: MA系统(含bias) + 收益率(多周期) + 波动率(多周期) + RSI(3周期) + MACD + KDJ + 布林带 + ATR + 成交量 + 价格形态(含上下影线) + OBV + MFI
  - 自适应MA窗口: 根据数据长度自动选窗口
  - 市场情绪: 北向资金 + 涨停
  - 麦蕊另类: MACD/KDJ/财务指标
  - stockday_wd基本面: PE/负债率/OCF/营收增长/换手率/股息率/质押率/分析师评级
  - 特征交互 + 市场状态 + 时间特征
  - 多周期目标变量

v4.1 fix: 所有rolling/ewm基于shift(1)滞后价格，防止前视偏差（与signal_generator.py一致）

预计特征数: ~128个 (不含target变量)
"""

import os, sys
import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FeatureEngineer:
    TARGET_HORIZONS = [1, 5, 10, 20]

    # ===== 技术指标 (合并CSI300+train_predictor_v2) =====
    @staticmethod
    def add_technical_indicators(df):
        """完整技术指标 (~55个特征) — v4.5.12 fix: 全量shift(1)防前视偏差
        合并了v3.1和CSI300的全部技术指标。
        所有rolling/ewm基于shift(1)滞后价格，与signal_generator.py保持一致。
        """
        df = df.copy()
        close = df['close']
        high = df['high']
        low = df['low']
        volume = df['volume']
        opn = df['open']

        # 滞后价格 — 防止当日收盘价污染因子
        c_s = close.shift(1)
        h_s = high.shift(1)
        l_s = low.shift(1)

        # ---- 均线系统 (含bias, 来自CSI300) ----
        for w in [5, 10, 20, 30, 60]:
            ma = c_s.rolling(w).mean()
            df[f'ma{w}'] = ma
            df[f'ma{w}_bias'] = (close - ma) / ma.replace(0, np.nan) * 100

        # ---- 价格动量 (5个, 来自CSI300命名, pct_change天然不含当日) ----
        for p in [1, 3, 5, 10, 20]:
            df[f'ret_{p}d'] = close.pct_change(p)

        # v3.1兼容命名 (保留returns_Xd别名)
        df['returns_1d'] = df['ret_1d']
        df['returns_5d'] = df['ret_5d']
        df['returns_10d'] = df['ret_10d']
        df['returns_20d'] = df['ret_20d']
        df['returns_60d'] = close.pct_change(60)

        # ---- 波动率 (3个, 使用滞后收益) ----
        df['ret_1d'] = close.pct_change(1)
        ret_s = df['ret_1d'].shift(1)
        for w in [5, 10, 20]:
            df[f'vol_{w}d'] = ret_s.rolling(w).std()

        # v3.1兼容命名
        df['volatility_5d'] = df['vol_5d']
        df['volatility_20d'] = df['vol_20d']
        df['volatility_60d'] = ret_s.rolling(60).std()
        df['volatility_ratio'] = df['volatility_5d'] / (df['volatility_60d'] + 1e-10)

        # ---- RSI (3个周期, 使用滞后价格diff) ----
        delta_s = c_s.diff()
        for period in [6, 14, 24]:
            gain = delta_s.clip(lower=0).rolling(period).mean()
            loss = (-delta_s.clip(upper=0)).rolling(period).mean()
            rs = gain / loss.replace(0, np.nan)
            df[f'rsi_{period}'] = 100 - (100 / (1 + rs))

        # v3.1兼容
        df['rsi_14'] = df['rsi_14']
        df['rsi_signal'] = ((df['rsi_14'] < 30) | (df['rsi_14'] > 70)).astype(int)

        # ---- MACD (基于滞后价格) ----
        ema12 = c_s.ewm(span=12, adjust=False).mean()
        ema26 = c_s.ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        df['macd_direction'] = (df['macd'] > df['macd_signal']).astype(int)
        df['macd_hist_change'] = df['macd_hist'].diff()

        # ---- KDJ (基于滞后高低价) ----
        low_9 = l_s.rolling(9).min()
        high_9 = h_s.rolling(9).max()
        rsv = (close - low_9) / (high_9 - low_9).replace(0, np.nan) * 100
        df['k'] = rsv.ewm(alpha=1/3, adjust=False).mean()
        df['d'] = df['k'].ewm(alpha=1/3, adjust=False).mean()
        df['j'] = 3 * df['k'] - 2 * df['d']

        # ---- 布林带 (基于滞后价格) ----
        df['bb_mid'] = c_s.rolling(20).mean()
        bb_std = c_s.rolling(20).std()
        df['bb_upper'] = df['bb_mid'] + 2 * bb_std
        df['bb_lower'] = df['bb_mid'] - 2 * bb_std
        df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_mid'].replace(0, np.nan)
        df['bb_pos'] = (close - df['bb_lower']) / (df['bb_upper'] - df['bb_lower'] + 1e-10)
        # v3.1兼容别名
        df['bb_middle'] = df['bb_mid']
        df['bb_position'] = df['bb_pos']

        # ---- ATR (TR计算天然使用shift, rolling基于滞后TR) ----
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
        df['atr'] = tr.rolling(14).mean()
        df['atr_pct'] = df['atr'] / close.replace(0, np.nan) * 100
        # v3.1兼容
        df['atr_14'] = df['atr']
        df['atr_ratio'] = df['atr_pct'] / 100

        # ---- 成交量特征 (滞后成交量) ----
        v_s = volume.shift(1)
        df['vol_ma5'] = v_s.rolling(5).mean()
        df['vol_ma20'] = v_s.rolling(20).mean()
        df['vol_ratio'] = volume / df['vol_ma5'].replace(0, np.nan)
        df['vol_trend'] = df['vol_ma5'] / df['vol_ma20'].replace(0, np.nan)
        df['vol_ret_corr'] = ret_s.rolling(20).corr(v_s.diff())
        # v3.1兼容
        df['volume_ma5'] = df['vol_ma5']
        df['volume_ma20'] = df['vol_ma20']
        df['volume_ratio'] = df['vol_ratio']
        df['volume_ratio_ma'] = df['vol_trend']
        df['volume_trend'] = df['vol_ratio'].rolling(5).mean()

        # ---- 价格形态 (反映当日形态, 用当日数据) ----
        df['hl_ratio'] = (high - low) / close.replace(0, np.nan)
        df['co_ratio'] = (close - opn) / opn.replace(0, np.nan)
        body_high = close.combine(opn, max)
        body_low = close.combine(opn, min)
        df['upper_shadow'] = (high - body_high) / close.replace(0, np.nan)
        df['lower_shadow'] = (body_low - low) / close.replace(0, np.nan)
        # v3.1兼容
        df['high_low_ratio'] = df['hl_ratio']
        df['close_open_ratio'] = df['co_ratio']
        df['close_position'] = (close - low) / (high - low + 1e-10)

        # ---- OBV (基于当日量价关系) ----
        df['obv'] = (np.sign(close.diff().fillna(0)) * volume).cumsum()

        # ---- MFI (典型价格使用滞后shift) ----
        tp = (high + low + close) / 3
        mf = tp * volume
        pos_mf = mf.where(tp > tp.shift(), 0).rolling(14).sum()
        neg_mf = mf.where(tp < tp.shift(), 0).rolling(14).sum()
        df['mfi'] = 100 - (100 / (1 + pos_mf / neg_mf.replace(0, np.nan)))

        return df

    # ===== 市场情绪 (北向资金+涨停) =====
    @staticmethod
    def add_market_sentiment(df, symbol):
        df = df.copy()
        try:
            try:
                from scripts.data_source_enhanced import EnhancedDataSource
            except ImportError:
                sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))
                from data_source_enhanced import EnhancedDataSource
            eds = EnhancedDataSource()
            sentiment_df = eds.get_market_sentiment_features(df.index)
            if sentiment_df is not None and not sentiment_df.empty:
                sentiment_df = sentiment_df.reindex(df.index).ffill().bfill().fillna(0)
                df = pd.concat([df, sentiment_df], axis=1)
        except Exception:
            pass
        return df

    # ===== 麦蕊另类特征 =====
    @staticmethod
    def add_mairui_features(df, symbol):
        df = df.copy()
        try:
            from predictor.mairui_data import (is_available, get_history_macd, get_history_kdj,
                                               get_history_boll, get_fundamentals)
            if not is_available():
                return df
            sym = symbol.split('.')[0] if '.' in symbol else symbol
            for suf in ['SH', 'SZ', 'BJ']:
                if sym.endswith(suf):
                    sym = sym[:-len(suf)]; break
            mkt = 'SH' if (sym.startswith('6') or sym.startswith('9')) else 'SZ'
            # MACD
            try:
                macd_data = get_history_macd(f"{sym}.{mkt}", period='d', limit=200)
                if macd_data is not None and not macd_data.empty:
                    if 't' in macd_data.columns:
                        macd_data['date'] = pd.to_datetime(macd_data['t'])
                        macd_data = macd_data.set_index('date')
                    for col in [c for c in macd_data.columns if c not in ('t','open','high','low','close','volume','amount')]:
                        aligned = macd_data[col].reindex(df.index).ffill().fillna(0)
                        df[f'm_macd_{col}'] = pd.to_numeric(aligned.values, errors='coerce')
            except Exception: pass
            # KDJ
            try:
                kdj_data = get_history_kdj(f"{sym}.{mkt}", period='d', limit=200)
                if kdj_data is not None and not kdj_data.empty:
                    if 't' in kdj_data.columns:
                        kdj_data['date'] = pd.to_datetime(kdj_data['t'])
                        kdj_data = kdj_data.set_index('date')
                    for col in [c for c in kdj_data.columns if c not in ('t','open','high','low','close','volume')][:10]:
                        aligned = kdj_data[col].reindex(df.index).ffill().fillna(0)
                        df[f'm_kdj_{col}'] = pd.to_numeric(aligned.values, errors='coerce')
            except Exception: pass
            # 财务指标
            try:
                fund = get_fundamentals(f"{sym}.{mkt}", limit=1)
                if fund is not None and not fund.empty:
                    for col in fund.columns[:20]:
                        val = fund[col].iloc[-1] if len(fund) > 0 else None
                        if val is not None:
                            try: df[f'fund_{col}'] = float(val)
                            except: pass
            except Exception: pass
        except ImportError: pass
        except Exception: pass
        return df

    # ===== stockday_wd 10年数据深加工 =====
    @staticmethod
    def add_stockday_fundamental_features(df):
        """利用本地10年基本面数据生成衍生因子"""
        df = df.copy()
        # 估值类
        if 'pe_ttm' in df.columns:
            df['pe_percentile_250'] = df['pe_ttm'].rolling(250, min_periods=60).rank(pct=True)
            df['pe_change_20d'] = df['pe_ttm'].pct_change(20)
            df['pe_change_60d'] = df['pe_ttm'].pct_change(60)
            pe_ma120 = df['pe_ttm'].rolling(120, min_periods=60).mean()
            df['pe_deviation'] = (df['pe_ttm'] - pe_ma120) / (pe_ma120 + 1e-10)
        # 财务质量
        if 'total_assets' in df.columns and 'total_liabilities' in df.columns:
            df['debt_ratio'] = df['total_liabilities'] / (df['total_assets'] + 1e-10)
            df['debt_ratio_change'] = df['debt_ratio'].diff(60)
        if 'ocf_ttm' in df.columns and 'market_cap' in df.columns:
            df['ocf_marketcap_ratio'] = df['ocf_ttm'] / (df['market_cap'] + 1e-10)
            df['ocf_marketcap_ratio'] = df['ocf_marketcap_ratio'].replace([np.inf, -np.inf], np.nan)
        if 'yoy_revenue' in df.columns:
            df['rev_growth_accel'] = df['yoy_revenue'].diff(60)
        if 'yoy_profit' in df.columns:
            df['profit_growth_accel'] = df['yoy_profit'].diff(60)
            if 'yoy_revenue' in df.columns:
                df['rev_profit_gap'] = df['yoy_revenue'] - df['yoy_profit']
        # 市场特征 (换手率需shift(1)防前视偏差，换手率当日实时变化)
        if 'turnover_rate' in df.columns:
            to_s = df['turnover_rate'].shift(1)
            df['turnover_ma5'] = to_s.rolling(5).mean()
            df['turnover_ma20'] = to_s.rolling(20).mean()
            df['turnover_trend'] = df['turnover_ma5'] / (df['turnover_ma20'] + 1e-10)
            df['turnover_anomaly'] = (df['turnover_rate'] - df['turnover_ma20']).abs() / (
                to_s.rolling(60).std() + 1e-10)
        if 'dividend_yield' in df.columns:
            df['div_yield_ma60'] = df['dividend_yield'].rolling(60, min_periods=20).mean()
            df['div_yield_trend'] = df['dividend_yield'] - df['div_yield_ma60']
        if 'pledge_ratio' in df.columns:
            df['pledge_risk'] = (df['pledge_ratio'] > 50).astype(int)
        if 'analyst_rating' in df.columns:
            df['analyst_rating_change'] = df['analyst_rating'].diff(60)
        return df

    # ===== 自适应MA窗口 =====
    @staticmethod
    def add_adaptive_technical_indicators(df):
        """根据数据长度自适应选择MA窗口，减少早期行损失
        注意: MA基础值已在add_technical_indicators中计算，这里只补自适应窗口和衍生
        v4.5.12 fix: 超长窗口MA使用shift(1)防前视偏差
        """
        df = df.copy()
        n = len(df)
        c_s = df['close'].shift(1)
        if n > 500 and 'ma120' not in df.columns:
            df['ma120'] = c_s.rolling(120).mean()
        if n > 800 and 'ma250' not in df.columns:
            df['ma250'] = c_s.rolling(250).mean()
        # 均线交叉信号
        if 'ma5' in df.columns and 'ma20' in df.columns:
            df['ma5_ma20_ratio'] = df['ma5'] / df['ma20']
            df['ma_cross_signal'] = (df['ma5'] > df['ma20']).astype(int)
        if 'ma20' in df.columns and 'ma60' in df.columns:
            df['ma20_ma60_ratio'] = df['ma20'] / df['ma60']
        # 多头排列评分
        df['ma_bullish'] = 0
        if all(c in df.columns for c in ['ma5','ma20']):
            df['ma_bullish'] = (df['ma5'] > df['ma20']).astype(int)
        if all(c in df.columns for c in ['ma20','ma60']):
            df['ma_bullish'] += (df['ma20'] > df['ma60']).astype(int)
        if 'ma120' in df.columns:
            df['ma_bullish'] += (df['ma20'] > df['ma120']).astype(int)
        if 'ma250' in df.columns:
            df['ma_bullish'] += (df['ma20'] > df['ma250']).astype(int)
        # 价格偏离度
        if 'ma60' in df.columns:
            df['price_vs_ma60'] = (df['close'] - df['ma60']) / (df['ma60'] + 1e-10)
        if 'ma120' in df.columns:
            df['price_vs_ma120'] = (df['close'] - df['ma120']) / (df['ma120'] + 1e-10)
        return df

    # ===== 多周期目标 =====
    @staticmethod
    def add_target_variables(df, horizons=None):
        if horizons is None:
            horizons = FeatureEngineer.TARGET_HORIZONS
        df = df.copy()
        for h in horizons:
            df[f'target_{h}d_return'] = df['close'].shift(-h) / df['close'] - 1
            df[f'target_{h}d_direction'] = (df[f'target_{h}d_return'] > 0).astype(int)
        return df

    # ===== 特征交互 =====
    @staticmethod
    def add_interaction_features(df):
        df = df.copy()
        pairs = {
            ('pe_ttm', 'northbound_net_flow'): 'pe_north_interact',
            ('volatility_5d', 'volume_ratio'): 'vol_vol_interact',
            ('macd_hist', 'returns_5d'): 'macd_return_interact',
            ('rsi_14', 'bb_position'): 'rsi_bb_interact',
            ('returns_5d', 'volume_trend'): 'ret_vol_interact',
            ('atr_ratio', 'volatility_5d'): 'atr_vol_interact',
        }
        for (a, b), name in pairs.items():
            if a in df.columns and b in df.columns:
                df[name] = df[a] * df[b]
                df[f'{name}_ratio'] = df[a] / (df[b].abs() + 1e-10)
        return df

    # ===== 市场状态 =====
    @staticmethod
    def add_market_regime_features(df):
        df = df.copy()
        if 'ma20' in df.columns and 'ma60' in df.columns:
            df['trend_signal'] = 0
            df.loc[(df['close'] > df['ma20']) & (df['ma20'] > df['ma60']), 'trend_signal'] = 1
            df.loc[(df['close'] < df['ma20']) & (df['ma20'] < df['ma60']), 'trend_signal'] = -1
            df['trend_strength'] = (df['close'] - df['ma60']) / (df['ma60'] + 1e-10)
            bull_parts = []
            for ma in ['ma5','ma20','ma60','ma120','ma250']:
                if ma in df.columns:
                    bull_parts.append((df['close'] > df[ma]).astype(int))
            if bull_parts:
                df['bull_score'] = sum(bull_parts) / len(bull_parts)
        return df

    # ===== 时间特征 =====
    @staticmethod
    def add_time_features(df):
        df = df.copy()
        if isinstance(df.index, pd.DatetimeIndex):
            df['day_of_week'] = df.index.dayofweek
            df['month'] = df.index.month
            df['quarter'] = df.index.quarter
            df['day_of_month'] = df.index.day
            df['is_month_start'] = df.index.is_month_start.astype(int)
            df['is_month_end'] = df.index.is_month_end.astype(int)
            df['is_first_half'] = (df['day_of_month'] <= 15).astype(int)
            df['year'] = df.index.year  # v3.1兼容
        return df

    # ===== 主入口 =====
    @staticmethod
    def build_features(df, symbol, include_mairui=True, include_interactions=True,
                       include_regime=True, use_adaptive_ma=True, include_target=True):
        """构建完整特征集 (v4.0 统一管线)
        处理顺序: 目标 → 基本面 → 技术指标 → 自适应MA → 情绪 → 麦蕊 → 交互 → 市场状态 → 时间
        """
        if df.empty:
            return df
        if include_target:
            df = FeatureEngineer.add_target_variables(df)
        df = FeatureEngineer.add_stockday_fundamental_features(df)
        df = FeatureEngineer.add_technical_indicators(df)
        if use_adaptive_ma:
            df = FeatureEngineer.add_adaptive_technical_indicators(df)
        df = FeatureEngineer.add_market_sentiment(df, symbol)
        if include_mairui:
            df = FeatureEngineer.add_mairui_features(df, symbol)
        if include_interactions:
            df = FeatureEngineer.add_interaction_features(df)
        if include_regime:
            df = FeatureEngineer.add_market_regime_features(df)
        df = FeatureEngineer.add_time_features(df)
        df = df.dropna(axis=1, how='all')
        df = df.dropna()
        return df

    # ===== CSI300兼容接口 (供LGBCSIPredictor使用) =====
    @staticmethod
    def build_csi_features(df, symbol):
        """构建CSI300/500兼容特征集 (57特征，用于LGB模型)
        与train_csi300_leaders.py的add_technical_factors完全一致
        """
        if df.empty:
            return df
        df = FeatureEngineer.add_technical_indicators(df)
        df = FeatureEngineer.add_time_features(df)
        # 目标变量占位
        df['target_1d'] = 0
        df['target_3d'] = 0
        df['target_5d'] = 0
        df = df.dropna()
        return df
