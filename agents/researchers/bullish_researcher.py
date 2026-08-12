"""
看多研究者Agent

负责寻找买入理由，反驳看空观点
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from ..base_agent import BaseAgent
from llm.provider import create_hybrid_llm_provider

logger = logging.getLogger(__name__)


class BullishResearcher(BaseAgent):
    """看多研究者
    
    职责：
    1. 分析其他Agent的正面信号
    2. 寻找潜在的上涨 catalysts
    3. 反驳看空观点
    """
    
    def __init__(self, name: str = "bullish", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        
        self.llm = create_hybrid_llm_provider(
            primary_type="volcengine",
            fallback_type="volcengine"
        )
        
        logger.info(f"BullishResearcher [{self.name}] initialized")
    
    def analyze(self, symbol: str, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """执行看多分析
        
        Args:
            symbol: 股票代码
            data: 包含其他Agent的分析结果
            - technical: 技术分析结果
            - sentiment: 情绪分析结果
            - macro: 宏观分析结果
            
        Returns:
            看多分析报告
        """
        self._increment_analysis()
        
        technical = data.get("technical", {})
        sentiment = data.get("sentiment", {})
        macro = data.get("macro", {})
        
        # LLM生成看多论点
        bullish_case = self._build_bullish_case(symbol, technical, sentiment, macro)
        
        return {
            "recommendation": "BUY",
            "confidence": bullish_case.get("confidence", 0.5),
            "reasoning": bullish_case.get("reasoning", ""),
            "key_catalysts": bullish_case.get("key_catalysts", []),
            "counter_arguments": bullish_case.get("counter_arguments", []),
            "bull_score": bullish_case.get("bull_score", 0.5),
            "metadata": {
                "symbol": symbol,
                "analyzed_at": datetime.now().isoformat()
            }
        }
    
    def _build_bullish_case(self, symbol: str, technical: Dict, sentiment: Dict, macro: Dict) -> Dict[str, Any]:
        """构建看多论点"""
        
        tech_signal = technical.get("recommendation", "HOLD")
        sent_score = sentiment.get("sentiment_score", 0.0)
        macro_score = macro.get("macro_score", 0.0)
        
        prompt = f"""
你是一位经验丰富的看多研究员。请为股票 {symbol} 构建看多论点。

**现有信号:**
- 技术面: {tech_signal}
- 情绪面: {sent_score} (-1到1)
- 宏观面: {macro_score} (-1到1)

**任务:**
1. 找出支持买入的关键理由（至少3条）
2. 预测潜在的上涨催化剂
3. 反驳可能的看空观点

请以JSON格式返回：
{{
    "confidence": 0.0-1.0,
    "bull_score": -1.0到1.0,
    "key_catalysts": ["催化剂1", "催化剂2", "催化剂3"],
    "counter_arguments": ["反驳看空观点1", "反驳看空观点2"],
    "reasoning": "完整的看多逻辑链"
}}
"""
        
        try:
            response = self.llm.chat(prompt, timeout=60)
            response = self._clean_json(response)
            import json
            return json.loads(response)
        except Exception as e:
            logger.error(f"看多分析失败: {e}")
            return self._default_bullish_case(tech_signal, sent_score, macro_score)
    
    def _default_bullish_case(self, tech: str, sent: float, macro: float) -> Dict:
        """默认看多案例"""
        score = (1 if tech == "BUY" else 0) + max(0, sent) + max(0, macro)
        score = score / 3.0
        
        return {
            "confidence": score,
            "bull_score": score,
            "key_catalysts": ["技术指标向好", "情绪面中性偏正"],
            "counter_arguments": [],
            "reasoning": "基于现有信号的量化评估"
        }
    
    def _clean_json(self, text: str) -> str:
        """清理JSON文本"""
        text = text.strip()
        for prefix in ["```json", "```"]:
            if text.startswith(prefix):
                text = text[len(prefix):]
        if text.endswith("```"):
            text = text[:-3]
        return text.strip()
    
    def get_role(self) -> str:
        return "BullishResearcher"
