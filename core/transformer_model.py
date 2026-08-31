#!/usr/bin/env python3
"""
core/transformer_model.py — DSL v4.6.5 时序Transformer预测模型

轻量级 PyTorch Transformer Encoder, 用于5日收益率预测。
与 LightGBM/CatBoost/XGBoost 并行训练, 集成投票增强精度。

架构:
  Input: (seq_len, n_features) 时序特征窗口
  → PositionalEncoding (正弦编码)
  → TransformerEncoder (2层, 4头注意力)
  → GlobalAvgPool + Linear → 单标量输出
  → 回归损失: HuberLoss (对异常值鲁棒) + 方向损失加权

训练策略:
  - CPU优先(无GPU依赖), 批次64, epoch=30
  - Early stopping patience=5
  - 学习率 warmup + cosine decay
  - 输出集成: Transformer + LGB/XGB/CB → 加权预测
"""
import os, sys, json, warnings, gc
from datetime import datetime
import numpy as np
import pandas as pd

# v4.7.3修复: 同进程内 LightGBM(homebrew libomp) 与 torch(自带libomp.dylib)
# 双OpenMP runtime冲突 → 间歇性barrier死锁(0% CPU挂起, 采样栈kmp_flag_64::wait)。
# 官方workaround: 允许重复runtime + 限制torch线程数, 降低死锁窗口。
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("OMP_NUM_THREADS", "4")

warnings.filterwarnings("ignore")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

HAS_TORCH = False
try:
    import torch
    import torch.nn as nn
    import torch.optim as optim
    from torch.utils.data import DataLoader, TensorDataset
    # v4.7.3修复: 限线程防与LightGBM的OpenMP运行时互锁死锁 (CPU友好: 2线程足够)
    try:
        torch.set_num_threads(1)  # v4.7.3: team=1绕过libomp双runtime barrier死锁
    except Exception:
        pass
    HAS_TORCH = True
except ImportError:
    print("⚠️ PyTorch未安装, Transformer模型不可用. pip install torch")


# ====== 模型参数 ======
SEQ_LEN = 20          # 输入序列长度 (20个交易日)
D_MODEL = 64          # 嵌入维度
NHEAD = 4             # 注意力头数
NUM_LAYERS = 2        # Transformer层数
DIM_FEEDFORWARD = 128 # FFN隐藏层大小
DROPOUT = 0.1         # Dropout率
BATCH_SIZE = 64       # 训练批次
EPOCHS = 30           # 最大训练轮数
PATIENCE = 5          # Early stopping
LEARNING_RATE = 1e-3  # 初始学习率
WEIGHT_DECAY = 1e-4   # L2正则化
DIR_LOSS_WEIGHT = 0.3 # 方向损失权重


class PositionalEncoding(nn.Module):
    """正弦位置编码"""
    def __init__(self, d_model: int, max_len: int = 500):
        super().__init__()
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float()
                            * (-np.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe.unsqueeze(0))

    def forward(self, x):
        return x + self.pe[:, :x.size(1), :]


