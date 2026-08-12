#!/usr/bin/env python3
"""
高级特征训练脚本：包含龙虎榜、市场情绪特征，预测未来3天涨跌
"""
import os
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from sklearn.preprocessing import MinMaxScaler
import lightgbm as lgb
from lightgbm import LGBMRegressor
import joblib
import akshare as ak
import ssl

# 导入增强版龙虎榜特征处理
from lhb_feature_enhanced import add_lhb_features

# 禁用SSL验证
ssl._create_default_https_context = ssl._create_unverified_context

# 路径配置
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(PROJECT_ROOT)
DATA_DIR = f"{PROJECT_ROOT}/data/history"
MODEL_SAVE_DIR = f"{PROJECT_ROOT}/models/ml"
SCALER_SAVE_DIR = f"{MODEL_SAVE_DIR}/scalers"
TRAIN_LOG_DIR = f"{PROJECT_ROOT}/logs/train"
os.makedirs(MODEL_SAVE_DIR, exist_ok=True)
os.makedirs(SCALER_SAVE_DIR, exist_ok=True)
os.makedirs(TRAIN_LOG_DIR, exist_ok=True)

# 训练参数配置
LOOKBACK_DAYS = 60  # 用过去60天数据预测未来
PREDICT_DAYS = 3    # 预测未来3天涨跌
TEST_RATIO = 0.2    # 测试集比例20%
MIN_ACCURACY_THRESHOLD = 0.54  # 最低准确率要求≥54%（优先级1方案目标）
N_ENSEMBLE_MODELS = 5  # 集成模型数量：5个不同参数的模型做Bagging集成

# 全局变量
train_results = []

def get_stock_pool():
    """获取待训练的股票池（完整6只，使用龙虎榜特征）"""
    return [
        {"symbol": "600760", "market": "sh", "name": "中航沈飞"},
        {"symbol": "000001", "market": "sz", "name": "平安银行"},
        {"symbol": "002594", "market": "sz", "name": "比亚迪"},
        {"symbol": "600519", "market": "sh", "name": "贵州茅台"},
        {"symbol": "000858", "market": "sz", "name": "五粮液"},
        {"symbol": "600036", "market": "sh", "name": "招商银行"}
    ]

def load_local_data(symbol, market):
    """从本地CSV加载历史数据"""
    csv_path = f"{DATA_DIR}/{symbol}_{market}_history.csv"
    if not os.path.exists(csv_path):
        print(f"❌ 本地数据文件不存在: {csv_path}")
        return None
    
    try:
        df = pd.read_csv(csv_path)
        print(f"✅ 加载{symbol}本地数据成功，共{len(df)}条")
        
        # 确保数据按日期排序
        df['date'] = pd.to_datetime(df['date'])
        df = df.sort_values('date')
        
        # 设置日期索引
        df = df.set_index('date')
        
        return df
    except Exception as e:
        print(f"❌ 加载{symbol}本地数据失败: {e}")
        return None

