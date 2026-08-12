#!/usr/bin/env python3
"""
core/pool_predictor.py - 模型池预测器 v4.5.12
从 models/pool/ 加载训练好的模型,统一预测接口

用途:
1. predictor_cron.py --task prediction  调用
2. morning_decision.py                 调用
3. paper_trade_sim.py                  调用
"""
import os, sys, json, glob, warnings
warnings.filterwarnings('ignore')
import pandas as pd, numpy as np
import joblib
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_POOL_DIR = os.path.join(PROJECT_ROOT, 'models', 'pool')

# PSI baseline 存储路径
PSI_BASELINE_PATH = os.path.join(PROJECT_ROOT, 'models', 'psi_baselines.json')
PSI_REPORT_PATH = os.path.join(PROJECT_ROOT, 'data', 'psi_reports.jsonl')

# v4.6.8: 扩展低精度黑名单 (基于benchmark分析, H20D mean_accuracy<50%且不在新池中)
# 这些模型的信号被强制降权为 hold，在重训练合格前不会产生交易信号
# 2026-07-19: benchmark优化, 13只低精度股永久禁止交易
LOW_ACCURACY_BLACKLIST = {
    # v4.6.8 新增: mean_accuracy < 50%, 已从主池移除
    '688608',  # 恒玄科技   mean=49.26% (已删除)
    '300750',  # 宁德时代   mean=49.22% (已删除)
    '300418',  # 昆仑万维   mean=48.85% (已删除)
    '300124',  # 汇川技术   mean=48.85% (已删除)
    '300857',  # 协创数据   mean=48.66% (已删除)
    '300390',  # 天华新能   mean=48.02% (已删除)
    '601899',  # 紫金矿业   mean=47.90% (已删除)
    '300458',  # 全志科技   mean=47.63% (已删除)
    '002709',  # 天赐材料   mean=46.21% (已删除)
    '300274',  # 阳光电源   mean=45.72% (已删除)
    '603599',  # 广信股份   mean=44.62% (已删除)
    '002460',  # 赣锋锂业   mean=43.65% (已删除)
    '002475',  # 立讯精密   mean=43.53% (已删除)
    # v4.5.12 原有 (已自动被新黑名单覆盖或移至bench层)
    '002466',  # 天齐锂业   last=0.4043 → bench层
    '603799',  # 华友钴业   mean=0.4400 (不在原池)
}

