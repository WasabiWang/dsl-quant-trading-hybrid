"""
交易员Agent

根据融合决策和风险评估，生成最终交易指令
集成对账引擎，为每个交易信号分配order_id
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from ..base_agent import BaseAgent

logger = logging.getLogger(__name__)


class TraderAgent(BaseAgent):
    """交易员Agent
    
    职责：
    1. 综合所有Agent输入
    2. 生成最终交易指令（含对账order_id）
    3. 记录交易日志
    """
    
    def __init__(self, name: str = "trader", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        self._reconciliation = None
        logger.info(f"TraderAgent [{self.name}] initialized")
    
    def _get_reconciliation(self):
        """延迟加载对账引擎"""
        if self._reconciliation is None:
            from execution_engine.reconciliation import get_reconciliation_engine
            self._reconciliation = get_reconciliation_engine()
        return self._reconciliation
    
    def analyze(self, symbol: str, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """生成交易指令（含对账追踪）"""
        self._increment_analysis()
        
        fusion_result = data.get("fusion", {})
        risk_result = data.get("risk", {})
        
        recommendation = fusion_result.get("recommendation", "HOLD")
        
        # 如果风控不通过，强制HOLD
        if risk_result.get("recommendation") == "REVIEW":
            recommendation = "HOLD"
            logger.warning(f"风控不通过，{symbol} 强制 HOLD")
        
        # 计算建议数量（依据仓位比例和当前价格）
        position_size = risk_result.get("position_size", 0) if recommendation != "HOLD" else 0
        price = risk_result.get("stop_loss", 0)  # fallback，实际价由上层传入
        
        # 生成交易指令
        order = {
            "symbol": symbol,
            "action": recommendation,
            "position_size": position_size,
            "stop_loss": risk_result.get("stop_loss", 0),
            "take_profit": risk_result.get("take_profit", 0),
            "reasoning": fusion_result.get("reasoning", ""),
            "timestamp": datetime.now().isoformat()
        }
        
        # ---- 对账集成 ----
        # 对非HOLD信号创建对账追踪
        if recommendation in ("BUY", "SELL"):
            try:
                recon = self._get_reconciliation()
                order_id = recon.record_signal(
                    symbol=symbol,
                    action=recommendation,
                    quantity=int(position_size) if position_size > 0 else 0,
                    price=price if price > 0 else 0.0,
                    confidence=fusion_result.get("confidence", 0.5),
                    source="trader_agent",
                    reasoning=order["reasoning"]
                )
                order["order_id"] = order_id
                logger.info(f"[对账] 指令关联order_id: {order_id} | {symbol} {recommendation}")
            except Exception as e:
                logger.warning(f"对账引擎记录失败（不影响主流程）: {e}")
                order["order_id"] = ""
        else:
            order["order_id"] = ""
        # ---- 对账集成结束 ----
        
        logger.info(f"交易指令: {order}")
        
        return order
    
    def get_role(self) -> str:
        return "TraderAgent"