def add_lhb_features(df, symbol):
    """添加龙虎榜特征（修复SSL错误和列名问题）"""
    try:
        # 获取最近2年的龙虎榜数据
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=2*365)).strftime("%Y%m%d")
        
        # 尝试获取龙虎榜数据，添加错误处理和重试
        lhb_df = None
        max_retries = 3
        
        for attempt in range(max_retries):
            try:
                # 禁用SSL验证（解决SSL错误）
                import ssl
                ssl._create_default_https_context = ssl._create_unverified_context
                
                lhb_df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
                
                if lhb_df is not None and len(lhb_df) > 0:
                    break
                else:
                    print(f"⚠️  第{attempt+1}次尝试：龙虎榜数据为空")
            except Exception as e:
                print(f"⚠️  第{attempt+1}次尝试失败: {e}")
                if attempt < max_retries - 1:
                    import time
                    time.sleep(2)  # 等待2秒后重试
                continue
        
        if lhb_df is not None and len(lhb_df) > 0:
            print(f"✅ 获取龙虎榜数据成功，共{len(lhb_df)}条")
            
            # 查看实际列名
            print(f"   实际列名: {list(lhb_df.columns)}")
            
            # 尝试不同的日期列名（兼容不同版本的akshare）
            date_column = None
            code_column = None
            buy_column = None
            sell_column = None
            net_column = None
            
            # 查找可能的列名（根据实际返回的列名调整）
            for col in lhb_df.columns:
                col_lower = col.lower()
                if '日期' in col or 'date' in col_lower or 'time' in col_lower or '上榜日' in col or 'trade_date' in col_lower:
                    date_column = col
                if '代码' in col or 'symbol' in col_lower or 'code' in col_lower:
                    code_column = col
                if '买入' in col or 'buy' in col_lower or '龙虎榜买入额' in col:
                    buy_column = col
                if '卖出' in col or 'sell' in col_lower or '龙虎榜卖出额' in col:
                    sell_column = col
                if '净额' in col or 'net' in col_lower or '龙虎榜净买额' in col:
                    net_column = col
            
            print(f"   识别列名: 日期={date_column}, 代码={code_column}, 买入={buy_column}, 卖出={sell_column}, 净额={net_column}")
            
            if date_column and code_column:
                # 转换日期列
                lhb_df[date_column] = pd.to_datetime(lhb_df[date_column])
                
                # 筛选该股票的龙虎榜数据
                stock_lhb = lhb_df[lhb_df[code_column] == symbol]
                
                if len(stock_lhb) > 0:
                    # 按日期聚合
                    agg_dict = {}
                    if buy_column:
                        agg_dict[buy_column] = 'sum'
                    if sell_column:
                        agg_dict[sell_column] = 'sum'
                    if net_column:
                        agg_dict[net_column] = 'sum'
                    
                    # 添加计数
                    agg_dict[date_column] = 'count'
                    
                    lhb_by_date = stock_lhb.groupby(date_column).agg(agg_dict)
                    
                    # 重命名列
                    rename_dict = {}
                    if buy_column:
                        rename_dict[buy_column] = 'lhb_buy_amount'
                    if sell_column:
                        rename_dict[sell_column] = 'lhb_sell_amount'
                    if net_column:
                        rename_dict[net_column] = 'lhb_net_amount'
                    rename_dict[date_column] = 'lhb_count'
                    
                    lhb_by_date = lhb_by_date.rename(columns=rename_dict)
                    
                    # 合并到主数据
                    df = df.join(lhb_by_date, how='left')
                    
                    print(f"✅ 加入龙虎榜特征，共{len(stock_lhb)}条记录")
                else:
                    print(f"⚠️  股票{symbol}无龙虎榜记录")
            else:
                print(f"⚠️  无法识别必要的列名")
        else:
            print(f"⚠️  龙虎榜数据获取失败或为空")
    except Exception as e:
        print(f"⚠️  添加龙虎榜特征失败: {e}")
    
    # 填充缺失值
    lhb_cols = ['lhb_buy_amount', 'lhb_sell_amount', 'lhb_net_amount', 'lhb_count']
    for col in lhb_cols:
        if col not in df.columns:
            df[col] = 0
    
    # 计算衍生指标（确保有数据）
    if 'lhb_net_amount' in df.columns and 'lhb_buy_amount' in df.columns and 'lhb_sell_amount' in df.columns:
        df['lhb_net_ratio'] = df['lhb_net_amount'] / (df['volume'] * df['close']).replace(0, 1)
        df['lhb_buy_sell_ratio'] = df['lhb_buy_amount'] / df['lhb_sell_amount'].replace(0, 1)
    else:
        df['lhb_net_ratio'] = 0
        df['lhb_buy_sell_ratio'] = 0
    
    return df

def add_market_sentiment_features(df, symbol, market):
    """添加市场情绪特征"""
    try:
        # 加载大盘指数数据作为市场情绪代理
        index_symbol = "000300" if market == "sh" else "399001"
        index_market = "sh" if market == "sh" else "sz"
        index_path = f"{DATA_DIR}/sector_{index_symbol}_history.csv"
        
        if os.path.exists(index_path):
            index_df = pd.read_csv(index_path)
            index_df['date'] = pd.to_datetime(index_df['date'])
            index_df = index_df.set_index('date')
            
            # 计算市场情绪指标
            if 'pctChg' in index_df.columns:
                # 市场涨跌幅
                df['market_return'] = index_df['pctChg']
                
                # 市场波动率
                df['market_volatility_5'] = index_df['pctChg'].rolling(window=5).std()
                df['market_volatility_10'] = index_df['pctChg'].rolling(window=10).std()
                
                # 市场动量
                if 'close' in index_df.columns:
                    df['market_momentum_5'] = index_df['close'].pct_change(periods=5)
                    df['market_momentum_10'] = index_df['close'].pct_change(periods=10)
                
                print(f"✅ 加入市场情绪特征（基于{index_symbol}指数）")
            else:
                print(f"⚠️  指数数据缺少pctChg列")
        else:
            print(f"⚠️  指数数据文件不存在: {index_path}")
    except Exception as e:
        print(f"⚠️  添加市场情绪特征失败: {e}")
    
    # 填充缺失值
    sentiment_cols = ['market_return', 'market_volatility_5', 'market_volatility_10',
                     'market_momentum_5', 'market_momentum_10']
    for col in sentiment_cols:
        if col not in df.columns:
            df[col] = 0
    
    return df