class TimeSeriesTransformer(nn.Module):
    """时序Transformer预测器
    
    Input:  (batch, seq_len, n_features)
    Output: (batch, 1) 预测5日收益率标量
    """
    def __init__(self, n_features: int, d_model: int = D_MODEL,
                 nhead: int = NHEAD, num_layers: int = NUM_LAYERS,
                 dim_feedforward: int = DIM_FEEDFORWARD, dropout: float = DROPOUT):
        super().__init__()
        self.n_features = n_features
        self.d_model = d_model
        
        # 输入投影
        self.input_proj = nn.Linear(n_features, d_model)
        self.pos_encoder = PositionalEncoding(d_model)
        
        # Transformer Encoder
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=dim_feedforward,
            dropout=dropout, batch_first=True, activation='gelu'
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        
        # 输出头
        self.output_proj = nn.Sequential(
            nn.Linear(d_model, d_model // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_model // 2, 1),
        )
    
    def forward(self, x):
        # x: (batch, seq_len, n_features)
        x = self.input_proj(x)           # → (batch, seq_len, d_model)
        x = self.pos_encoder(x)
        x = self.transformer(x)          # → (batch, seq_len, d_model)
        x = x.mean(dim=1)                # Global average pooling
        x = self.output_proj(x)          # → (batch, 1)
        return x.squeeze(-1)


def _prepare_sequences(features: np.ndarray, targets: np.ndarray,
                       seq_len: int = SEQ_LEN) -> tuple:
    """将特征矩阵转换为时序序列
    
    Args:
        features: (n_samples, n_features) 特征矩阵
        targets: (n_samples,) 目标值
        seq_len: 序列长度
    
    Returns:
        X: (n_samples - seq_len + 1, seq_len, n_features)
        y: (n_samples - seq_len + 1,)
    """
    n_samples, n_features = features.shape
    if n_samples <= seq_len:
        return None, None
    
    X_seqs = []
    y_seqs = []
    for i in range(n_samples - seq_len + 1):
        X_seqs.append(features[i:i+seq_len])
        y_seqs.append(targets[i+seq_len-1])
    
    return np.array(X_seqs), np.array(y_seqs)


class DirectionLoss(nn.Module):
    """方向一致性损失：预测方向与真实方向不一致时惩罚"""
    def __init__(self, weight: float = DIR_LOSS_WEIGHT):
        super().__init__()
        self.weight = weight
    
    def forward(self, pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        pred_sign = torch.sign(pred)
        target_sign = torch.sign(target)
        mismatch = (pred_sign != target_sign).float()
        return self.weight * mismatch.mean()


def train_transformer(features_df: pd.DataFrame, target_col: str = "target",
                      model_dir: str = None, code: str = "",
                      seq_len: int = SEQ_LEN, epochs: int = EPOCHS,
                      batch_size: int = BATCH_SIZE, patience: int = PATIENCE,
                      verbose: bool = True) -> dict:
    """训练Transformer模型（时序窗口）
    
    Args:
        features_df: 含target列的特征DataFrame
        target_col: 目标列名
        model_dir: 模型保存目录
        code: 股票代码(日志用)
    
    Returns:
        {"model": nn.Module, "scaler": None, "direction_accuracy": float,
         "mse": float, "n_samples": int, "n_features": int}
    """
    if not HAS_TORCH:
        return None
    
    # 分离特征和目标
    feature_cols = [c for c in features_df.columns if c != target_col and not c.startswith("sent__")]
    if not feature_cols:
        return None
    
    X_raw = features_df[feature_cols].values.astype(np.float32)
    y_raw = features_df[target_col].values.astype(np.float32)
    
    # Drop NaN
    mask = ~(np.isnan(X_raw).any(axis=1) | np.isnan(y_raw))
    X_raw, y_raw = X_raw[mask], y_raw[mask]
    
    if len(X_raw) < seq_len + 50:
        if verbose:
            print(f"  ⚠️ Transformer需要≥{seq_len+50}样本, 当前{len(X_raw)}")
        return None
    
    n_features = X_raw.shape[1]
    
    # 标准化
    from sklearn.preprocessing import StandardScaler
    scaler = StandardScaler()
    X_norm = scaler.fit_transform(X_raw)
    
    # 时序切分
    X_seq, y_seq = _prepare_sequences(X_norm, y_raw, seq_len)
    if X_seq is None:
        return None
    
    n_total = len(X_seq)
    n_train = int(n_total * 0.75)
    n_val = int(n_total * 0.10)
    
    X_train, y_train = X_seq[:n_train], y_seq[:n_train]
    X_val, y_val = X_seq[n_train:n_train+n_val], y_seq[n_train:n_train+n_val]
    X_test, y_test = X_seq[n_train+n_val:], y_seq[n_train+n_val:]
    
    if len(X_train) < batch_size:
        return None
    
    # 创建模型
    model = TimeSeriesTransformer(n_features=n_features)
    device = torch.device('cpu')  # CPU优先
    model.to(device)
    
    # 数据加载器
    train_dataset = TensorDataset(torch.FloatTensor(X_train), torch.FloatTensor(y_train))
    val_dataset = TensorDataset(torch.FloatTensor(X_val), torch.FloatTensor(y_val))
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size * 2)
    
    # 优化器和损失
    optimizer = optim.AdamW(model.parameters(), lr=LEARNING_RATE,
                           weight_decay=WEIGHT_DECAY)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)
    huber = nn.HuberLoss(delta=0.1)
    dir_loss_fn = DirectionLoss(weight=DIR_LOSS_WEIGHT)
    
    best_val_loss = float('inf')
    best_epoch = 0
    patience_counter = 0
    
    for epoch in range(epochs):
        # 训练
        model.train()
        train_loss = 0.0
        for Xb, yb in train_loader:
            Xb, yb = Xb.to(device), yb.to(device)
            optimizer.zero_grad()
            pred = model(Xb)
            loss = huber(pred, yb) + dir_loss_fn(pred, yb)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            train_loss += loss.item()
        train_loss /= len(train_loader)
        
        # 验证
        model.eval()
        val_loss = 0.0
        with torch.no_grad():
            for Xb, yb in val_loader:
                Xb, yb = Xb.to(device), yb.to(device)
                pred = model(Xb)
                val_loss += huber(pred, yb).item()
        val_loss /= len(val_loader)
        
        scheduler.step()
        
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_epoch = epoch
            patience_counter = 0
            # 保存最佳模型
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
        else:
            patience_counter += 1
        
        if patience_counter >= patience:
            if verbose:
                print(f"  ⏹️ Transformer early stop @ epoch {epoch+1}")
            break
    
    # 恢复最佳模型
    model.load_state_dict(best_state)
    model.eval()
    
    # 测试集评估
    with torch.no_grad():
        X_test_t = torch.FloatTensor(X_test).to(device)
        test_pred = model(X_test_t).cpu().numpy()
    
    # 方向精度
    dir_correct = (np.sign(test_pred) == np.sign(y_test)).sum()
    dir_acc = dir_correct / len(y_test) if len(y_test) > 0 else 0.5
    mse = np.mean((test_pred - y_test) ** 2)
    
    if verbose:
        print(f"  🧠 Transformer训练完成: dir_acc={dir_acc:.4f}, MSE={mse:.6f}, "
              f"epochs={best_epoch+1}, samples={n_total}")
    
    result = {
        "model": model,
        "scaler": scaler,
        "direction_accuracy": round(float(dir_acc), 4),
        "mse": round(float(mse), 6),
        "n_samples": n_total,
        "n_features": n_features,
        "feature_cols": feature_cols,
        "seq_len": seq_len,
    }
    
    # 保存模型
    if model_dir and code:
        os.makedirs(model_dir, exist_ok=True)
        save_path = os.path.join(model_dir, f"{code}_transformer.pt")
        torch.save({
            "model_state_dict": model.state_dict(),
            "scaler": scaler,
            "feature_cols": feature_cols,
            "n_features": n_features,
            "seq_len": seq_len,
            "direction_accuracy": result["direction_accuracy"],
        }, save_path)
        if verbose:
            print(f"  💾 Transformer模型已保存: {save_path}")
    
    gc.collect()
    return result


