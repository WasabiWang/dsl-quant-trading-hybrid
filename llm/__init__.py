"""
LLM集成模块 - 轻量级大语言模型集成（混合模式）

支持：
- Ollama本地模型（主）
- OpenRouter云端模型（备）
- 自动故障转移
"""

from .provider import (
    LLMProvider,
    OllamaProvider,
    OpenRouterProvider,
    HybridLLMProvider,
    create_hybrid_llm_provider
)

__all__ = [
    "LLMProvider",
    "OllamaProvider",
    "OpenRouterProvider",
    "HybridLLMProvider",
    "create_hybrid_llm_provider"
]