def calculate_technical_indicators(df):
    """计算技术指标"""
    # 确保有必要的列
    required_cols = ['open', 'high', 'low', 'close', 'volume', 'turn', 'pctChg']
    for col in required_cols:
        if col not in df.columns:
            print(f"⚠️ 缺少列{col}，使用默认值")
            df[col] = 0
    
    # 1. 波动率指标
    df['volatility_5'] = df['pctChg'].rolling(window=5).std()
    df['volatility_10'] = df['pctChg'].rolling(window=10).std()
    df['volatility_20'] = df['pctChg'].rolling(window=20).std()
    
    # 2. 动量指标
    df['momentum_5'] = df['close'].pct_change(periods=5)
    df['momentum_10'] = df['close'].pct_change(periods=10)
    df['momentum_20'] = df['close'].pct_change(periods=20)
    
    # 3. 成交量指标
    df['volume_ma5'] = df['volume'].rolling(window=5).mean()
    df['volume_ma10'] = df['volume'].rolling(window=10).mean()
    df['volume_ratio'] = df['volume'] / df['volume_ma5'].replace(0, 1)
    
    # 4. RSI指标
    df['rsi'] = calculate_rsi(df['close'], period=14)
    
    # 5. MACD指标
    macd, signal, hist = calculate_macd(df['close'])
    df['macd'] = macd
    df['macd_signal'] = signal
    df['macd_hist'] = hist
    
    # 6. 布林带
    df['bb_middle'] = df['close'].rolling(window=20).mean()
    bb_std = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_middle'] + 2 * bb_std
    df['bb_lower'] = df['bb_middle'] - 2 * bb_std
    df['bb_width'] = (df['bb_upper'] - df['bb_lower']) / df['bb_middle'].replace(0, 1)
    
    # 7. 价格位置
    df['price_position'] = (df['close'] - df['low'].rolling(window=20).min()) / \
                          (df['high'].rolling(window=20).max() - df['low'].rolling(window=20).min()).replace(0, 1)
    
    # 填充NaN
    df = df.ffill().fillna(0)
    
    return df

def calculate_rsi(prices, period=14):
    """计算RSI指标"""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    
    rs = avg_gain / avg_loss.replace(0, 1)
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(prices, fast=12, slow=26, signal=9):
    """计算MACD指标"""
    exp1 = prices.ewm(span=fast, adjust=False).mean()
    exp2 = prices.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    macd_signal = macd.ewm(span=signal, adjust=False).mean()
    macd_hist = macd - macd_signal
    return macd, macd_signal, macd_hist

def add_time_series_stat_features(df):
    """添加增强时序统计特征（优先级1方案核心新增）
    生成滑动窗口3/7/15/30/60天的统计特征，补足时序依赖信息
    """
    # 选择需要统计的核心特征列
    stat_columns = [
        'close', 'volume', 'pctChg', 'turn',
        'lhb_net_amount', 'lhb_net_ratio', 'lhb_count',
        'volatility_5', 'momentum_5', 'rsi', 'macd_hist',
        'market_return', 'market_momentum_5'
    ]
    
    # 要计算的统计窗口
    windows = [3,7,15,30,60]
    
    # 要计算的统计指标
    stats = [
        ('mean', lambda x: x.mean()),
        ('std', lambda x: x.std()),
        ('max', lambda x: x.max()),
        ('min', lambda x: x.min()),
        ('slope', lambda x: np.polyfit(range(len(x)), x.values, 1)[0] if len(x.dropna()) >= 3 and not np.any(np.isinf(x.values)) and not np.all(x.values == 0) else 0),
        ('pct_change', lambda x: x.pct_change(len(x)-1).iloc[-1] if len(x.dropna()) >= 2 else 0)
    ]
    
    print(f"🔧 开始生成增强时序统计特征，共{len(stat_columns)}列 × {len(windows)}窗口 × {len(stats)}指标 = {len(stat_columns)*len(windows)*len(stats)}个新特征...")
    
    for col in stat_columns:
        if col not in df.columns:
            continue
            
        for window in windows:
            rolling = df[col].rolling(window=window, min_periods=int(window/2))
            
            for stat_name, stat_func in stats:
                feature_name = f"{col}_w{window}_{stat_name}"
                df[feature_name] = rolling.apply(stat_func)
    
    # 填充缺失值和无穷大值
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.ffill().fillna(0)
    print(f"✅ 增强时序特征生成完成，总特征数现在为{len(df.columns)}个")
    return df

