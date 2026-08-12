"""
风险管理Agent

评估交易风险，提供仓位建议和止损止盈点位
"""

from typing import Dict, Any, Optional, List
import logging
from datetime import datetime

from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)


class RiskManager(BaseAgent):
    """风险管理Agent
    
    职责：
    1. 评估交易风险等级
    2. 计算建议仓位
    3. 设定止损止盈点位
    """
    
    def __init__(self, name: str = "risk", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        
        # 风险参数
        self.max_position_size = self.config.get("max_position_size", 0.1)  # 10%
        self.stop_loss_pct = self.config.get("stop_loss_pct", 0.05)  # 5%
        self.take_profit_pct = self.config.get("take_profit_pct", 0.10)  # 10%
        
        logger.info(f"RiskManager [{self.name}] initialized")
    
    def analyze(self, symbol: str, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """执行风险评估"""
        self._increment_analysis()
        
        price = data.get("price", 0)
        final_score = data.get("final_score", 0)
        confidence = data.get("confidence", 0.5)
        
        # 计算风险等级
        risk_level = self._assess_risk_level(final_score, confidence)
        
        # 计算建议仓位
        position_size = self._calculate_position_size(confidence, risk_level)
        
        # v4.6.x P0: 统一委托 paper_trader.get_stop_loss_pct (ATR动态+板块固定)
        try:
            from scripts.paper_trader import get_stop_loss_pct
            sl_pct = get_stop_loss_pct(symbol)
            stop_loss = price * (1 + sl_pct) if price > 0 and sl_pct < 0 else price * 0.92
        except Exception:
            stop_loss = price * 0.92  # 硬回退 -8%

        atr = data.get("atr", None)
        if atr and atr > 0 and price > 0:
            take_profit = price + 3 * atr  # 盈亏比1.5:1
        else:
            take_profit = price * (1 + self.take_profit_pct) if price > 0 else 0
        
        return {
            "recommendation": "APPROVE" if risk_level != "high" else "REVIEW",
            "risk_level": risk_level,
            "position_size": position_size,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_factors": self._identify_risk_factors(data),
            "metadata": {
                "symbol": symbol,
                "analyzed_at": datetime.now().isoformat()
            }
        }
    
    def _assess_risk_level(self, score: float, confidence: float) -> str:
        """评估风险等级 (P0-FIX: 修正反转的风险逻辑)

        风险等级应独立于信号质量评估：
        - high risk: 低置信度 (< 0.3) = 不确定性高
        - medium risk: 中等置信度 (0.3-0.7)
        - low risk: 高置信度 (> 0.7) = 确定性强
        """
        if confidence < 0.3:
            return "high"
        elif confidence < 0.7:
            return "medium"
        else:
            return "low"
    
    def _calculate_position_size(self, confidence: float, risk_level: str) -> float:
        """计算建议仓位"""
        base_size = self.max_position_size
        
        # 根据置信度调整
        adjusted = base_size * confidence
        
        # 根据风险等级调整
        if risk_level == "high":
            adjusted *= 0.5
        elif risk_level == "medium":
            adjusted *= 0.75
        
        return min(adjusted, self.max_position_size)
    
    def _identify_risk_factors(self, data: Dict) -> List[str]:
        """识别风险因素"""
        risks = []
        
        if data.get("confidence", 1) < 0.5:
            risks.append("信号置信度低")
        
        component = data.get("component_scores", {})
        if abs(component.get("sentiment", 0)) > 0.5:
            risks.append("情绪面波动大")
        
        if abs(component.get("macro", 0)) > 0.5:
            risks.append("宏观环境不稳定")
        
        return risks if risks else ["风险可控"]
    
    def get_role(self) -> str:
        return "RiskManager"