class PoolPredictor:
    """模型池预测器 - 加载所有已训练模型并统一预测"""

    def __init__(self, model_dir=None):
        self.model_dir = model_dir or MODEL_POOL_DIR
        self.models = {}    # {code: {model, feature_cols, use_regression, r2, dir_accuracy, ...}}
        self._load_all()

    def _load_all(self):
        """扫描并加载所有模型 (models/pool/旧格式 + models/{code}/v3格式)"""
        # 旧格式: models/pool/*_lgb_*.pkl
        for path in sorted(glob.glob(os.path.join(self.model_dir, '*_lgb_*.pkl'))):
            try:
                data = joblib.load(path)
                if isinstance(data, dict) and 'model' in data:
                    basename = os.path.basename(path)
                    code = basename.split('_')[0]
                    data['path'] = path
                    self.models[code] = data
            except Exception:
                pass
        
        # Phase 2/3: 加载v3格式 models/{code}/lightgbm.pkl + lightgbm_clf.pkl
        v3_root = os.path.dirname(self.model_dir)  # models/ (pool的上层)
        v3_count = 0
        for entry in sorted(os.listdir(v3_root)):
            code_dir = os.path.join(v3_root, entry)
            if not os.path.isdir(code_dir) or entry == 'pool':
                continue
            code = entry
            if code in self.models:
                continue
            lgb_path = os.path.join(code_dir, 'lightgbm.pkl')
            clf_path = os.path.join(code_dir, 'lightgbm_clf.pkl')
            scaler_path = os.path.join(code_dir, 'scaler.pkl')
            meta_path = os.path.join(code_dir, 'model_metadata.pkl')
            if os.path.exists(lgb_path):
                try:
                    model = joblib.load(lgb_path)
                    clf_model = joblib.load(clf_path) if os.path.exists(clf_path) else None
                    scaler = joblib.load(scaler_path) if os.path.exists(scaler_path) else None
                    # P0fix: 加载feature_cols从metadata
                    metadata = {}
                    if os.path.exists(meta_path):
                        try:
                            metadata = joblib.load(meta_path)
                        except Exception:
                            pass
                    self.models[code] = {
                        'model': model,
                        'clf_model': clf_model,
                        'scaler': scaler,
                        'path': lgb_path,
                        'use_regression': metadata.get('use_regression', False),
                        'dir_accuracy': metadata.get('dir_accuracy', 0.55),
                        'val_dir_accuracy': metadata.get('val_dir_accuracy'),
                        'clf_accuracy': metadata.get('clf_accuracy'),
                        'lgb_polarity': metadata.get('lgb_polarity', 1),
                        'clf_polarity': metadata.get('clf_polarity', 1),
                        'feature_cols': metadata.get('feature_cols', []),
                        'r2': metadata.get('r2', -1),
                    }
                    if clf_model is not None:
                        v3_count += 1
                except Exception:
                    pass
        
        # 为已加载的旧格式模型附加分类器（如果存在）
        v3_root = os.path.dirname(self.model_dir)
        for code in list(self.models.keys()):
            if self.models[code].get('clf_model') is not None:
                continue  # 已有分类器
            clf_path = os.path.join(v3_root, code, 'lightgbm_clf.pkl')
            if os.path.exists(clf_path):
                try:
                    self.models[code]['clf_model'] = joblib.load(clf_path)
                    v3_count += 1
                except Exception:
                    pass
        
        print(f"[PoolPredictor] 加载 {len(self.models)} 个模型 ({len(self.models)-v3_count}个旧格式+{v3_count}个含分类器)")

    def get_model(self, code):
        """获取单个股票模型"""
        return self.models.get(code)

    def get_all_codes(self):
        return list(self.models.keys())

    def _calibrate_confidence(self, raw_accuracy, val_accuracy=None):
        """P1fix: 反过拟合校准 — 高准确率≠高置信度

        Layer1验证发现: 置信度与真实准确率倒挂。
        >56%置信度→实际40%准确率; <45%置信度→实际100%准确率。
        原因: 过拟合模型的历史准确率被高估。

        修复:
        1. 用过拟合gap惩罚 (val>test = 过拟合; test>val = 幸运分割)
        2. 保守取两者较低值作为有效准确率
        3. 硬上限0.65
        """
        import math
        if val_accuracy is not None:
            # 取test和val的较低者 + 对过大差距额外惩罚
            effective_acc = min(raw_accuracy, val_accuracy)
            gap = val_accuracy - raw_accuracy
            if abs(gap) > 0.05:
                # 超过5%的gap → 两个方向都惩罚(过拟合或幸运分割)
                penalty = max(0.5, 1.0 - abs(gap) * 2)
                effective_acc = effective_acc * penalty
        else:
            effective_acc = raw_accuracy

        # sigmoid centered at 0.48
        cal = 1.0 / (1.0 + math.exp(-8 * (effective_acc - 0.48)))
        return round(min(0.65, max(0.25, cal)), 4)

    def predict(self, code, df_ohlcv, name=""):
        """对单个股票预测

        Args:
            code: 股票代码 (如 '600487')
            df_ohlcv: 含 open/high/low/close/volume 的 DataFrame (需至少60行)

        Returns:
            dict: {signal, score, confidence, predicted_return, mode, r2, accuracy}
        """
        # v4.5.12 fix: 低精度模型黑名单检查
        if code in LOW_ACCURACY_BLACKLIST:
            return {
                'code': code,
                'name': name,
                'signal': 'hold',
                'score': 0,
                'confidence': 0,
                'predicted_return': 0,
                'mode': 'blacklisted_low_accuracy',
                'r2': -1,
                'accuracy': self.models.get(code, {}).get('dir_accuracy', 0),
                'source': 'pool_model'
            }

        model_data = self.models.get(code)
        if not model_data:
            return self._fallback_signal(code, name)

        # 特征计算
        try:
            X = self._compute_features(df_ohlcv, model_data['feature_cols'])
            if X is None or len(X) == 0:
                return self._fallback_signal(code, name)
        except Exception:
            return self._fallback_signal(code, name)

        # 预测
        latest_features = X[-1:].reshape(1, -1) if X.ndim == 1 else X[-1:]
        scaler = model_data.get('scaler') if isinstance(model_data, dict) else None
        if scaler is not None:
            try:
                latest_features = scaler.transform(latest_features)
            except Exception:
                return self._fallback_signal(code, name)
        try:
            pred_return = float(model_data['model'].predict(latest_features)[0])
            pred_return *= int(model_data.get('lgb_polarity', 1) or 1)
        except (ValueError, Exception) as e:
            # Feature mismatch - fallback
            return self._fallback_signal(code, name)
        use_regression = model_data.get('use_regression', False)

        # Phase 2/3: 分类器预测（当可用时）
        clf_model = model_data.get('clf_model') if isinstance(model_data, dict) else None
        if clf_model is not None:
            try:
                clf_proba = clf_model.predict_proba(latest_features)[0, 1]  # P(up)
                if int(model_data.get('clf_polarity', 1) or 1) < 0:
                    clf_proba = 1 - clf_proba
                clf_signal = 1 if clf_proba > 0.5 else -1
                # 分类器与回归融合: 方向一致时增强，相反时弱化
                reg_signal = 1 if pred_return > 0 else -1
                if reg_signal == clf_signal:
                    pred_return = pred_return * 1.1
                else:
                    clf_acc = model_data.get('clf_accuracy', model_data.get('dir_accuracy', 0.55))
                    discount = max(0.2, min(0.8, clf_acc * 1.2 - 0.1))
                    pred_return = pred_return * discount
            except Exception:
                pass

        # 信号判断 (A: 提升阈值 → 过滤噪音)
        if use_regression and abs(pred_return) > 0.008:
            # R2>0: 回归+方向, 阈值0.8%覆盖双边交易成本
            signal = 'buy' if pred_return > 0 else 'sell'
            score = min(10, 5 + pred_return * 100)
            raw_acc = model_data.get('dir_accuracy', 0.55)
            val_acc = model_data.get('val_dir_accuracy', None)
            confidence = self._calibrate_confidence(raw_acc, val_acc)
            mode = 'regression+direction'
        else:
            # R2<0: 仅方向, 阈值0.5%
            signal = 'buy' if pred_return > 0.005 else ('sell' if pred_return < -0.005 else 'hold')
            score = 5.0 + (2 if pred_return > 0.005 else (-2 if pred_return < -0.005 else 0))
            raw_acc = model_data.get('dir_accuracy', 0.52)
            val_acc = model_data.get('val_dir_accuracy', None)
            confidence = self._calibrate_confidence(raw_acc, val_acc)
            mode = 'direction_only'

        return {
            'code': code,
            'name': name,
            'signal': signal,
            'score': round(score, 2),
            'confidence': round(confidence, 2),
            'predicted_return': round(pred_return, 4),
            'mode': mode,
            'r2': model_data.get('r2', -1),
            'accuracy': model_data.get('dir_accuracy', 0.5),
            'source': 'pool_model'
        }

    def predict_batch(self, stock_data, names=None):
        """批量预测 + P0跨截面标准化

        Args:
            stock_data: {code: DataFrame}
            names: {code: name} 可选

        Returns:
            list of prediction dicts with cross-sectional z_score, sorted by cs_score desc
        """
        results = []
        for code, df in stock_data.items():
            name = (names or {}).get(code, code)
            pred = self.predict(code, df, name)
            results.append(pred)

        # P0: 跨截面Z-score标准化
        scores = [r['score'] for r in results]
        returns = [r['predicted_return'] for r in results]
        confs = [r['confidence'] for r in results]
        mean_s, std_s = np.mean(scores), np.std(scores) + 1e-8
        mean_r, std_r = np.mean(returns), np.std(returns) + 1e-8

        for i, r in enumerate(results):
            z_score = (scores[i] - mean_s) / std_s
            z_return = (returns[i] - mean_r) / std_r
            r['z_score'] = round(z_score, 3)
            r['cs_score'] = round(z_score * 0.5 + z_return * 0.3 + confs[i] * 0.2, 3)
            if z_score > 0.5:
                r['cs_signal'] = 'buy_strong'
            elif z_score > 0:
                r['cs_signal'] = 'buy_weak'
            elif z_score > -0.5:
                r['cs_signal'] = 'hold'
            else:
                r['cs_signal'] = 'sell'

        results.sort(key=lambda x: x['cs_score'], reverse=True)

        # v4.5.12 fix: PSI监控接入生产预测流程
        try:
            # PSI基线不存在时自动初始化
            if not os.path.exists(PSI_BASELINE_PATH):
                feature_cols = {code: self.models[code].get('feature_cols', [])
                               for code in stock_data if code in self.models}
                if feature_cols:
                    self.save_psi_baseline(stock_data, feature_cols)
            # 检测特征漂移
            drift_alerts = self.check_psi_drift(stock_data, names)
            if drift_alerts:
                print(f"🔴 [PSI告警] {len(drift_alerts)}只股票出现特征分布显著漂移!")
                for a in drift_alerts[:5]:
                    print(f"   {a['name']}({a['code']}): max_psi={a['max_psi']:.3f}, "
                          f"漂移特征={a['drifting_features'][:3]}")
        except Exception:
            pass

        return results

    # ═══════════════ PSI 特征稳定性监控 ═══════════════

    def check_psi_drift(self, stock_data: dict, names: dict = None) -> list:
        """对当前特征分布做PSI检测，返回漂移告警列表 v4.5.12

        比较当前特征 vs 训练基线 (models/psi_baselines.json)
        PSI < 0.1: 稳定 | 0.1-0.25: 轻微 | > 0.25: 显著漂移
        """
        from core.psi_monitor import calculate_psi_matrix

        # 加载基线
        baselines = {}
        if os.path.exists(PSI_BASELINE_PATH):
            with open(PSI_BASELINE_PATH) as f:
                baselines = json.load(f)

        alerts = []
        for code, df in stock_data.items():
            # 仅对已有基线的股票做PSI检测
            if code not in baselines:
                continue

            name = (names or {}).get(code, code)
            model_data = self.models.get(code)
            if not model_data:
                continue

            feature_cols = model_data.get('feature_cols', [])
            try:
                X = self._compute_features(df.copy(), feature_cols)
                if X is None:
                    continue
                if X.ndim == 1:
                    X = X.reshape(1, -1)
                # 仅用最近20日特征做PSI (避免历史数据稀释漂移信号)
                recent_X = X[-20:] if len(X) > 20 else X
                current_df = pd.DataFrame(recent_X, columns=feature_cols[:recent_X.shape[1]])
            except Exception:
                continue

            # 基线是各特征的5/50/95分位数 → 需要重建分布
            try:
                baseline_feats = baselines[code]
                base_data = {}
                for feat in feature_cols:
                    if feat in baseline_feats and feat in current_df.columns:
                        q = baseline_feats[feat]
                        # 用分位数近似重建基线分布
                        base_samples = np.random.normal(
                            loc=q.get('p50', 0), scale=(q.get('p95', 1) - q.get('p5', -1)) / 4,
                            size=min(1000, len(recent_X) * 2)
                        )
                        base_data[feat] = pd.Series(base_samples)

                if base_data:
                    base_df = pd.DataFrame(base_data)
                    psi_result = calculate_psi_matrix(base_df, current_df[current_df.columns.intersection(base_df.columns)])
                    drifting = [k for k, v in psi_result.items() if v['psi'] >= 0.25]
                    if drifting:
                        alerts.append({
                            'code': code, 'name': name,
                            'timestamp': datetime.now().isoformat(),
                            'drifting_features': drifting,
                            'max_psi': max(v['psi'] for v in psi_result.values()),
                            'level': 'significant' if any(v['psi'] >= 0.25 for v in psi_result.values()) else 'moderate',
                        })
            except Exception:
                continue

        # 记录告警
        if alerts:
            os.makedirs(os.path.dirname(PSI_REPORT_PATH), exist_ok=True)
            for alert in alerts:
                with open(PSI_REPORT_PATH, 'a') as f:
                    f.write(json.dumps(alert, ensure_ascii=False) + '\n')
                print(f"🔴 PSI漂移告警: {alert['name']}({alert['code']}) "
                      f"max_psi={alert['max_psi']:.3f} | 漂移特征: {alert['drifting_features']}")

        return alerts

    def save_psi_baseline(self, stock_data: dict, feature_cols: dict):
        """保存当前特征分布作为PSI基线 (训练完成后调用)

        Args:
            stock_data: {code: DataFrame}
            feature_cols: {code: [feature names]}
        """
        from core.psi_monitor import _bin_values
        baselines = {}
        if os.path.exists(PSI_BASELINE_PATH):
            with open(PSI_BASELINE_PATH) as f:
                baselines = json.load(f)

        for code, df in stock_data.items():
            cols = feature_cols.get(code, [])
            if not cols:
                continue
            try:
                model_data = self.models.get(code)
                if not model_data:
                    continue
                X = self._compute_features(df.copy(), model_data.get('feature_cols', cols))
                if X is None:
                    continue
                if X.ndim == 1:
                    X = X.reshape(1, -1)
                feat_df = pd.DataFrame(X, columns=model_data.get('feature_cols', cols)[:X.shape[1]])
                baseline_entry = {}
                for col in feat_df.columns:
                    series = feat_df[col].dropna()
                    if len(series) < 10:
                        continue
                    baseline_entry[col] = {
                        'p5': float(series.quantile(0.05)),
                        'p50': float(series.quantile(0.50)),
                        'p95': float(series.quantile(0.95)),
                        'mean': float(series.mean()),
                        'std': float(series.std()),
                    }
                baselines[code] = baseline_entry
            except Exception:
                continue

        os.makedirs(os.path.dirname(PSI_BASELINE_PATH), exist_ok=True)
        with open(PSI_BASELINE_PATH, 'w') as f:
            json.dump(baselines, f, indent=2, ensure_ascii=False)
        print(f"✅ PSI基线已保存: {len(baselines)} 只股票 → {PSI_BASELINE_PATH}")

    # ═══════════════ 特征计算 ═══════════════
    def _compute_features(self, df, feature_cols):
        """从OHLCV计算特征 — v4.5.12 统一因子计算 (两路径，均含shift(1)防偏)

        优先级:
        1. train_predictor_v3.build_features() — v3模型训练同名特征
        2. SignalGenerator.compute_factors() — 统一因子计算 (与回测一致)
        3. _compute_features_internal() — fallback (v4.5.12已修复shift(1))
        """
        model_data = self.models.get(df) if isinstance(df, str) else None

        try:
            X = self._compute_train_v3_features(df, feature_cols)
            if X is not None and len(X) > 0:
                return X
        except Exception:
            pass

        # P1: 优先使用 SignalGenerator 统一因子 (与 backtest_engine 一致)
        try:
            from core.signal_generator import SignalGenerator
            sg = SignalGenerator()
            feats_df = sg.compute_factors(df)
            if feats_df is not None and len(feats_df) > 0:
                available = [c for c in feature_cols if c in feats_df.columns]
                if len(available) >= len(feature_cols) * 0.7:
                    X = feats_df[available].fillna(0).values
                    return X
        except Exception:
            pass

        # Fallback: 用内部方法 (v4.5.12已修复shift(1)，与SignalGenerator一致)
        return self._compute_features_internal(df, feature_cols)

    def _compute_train_v3_features(self, df, feature_cols):
        """Compute features with the same builder used by train_predictor_v3."""
        try:
            from scripts.train_predictor_v3 import build_features
        except Exception:
            return None

        df = df.copy()
        if 'date' in df.columns:
            try:
                df['date'] = pd.to_datetime(df['date'], errors='coerce')
                df = df.dropna(subset=['date']).sort_values('date').set_index('date', drop=False)
            except Exception:
                pass

        feats = build_features(df)
        if feats is None or len(feats) == 0:
            return None

        matched = 0
        for feat in feature_cols:
            if feat in feats.columns or feat.startswith('rank_') or feat.startswith('rel_'):
                matched += 1
        if matched < max(5, int(len(feature_cols) * 0.4)):
            return None

        feats = feats.replace([np.inf, -np.inf], np.nan)
        for feat in feature_cols:
            if feat.startswith('rank_') and feat not in feats.columns:
                feats[feat] = 0.5
            elif feat.startswith('rel_') and feat not in feats.columns:
                feats[feat] = 0.0
            elif feat not in feats.columns:
                feats[feat] = 0.0

        X = feats[feature_cols].fillna(0).values
        return X if len(X) else None

    def _compute_features_internal(self, df, feature_cols):
        """旧版内部特征计算 - 作为fallback保留

        v4.5.12 fix: 所有rolling窗口使用shift(1)防止前视偏差，
        与 SignalGenerator.compute_factors() 保持一致。
        注意：已有模型如果训练时未使用shift(1)，需重新训练才能正确预测。
        """
        df = df.copy()
        df = df.sort_values(df.columns[0])  # try date-like column
        o, h, l, c, v = df['open'], df['high'], df['low'], df['close'], df['volume']

        # 滞后价格 (防止当日收盘价污染因子)
        c_s = c.shift(1)
        v_s = v.shift(1)

        # P0-FIX: pct_change()包含当日信息，必须shift(1)防止前视偏差
        df['ret_1d'] = c.pct_change().shift(1)  # P0-FIX: shift防止前视偏差
        df['ret_5d'] = c.pct_change(5).shift(1)  # P0-FIX: shift防止前视偏差
        df['ret_20d'] = c.pct_change(20).shift(1)  # P0-FIX: shift防止前视偏差
        df['ret_60d'] = c.pct_change(60).shift(1)  # P0-FIX: shift防止前视偏差

        # Moving Averages — 使用滞后价格
        for p in [5, 10, 20, 60, 120]:
            ma = c_s.rolling(p).mean()
            df[f'ma_{p}'] = ma
            df[f'ma_ratio_{p}'] = c_s / ma.replace(0, np.nan)

        # Volatility — ret_1d已在L467 shift(1)，无需再次shift
        df['vol_20d'] = df['ret_1d'].rolling(20).std()
        df['vol_60d'] = df['ret_1d'].rolling(60).std()

        # Volume — 使用滞后成交量
        df['vol_ma_5'] = v_s.rolling(5).mean()
        df['vol_ma_20'] = v_s.rolling(20).mean()
        df['vol_ratio_5'] = v_s / df['vol_ma_5'].replace(0, np.nan)
        df['vol_ratio_20'] = v_s / df['vol_ma_20'].replace(0, np.nan)

        # P0-FIX: Amplitude & Shadows 使用shift(1)的OHLC，防止前视偏差
        o_s, h_s, l_s = o.shift(1), h.shift(1), l.shift(1)
        df['amplitude'] = (h_s - l_s) / c_s
        df['upper_shadow'] = (h_s - pd.concat([o_s, c_s], axis=1).max(axis=1)) / c_s
        df['lower_shadow'] = (pd.concat([o_s, c_s], axis=1).min(axis=1) - l_s) / c_s

        # Trends — 使用滞后价格均线
        ma5_s = c_s.rolling(5).mean()
        ma20_s = c_s.rolling(20).mean()
        ma60_s = c_s.rolling(60).mean()
        ma120_s = c_s.rolling(120).mean()
        df['trend_5_20'] = (ma5_s - ma20_s) / ma20_s.replace(0, np.nan)
        df['trend_20_60'] = (ma20_s - ma60_s) / ma60_s.replace(0, np.nan)
        df['trend_60_120'] = (ma60_s - ma120_s) / ma120_s.replace(0, np.nan)

        # MACD — EMA 基于滞后价格
        ema12 = c_s.ewm(span=12).mean()
        ema26 = c_s.ewm(span=26).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9).mean()
        df['macd_hist'] = df['macd'] - df['macd_signal']
        df['macd_ratio'] = df['macd'] / c_s.replace(0, np.nan)

        # RSI — 基于滞后价格差分
        delta = c_s.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        df['rsi'] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

        # Bollinger — 基于滞后价格
        bb_mid = c_s.rolling(20).mean()
        bb_std = c_s.rolling(20).std()
        df['bb_mid'] = bb_mid
        df['bb_width'] = 2 * bb_std / bb_mid.replace(0, np.nan)
        df['bb_position'] = (c_s - bb_mid) / (2 * bb_std + 1e-9)

        # Fundamental fields (not in OHLCV, default 0)
        # P2-1: 只保留 amount 字段,其余基本面特征由 build_features 统一处理
        # 旧的 52 特征模式中基本面全为0,已在新版 build_features 中移除
        if 'amount' not in df.columns:
            df['amount'] = 0.0

        # 这些字段不再在内部特征计算中填充0,避免噪音
        # (保留注释以兼容旧模型feature_cols)
        for fund_field in ['pe', 'pe_pct_1y', 'pb', 'roe_approx', 'revenue_growth',
                           'dividend_yield', 'turnover_rate', 'turnover_ma_20',
                           'turnover_ratio', 'rating_score', 'pe_pb_ratio', 'quality_score']:
            if fund_field not in df.columns:
                df[fund_field] = 0.0

        # Cross-sectional features are computed from the full stock pool during
        # training. Single-stock fallback prediction uses neutral values instead
        # of dropping the model because the caller does not have the full pool.
        for feat in feature_cols:
            if feat.startswith('rank_') and feat not in df.columns:
                df[feat] = 0.5
            elif feat.startswith('rel_') and feat not in df.columns:
                df[feat] = 0.0

        # Fill NaN (may occur from rolling windows with insufficient data)
        # Use 0 for features and drop rows where close is NaN (should never happen)
        df = df[df['close'].notna()].fillna(0)
        if len(df) == 0:
            return None

        # Select features matching model's feature_cols
        available = [c for c in feature_cols if c in df.columns]
        if len(available) < 5:
            return None

        X = df[available].fillna(0).values
        return X

    def _fallback_signal(self, code, name):
        """模型不存在时的回退信号"""
        return {
            'code': code, 'name': name,
            'signal': 'hold', 'score': 5.0, 'confidence': 0.3,
            'predicted_return': 0.0, 'mode': 'fallback',
            'r2': -99, 'accuracy': 0.5, 'source': 'rule_based'
        }


# 全局单例
_predictor = None

def get_predictor(model_dir=None):
    global _predictor
    if _predictor is None:
        _predictor = PoolPredictor(model_dir)
    return _predictor


if __name__ == '__main__':
    print("🧪 PoolPredictor 测试")
    pred = PoolPredictor()
    print(f"   已加载: {len(pred.models)} 个模型")
    for code, data in sorted(pred.models.items()):
        mode = '📈回归' if data.get('use_regression') else '🧭方向'
        print(f"   {code} | acc={data.get('dir_accuracy',0):.2%} | R2={data.get('r2',-1):.4f} | {mode}")