def preprocess_data(df):
    """数据预处理：构建训练样本、归一化"""
    try:
        # 构建标签：未来PREDICT_DAYS天的累计涨跌幅
        df["label"] = df["close"].pct_change(periods=PREDICT_DAYS).shift(-PREDICT_DAYS)
        
        # 删除最后PREDICT_DAYS行（无标签）
        df = df.dropna()
        if len(df) < LOOKBACK_DAYS + PREDICT_DAYS + 10:
            return None, None, None, None, None
        
        # 选择特征列（排除非数值列和标签列）
        exclude_cols = ['code', 'label']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
        features = df[feature_cols].values
        labels = df["label"].values.reshape(-1, 1)
        
        # 特征归一化
        scaler = MinMaxScaler(feature_range=(0, 1))
        features_scaled = scaler.fit_transform(features)
        labels_scaled = scaler.fit_transform(labels)
        
        # 构建时间序列样本：把过去LOOKBACK_DAYS的特征展平为一维
        X, y = [], []
        for i in range(LOOKBACK_DAYS, len(features_scaled)):
            X.append(features_scaled[i-LOOKBACK_DAYS:i, :].flatten())
            y.append(labels_scaled[i, 0])
        
        X = np.array(X)
        y = np.array(y)
        
        # 划分训练集和测试集（时序划分，不打乱）
        split_idx = int(len(X) * (1 - TEST_RATIO))
        X_train, X_test = X[:split_idx], X[split_idx:]
        y_train, y_test = y[:split_idx], y[split_idx:]
        
        print(f"✅ 数据预处理完成，训练集{len(X_train)}条，测试集{len(X_test)}条，特征维度{X_train.shape[1]}")
        return X_train, X_test, y_train, y_test, scaler
    except Exception as e:
        print(f"❌ 数据预处理失败: {e}")
        return None, None, None, None, None

def build_ensemble_models():
    """构建N个不同参数的LightGBM模型用于Bagging集成（优先级1方案核心）
    每个模型使用不同的参数和随机种子，降低过拟合风险，提升泛化能力
    """
    models = []
    
    # 不同的参数组合，增加模型多样性
    params_list = [
        # 基础参数
        {"n_estimators": 300, "learning_rate": 0.03, "max_depth": 7, "num_leaves": 50, "subsample": 0.7, "colsample_bytree": 0.7, "reg_alpha": 0.1, "reg_lambda": 0.1, "random_state": 2024},
        # 更深的树，更低学习率
        {"n_estimators": 400, "learning_rate": 0.02, "max_depth": 9, "num_leaves": 80, "subsample": 0.8, "colsample_bytree": 0.8, "reg_alpha": 0.2, "reg_lambda": 0.2, "random_state": 2025},
        # 更浅的树，更高学习率
        {"n_estimators": 250, "learning_rate": 0.05, "max_depth": 5, "num_leaves": 30, "subsample": 0.6, "colsample_bytree": 0.6, "reg_alpha": 0.05, "reg_lambda": 0.05, "random_state": 2026},
        # 特征采样更少
        {"n_estimators": 350, "learning_rate": 0.025, "max_depth": 7, "num_leaves": 50, "subsample": 0.8, "colsample_bytree": 0.5, "reg_alpha": 0.15, "reg_lambda": 0.15, "random_state": 2027},
        # 样本采样更少
        {"n_estimators": 300, "learning_rate": 0.03, "max_depth": 8, "num_leaves": 60, "subsample": 0.5, "colsample_bytree": 0.8, "reg_alpha": 0.1, "reg_lambda": 0.2, "random_state": 2028}
    ]
    
    # 只取前N_ENSEMBLE_MODELS个
    for params in params_list[:N_ENSEMBLE_MODELS]:
        model = LGBMRegressor(
            boosting_type="gbdt",
            objective="regression",
            metric="mse",
            verbose=0,
            **params
        )
        models.append(model)
    
    print(f"✅ 构建了{len(models)}个不同参数的LightGBM模型用于集成")
    return models