def load_transformer(model_dir: str, code: str) -> dict:
    """加载已训练的Transformer模型用于推理"""
    if not HAS_TORCH:
        return None
    
    save_path = os.path.join(model_dir, f"{code}_transformer.pt")
    if not os.path.exists(save_path):
        return None
    
    try:
        checkpoint = torch.load(save_path, map_location='cpu', weights_only=False)
        n_features = checkpoint["n_features"]
        model = TimeSeriesTransformer(n_features=n_features)
        model.load_state_dict(checkpoint["model_state_dict"])
        model.eval()
        
        return {
            "model": model,
            "scaler": checkpoint["scaler"],
            "feature_cols": checkpoint["feature_cols"],
            "seq_len": checkpoint.get("seq_len", SEQ_LEN),
            "direction_accuracy": checkpoint.get("direction_accuracy", 0.5),
        }
    except Exception as e:
        print(f"  ⚠️ Transformer加载失败({code}): {e}")
        return None


def predict_transformer(transformer_data: dict, features_df: pd.DataFrame,
                        seq_len: int = SEQ_LEN) -> dict:
    """用Transformer模型预测
    
    Args:
        transformer_data: load_transformer返回的dict
        features_df: 特征DataFrame (不含target列)
        seq_len: 序列长度
    
    Returns:
        {"predicted_return": float, "confidence": float, "signal": str}
    """
    if not HAS_TORCH or transformer_data is None:
        return {"predicted_return": 0, "confidence": 0, "signal": "hold"}
    
    model = transformer_data["model"]
    scaler = transformer_data["scaler"]
    feature_cols = transformer_data["feature_cols"]
    
    # 提取特征
    available_cols = [c for c in feature_cols if c in features_df.columns]
    if len(available_cols) < len(feature_cols) * 0.5:
        return {"predicted_return": 0, "confidence": 0, "signal": "hold"}
    
    X = features_df[available_cols].values.astype(np.float32)
    X = np.nan_to_num(X, nan=0.0)
    
    # 对缺失特征补零
    if len(available_cols) < len(feature_cols):
        full_X = np.zeros((X.shape[0], len(feature_cols)), dtype=np.float32)
        col_idx = {c: i for i, c in enumerate(feature_cols)}
        for i, c in enumerate(available_cols):
            if c in col_idx:
                full_X[:, col_idx[c]] = X[:, i]
        X = full_X
    
    # 标准化
    try:
        if hasattr(scaler, 'transform'):
            X = scaler.transform(X)
    except Exception:
        pass
    
    # 取最后seq_len条
    if len(X) < seq_len:
        return {"predicted_return": 0, "confidence": 0, "signal": "hold"}
    
    X_seq = X[-seq_len:].reshape(1, seq_len, -1)
    X_tensor = torch.FloatTensor(X_seq)
    
    with torch.no_grad():
        pred = model(X_tensor).item()
    
    # 置信度: 基于模型方向精度
    dir_acc = transformer_data.get("direction_accuracy", 0.5)
    confidence = min(0.85, max(0.25, dir_acc * 1.15))
    
    signal = "buy" if pred > 0.005 else ("sell" if pred < -0.005 else "hold")
    
    return {
        "predicted_return": round(pred, 6),
        "confidence": round(confidence, 4),
        "signal": signal,
        "source": "transformer",
    }
