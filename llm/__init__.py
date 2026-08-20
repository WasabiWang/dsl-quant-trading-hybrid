"""
LLM集成模块 - 轻量级大语言模型集成（混合模式）

支持：
- DeepSeek云端模型（主，v4-pro 深度推理 / v4-flash 快速执行）
- OpenRouter云端模型（备）
- 自动故障转移
"""

from .provider import (
    LLMProvider,
    OllamaProvider,
    OpenRouterProvider,
    DeepSeekProvider,
    HybridLLMProvider,
    create_hybrid_llm_provider,
    get_default_provider
)

__all__ = [
    "LLMProvider",
    "OllamaProvider",
    "OpenRouterProvider",
    "DeepSeekProvider",
    "HybridLLMProvider",
    "create_hybrid_llm_provider",
    "get_default_provider"
]
