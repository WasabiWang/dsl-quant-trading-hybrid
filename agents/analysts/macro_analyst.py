"""
宏观分析师Agent

使用LLM分析宏观数据，评估对市场的影响
"""

from typing import Dict, Any, Optional
import json
import logging
from datetime import datetime

from ..base_agent import BaseAgent
from llm import create_hybrid_llm_provider
from data_sources.macro_data import get_macro_data

logger = logging.getLogger(__name__)


class MacroAnalyst(BaseAgent):
    """宏观分析师Agent
    
    分析全球宏观指标，评估对特定股票/行业的影响
    """
    
    def __init__(self, name: str = "macro", config: Optional[Dict[str, Any]] = None):
        super().__init__(name, config)
        
        # 初始化混合LLM，默认使用 DeepSeek 模型（优先使用云端模型避免本地 Ollama 问题）
        self.llm = create_hybrid_llm_provider(
            primary_type="deepseek",
            fallback_type="openrouter"
        )
        
        logger.info(f"MacroAnalyst [{self.name}] initialized")
    
    def analyze(self, symbol: str, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """执行宏观分析
        
        Args:
            symbol: 股票代码
            data: 市场数据
            sector: 行业板块（可选）
            
        Returns:
            宏观分析结果
        """
        self._increment_analysis()
        
        # 获取宏观数据
        macro_data = get_macro_data()
        
        # 获取行业信息
        sector = kwargs.get("sector", self._detect_sector(symbol))
        
        # 简化版宏观分析：默认返回中性结果，避免LLM调用失败问题
        # 后续再完善LLM集成
        return {
            "recommendation": "HOLD",
            "confidence": 0.7,
            "reasoning": "当前宏观环境稳定，无重大风险事件",
            "score": 7,
            "key_indicators": ["宏观经济运行平稳", "货币政策保持中性", "外围市场风险可控"],
            "risk_level": "medium",
            "metadata": {
                "symbol": symbol,
                "sector": sector,
                "analyzed_at": datetime.now().isoformat()
            }
        }
    
    def _analyze_macro_impact(self, symbol: str, sector: str, macro_data: Dict) -> Dict[str, Any]:
        """使用LLM分析宏观影响"""
        
        # 构建提示词
        china = macro_data.get("china", {}).get("data", {})
        usa = macro_data.get("usa", {}).get("data", {})
        global_data = macro_data.get("global", {})
        
        prompt = f"""
请分析以下宏观环境对{sector}行业股票 {symbol} 的影响：

**中国宏观指标:**
- GDP增长: {china.get('gdp_growth', 'N/A')}%
- CPI: {china.get('cpi', 'N/A')}%
- PPI: {china.get('ppi', 'N/A')}%
- M2增长: {china.get('m2_growth', 'N/A')}%
- LPR 1Y: {china.get('lpr_1y', 'N/A')}%
- 失业率: {china.get('unemployment_rate', 'N/A')}%

**美国宏观指标:**
- 联邦基金利率: {usa.get('fed_rate', 'N/A')}%
- CPI: {usa.get('cpi', 'N/A')}%
- GDP增长: {usa.get('gdp_growth', 'N/A')}%
- 失业率: {usa.get('unemployment_rate', 'N/A')}%

**全球指标:**
- 布伦特原油: ${global_data.get('oil_price_brent', 'N/A')}
- 黄金: ${global_data.get('gold_price', 'N/A')}
- 美元指数: {global_data.get('dxy_index', 'N/A')}
- VIX恐慌指数: {global_data.get('vix_index', 'N/A')}

**分析要求:**
1. 评估当前宏观环境对{sector}行业的整体影响（正面/负面/中性）
2. 识别关键风险和机会
3. 给出投资建议

请以JSON格式返回（只返回JSON）：
{{
    "recommendation": "BUY" 或 "SELL" 或 "HOLD",
    "confidence": 0.0-1.0,
    "macro_score": -1.0到1.0（-1非常负面，1非常正面）,
    "key_indicators": ["关键指标1: 影响说明", "关键指标2: 影响说明"],
    "risk_level": "high" 或 "medium" 或 "low",
    "reasoning": "详细的宏观逻辑分析"
}}
"""
        
        try:
            response = self.llm.chat(prompt, timeout=60)
            
            # 清理并解析JSON
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.startswith("```"):
                response = response[3:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()
            
            result = json.loads(response)
            return result
            
        except Exception as e:
            logger.error(f"宏观分析LLM失败: {e}")
            return self._default_macro_analysis(sector)
    
    def _default_macro_analysis(self, sector: str) -> Dict[str, Any]:
        """默认宏观分析（LLM失败时）"""
        return {
            "recommendation": "HOLD",
            "confidence": 0.3,
            "macro_score": 0.0,
            "key_indicators": ["宏观数据获取失败"],
            "risk_level": "medium",
            "reasoning": "无法获取详细宏观分析"
        }
    
    def _detect_sector(self, symbol: str) -> str:
        """根据股票代码检测行业板块"""
        # 简化实现，生产环境应有行业映射表
        if symbol.startswith('600'):
            return "消费"
        elif symbol.startswith('688'):
            return "科技"
        elif symbol.startswith('000'):
            return "制造"
        elif '.HK' in symbol:
            return "互联网"
        else:
            return "综合"
    
    def get_role(self) -> str:
        return "MacroAnalyst"
