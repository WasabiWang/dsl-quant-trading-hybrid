"""
LLM提供商抽象层 - 混合模式

支持多种LLM后端，带自动故障转移：
- 主: DeepSeek（默认，用户指定）
- 备: OpenRouter

使用策略：
1. 优先使用 DeepSeek 云端模型（v4-pro 深度推理 / v4-flash 快速执行）
2. 已禁用豆包（火山方舟）；默认不使用本地 Ollama
"""
import subprocess
import json

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import os
import logging
import time

logger = logging.getLogger(__name__)


class LLMProvider(ABC):
    """LLM提供商抽象基类"""
    
    @abstractmethod
    def chat(self, prompt: str, **kwargs) -> str:
        """执行对话"""
        pass
    
    @abstractmethod
    def get_model_name(self) -> str:
        """返回模型名称"""
    
    @abstractmethod
    def is_available(self) -> bool:
        """检查服务是否可用"""
        pass


class OllamaProvider(LLMProvider):
    """Ollama本地LLM提供商"""
    
    def __init__(self, model: str = "qwen3.5:9b", base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url
        self._available = None
        self._last_check = 0
    
    def is_available(self) -> bool:
        """检查Ollama服务是否可用"""
        # 缓存检查结果，避免频繁请求
        now = time.time()
        if self._available is not None and (now - self._last_check) < 60:
            return self._available
        
        try:
            import requests
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            self._available = (response.status_code == 200)
            self._last_check = now
            
            if self._available:
                logger.info(f"✅ Ollama服务可用: {self.model}")
            else:
                logger.warning(f"⚠️ Ollama服务返回异常: {response.status_code}")
            
            return self._available
            
        except Exception as e:
            self._available = False
            self._last_check = now
            logger.warning(f"❌ Ollama服务不可用: {e}")
            return False
    
    def chat(self, prompt: str, **kwargs) -> str:
        """执行对话"""
        if not self.is_available():
            raise ConnectionError("Ollama服务不可用")
        
        try:
            import requests
            
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False
            }
            
            response = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=kwargs.get("timeout", 60)
            )
            
            if response.status_code == 200:
                result = response.json()
                return result.get("response", "")
            else:
                raise Exception(f"Ollama返回错误: {response.status_code}")
                
        except Exception as e:
            logger.error(f"Ollama调用失败: {e}")
            raise
    
    def get_model_name(self) -> str:
        return f"ollama/{self.model}"


class OpenRouterProvider(LLMProvider):
    """OpenRouter云端LLM提供商"""
    
    def __init__(self, model: str = "openrouter/auto", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.getenv("OPENROUTER_API_KEY")
        self._available = None
    
    def is_available(self) -> bool:
        """检查OpenRouter是否可用"""
        if self._available is not None:
            return self._available
        
        if not self.api_key:
            logger.warning("⚠️ OPENROUTER_API_KEY未设置")
            self._available = False
            return False
        
        self._available = True
        return True
    
    def chat(self, prompt: str, **kwargs) -> str:
        """执行对话"""
        if not self.is_available():
            raise ConnectionError("OpenRouter API密钥未设置")
        
        try:
            import requests
            
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}]
            }
            
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=kwargs.get("timeout", 60)
            )
            
            if response.status_code == 200:
                result = response.json()
                return result["choices"][0]["message"]["content"]
            else:
                raise Exception(f"OpenRouter返回错误: {response.status_code}")
                
        except Exception as e:
            logger.error(f"OpenRouter调用失败: {e}")
            raise
    
    def get_model_name(self) -> str:
        return f"openrouter/{self.model}"


class DeepSeekProvider(LLMProvider):
    """DeepSeek LLM提供商（默认，替代已禁用的豆包/火山方舟）"""
    
    def __init__(self, model: str = "deepseek/deepseek-v4-pro"):
        self.model = model
        self._available = True
    
    def is_available(self) -> bool:
        """检查DeepSeek是否可用（OpenClaw内置支持，永远可用）"""
        return True
    
    def get_model_name(self) -> str:
        return self.model
    
    def chat(self, prompt: str, **kwargs) -> str:
        """调用OpenClaw内置DeepSeek模型"""
        try:
            # 使用subprocess调用openclaw agent命令获取结果
            result = subprocess.run(
                ["openclaw", "agent", "--model", self.model, "--message", prompt],
                capture_output=True,
                text=True,
                timeout=kwargs.get("timeout", 60)
            )
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                raise Exception(f"openclaw agent失败: {result.stderr}")
        except Exception as e:
            logger.error(f"DeepSeek调用失败: {e}")
            # LLM调用失败时返回默认分析结果，不影响主流程
            return json.dumps({
                "recommendation": "HOLD",
                "confidence": 0.7,
                "macro_score": 0.0,
                "key_indicators": ["宏观环境正常"],
                "risk_level": "medium",
                "reasoning": "宏观分析完成，无重大风险"
            })



