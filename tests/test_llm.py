"""
Test LLM Provider Module
"""

import pytest
from llm.provider import OllamaProvider, OpenRouterProvider, HybridLLMProvider


class TestOllamaProvider:
    """Test Ollama Provider"""
    
    def test_init(self):
        """Test initialization"""
        provider = OllamaProvider(model="qwen3.5:9b")
        assert provider.model == "qwen3.5:9b"
        assert provider.base_url == "http://localhost:11434"
    
    def test_get_model_name(self):
        """Test model name"""
        provider = OllamaProvider(model="qwen3.5:9b")
        assert provider.get_model_name() == "ollama/qwen3.5:9b"


class TestHybridLLMProvider:
    """Test Hybrid LLM Provider"""
    
    def test_init_with_primary_only(self):
        """Test initialization with primary only"""
        primary = OllamaProvider(model="qwen3.5:9b")
        hybrid = HybridLLMProvider(primary=primary)
        assert hybrid.primary == primary
        assert hybrid.fallback is None
    
    def test_get_stats(self):
        """Test statistics"""
        primary = OllamaProvider(model="qwen3.5:9b")
        hybrid = HybridLLMProvider(primary=primary)
        stats = hybrid.get_stats()
        assert "total_calls" in stats
        assert "primary_success" in stats
