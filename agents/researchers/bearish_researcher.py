"""
看空研究者Agent

负责寻找风险，反驳看多观点
"""

from typing import Dict, Any, Optional
import logging
from datetime import datetime

from ..base_agent import BaseAgent
from llm.provider import create_hybrid_llm_provider

logger = logging.getLogger(__name__)


class BearishResearcher(BaseAgent):
    """看空研究者
    
    职责：
    1. 分析其他Agent的负面信号
    2. 识别潜在风险
    3. 反驳看多观点
    """
    
    def __init__(self, name: str = "bearish", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        
        self.llm = create_hybrid_llm_provider(
            primary_type="deepseek",
            fallback_type="openrouter"
        )
        
        logger.info(f"BearishResearcher [{self.name}] initialized")
    
    def analyze(self, symbol: str, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """执行看空分析"""
        self._increment_analysis()
        
        technical = data.get("technical", {})
        sentiment = data.get("sentiment", {})
        macro = data.get("macro", {})
        
        # LLM生成看空论点
        bearish_case = self._build_bearish_case(symbol, technical, sentiment, macro)
        
        return {
            "recommendation": "SELL",
            "confidence": bearish_case.get("confidence", 0.5),
            "reasoning": bearish_case.get("reasoning", ""),
            "key_risks": bearish_case.get("key_risks", []),
            "counter_arguments": bearish_case.get("counter_arguments", []),
            "bear_score": bearish_case.get("bear_score", 0.5),
            "metadata": {
                "symbol": symbol,
                "analyzed_at": datetime.now().isoformat()
            }
        }
    
    def _build_bearish_case(self, symbol: str, technical: Dict, sentiment: Dict, macro: Dict) -> Dict[str, Any]:
        """构建看空论点"""
        
        tech_signal = technical.get("recommendation", "HOLD")
        sent_score = sentiment.get("sentiment_score", 0.0)
        macro_score = macro.get("macro_score", 0.0)
        
        prompt = f"""
你是一位经验丰富的看空研究员。请为股票 {symbol} 识别风险。

**现有信号:**
- 技术面: {tech_signal}
- 情绪面: {sent_score} (-1到1)
- 宏观面: {macro_score} (-1到1)

**任务:**
1. 找出潜在风险（至少3条）
2. 预测潜在的下跌催化剂
3. 反驳可能的看多观点

请以JSON格式返回：
{{
    "confidence": 0.0-1.0,
    "bear_score": -1.0到1.0 (负值表示看空),
    "key_risks": ["风险1", "风险2", "风险3"],
    "counter_arguments": ["反驳看多观点1", "反驳看多观点2"],
    "reasoning": "完整的看空逻辑链"
}}
"""
        
        try:
            response = self.llm.chat(prompt, timeout=60)
            response = self._clean_json(response)
            import json
            return json.loads(response)
        except Exception as e:
            logger.error(f"看空分析失败: {e}")
            return self._default_bearish_case(tech_signal, sent_score, macro_score)
    
    def _default_bearish_case(self, tech: str, sent: float, macro: float) -> Dict:
        """默认看空案例"""
        score = (1 if tech == "SELL" else 0) + max(0, -sent) + max(0, -macro)
        score = score / 3.0
        
        return {
            "confidence": score,
            "bear_score": -score,
            "key_risks": ["市场不确定性", "宏观压力"],
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
        return "BearishResearcher"