def evaluate_ensemble_models(models, scaler, X_test, y_test):
    """评估集成模型效果，计算涨跌方向准确率（优先级1方案核心）
    用多个模型的预测结果取平均值作为最终预测，降低过拟合，提升准确率
    """
    try:
        # 所有模型分别预测，然后取平均
        y_pred_scaled_list = []
        for model in models:
            y_pred_scaled = model.predict(X_test).reshape(-1, 1)
            y_pred_scaled_list.append(y_pred_scaled)
        
        # 集成预测：简单平均
        y_pred_scaled = np.mean(np.array(y_pred_scaled_list), axis=0)
        y_pred = scaler.inverse_transform(y_pred_scaled).flatten()
        y_true = scaler.inverse_transform(y_test.reshape(-1, 1)).flatten()
        
        # 计算涨跌方向准确率：预测和真实值同号即正确
        direction_correct = np.sum((y_pred > 0) == (y_true > 0))
        direction_accuracy = direction_correct / len(y_true)
        
        # 计算平均绝对误差（MAE）
        mae = np.mean(np.abs(y_pred - y_true)) * 100  # 转成百分比
        
        print(f"✅ 集成模型评估完成：涨跌方向准确率{direction_accuracy:.2%}，平均预测误差{mae:.2f}%")
        return round(direction_accuracy, 4), round(mae, 4)
    except Exception as e:
        print(f"❌ 集成模型评估失败: {e}")
        return 0, 0

def save_ensemble_models(symbol, models, scaler):
    """保存训练好的集成模型列表和scaler"""
    try:
        # 保存集成模型列表
        model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb_ensemble.pkl"
        joblib.dump(models, model_path)
        # 保存scaler
        scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler_ensemble.pkl"
        joblib.dump(scaler, scaler_path)
        # 同时兼容旧版本路径，确保盘前脚本可以直接加载
        old_model_path = f"{MODEL_SAVE_DIR}/{symbol}_lgb_advanced.pkl"
        old_scaler_path = f"{SCALER_SAVE_DIR}/{symbol}_scaler_advanced.pkl"
        joblib.dump(models, old_model_path)
        joblib.dump(scaler, old_scaler_path)
        print(f"✅ 集成模型已保存到{model_path}（同时兼容旧版本路径）")
        return True
    except Exception as e:
        print(f"❌ 保存集成模型失败: {e}")
        return False

def train_single_stock(stock):
    """训练单只股票的高级特征LightGBM模型"""
    symbol = stock["symbol"]
    market = stock["market"]
    name = stock["name"]
    print(f"\n{'='*70}")
    print(f"🤖 开始训练{name}({symbol})的高级特征LightGBM模型（预测未来{PREDICT_DAYS}天）")
    print(f"{'='*70}")
    
    # 1. 加载本地数据
    df = load_local_data(symbol, market)
    if df is None:
        train_results.append({"symbol": symbol, "name": name, "status": "failed", "accuracy": 0, "mae": 0})
        return
    
    # 2. 添加高级特征（含优先级1新增的增强时序统计特征）
    df = calculate_technical_indicators(df)
    df = add_lhb_features(df, symbol)
    df = add_market_sentiment_features(df, symbol, market)
    df = add_time_series_stat_features(df)  # 优先级1核心新增：200+时序统计特征
    
    # 3. 数据预处理
    X_train, X_test, y_train, y_test, scaler = preprocess_data(df)
    if X_train is None:
        train_results.append({"symbol": symbol, "name": name, "status": "failed", "accuracy": 0, "mae": 0})
        return
    
    # 4. 构建并训练集成模型（优先级1核心新增：5个不同参数模型的Bagging集成）
    print(f"🏋️ 开始训练集成模型，共{N_ENSEMBLE_MODELS}个模型，特征数={X_train.shape[1]}...")
    try:
        models = build_ensemble_models()
        trained_models = []
        for i, model in enumerate(models):
            print(f"   训练第{i+1}/{N_ENSEMBLE_MODELS}个模型...")
            model.fit(
                X_train, y_train, 
                eval_set=[(X_test, y_test)], 
                callbacks=[lgb.early_stopping(stopping_rounds=20, verbose=0)]
            )
            trained_models.append(model)
    except Exception as e:
        print(f"❌ 集成模型训练失败: {e}")
        train_results.append({"symbol": symbol, "name": name, "status": "failed", "accuracy": 0, "mae": 0})
        return
    
    # 5. 评估集成模型
    accuracy, mae = evaluate_ensemble_models(trained_models, scaler, X_test, y_test)
    
    # 6. 保存模型（达标才保存，不达标用默认值）
    if accuracy >= MIN_ACCURACY_THRESHOLD:
        save_success = save_ensemble_models(symbol, trained_models, scaler)
        status = "success" if save_success else "save_failed"
    else:
        print(f"⚠️ 集成模型准确率{accuracy:.2%}低于要求的{MIN_ACCURACY_THRESHOLD*100:.1f}%，不保存，后续继续优化")
        status = "accuracy_not_enough"
    
    # 记录结果
    train_results.append({"symbol": symbol, "name": name, "status": status, "accuracy": accuracy, "mae": mae})

