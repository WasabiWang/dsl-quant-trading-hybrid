#!/usr/bin/env python3
"""
predict_example.py - 预测模型使用示例
展示如何加载训练好的模型并进行预测

P0修复 (2026-04-26):
  1. 特征对齐: 根据模型feature_names_in_动态对齐特征列
  2. 模型路径统一: 支持从models/{symbol}/和models/ml/两种路径加载模型

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import sys
import json
import joblib
import pandas as pd
import numpy as np

# 添加项目根目录到路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 导入项目模块
from scripts.data_source_v2 import DataSource
from predictor.features import FeatureEngineer  # v4.0统一特征管线


class LGBCSIPredictor:
    """LightGBM CSI300/500 模型专用预测器
    
    特征管线: 57个基础特征 → MinMaxScaler → 60天滑动窗口展平 → LightGBM(3420特征)
    与 train_csi300_leaders.py 的预处理逻辑完全一致
    """
    
    LOOKBACK_DAYS = 60
    EXCLUDE_COLS = ['open', 'high', 'low', 'close', 'volume', 'target_1d', 'target_3d', 'target_5d']
    
    def __init__(self, symbol: str, variant: str = 'csi300'):
        self.symbol = symbol
        self.variant = variant  # csi300 / csi500 / advanced / local
        self.model = None
        self.scaler = None
        self.model_source = f'lgb_{variant}'
        
        # 查找模型和scaler路径
        if variant == 'csi500':
            self.model_path = os.path.join(PROJECT_ROOT, "models", "ml", "csi500", f"{symbol}_lgb_csi500.pkl")
            self.scaler_path = os.path.join(PROJECT_ROOT, "models", "ml", "csi500", f"{symbol}_scaler_csi500.pkl")
        else:
            self.model_path = os.path.join(PROJECT_ROOT, "models", "ml", f"{symbol}_lgb_{variant}.pkl")
            self.scaler_path = os.path.join(PROJECT_ROOT, "models", "ml", "scalers", f"{symbol}_scaler_{variant}.pkl")
        
        self._load()
    
    def _load(self):
        """加载模型和scaler"""
        if not os.path.exists(self.model_path):
            print(f"❌ LGB模型不存在: {self.model_path}")
            return False
        try:
            self.model = joblib.load(self.model_path)
            print(f"✅ 加载LGB模型: {self.model_path} ({self.model.n_features_in_}特征)")
            if os.path.exists(self.scaler_path):
                self.scaler = joblib.load(self.scaler_path)
                print(f"✅ 加载scaler: {self.scaler_path} ({self.scaler.n_features_in_}特征)")
            return True
        except Exception as e:
            print(f"❌ 加载LGB模型失败: {e}")
            return False
    
    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """构建CSI300/500兼容特征集
        使用统一特征管线 predictor.features.build_csi_features
        与 train_csi300_leaders.py 的 add_technical_factors 完全一致
        """
        from predictor.features import FeatureEngineer
        return FeatureEngineer.build_csi_features(df, self.symbol)
    
    def predict(self, days: int = 100):
        """使用CSI300/500专用管线进行预测"""
        if self.model is None:
            return {"error": "模型未加载"}
        
        ds = DataSource()
        # 多拉一些数据确保有足够的lookback窗口 (加30天缓冲给dropna)
        fetch_days = max(days, self.LOOKBACK_DAYS + 90)
        df = ds.get_kline(self.symbol, num=fetch_days)
        
        if df is None or df.empty:
            return {"error": "无法获取数据"}
        
        print(f"📊 获取到{self.symbol}的{len(df)}条数据")
        
        # 构建特征
        df_features = self._build_features(df)
        if df_features.empty or len(df_features) < self.LOOKBACK_DAYS:
            return {"error": f"数据不足: 需要至少{self.LOOKBACK_DAYS}条有效数据"}
        
        # 提取特征列（排除目标列和基础列）
        feature_cols = [c for c in df_features.columns if c not in self.EXCLUDE_COLS]
        features = df_features[feature_cols].values.astype(np.float32)
        
        # 处理异常值
        features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
        
        # 标准化
        if self.scaler is not None:
            # Scaler是fit在57个特征上的，需要对特征列做对齐
            if features.shape[1] == self.scaler.n_features_in_:
                features = self.scaler.transform(features)
            else:
                print(f"⚠️ 特征数不匹配: 生成{features.shape[1]}, scaler期望{self.scaler.n_features_in_}, 跳过scaler")
        
        # 构建滑动窗口
        last_window = features[-self.LOOKBACK_DAYS:]  # (60, n_features)
        X = last_window.flatten().reshape(1, -1)  # (1, 60*n_features)
        
        # 特征数对齐
        expected = self.model.n_features_in_
        if X.shape[1] != expected:
            if X.shape[1] < expected:
                pad = np.zeros((1, expected - X.shape[1]), dtype=np.float32)
                X = np.concatenate([X, pad], axis=1)
            else:
                X = X[:, :expected]
            print(f"🔧 特征对齐: {X.shape[1]} → {expected}")
        
        # 预测
        try:
            prediction = float(self.model.predict(X)[0])
            latest_price = float(df['close'].iloc[-1])
            predicted_price = latest_price * (1 + prediction)
            
            result = {
                "symbol": self.symbol,
                "model": f"lightgbm_{self.variant}",
                "model_source": self.model_source,
                "latest_price": latest_price,
                "predicted_return": prediction,
                "predicted_price": predicted_price,
                "prediction_date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data_points": len(df),
                "features_used": expected
            }
            
            print(f"🎯 预测结果:")
            print(f"   最新价格: {latest_price:.2f}")
            print(f"   预测收益率: {prediction:.4%}")
            print(f"   预测价格: {predicted_price:.2f}")
            
            return result
        except Exception as e:
            return {"error": f"预测失败: {e}"}


class Predictor:
    """预测器类 - 加载训练好的模型进行预测
    
    支持两种模型存储路径:
      - models/{symbol}/{model_name}.pkl          (老格式，train_predictor_v2产出)
      - models/ml/{symbol}_lgb_{variant}.pkl      (新格式，LightGBM CSI300/500模型)
    """
    
    # 模型搜索路径优先级
    MODEL_SEARCH_PATHS = [
        # 优先级1: 老格式 models/{symbol}/
        lambda symbol, model_name: (
            os.path.join(PROJECT_ROOT, "models", symbol, f"{model_name}.pkl"),
            os.path.join(PROJECT_ROOT, "models", symbol, f"{model_name}_scaler.pkl"),
            os.path.join(PROJECT_ROOT, "models", symbol, f"{model_name}_evaluation.json"),
            "legacy"
        ),
        # 优先级2: models/ml/ CSI300模型
        lambda symbol, model_name: (
            os.path.join(PROJECT_ROOT, "models", "ml", f"{symbol}_lgb_csi300.pkl"),
            os.path.join(PROJECT_ROOT, "models", "ml", "scalers", f"{symbol}_scaler_csi300.pkl"),
            None,
            "csi300"
        ),
        # 优先级3: models/ml/ CSI500模型
        lambda symbol, model_name: (
            os.path.join(PROJECT_ROOT, "models", "ml", "csi500", f"{symbol}_lgb_csi500.pkl"),
            os.path.join(PROJECT_ROOT, "models", "ml", "csi500", f"{symbol}_scaler_csi500.pkl"),
            None,
            "csi500"
        ),
        # 优先级4: models/ml/ advanced/ensemble/local模型
        lambda symbol, model_name: (
            os.path.join(PROJECT_ROOT, "models", "ml", f"{symbol}_lgb_advanced.pkl"),
            None,
            None,
            "advanced"
        ),
        # 优先级5: models/ml/ local模型
        lambda symbol, model_name: (
            os.path.join(PROJECT_ROOT, "models", "ml", f"{symbol}_lgb_local.pkl"),
            None,
            None,
            "local"
        ),
    ]
    
    def __init__(self, symbol: str, model_name: str = 'random_forest'):
        """
        初始化预测器
        
        参数:
            symbol: 股票代码
            model_name: 模型名称 ('linear_regression', 'random_forest', 'xgboost', 'lightgbm')
                         model_name='lightgbm'时会优先搜索models/ml/路径
        """
        self.symbol = symbol
        self.model_name = model_name
        self.model = None
        self.scaler = None
        self.feature_cols = None
        self.model_source = None  # 记录模型来源: legacy/csi300/csi500/advanced/local
        self._is_lgb_csi = False  # 是否为CSI专用模型
        self._lgb_predictor = None  # CSI专用预测器实例
        
        # 加载模型
        self.load_model()
    
    def load_model(self):
        """加载模型和标准化器 - 自动搜索所有可用路径
        
        当检测到CSI300/500/advanced/local格式的LightGBM模型时，
        自动委托给LGBCSIPredictor处理其专用的滑动窗口特征管线
        """
        for path_builder in self.MODEL_SEARCH_PATHS:
            model_path, scaler_path, eval_path, source = path_builder(self.symbol, self.model_name)
            
            if model_path and os.path.exists(model_path):
                # CSI300/500/advanced/local模型需要专用预测管线
                if source in ('csi300', 'csi500', 'advanced', 'local'):
                    variant = source  # csi300/csi500/advanced/local
                    try:
                        self._lgb_predictor = LGBCSIPredictor(self.symbol, variant)
                        if self._lgb_predictor.model is not None:
                            self.model = self._lgb_predictor.model
                            self.scaler = self._lgb_predictor.scaler
                            self.model_source = self._lgb_predictor.model_source
                            self._is_lgb_csi = True
                            return True
                    except Exception as e:
                        print(f"⚠️ LGB CSI预测器加载失败 {model_path}: {e}")
                        continue
                
                # 老格式模型 (legacy)
                try:
                    self.model = joblib.load(model_path)
                    self.model_source = source
                    self._is_lgb_csi = False
                    print(f"✅ 加载模型: {model_path} (来源: {source})")
                    
                    # 加载标准化器
                    if scaler_path and os.path.exists(scaler_path):
                        self.scaler = joblib.load(scaler_path)
                        print(f"✅ 加载标准化器: {scaler_path}")
                    
                    # 加载评估结果
                    if eval_path and os.path.exists(eval_path):
                        try:
                            with open(eval_path, 'r', encoding='utf-8') as f:
                                self._eval_data = json.load(f)
                        except Exception:
                            self._eval_data = {}
                    
                    return True
                except Exception as e:
                    print(f"⚠️ 加载模型失败 {model_path}: {e}")
                    continue
        
        print(f"❌ 未找到{self.symbol}的{self.model_name}模型 (搜索了所有路径)")
        return False
    
    def prepare_features(self, df: pd.DataFrame):
        """准备特征数据 - 核心修复：动态特征对齐
        
        根据模型的 feature_names_in_ 自动对齐特征列:
          - 模型期望但缺失的特征 → 补0
          - 模型不期望的多余特征 → 丢弃
          - 特征顺序与模型期望一致
        """
        if df.empty:
            return None
        
        # 特征工程
        fe = FeatureEngineer()
        df_features = fe.build_features(df, self.symbol)
        
        if df_features.empty:
            return None
        
        # 排除目标列和基础价格列
        exclude_cols = ['target_1d_return', 'open', 'high', 'low', 'close', 'volume']
        all_feature_cols = [col for col in df_features.columns if col not in exclude_cols]
        
        # 获取最新数据点
        latest_features = df_features[all_feature_cols].iloc[-1:]
        
        # ===== 关键修复: 动态特征对齐 =====
        if self.model is not None and hasattr(self.model, 'feature_names_in_'):
            expected_features = list(self.model.feature_names_in_)
            actual_features = list(latest_features.columns)
            
            missing_features = [f for f in expected_features if f not in actual_features]
            extra_features = [f for f in actual_features if f not in expected_features]
            
            if missing_features or extra_features:
                print(f"🔧 特征对齐: 模型期望{len(expected_features)}个, 实际生成{len(actual_features)}个")
                if missing_features:
                    print(f"   ➕ 补零({len(missing_features)}个): {missing_features[:5]}{'...' if len(missing_features) > 5 else ''}")
                if extra_features:
                    print(f"   ➖ 丢弃({len(extra_features)}个): {extra_features[:5]}{'...' if len(extra_features) > 5 else ''}")
                
                # 按模型期望的特征顺序构建DataFrame
                aligned = pd.DataFrame(index=latest_features.index)
                for feat in expected_features:
                    if feat in actual_features:
                        aligned[feat] = latest_features[feat].values
                    else:
                        aligned[feat] = 0.0  # 缺失特征补零
                
                return aligned, expected_features
            else:
                # 特征完全匹配，按模型期望顺序排列
                latest_features = latest_features[expected_features]
                return latest_features, expected_features
        
        # 没有feature_names_in_的模型（如旧版sklearn），使用原逻辑
        return latest_features, all_feature_cols
    
    def predict(self, days: int = 50):
        """
        进行预测
        
        参数:
            days: 获取多少天的历史数据
            
        返回:
            预测结果字典
        """
        if self.model is None:
            return {"error": "模型未加载"}
        
        # CSI300/500模型使用专用预测管线
        if self._is_lgb_csi and self._lgb_predictor is not None:
            return self._lgb_predictor.predict(days=days)
        
        # 获取最新数据
        ds = DataSource()
        df = ds.get_kline(self.symbol, num=days)
        
        if df is None or df.empty:
            return {"error": "无法获取数据"}
        
        print(f"📊 获取到{self.symbol}的{len(df)}条数据")
        
        # 准备特征
        features, feature_cols = self.prepare_features(df)
        if features is None:
            return {"error": "特征工程失败"}
        
        print(f"🔧 生成{len(feature_cols)}个特征")
        
        # 准备预测数据
        X = features.values
        
        # 标准化（如果需要）
        if self.scaler is not None:
            X = self.scaler.transform(X)
        
        # 进行预测
        try:
            prediction = self.model.predict(X)[0]
            
            # 获取最新价格
            latest_price = df['close'].iloc[-1]
            
            # 计算预测价格
            predicted_return = prediction
            predicted_price = latest_price * (1 + predicted_return)
            
            result = {
                "symbol": self.symbol,
                "model": self.model_name,
                "model_source": self.model_source,
                "latest_price": float(latest_price),
                "predicted_return": float(predicted_return),
                "predicted_price": float(predicted_price),
                "prediction_date": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data_points": len(df),
                "features_used": len(feature_cols)
            }
            
            print(f"🎯 预测结果:")
            print(f"   最新价格: {latest_price:.2f}")
            print(f"   预测收益率: {predicted_return:.4%}")
            print(f"   预测价格: {predicted_price:.2f}")
            
            return result
        except Exception as e:
            return {"error": f"预测失败: {e}"}
    
    def get_model_info(self):
        """获取模型信息"""
        info = {
            "symbol": self.symbol,
            "model_name": self.model_name,
            "model_source": self.model_source,
            "model_loaded": self.model is not None,
            "scaler_loaded": self.scaler is not None
        }
        
        # 添加模型特征信息
        if self.model is not None and hasattr(self.model, 'feature_names_in_'):
            info["expected_features"] = len(self.model.feature_names_in_)
            info["feature_names"] = list(self.model.feature_names_in_)
        
        # 添加评估信息
        if hasattr(self, '_eval_data') and self._eval_data:
            info["evaluation"] = {
                "r2": self._eval_data.get("r2"),
                "direction_accuracy": self._eval_data.get("direction_accuracy"),
                "mse": self._eval_data.get("mse")
            }
        
        return info


def discover_available_symbols():
    """发现所有可用模型的标的列表"""
    symbols = set()
    
    # 老格式: models/{symbol}/
    models_dir = os.path.join(PROJECT_ROOT, "models")
    if os.path.exists(models_dir):
        for d in os.listdir(models_dir):
            if d.isdigit() and os.path.isdir(os.path.join(models_dir, d)):
                symbols.add(d)
    
    # 新格式: models/ml/*.pkl
    ml_dir = os.path.join(models_dir, "ml")
    if os.path.exists(ml_dir):
        import re
        for f in os.listdir(ml_dir):
            m = re.match(r'^(\d+)_lgb_\w+\.pkl$', f)
            if m:
                symbols.add(m.group(1))
    
    # CSI500: models/ml/csi500/
    csi500_dir = os.path.join(ml_dir, "csi500")
    if os.path.exists(csi500_dir):
        import re
        for f in os.listdir(csi500_dir):
            m = re.match(r'^(\d+)_lgb_csi500\.pkl$', f)
            if m:
                symbols.add(m.group(1))
    
    return sorted(symbols)


def main():
    """主函数"""
    print("=" * 70)
    print("🔮 W7 预测模型 - 使用示例")
    print("=" * 70)
    
    # 发现所有可用标的
    all_symbols = discover_available_symbols()
    print(f"\n📋 可用模型标的: {len(all_symbols)}只")
    print(f"   {', '.join(all_symbols[:20])}{'...' if len(all_symbols) > 20 else ''}")
    
    # 示例：使用600760的随机森林模型进行预测
    symbol = "600760"
    model_name = "random_forest"
    
    print(f"\n📈 使用{symbol}的{model_name}模型进行预测...")
    
    # 初始化预测器
    predictor = Predictor(symbol, model_name)
    
    if predictor.model is None:
        print("❌ 无法加载模型，退出")
        return
    
    # 获取模型信息
    model_info = predictor.get_model_info()
    print(f"\n📋 模型信息:")
    print(f"   股票代码: {model_info['symbol']}")
    print(f"   模型名称: {model_info['model_name']}")
    print(f"   模型来源: {model_info['model_source']}")
    if 'expected_features' in model_info:
        print(f"   期望特征数: {model_info['expected_features']}")
    if 'evaluation' in model_info:
        eval_info = model_info['evaluation']
        print(f"   模型R²: {eval_info['r2']:.4f}")
        print(f"   方向准确率: {eval_info['direction_accuracy']:.2%}")
    
    # 进行预测
    print(f"\n🔮 进行预测...")
    result = predictor.predict(days=100)
    
    if 'error' in result:
        print(f"❌ 预测失败: {result['error']}")
    else:
        print(f"\n✅ 预测完成!")
        print(f"   预测时间: {result['prediction_date']}")
        print(f"   使用数据点: {result['data_points']}")
        print(f"   使用特征数: {result['features_used']}")
    
    print("\n" + "=" * 70)
    print("💡 使用说明:")
    print("=" * 70)
    print("1. 修改symbol和model_name参数使用不同股票和模型")
    print("2. 可用的模型: 'linear_regression', 'random_forest'")
    print("3. 安装xgboost和lightgbm后可使用更多模型")
    print("4. 预测结果为未来1天的收益率预测")
    print("=" * 70)

if __name__ == "__main__":
    main()
