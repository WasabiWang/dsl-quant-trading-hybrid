#!/usr/bin/env python3
"""
预测模型加载器，自动兼容LightGBM和LSTM模型，优先加载效果更好的LGB模型
"""
import os
import sys
import numpy as np
import joblib
from datetime import datetime, timedelta
try:
    import akshare as ak
except ImportError:
    ak = None
from sklearn.preprocessing import MinMaxScaler

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.data_adapter import data_adapter

# 路径配置
MODEL_SAVE_DIR = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/models/ml"
SCALER_SAVE_DIR = f"{MODEL_SAVE_DIR}/scalers"
LOOKBACK_DAYS = 60  # 和训练参数保持一致

class MLPredictor:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
    
    def _init(self):
        self.model_cache = {}  # 模型缓存，避免重复加载
        self.scaler_cache = {} # scaler缓存
    
    def _get_stock_data(self, symbol, days=LOOKBACK_DAYS+10):
        """拉取最近的行情数据用于预测"""
        try:
            # A股代码适配
            if symbol.startswith('6'):
                ak_symbol = f"sh{symbol}"
            else:
                ak_symbol = f"sz{symbol}"
            # 拉取最近的日线数据
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=days+30)).strftime("%Y%m%d")
            df = ak.stock_zh_a_daily(symbol=ak_symbol, start_date=start_date, end_date=end_date, adjust="qfq")
            if len(df) < days:
                return None
            # 计算技术指标因子，和训练时保持一致
            df = self._calculate_technical_factors(df)
            # 取最近LOOKBACK_DAYS的数据
            feature_cols = [
                "open", "close", "high", "low", "volume", "turnover",
                "ma5", "ma10", "ma20", "ma60", "rsi", "macd", "macd_signal",
                "vol_ratio", "bias", "wr", "cci"
            ]
            df = df[feature_cols].astype(float).tail(LOOKBACK_DAYS)
            return df
        except Exception as e:
            print(f"⚠️ 拉取{symbol}预测数据失败: {e}")
            return None
    
    def _calculate_technical_factors(self, df):
        """计算技术指标因子，和训练时保持完全一致"""
        # 均线
        df["ma5"] = df["close"].rolling(window=5).mean()
        df["ma10"] = df["close"].rolling(window=10).mean()
        df["ma20"] = df["close"].rolling(window=20).mean()
        df["ma60"] = df["close"].rolling(window=60).mean()
        
        # RSI指标
        delta = df["close"].diff(1)
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(window=14).mean()
        avg_loss = loss.rolling(window=14).mean()
        rs = avg_gain / avg_loss
        df["rsi"] = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        
        # 量比
        df["vol_ratio"] = df["volume"] / df["volume"].rolling(window=5).mean()
        
        # BIAS乖离率
        df["bias"] = (df["close"] - df["ma20"]) / df["ma20"] * 100
        
        # WR威廉指标
        high = df["high"].rolling(window=14).max()
        low = df["low"].rolling(window=14).min()
        df["wr"] = (high - df["close"]) / (high - low) * 100
        
        # CCI顺势指标
        tp = (df["high"] + df["low"] + df["close"]) / 3
        ma_tp = tp.rolling(window=20).mean()
        md = tp.rolling(window=20).apply(lambda x: np.mean(np.abs(x - x.mean())))
        df["cci"] = (tp - ma_tp) / (0.015 * md)
        
        # 去掉空值
        df = df.dropna()
        return df
    
    def _load_model_and_scaler(self, symbol):
        """加载模型和scaler，优先加载LightGBM模型，没有则返回None"""
        # 先查缓存
        if symbol in self.model_cache and symbol in self.scaler_cache:
            return self.model_cache[symbol], self.scaler_cache[symbol]
        
        # 优先加载高级特征训练的LGB模型（龙虎榜+市场情绪+预测未来3天）
        lgb_advanced_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb_advanced.pkl"
        lgb_advanced_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler_advanced.pkl"
        if os.path.exists(lgb_advanced_model_path) and os.path.exists(lgb_advanced_scaler_path):
            try:
                model = joblib.load(lgb_advanced_model_path)
                scaler = joblib.load(lgb_advanced_scaler_path)
                self.model_cache[symbol] = model
                self.scaler_cache[symbol] = scaler
                print(f"✅ 加载{symbol}的高级特征LightGBM模型成功（预测未来3天）")
                return model, scaler
            except Exception as e:
                print(f"⚠️ 加载高级特征LGB模型失败: {e}")
        
        # 其次加载CSI300行业龙头训练的LGB模型（2026-04-25新增，41只训练，11只达标）
        lgb_csi300_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb_csi300.pkl"
        lgb_csi300_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler_csi300.pkl"
        if os.path.exists(lgb_csi300_model_path) and os.path.exists(lgb_csi300_scaler_path):
            try:
                model = joblib.load(lgb_csi300_model_path)
                scaler = joblib.load(lgb_csi300_scaler_path)
                self.model_cache[symbol] = model
                self.scaler_cache[symbol] = scaler
                print(f"✅ 加载{symbol}的CSI300行业龙头LightGBM模型成功")
                return model, scaler
            except Exception as e:
                print(f"⚠️ 加载CSI300 LGB模型失败: {e}")
        
        # 其次加载本地真实数据训练的LGB模型
        lgb_local_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb_local.pkl"
        lgb_local_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler_local.pkl"
        if os.path.exists(lgb_local_model_path) and os.path.exists(lgb_local_scaler_path):
            try:
                model = joblib.load(lgb_local_model_path)
                scaler = joblib.load(lgb_local_scaler_path)
                self.model_cache[symbol] = model
                self.scaler_cache[symbol] = scaler
                print(f"✅ 加载{symbol}的本地数据LightGBM模型成功")
                return model, scaler
            except Exception as e:
                print(f"⚠️ 加载本地数据LGB模型失败: {e}")
        
        # 其次加载增强版LGB模型
        lgb_enhanced_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb_enhanced.pkl"
        lgb_enhanced_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler_enhanced.pkl"
        if os.path.exists(lgb_enhanced_model_path) and os.path.exists(lgb_enhanced_scaler_path):
            try:
                model = joblib.load(lgb_enhanced_model_path)
                scaler = joblib.load(lgb_enhanced_scaler_path)
                self.model_cache[symbol] = model
                self.scaler_cache[symbol] = scaler
                print(f"✅ 加载{symbol}的增强版LightGBM模型成功")
                return model, scaler
            except Exception as e:
                print(f"⚠️ 加载增强版LGB模型失败: {e}")
        
        # 其次加载普通LGB模型
        lgb_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb.pkl"
        lgb_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler.pkl"
        if os.path.exists(lgb_model_path) and os.path.exists(lgb_scaler_path):
            try:
                model = joblib.load(lgb_model_path)
                scaler = joblib.load(lgb_scaler_path)
                self.model_cache[symbol] = model
                self.scaler_cache[symbol] = scaler
                print(f"✅ 加载{symbol}的LightGBM模型成功")
                return model, scaler
            except Exception as e:
                print(f"⚠️ 加载LGB模型失败: {e}")
        
        # 兼容旧的LSTM模型，如果有的话
        lstm_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lstm.h5"
        lstm_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler.pkl"
        if os.path.exists(lstm_model_path) and os.path.exists(lstm_scaler_path):
            try:
                # 动态导入TensorFlow，避免不必要的依赖
                from tensorflow.keras.models import load_model
                model = load_model(lstm_model_path, compile=False)
                scaler = joblib.load(lstm_scaler_path)
                self.model_cache[symbol] = model
                self.scaler_cache[symbol] = scaler
                print(f"✅ 加载{symbol}的LSTM模型成功")
                return model, scaler
            except Exception as e:
                print(f"⚠️ 加载LSTM模型失败: {e}")
        
        # 没有模型返回None
        return None, None
    
    def predict_return(self, symbol):
        """预测个股未来1日收益率，返回0表示没有模型，用默认值"""
        model, scaler = self._load_model_and_scaler(symbol)
        if model is None or scaler is None:
            return 0.0
        
        try:
            # 获取最近的行情数据
            df = self._get_stock_data(symbol)
            if df is None:
                return 0.0
            
            # 预处理数据
            features = df.values
            features_scaled = scaler.fit_transform(features)
            
            # 展平成LGB需要的一维向量
            X_pred = features_scaled.flatten().reshape(1, -1)
            
            # 预测
            y_pred_scaled = model.predict(X_pred).reshape(-1, 1)
            pred_return = scaler.inverse_transform(y_pred_scaled)[0][0]
            
            return round(float(pred_return), 4)
        except Exception as e:
            print(f"⚠️ 预测{symbol}收益率失败: {e}")
            return 0.0
    
    def get_prediction_score(self, symbol):
        """获取预测得分（0-10分），用于Alpha得分加权"""
        pred_return = self.predict_return(symbol)
        # 收益率映射到0-10分：涨幅3%对应10分，跌幅3%对应0分
        score = min(10, max(0, (pred_return * 100 / 3) * 5 + 5))
        return round(score, 2)

# 全局实例
ml_predictor = MLPredictor()
