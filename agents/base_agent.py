"""
Agent基类 - 所有Agent的抽象父类

定义Agent的通用接口和行为规范
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Agent基类
    
    所有Agent必须实现以下方法：
    - analyze(): 执行分析
    - get_role(): 返回Agent角色名称
    """
    
    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """初始化Agent
        
        Args:
            name: Agent名称
            config: 配置字典
        """
        self.name = name
        self.config = config or {}
        self.created_at = datetime.now()
        self.analysis_count = 0
        
        logger.info(f"Agent [{self.name}] initialized")
    
    @abstractmethod
    def analyze(self, symbol: str, data: Dict[str, Any], **kwargs) -> Dict[str, Any]:
        """执行分析
        
        Args:
            symbol: 股票代码
            data: 市场数据
            **kwargs: 其他参数
            
        Returns:
            分析结果字典，包含：
            - recommendation: 建议 (BUY/SELL/HOLD)
            - confidence: 置信度 (0-1)
            - reasoning: 推理过程
            - metadata: 元数据
        """
        pass
    
    def get_role(self) -> str:
        """返回Agent角色名称"""
        return self.__class__.__name__
    
    def get_stats(self) -> Dict[str, Any]:
        """获取Agent统计信息"""
        return {
            "name": self.name,
            "role": self.get_role(),
            "analysis_count": self.analysis_count,
            "created_at": self.created_at.isoformat()
        }
    
    def _increment_analysis(self):
        """增加分析计数"""
        self.analysis_count += 1
    
    def __repr__(self):
        return f"{self.get_role()}(name='{self.name}')"
    
    def get_llm_stats(self) -> Optional[Dict[str, Any]]:
        """获取LLM调用统计（如果Agent使用LLM）"""
        if hasattr(self, 'llm') and hasattr(self.llm, 'get_stats'):
            return self.llm.get_stats()
        return None
