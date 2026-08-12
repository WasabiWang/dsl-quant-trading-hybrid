"""
决策融合引擎

综合所有Agent的信号，生成最终交易决策

信号协议（所有Agent必须遵守）：
- score ∈ [-1, 1]，连续值
  - 正值 = 看多/正面
  - 负值 = 看空/负面  
  - 0 = 中性/无信号
- 若无信号源（agent未运行/出错），score = None（区别于中性0）
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 信号缺失标记：区别于中性0
SIGNAL_MISSING = None


class DecisionFusion:
    """决策融合器
    
    策略：
    1. 收集所有Agent信号
    2. 加权平均，区分"信号缺失"vs"中性信号"
    3. 动态分配权重（缺失信号源的权重重新分配）
    4. 生成最终决策
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        
        # 权重配置
        # 信号协议：所有Agent的score值归一化到[-1, 1]范围
        # - 正值 = 看多/正面，负值 = 看空/负面，0 = 中性
        # - SIGNAL_MISSING = 无此信号源（不计入权重）
        # bearish权重为正：bear_score为负值时正确压低总分
        self.weights = {
            "technical": self.config.get("technical_weight", 0.3),
            "sentiment": self.config.get("sentiment_weight", 0.2),
            "macro": self.config.get("macro_weight", 0.3),
            "bullish": self.config.get("bullish_weight", 0.1),
            "bearish": self.config.get("bearish_weight", 0.1),  # bear_score∈[-1,0)，负数占优时拉低总分
        }
        
        # 阈值配置（可配置化）
        self._thresholds = {
            "buy": self.config.get("buy_threshold", 0.3),
            "sell": self.config.get("sell_threshold", -0.3),
        }
        
        logger.info(f"DecisionFusion initialized: weights={self.weights}, thresholds={self._thresholds}")
    
    def fuse(self, signals: Dict[str, Any]) -> Dict[str, Any]:
        """融合所有信号
        
        Args:
            signals: 包含各Agent分析结果的字典
            {
                "technical": {...},    # None表示技术面Agent未运行
                "sentiment": {...},
                "macro": {...},
                "bullish": {...},
                "bearish": {...}
            }
            
        Returns:
            最终决策
            
        P0-FIX: 如果检测到数据来源为mock_random，强制输出HOLD
        """
        
        # P0-FIX: 检测所有mock数据源(而非仅第一个)
        mock_sources = []
        for source_name, signal_data in signals.items():
            if isinstance(signal_data, dict):
                data_quality = signal_data.get("data_quality", "")
                data_source = signal_data.get("source", "")
                if data_quality == "unreliable" or data_source == "mock_random":
                    mock_sources.append(source_name)

        if mock_sources:
            logger.warning(f"⚠️ 信号源 {mock_sources} 使用mock数据，强制HOLD决策")
            return {
                "recommendation": "HOLD",
                "confidence": 0.0,
                "final_score": 0.0,
                "reasoning": f"数据质量不可靠: {','.join(mock_sources)}使用mock数据，拒绝生成交易信号",
                "component_scores": {n: 0.0 for n in self.weights},
                "signal_status": {n: ("mock_blocked" if n in mock_sources else "active") for n in self.weights},
                "metadata": {
                    "analyzed_at": datetime.now().isoformat(),
                    "mock_data_detected": True,
                    "blocked_sources": mock_sources,
                }
            }
        
        # 1. 提取各信号分数（区分缺失SIGNAL_MISSING vs 中性0）
        score_sources = [
            ("technical", self._extract_tech_score(signals.get("technical", None))),
            ("sentiment", self._extract_continuous_score(
                signals.get("sentiment", None), "sentiment_score")),
            ("macro", self._extract_continuous_score(
                signals.get("macro", None), "macro_score")),
            ("bullish", self._extract_bullish_score(signals.get("bullish", None))),
            ("bearish", self._extract_bearish_score(signals.get("bearish", None))),
        ]
        
        # 2. 动态加权：只对实际存在的信号源分配权重
        # 信号缺失（SIGNAL_MISSING）时，其权重重新分配给其他存在的信号源
        active_scores = []
        total_active_weight = 0.0
        
        for name, score in score_sources:
            weight = self.weights.get(name, 0.1)
            if score is not SIGNAL_MISSING:
                active_scores.append((name, score, weight))
                total_active_weight += weight
            else:
                logger.debug(f"信号源 {name} 缺失，跳过（权重{weight:.1f}重新分配）")
        
        # 如果没有有效信号，返回中性决策
        if not active_scores:
            logger.warning("无任何有效信号源，返回中性决策")
            return self._neutral_decision()

        # P1-FIX: 最低票数要求(至少2个活跃信号源)
        MIN_QUORUM = 2
        if len(active_scores) < MIN_QUORUM:
            logger.warning(f"⚠️ 活跃信号源不足({len(active_scores)}<{MIN_QUORUM})，强制HOLD")
            return {
                "recommendation": "HOLD",
                "confidence": 0.0,
                "final_score": 0.0,
                "reasoning": f"信号源不足: 仅{len(active_scores)}个活跃源(需≥{MIN_QUORUM})",
                "component_scores": {n: s for n, s, _ in active_scores},
                "signal_status": {n: ("active" if any(an == n for an, _, _ in active_scores) else "missing") for n in self.weights},
                "metadata": {"analyzed_at": datetime.now().isoformat(), "quorum_not_met": True},
            }

        # P1-FIX: 信号冲突检测(多空对立时降低置信度)
        positive_scores = [(n, s) for n, s, _ in active_scores if s > 0.3]
        negative_scores = [(n, s) for n, s, _ in active_scores if s < -0.3]
        conflict_penalty = 0.0
        if positive_scores and negative_scores:
            conflict_penalty = 0.3  # 多空冲突时置信度扣30%
            logger.info(f"⚠️ 信号冲突: 看多{[n for n,_ in positive_scores]} vs 看空{[n for n,_ in negative_scores]}")
        
        # 归一化权重到总和为1
        if total_active_weight > 0:
            final_score = sum(
                score * (weight / total_active_weight)
                for _, score, weight in active_scores
            )
        else:
            final_score = 0.0
        
        # 硬限制到 [-1, 1]
        final_score = max(-1.0, min(1.0, final_score))
        
        # 3. 生成决策
        recommendation = self._score_to_recommendation(final_score)
        confidence = min(1.0, abs(final_score) * 1.5)
        confidence = confidence * (1 - conflict_penalty)  # P1-FIX: 冲突惩罚
        
        # 记录参与的信号源
        active_source_names = [name for name, _, _ in active_scores]
        
        # 4. 生成综合分析
        reasoning = self._generate_reasoning(signals, final_score, active_source_names)
        
        return {
            "recommendation": recommendation,
            "confidence": confidence,
            "final_score": final_score,
            "reasoning": reasoning,
            "component_scores": {
                name: score if score is not SIGNAL_MISSING else 0.0
                for name, score in score_sources
            },
            "signal_status": {
                name: "active" if score is not SIGNAL_MISSING else "missing"
                for name, score in score_sources
            },
            "metadata": {
                "analyzed_at": datetime.now().isoformat(),
                "weights_used": self.weights,
                "active_signal_count": len(active_scores),
                "dynamic_weights": {
                    name: f"{weight/total_active_weight:.2f}"
                    for name, score, weight in active_scores
                } if total_active_weight > 0 else {}
            }
        }
    
    def _neutral_decision(self) -> Dict[str, Any]:
        """无有效信号时的中性决策"""
        return {
            "recommendation": "HOLD",
            "confidence": 0.0,
            "final_score": 0.0,
            "reasoning": "无有效信号源，维持HOLD（中性决策）",
            "component_scores": {},
            "signal_status": {},
            "metadata": {
                "analyzed_at": datetime.now().isoformat(),
                "weights_used": self.weights,
                "active_signal_count": 0
            }
        }
    
    def _extract_tech_score(self, signal: Optional[Dict]) -> Optional[float]:
        """
        从技术面信号提取连续分数 (P0-FIX: 从离散映射改为连续评分)
        
        旧版: BUY→0.7, SELL→-0.7, HOLD→0.0 (仅3档, 信息损失严重)
        新版: 从技术指标计算连续得分 ∈ [-1, 1]
        
        连续化策略:
        1. 如果signal含tech_score字段, 直接使用
        2. 否则从recommendation映射+置信度微调
        3. RSI/乖离率等连续指标修正
        """
        if signal is None:  # 无信号源
            return SIGNAL_MISSING
        if not signal:      # 空信号字典 = 中性
            return 0.0
        
        # 优先使用连续分数 (如果signal已提供)
        if "tech_score" in signal:
            score = signal["tech_score"]
            if score is not None:
                return max(-1.0, min(1.0, float(score)))
        
        # 从RSI修正 (如有)
        rsi = signal.get("rsi")
        rec = signal.get("recommendation", "HOLD")
        confidence = signal.get("confidence", 0.5)
        
        # 基础离散映射
        base_score = {"BUY": 0.7, "SELL": -0.7, "HOLD": 0.0}.get(rec, 0.0)
        
        # 用置信度微调: 高置信度放大信号, 低置信度压缩
        adjusted = base_score * (0.5 + 0.5 * confidence)
        
        # RSI修正: RSI>70弱化买入, RSI<30弱化卖出
        if rsi is not None:
            try:
                rsi_val = float(rsi)
                if rsi_val > 70 and base_score > 0:
                    adjusted *= max(0.3, 1.0 - (rsi_val - 70) / 50)  # RSI越高买入越弱
                elif rsi_val < 30 and base_score < 0:
                    adjusted *= max(0.3, 1.0 - (30 - rsi_val) / 50)  # RSI越低卖出越弱
            except (ValueError, TypeError):
                pass
        
        return max(-1.0, min(1.0, adjusted))
    
    def _extract_continuous_score(self, signal: Optional[Dict],
                                  field: str) -> Optional[float]:
        """提取连续信号分数，区分缺失与中性"""
        if signal is None:
            return SIGNAL_MISSING
        if not signal:
            return 0.0
        
        score = signal.get(field, 0.0)
        if score is None:
            return 0.0
        return max(-1.0, min(1.0, float(score)))
    
    def _extract_bullish_score(self, signal: Optional[Dict]) -> Optional[float]:
        """提取看多方信号分数，范围 [0, 1]"""
        if signal is None:
            return SIGNAL_MISSING
        if not signal:
            return 0.0
        score = signal.get("bull_score", 0.0)
        if score is None:
            return 0.0
        return max(0.0, min(1.0, float(score)))
    
    def _extract_bearish_score(self, signal: Optional[Dict]) -> Optional[float]:
        """提取看空方信号分数，范围 [-1, 0)"""
        if signal is None:
            return SIGNAL_MISSING
        if not signal:
            return 0.0
        score = signal.get("bear_score", 0.0)
        if score is None:
            return 0.0
        return max(-1.0, min(0.0, float(score)))
    
    def _score_to_recommendation(self, score: float) -> str:
        """将分数转换为建议（可配置阈值）"""
        if score > self._thresholds["buy"]:
            return "BUY"
        elif score < self._thresholds["sell"]:
            return "SELL"
        else:
            return "HOLD"
    
    def _generate_reasoning(self, signals: Dict, final_score: float,
                           active_sources: list) -> str:
        """生成综合推理"""
        reasons = []
        
        if "technical" in active_sources:
            tech = signals.get("technical", {})
            if tech:
                rec = tech.get("recommendation", "HOLD")
                if rec == "BUY":
                    reasons.append("技术面看涨")
                elif rec == "SELL":
                    reasons.append("技术面看跌")
        
        if "sentiment" in active_sources:
            sent = signals.get("sentiment", {})
            if sent:
                s = sent.get("sentiment_score", 0)
                if abs(s) > 0.3:
                    reasons.append(f"情绪面{'正面' if s > 0 else '负面'}")
        
        if "macro" in active_sources:
            macro = signals.get("macro", {})
            if macro:
                m = macro.get("macro_score", 0)
                if abs(m) > 0.3:
                    reasons.append(f"宏观面{'有利' if m > 0 else '不利'}")
        
        if not reasons:
            reasons.append("各因素综合平衡")
        
        source_info = f"[活跃:{','.join(active_sources)}]"
        return f"综合评分: {final_score:.2f} {source_info}. " + "; ".join(reasons)