def generate_train_report():
    """生成训练总结报告"""
    print(f"\n{'='*70}")
    print(f"📊 训练结果总览")
    print(f"{'='*70}")
    
    total = len(train_results)
    success = len([r for r in train_results if r["status"] == "success"])
    failed = len([r for r in train_results if r["status"] == "failed"])
    skipped = len([r for r in train_results if r["status"] == "skipped"])
    acc_not_enough = len([r for r in train_results if r["status"] == "accuracy_not_enough"])
    
    print(f"总训练数：{total}只")
    print(f"训练成功（达标）：{success}只")
    print(f"准确率不达标：{acc_not_enough}只")
    print(f"训练失败：{failed}只")
    print(f"跳过（已训练）：{skipped}只")
    
    if success > 0:
        avg_acc = np.mean([r["accuracy"] for r in train_results if r["status"] == "success"]) * 100
        avg_mae = np.mean([r["mae"] for r in train_results if r["status"] == "success"])
        print(f"✅ 达标模型平均准确率：{avg_acc:.2f}%，平均预测误差：{avg_mae:.2f}%")
    
    # 单只详情
    print(f"\n📋 单只详情：")
    for res in train_results:
        status_icon = {"success": "✅", "failed": "❌", "skipped": "ℹ️", "accuracy_not_enough": "⚠️"}.get(res["status"], "❓")
        if res["accuracy"] > 0:
            acc_str = f"{res['accuracy']*100:.1f}%"
        else:
            acc_str = "-"
        if res["mae"] > 0:
            mae_str = f"{res['mae']:.2f}%"
        else:
            mae_str = "-"
        print(f"{status_icon} {res['name']}({res['symbol']})：{res['status']}，准确率{acc_str}，误差{mae_str}")
    
    # 保存报告到本地
    report = {
        "train_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"lookback_days": LOOKBACK_DAYS, "predict_days": PREDICT_DAYS, "n_estimators": 300, "min_accuracy": MIN_ACCURACY_THRESHOLD},
        "summary": {"total": total, "success": success, "failed": failed, "skipped": skipped, "acc_not_enough": acc_not_enough},
        "details": train_results
    }
    report_path = f"{TRAIN_LOG_DIR}/train_report_advanced_{datetime.now().strftime('%Y%m%d%H%M')}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    
    print(f"\n✅ 训练报告已保存到{report_path}")
    return report

def main():
    """主函数"""
    print(f"🚀 开始高级特征训练：龙虎榜 + 市场情绪 + 预测未来{PREDICT_DAYS}天涨跌")
    stock_pool = get_stock_pool()
    print(f"📋 待训练股票共{len(stock_pool)}只：{','.join([s['name'] for s in stock_pool])}")
    
    # 逐个训练
    for stock in stock_pool:
        train_single_stock(stock)
    
    # 生成报告
    report = generate_train_report()
    
    print(f"\n🎉 全部训练任务完成！")
    print(f"👉 达标模型已经自动保存，今晚21:00的盘前预案就会自动加载使用高级特征预测结果")
    return report

if __name__ == "__main__":
    main()