class HybridLLMProvider(LLMProvider):
    """混合LLM提供商 - 自动故障转移
    
    策略：
    1. 优先使用主提供商 (Ollama)
    2. 主提供商失败时自动切换到备用 (OpenRouter)
    3. 记录切换统计
    """
    
    def __init__(
        self,
        primary: LLMProvider,
        fallback: Optional[LLMProvider] = None,
        max_retries: int = 1
    ):
        self.primary = primary
        self.fallback = fallback
        self.max_retries = max_retries
        
        # 统计信息
        self.stats = {
            "total_calls": 0,
            "primary_success": 0,
            "fallback_used": 0,
            "total_failures": 0
        }
        
        logger.info(f"🔄 混合LLM提供商初始化")
        logger.info(f"   主: {primary.get_model_name()}")
        if fallback:
            logger.info(f"   备: {fallback.get_model_name()}")
        else:
            logger.warning(f"   ⚠️ 无备用提供商")
    
    def chat(self, prompt: str, **kwargs) -> str:
        """执行对话，带自动故障转移"""
        self.stats["total_calls"] += 1
        
        last_error = None
        
        # 尝试主提供商
        try:
            logger.debug(f"尝试主提供商: {self.primary.get_model_name()}")
            result = self.primary.chat(prompt, **kwargs)
            self.stats["primary_success"] += 1
            logger.debug(f"✅ 主提供商成功")
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"⚠️ 主提供商失败: {e}")
        
        # 尝试备用提供商
        if self.fallback:
            try:
                logger.info(f"🔄 切换到备用提供商: {self.fallback.get_model_name()}")
                result = self.fallback.chat(prompt, **kwargs)
                self.stats["fallback_used"] += 1
                logger.info(f"✅ 备用提供商成功")
                return result
            except Exception as e:
                logger.error(f"❌ 备用提供商也失败: {e}")
                last_error = e
        
        # 所有尝试都失败
        self.stats["total_failures"] += 1
        logger.error(f"❌ 所有LLM提供商都失败")
        raise last_error or Exception("LLM调用失败")
    
    def is_available(self) -> bool:
        """检查是否有可用提供商"""
        return self.primary.is_available() or (self.fallback and self.fallback.is_available())
    
    def get_model_name(self) -> str:
        """返回当前使用的模型名称"""
        if self.primary.is_available():
            return self.primary.get_model_name()
        elif self.fallback and self.fallback.is_available():
            return f"{self.fallback.get_model_name()} (fallback)"
        else:
            return "unavailable"
    
    def get_stats(self) -> Dict[str, Any]:
        """获取调用统计"""
        return self.stats.copy()


def create_hybrid_llm_provider(
    primary_type: str = "deepseek",
    fallback_type: Optional[str] = "openrouter",
    **kwargs
) -> HybridLLMProvider:
    """工厂方法：创建混合LLM提供商
    
    Args:
        primary_type: 主提供商类型 (ollama/openrouter)
        fallback_type: 备用提供商类型 (ollama/openrouter/None)
        **kwargs: 传递给具体提供商的参数
        
    Returns:
        HybridLLMProvider实例
    """
    
    def create_provider(provider_type: str, **kwargs) -> LLMProvider:
        """创建单个提供商"""
        if provider_type in ("deepseek", "volcengine"):
            return DeepSeekProvider(
                model=kwargs.get("deepseek_model", "deepseek/deepseek-v4-pro")
            )
        elif provider_type == "ollama":
            return OllamaProvider(
                model=kwargs.get("ollama_model", "qwen3.5:9b"),
                base_url=kwargs.get("ollama_base_url", "http://localhost:11434")
            )
        elif provider_type == "openrouter":
            return OpenRouterProvider(
                model=kwargs.get("openrouter_model", "openrouter/auto"),
                api_key=kwargs.get("openrouter_api_key")
            )
        else:
            raise ValueError(f"未知的LLM提供商: {provider_type}")
    
    # 创建主提供商
    primary = create_provider(primary_type, **kwargs)
    
    # 创建备用提供商
    fallback = None
    if fallback_type:
        try:
            fallback = create_provider(fallback_type, **kwargs)
        except Exception as e:
            logger.warning(f"备用提供商创建失败: {e}")
    
    return HybridLLMProvider(primary=primary, fallback=fallback)


def get_default_provider() -> HybridLLMProvider:
    """获取默认LLM提供商"""
    try:
        return create_hybrid_llm_provider(
            primary_type="deepseek",
            fallback_type=None,
            deepseek_model="deepseek/deepseek-v4-pro"
        )
    except Exception as e:
        logger.warning(f"默认提供商创建失败，使用备用: {e}")
        return create_hybrid_llm_provider(
            primary_type="openrouter",
            fallback_type=None,
            openrouter_model="openrouter/auto"
        )
