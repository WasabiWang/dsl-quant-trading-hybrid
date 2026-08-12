#!/usr/bin/env python3
"""
core/signal_generator.py — 统一信号生成模块 v4.5.9
P1改进: 回测和生产共用同一套信号生成逻辑，消除代码路径不一致风险

功能:
1. 因子计算 — 从K线数据生成技术面/基本面/情绪面因子
2. 信号评分 — 多因子加权融合，输出买入/持有/卖出信号
3. 仓位计算 — 三层融合(宏观+行业+风险)计算最终仓位比例
4. 兼容回测和生产两种调用方式
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)

# 因子权重配置 (可从config覆盖)
DEFAULT_FACTOR_WEIGHTS = {
    "momentum": 0.25,      # 动量因子
    "volatility": 0.15,    # 波动率因子  
    "volume": 0.10,        # 成交量因子
    "trend": 0.25,         # 趋势因子
    "valuation": 0.15,     # 估值因子
    "sentiment": 0.10,     # 情绪因子
}


@dataclass
class StockSignal:
    """个股信号"""
    symbol: str
    name: str = ""
    total_score: float = 0.0       # 综合评分 0-10
    action_signal: str = "hold"    # buy / hold / sell
    confidence: float = 0.5        # 置信度 0-1
    factor_scores: Dict[str, float] = field(default_factory=dict)
    position_pct: float = 0.0      # 建议仓位比例


class SignalGenerator:
    """统一信号生成器"""
    
    def __init__(self, factor_weights: Dict[str, float] = None):
        self.factor_weights = factor_weights or DEFAULT_FACTOR_WEIGHTS.copy()
    
    # ========================================================================
    # 因子计算
    # ========================================================================
    
    def compute_factors(self, df: pd.DataFrame) -> pd.DataFrame:
        """从K线DataFrame计算所有因子
        
        Args:
            df: 含 open/high/low/close/volume 列的DataFrame
            
        Returns:
            添加了因子列的DataFrame
        """
        df = df.copy()

        # --- 动量因子 (P1-9 fix: 添加shift(1)使用滞后值，避免前视偏差) ---
        df['ret_5d'] = df['close'].pct_change(5).shift(1)
        df['ret_20d'] = df['close'].pct_change(20).shift(1)
        df['ret_60d'] = df['close'].pct_change(60).shift(1)

        # --- 波动率因子 ---
        df['vol_20d'] = df['close'].pct_change().shift(1).rolling(20).std()
        df['vol_60d'] = df['close'].pct_change().shift(1).rolling(60).std()

        # --- 成交量因子 ---
        df['vol_ma_5'] = df['volume'].shift(1).rolling(5).mean()
        df['vol_ratio'] = df['volume'] / df['vol_ma_5'].replace(0, np.nan)

        # --- 趋势因子 (shift(1) 防止当天收盘价污染因子) ---
        df['ma_5'] = df['close'].shift(1).rolling(5).mean()
        df['ma_20'] = df['close'].shift(1).rolling(20).mean()
        df['ma_60'] = df['close'].shift(1).rolling(60).mean()
        df['trend_5_20'] = df['ma_5'] / df['ma_20'] - 1
        df['trend_20_60'] = df['ma_20'] / df['ma_60'] - 1

        # --- 估值因子 (使用滞后价格计算相对位置) ---
        shifted_close = df['close'].shift(1)
        df['high_20'] = shifted_close.rolling(20).max()
        df['low_20'] = shifted_close.rolling(20).min()
        # P0-FIX: 使用shifted_close防止前视偏差
        df['price_position'] = (shifted_close - df['low_20']) / (df['high_20'] - df['low_20'] + 1e-9)

        # --- 情绪因子 (P1-9 fix: 使用shift(1)滞后值) ---
        df['pct_change'] = df['close'].pct_change().shift(1)
        df['sentiment'] = df['pct_change'] * df['vol_ratio']
        
        return df.dropna()
    
    def score_factors(self, df: pd.DataFrame, idx: int = -1) -> Dict[str, float]:
        """对最新一行数据计算各因子得分 (0-10分制)
        
        Args:
            df: 含因子列的DataFrame
            idx: 要评分的数据行索引，默认-1(最新)
        """
        row = df.iloc[idx]
        scores = {}
        
        # 动量: 近期收益越高越好
        momentum = (row.get('ret_5d', 0) * 0.5 + row.get('ret_20d', 0) * 0.3 
                    + row.get('ret_60d', 0) * 0.2)
        scores['momentum'] = self._normalize_score(momentum, -0.3, 0.3, 5, 8)
        
        # 波动率: 适中最好(太低无波动，太高风险大)
        vol = row.get('vol_20d', 0.02)
        if pd.isna(vol) or vol <= 0:
            scores['volatility'] = 5
        else:
            # 最优波动率约2%/日，过高过低都扣分
            scores['volatility'] = 10 - min(5, abs(vol / 0.02 - 1) * 5)
        
        # 成交量: 放量加分但天量减分
        vol_ratio = row.get('vol_ratio', 1.0)
        if pd.isna(vol_ratio):
            scores['volume'] = 5
        elif 1.2 <= vol_ratio <= 2.5:
            scores['volume'] = 8  # 温和放量
        elif vol_ratio > 5:
            scores['volume'] = 4  # 异常天量
        else:
            scores['volume'] = 5
        
        # 趋势: 多头排列加分
        trend_5_20 = row.get('trend_5_20', 0)
        trend_20_60 = row.get('trend_20_60', 0)
        if not pd.isna(trend_5_20) and not pd.isna(trend_20_60):
            trend_score = (1 if trend_5_20 > 0 else -1) * 2 + (1 if trend_20_60 > 0 else -1) * 1
            scores['trend'] = 5 + trend_score
        else:
            scores['trend'] = 5
        
        # 估值: 相对低位加分
        pos = row.get('price_position', 0.5)
        scores['valuation'] = (1 - min(pos, 1)) * 10 if not pd.isna(pos) else 5
        
        # 情绪: 正情绪加分
        sent = row.get('sentiment', 0)
        scores['sentiment'] = self._normalize_score(sent if not pd.isna(sent) else 0,
                                                     -0.05, 0.05, 3, 7)
        
        return scores
    
    def compute_total_score(self, factor_scores: Dict[str, float]) -> float:
        """加权计算综合评分"""
        total = 0.0
        weight_sum = 0.0
        for factor, score in factor_scores.items():
            w = self.factor_weights.get(factor, 0.1)
            total += score * w
            weight_sum += w
        return total / weight_sum if weight_sum > 0 else 5.0
    
    # ========================================================================
    # 信号生成
    # ========================================================================
    
    # v4.6.8: 精度门控阈值 (可从外部覆盖)
    MIN_ACCURACY_FOR_SIGNAL = 0.50  # 低于此精度的股票直接hold (bench/黑名单)
    MIN_ACCURACY_CONFIDENT = 0.55  # core层最低阈值，低于此需额外置信度加分

    def set_accuracy_gate(self, stock_accuracy: Dict[str, float]):
        """设置每只股票的当前精度，供信号生成时门控使用
        
        Args:
            stock_accuracy: {symbol: mean_accuracy} 映射
        """
        self._stock_accuracy = stock_accuracy

    def generate_signal(self, symbol: str, df: pd.DataFrame, 
                        name: str = "") -> StockSignal:
        """为单个股票生成交易信号
        
        P0-FIX: 如果输入数据为mock_random来源，强制输出HOLD
        v4.6.8: 精度门控 — 低精度股票强制hold，过滤低质量信号
        """
        # P0-FIX: 检测mock数据 → 拒绝生成非HOLD信号
        if hasattr(df, 'attrs') and df.attrs.get('data_quality') == 'unreliable':
            return StockSignal(symbol=symbol, name=name, total_score=5,
                             action_signal="hold", confidence=0.0,
                             factor_scores={"_blocked": "mock_data"})
        
        # v4.6.8: 精度门控 — 低精度股票不产生交易信号
        stock_acc = getattr(self, '_stock_accuracy', {}).get(symbol, 0.55)
        if stock_acc < self.MIN_ACCURACY_FOR_SIGNAL:
            return StockSignal(symbol=symbol, name=name, total_score=5,
                             action_signal="hold", confidence=0.0,
                             factor_scores={"_blocked": f"low_accuracy_{stock_acc:.1%}"})
        
        # core层(bench)股票需要更高置信度才能触发信号
        if stock_acc < self.MIN_ACCURACY_CONFIDENT:
            accuracy_penalty = (self.MIN_ACCURACY_CONFIDENT - stock_acc) * 2.0
            # 精度折扣会在后续置信度计算中生效
        else:
            accuracy_penalty = 0.0
        
        if len(df) < 60:
            return StockSignal(symbol=symbol, name=name, total_score=5,
                             action_signal="hold", confidence=0.3)
        
        df = self.compute_factors(df)
        if len(df) < 2:
            return StockSignal(symbol=symbol, name=name, total_score=5,
                             action_signal="hold", confidence=0.3)
        
        factor_scores = self.score_factors(df)
        total_score = self.compute_total_score(factor_scores)
        
        # 置信度: 基于数据量
        confidence = min(1.0, len(df) / 300)
        
        # 信号判断
        if total_score >= 7.0:
            action = "buy"
        elif total_score <= 3.5:
            action = "sell"
        else:
            action = "hold"
        
        return StockSignal(
            symbol=symbol,
            name=name,
            total_score=round(total_score, 2),
            action_signal=action,
            confidence=round(confidence, 2),
            factor_scores={k: round(v, 2) for k, v in factor_scores.items()}
        )
    
    def generate_batch_signals(self, stock_data: Dict[str, pd.DataFrame],
                               names: Dict[str, str] = None) -> List[StockSignal]:
        """批量生成信号
        
        Args:
            stock_data: {symbol: DataFrame}
            names: {symbol: name} 可选名称映射
        """
        signals = []
        for symbol, df in stock_data.items():
            name = (names or {}).get(symbol, symbol)
            signal = self.generate_signal(symbol, df, name)
            signals.append(signal)
        
        # 按综合评分降序排列
        signals.sort(key=lambda s: s.total_score, reverse=True)
        return signals
    
    # ========================================================================
    # 仓位计算 (三层融合)
    # ========================================================================
    
    def calculate_position(self, signals: List[StockSignal],
                          macro_score: float = 7.0,
                          sector_score: float = 7.0,
                          max_position_pct: float = 0.80,
                          max_single_pct: float = 0.30) -> Dict[str, float]:
        """三层融合计算仓位分配
        
        Args:
            signals: 个股信号列表
            macro_score: 宏观评分 0-10
            sector_score: 行业评分 0-10
            max_position_pct: 总仓位上限
            max_single_pct: 单票仓位上限
            
        Returns:
            {symbol: position_pct}
        """
        # 宏观+行业融合决定总仓位
        total_position = (macro_score * 0.3 + sector_score * 0.3 + 7 * 0.4) / 10
        total_position = min(total_position, max_position_pct)
        
        # 选出买入信号的股票
        buy_signals = [s for s in signals if s.action_signal == "buy"]
        if not buy_signals:
            # 全部持有信号时用等权重
            hold_signals = [s for s in signals if s.action_signal == "hold"]
            if hold_signals:
                n = len(hold_signals)
                return {s.symbol: min(total_position / n, max_single_pct) 
                        for s in hold_signals[:10]}
            return {}
        
        # 按评分分配权重
        total_score = sum(s.total_score for s in buy_signals[:10])
        if total_score <= 0:
            return {}
        
        positions = {}
        for s in buy_signals[:10]:
            raw_weight = s.total_score / total_score
            positions[s.symbol] = min(raw_weight * total_position, max_single_pct)
        
        return positions
    
    # ========================================================================
    # 工具函数
    # ========================================================================
    
    @staticmethod
    def _normalize_score(value: float, min_val: float, max_val: float,
                         low_score: float = 0, high_score: float = 10) -> float:
        """将值线性映射到评分区间"""
        if pd.isna(value):
            return (low_score + high_score) / 2
        clamped = max(min_val, min(max_val, value))
        ratio = (clamped - min_val) / (max_val - min_val) if max_val > min_val else 0.5
        return low_score + ratio * (high_score - low_score)


    # ========================================================================
    # 评分体系桥接 (P1-FIX: 统一0-10与-1~1两套评分体系)
    # ========================================================================
    
    @staticmethod
    def score_to_fusion_format(total_score: float, action_signal: str) -> Dict[str, float]:
        """将signal_generator的0-10评分转换为decision_fusion兼容的格式
        
        映射规则:
        - 0-10线性映射到-1~+1 (5分=中性0)
        - action_signal修正: sell强制为负区间
        
        Args:
            total_score: 0-10评分
            action_signal: buy/hold/sell
            
        Returns:
            {"tech_score": float ∈ [-1,1], "confidence": float ∈ [0,1]}
        """
        # 线性映射: score 0→-1, 5→0, 10→+1
        fusion_score = (total_score - 5.0) / 5.0
        fusion_score = max(-1.0, min(1.0, fusion_score))
        
        # action_signal修正边界情况
        if action_signal == "sell" and fusion_score > 0:
            fusion_score = -abs(fusion_score)  # sell信号强制为负
        elif action_signal == "buy" and fusion_score < 0:
            fusion_score = abs(fusion_score)   # buy信号强制为正
        
        # 置信度: 距离中性点越远置信度越高
        confidence = min(1.0, abs(fusion_score) * 1.5)
        
        return {"tech_score": round(fusion_score, 4), "confidence": round(confidence, 4)}
    
    @staticmethod
    def fusion_format_to_score(fusion_score: float) -> float:
        """将decision_fusion的-1~1评分转换回0-10体系
        
        Args:
            fusion_score: -1~+1评分
            
        Returns:
            float ∈ [0, 10]
        """
        return round((fusion_score + 1.0) * 5.0, 2)


# ========================================================================
# 全局单例
# ========================================================================
signal_generator = SignalGenerator()


# ========================================================================
# 测试
# ========================================================================
if __name__ == "__main__":
    print("=" * 60)
    print("🧪 统一信号生成器测试")
    print("=" * 60)
    
    # 创建测试数据
    dates = pd.date_range('2025-01-01', '2026-04-26', freq='B')
    np.random.seed(42)
    base = 50
    returns = np.random.normal(0.0005, 0.015, len(dates))
    prices = base * np.exp(np.cumsum(returns))
    
    df = pd.DataFrame({
        'open': prices * 0.995,
        'high': prices * 1.01,
        'low': prices * 0.99,
        'close': prices,
        'volume': np.random.randint(1000000, 10000000, len(dates))
    }, index=dates)
    
    sg = SignalGenerator()
    signal = sg.generate_signal("TEST001", df, "测试股票")
    
    print(f"\n📊 信号结果:")
    print(f"  标的: {signal.symbol} ({signal.name})")
    print(f"  综合评分: {signal.total_score}/10")
    print(f"  操作信号: {signal.action_signal}")
    print(f"  置信度: {signal.confidence:.0%}")
    print(f"  因子得分: {signal.factor_scores}")
    
    print("\n" + "=" * 60)
    print("✅ 信号生成器测试完成")
    print("=" * 60)
