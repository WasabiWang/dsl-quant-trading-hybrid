#!/usr/bin/env python3
"""6模型训练器v3 — 多周期 + 截面排名 + 市场过滤 + 权重优化

改进 (P0-P2):
  P0: 多周期训练 (1d/5d/10d/20d) + 截面排名评估
  P1: 市场状态过滤 + 模型权重优化 (验证集学习最优权重)
  P2: 扩展选股池 + 滚动窗口训练
"""

import os
import sys
import json
import joblib
import numpy as np
import pandas as pd
from datetime import datetime
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(PROJECT_ROOT, 'models')


class ModelTrainer:
    """模型训练与评估类 (v3)"""
    
    def __init__(self):
        self.models = {}
        self.results = {}
        self.feature_importance = {}
        
    def prepare_data(self, df: pd.DataFrame, horizon: int = 5,
                     exclude_patterns: list = None):
        """准备训练和测试数据 (支持多周期)
        
        Args:
            df: 特征DataFrame
            horizon: 预测周期 (1/5/10/20)
            exclude_patterns: 额外需要排除的特征模式
        """
        target_col = f'target_{horizon}d_return'
        
        if target_col not in df.columns:
            raise ValueError(f"目标列 '{target_col}' 不在DataFrame中")
        
        # 排除目标列和基础价格列
        base_excludes = [target_col, 'open', 'high', 'low', 'close', 'volume']
        # 也排除其他周期的target
        for h in [1, 5, 10, 20]:
            t = f'target_{h}d_return'
            if t != target_col and t in df.columns:
                base_excludes.append(t)
            d = f'target_{h}d_direction'
            if d in df.columns:
                base_excludes.append(d)
        
        if exclude_patterns:
            base_excludes.extend(exclude_patterns)
        
        feature_cols = [col for col in df.columns if col not in base_excludes]
        
        X = df[feature_cols].fillna(0).replace([float('inf'), float('-inf')], 0)
        y = df[target_col]
        
        return X, y, feature_cols
    
    def train_linear_regression(self, X_train, y_train):
        from sklearn.linear_model import LinearRegression
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        model = LinearRegression()
        model.fit(X_train_scaled, y_train)
        return model, scaler
    
    def train_random_forest(self, X_train, y_train):
        from sklearn.ensemble import RandomForestRegressor
        model = RandomForestRegressor(
            n_estimators=100, max_depth=10, min_samples_split=5,
            random_state=42, n_jobs=-1
        )
        model.fit(X_train, y_train)
        return model, None
    
    def train_xgboost(self, X_train, y_train):
        try:
            import xgboost as xgb
            model = xgb.XGBRegressor(
                n_estimators=100, max_depth=6, learning_rate=0.1,
                random_state=42, n_jobs=-1
            )
            model.fit(X_train, y_train)
            return model, None
        except ImportError:
            return None, None
    
    def train_lightgbm(self, X_train, y_train):
        try:
            import lightgbm as lgb
            model = lgb.LGBMRegressor(
                n_estimators=200, max_depth=7, learning_rate=0.05,
                random_state=42, n_jobs=-1, verbose=-1
            )
            model.fit(X_train, y_train)
            return model, None
        except ImportError:
            return None, None
    
    def evaluate_model(self, model, scaler, X_test, y_test, model_name: str):
        """评估模型性能 (增强版: 加入排名评估)"""
        if scaler:
            X_test_scaled = scaler.transform(X_test)
            y_pred = model.predict(X_test_scaled)
        else:
            y_pred = model.predict(X_test)
        
        # 标准指标
        mse = mean_squared_error(y_test, y_pred)
        mae = mean_absolute_error(y_test, y_pred)
        r2 = r2_score(y_test, y_pred)
        
        # 方向准确率
        direction_acc = float(np.mean((np.sign(y_pred) == np.sign(y_test.values))))
        
        # P0.2: 截面排名评估 — Spearman秩相关系数 (IC)
        try:
            from scipy.stats import spearmanr
            rank_ic, rank_p = spearmanr(y_pred, y_test.values)
        except ImportError:
            rank_ic, rank_p = np.corrcoef(y_pred, y_test.values)[0, 1], 0
        
        # 排序方向准确率 (如果y_pred排序和y_test排序同向)
        if len(y_pred) > 3:
            pred_rank = np.argsort(np.argsort(y_pred))
            true_rank = np.argsort(np.argsort(y_test.values))
            rank_direction_acc = float(np.mean(
                (pred_rank > len(y_pred)/2) == (true_rank > len(y_pred)/2)
            ))
        else:
            rank_direction_acc = direction_acc
        
        # 信息比率 (Information Ratio)
        returns = y_pred * y_test.values  # 策略收益 = 预测×实际
        ir = float(np.mean(returns) / (np.std(returns) + 1e-10)) if len(returns) > 1 else 0
        
        # 胜率 (Win Rate)
        win_rate = float(np.mean(returns > 0))
        
        return {
            'model_name': model_name,
            'mse': float(mse),
            'mae': float(mae),
            'r2': float(r2),
            'direction_accuracy': direction_acc,
            'rank_ic': float(rank_ic) if not np.isnan(rank_ic) else 0,
            'rank_direction_accuracy': rank_direction_acc,
            'information_ratio': ir,
            'win_rate': win_rate,
            'predictions': y_pred,
            'actuals': y_test.values
        }
    
    def train_all_models(self, X_train, X_test, y_train, y_test, symbol: str):
        """训练所有模型 (原有6模型)"""
        print(f"\n📊 训练{symbol}的预测模型...")
        
        models_to_train = [
            ('linear_regression', self.train_linear_regression),
            ('random_forest', self.train_random_forest),
            ('xgboost', self.train_xgboost),
            ('lightgbm', self.train_lightgbm)
        ]
        
        results = {}
        base_models = {}
        
        for model_name, train_func in models_to_train:
            print(f"  🚀 训练{model_name}...")
            try:
                model, scaler = train_func(X_train, y_train)
                if model is not None:
                    eval_result = self.evaluate_model(model, scaler, X_test, y_test, model_name)
                    results[model_name] = {
                        'model': model, 'scaler': scaler, 'evaluation': eval_result
                    }
                    base_models[model_name] = {'model': model, 'scaler': scaler}
                    print(f"    ✅ R²={eval_result['r2']:.4f} 方向={eval_result['direction_accuracy']:.2%} IC={eval_result['rank_ic']:.4f}")
            except Exception as e:
                print(f"    ❌ {e}")
        
        # 集成模型
        if len(base_models) >= 2:
            try:
                from sklearn.ensemble import VotingRegressor
                ests = [(n, m['model']) for n, m in base_models.items()]
                em = VotingRegressor(estimators=ests, n_jobs=-1)
                em.fit(X_train, y_train)
                ev = self.evaluate_model(em, None, X_test, y_test, 'ensemble_voting')
                results['ensemble_voting'] = {'model': em, 'scaler': None, 'evaluation': ev}
                print(f"  ✅ ensemble_voting: R²={ev['r2']:.4f} 方向={ev['direction_accuracy']:.2%}")
            except Exception as e:
                print(f"  ⚠️ ensemble_voting: {e}")
            
            try:
                from sklearn.ensemble import StackingRegressor
                from sklearn.linear_model import RidgeCV
                ests = [(n, m['model']) for n, m in base_models.items()]
                sm = StackingRegressor(estimators=ests, final_estimator=RidgeCV(), cv=5, n_jobs=-1)
                sm.fit(X_train, y_train)
                ev = self.evaluate_model(sm, None, X_test, y_test, 'ensemble_stacking')
                results['ensemble_stacking'] = {'model': sm, 'scaler': None, 'evaluation': ev}
                print(f"  ✅ ensemble_stacking: R²={ev['r2']:.4f} 方向={ev['direction_accuracy']:.2%}")
            except Exception as e:
                print(f"  ⚠️ ensemble_stacking: {e}")
        
        self.results[symbol] = results
        return results
    
    # ===== P1.6: 模型权重优化 =====
    
    def optimize_model_weights(self, X_val, y_val, base_models: dict) -> dict:
        """在验证集上学习最优模型权重 (最大化方向准确率)
        
        不平均集成，而是用验证集学习每个模型的贡献权重。
        """
        predictions = {}
        for name, data in base_models.items():
            model = data['model']
            scaler = data['scaler']
            if scaler:
                pred = model.predict(scaler.transform(X_val))
            else:
                pred = model.predict(X_val)
            predictions[name] = pred
        
        # 网格搜索最优权重 (最大化方向准确率)
        best_weights = {}
        best_acc = 0
        model_names = list(predictions.keys())
        
        if len(model_names) == 1:
            return {model_names[0]: 1.0}
        
        # 简化版: 交叉验证搜索
        for _ in range(500):
            weights = np.random.dirichlet(np.ones(len(model_names)) * 2)
            weighted_pred = np.zeros(len(y_val))
            for i, name in enumerate(model_names):
                weighted_pred += weights[i] * predictions[name]
            
            acc = float(np.mean((np.sign(weighted_pred) == np.sign(y_val.values))))
            if acc > best_acc:
                best_acc = acc
                best_weights = dict(zip(model_names, weights))
        
        return best_weights
    
    def apply_optimized_weights(self, predictions: dict, weights: dict) -> np.ndarray:
        """应用优化权重生成最终预测"""
        result = np.zeros(len(list(predictions.values())[0]))
        for name, pred in predictions.items():
            if name in weights:
                result += weights[name] * pred
        return result
    
    # ===== P1.4: 市场状态过滤 =====
    
    @staticmethod
    def filter_by_regime(df: pd.DataFrame, regime_col: str = 'trend_signal',
                          allowed_regimes: list = None) -> pd.DataFrame:
        """按市场状态过滤数据 (仅在指定状态下训练/预测)
        
        Args:
            df: 含市场状态特征的数据
            regime_col: 状态列名 'trend_signal' (1=牛, -1=熊, 0=震荡)
            allowed_regimes: 允许的状态列表，默认[1,0] (牛市和震荡)
        """
        if allowed_regimes is None:
            allowed_regimes = [1, 0]
        
        if regime_col in df.columns:
            return df[df[regime_col].isin(allowed_regimes)]
        return df
    
    # ===== P0.2: 截面排名训练 =====
    
    def train_cross_sectional(self, dfs: dict, horizon: int = 5,
                               train_ratio: float = 0.8) -> dict:
        """截面排名预测: 每天对N只标的排序
        
        Args:
            dfs: {symbol: DataFrame} 多只股票的DataFrame (需对齐日期)
            horizon: 预测周期
            train_ratio: 训练集比例
        
        Returns:
            训练结果，含排名IC
        """
        print(f"\n📊 截面排名训练 ({len(dfs)}只标的, {horizon}d周期)")
        
        # 找共同日期
        all_dates = None
        for sym, df in dfs.items():
            if all_dates is None:
                all_dates = set(df.index)
            else:
                all_dates = all_dates & set(df.index)
        
        common_dates = sorted(all_dates)
        n_dates = len(common_dates)
        split_idx = int(n_dates * train_ratio)
        train_dates = common_dates[:split_idx]
        test_dates = common_dates[split_idx:]
        
        print(f"  共同日期: {len(common_dates)}天 (训练{len(train_dates)} 测试{len(test_dates)})")
        
        # 构建截面排名得分
        # 对每个交易日，按target排序生成排名标签 (0~1区间)
        target_col = f'target_{horizon}d_return'
        
        for sym, df in dfs.items():
            if target_col in df.columns:
                df['_ranking_label'] = np.nan
        
        # 计算每日截面排名
        for date in common_dates:
            returns = {}
            for sym, df in dfs.items():
                if date in df.index:
                    row = df.loc[date]
                    if isinstance(row, pd.DataFrame):
                        row = row.iloc[0]
                    if target_col in row.index and not pd.isna(row[target_col]):
                        returns[sym] = row[target_col]
            
            if len(returns) >= 2:
                # 按收益率排序，赋予排名标签
                sorted_stocks = sorted(returns, key=returns.get)
                for rank, sym in enumerate(sorted_stocks):
                    dfs[sym].loc[date, '_ranking_label'] = rank / (len(sorted_stocks) - 1)
        
        # 训练（使用排名标签）
        results = {}
        for sym, df in dfs.items():
            valid = df['_ranking_label'].notna()
            if valid.sum() < 50:
                continue
            
            X = df[valid].drop(columns=['_ranking_label'] if '_ranking_label' in df.columns else [])
            y = df.loc[valid, '_ranking_label']
            
            # 与train_all_models相同的逻辑...
            # 简化：只训练LightGBM
            try:
                import lightgbm as lgb
                X_clean = X.fillna(0).replace([float('inf'), float('-inf')], 0)
                split = int(len(X_clean) * train_ratio)
                model = lgb.LGBMRegressor(n_estimators=200, max_depth=7, 
                                          learning_rate=0.05, random_state=42,
                                          verbose=-1, n_jobs=-1)
                model.fit(X_clean.iloc[:split], y.iloc[:split])
                pred = model.predict(X_clean.iloc[split:])
                
                from scipy.stats import spearmanr
                ic, _ = spearmanr(pred, y.iloc[split:])
                results[sym] = {'model': model, 'rank_ic': ic if not np.isnan(ic) else 0}
            except Exception:
                pass
        
        return results
    
    # ===== P2: 滚动窗口训练 =====
    
    def train_walk_forward(self, df: pd.DataFrame, horizon: int = 5,
                            window_size: int = None, step_size: int = None):
        """Walk-forward滚动窗口训练 (P2)
        
        Args:
            df: 特征DataFrame
            horizon: 预测周期
            window_size: 训练窗口天数 (默认: 全部数据的60%)
            step_size: 每次前进天数 (默认: 60天)
        """
        if window_size is None:
            window_size = int(len(df) * 0.6)
        if step_size is None:
            step_size = 60
        
        target_col = f'target_{horizon}d_return'
        exclude_cols = [target_col, 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [c for c in df.columns if c not in exclude_cols 
                       and df[c].dtype in ['float64', 'int64']]
        
        X = df[feature_cols].fillna(0)
        y = df[target_col]
        
        results = []
        start = 0
        fold = 0
        
        while start + window_size + 60 < len(X):
            fold += 1
            train_end = start + window_size
            test_end = min(train_end + 60, len(X))
            
            X_train = X.iloc[start:train_end]
            y_train = y.iloc[start:train_end]
            X_test = X.iloc[train_end:test_end]
            y_test = y.iloc[train_end:test_end]
            
            if len(X_test) < 10:
                break
            
            # 只用LightGBM做滚动训练（快速）
            try:
                import lightgbm as lgb
                model = lgb.LGBMRegressor(
                    n_estimators=200, max_depth=7, learning_rate=0.05,
                    random_state=fold, verbose=-1, n_jobs=-1
                )
                model.fit(X_train, y_train)
                pred = model.predict(X_test)
                
                dir_acc = float(np.mean((np.sign(pred) == np.sign(y_test.values))))
                r2 = r2_score(y_test, pred)
                
                results.append({
                    'fold': fold, 'start_idx': start, 'end_idx': test_end,
                    'dir_acc': dir_acc, 'r2': r2
                })
            except Exception:
                pass
            
            start += step_size
        
        if results:
            avg_dir = np.mean([r['dir_acc'] for r in results])
            avg_r2 = np.mean([r['r2'] for r in results])
            print(f"  📊 Walk-forward: {len(results)}折 平均方向={avg_dir:.2%} R²={avg_r2:.4f}")
        
        return results
    
    def save_models(self, symbol: str):
        """保存训练好的模型"""
        if symbol not in self.results:
            return
        
        symbol_dir = os.path.join(MODEL_DIR, symbol)
        os.makedirs(symbol_dir, exist_ok=True)
        
        saved_models = []
        for model_name, model_data in self.results[symbol].items():
            model = model_data['model']
            scaler = model_data['scaler']
            
            model_path = os.path.join(symbol_dir, f"{model_name}.pkl")
            joblib.dump(model, model_path)
            
            if scaler:
                scaler_path = os.path.join(symbol_dir, f"{model_name}_scaler.pkl")
                joblib.dump(scaler, scaler_path)
            
            eval_path = os.path.join(symbol_dir, f"{model_name}_evaluation.json")
            eval_data = model_data['evaluation'].copy()
            for key in ['predictions', 'actuals']:
                if key in eval_data and eval_data[key] is not None:
                    eval_data[key] = eval_data[key].tolist() if hasattr(eval_data[key], 'tolist') else list(eval_data[key])
            with open(eval_path, 'w', encoding='utf-8') as f:
                json.dump(eval_data, f, indent=2, ensure_ascii=False, default=str)
            
            saved_models.append(model_name)
        
        print(f"  💾 保存{symbol}的{len(saved_models)}个模型到{symbol_dir}")
        return saved_models
