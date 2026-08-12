#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek 全系列模型API调用工具
支持V3/V4 Pro/V4 Flash/R1思考版，内置3次指数退避重试
"""
import os
import time
from openai import OpenAI
from openai import APIError, APIConnectionError, RateLimitError, Timeout

# 初始化DeepSeek客户端
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
if not DEEPSEEK_API_KEY:
    raise ValueError("请配置环境变量DEEPSEEK_API_KEY")

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

def deepseek_chat(content, model="deepseek-v4-pro", temperature=0.3, response_format="text", max_tokens=4096, system_prompt=None):
    """
    调用DeepSeek聊天模型
    :param content: 用户提问内容
    :param model: 模型名：deepseek-v4-pro / deepseek-v4-flash / deepseek-chat(V3)
    :param temperature: 温度，0-2，量化场景建议0.1-0.3减少随机性
    :param response_format: text/json
    :param max_tokens: 最大输出长度
    :param system_prompt: 系统提示词，可选
    :return: 返回结果文本，json格式自动解析为字典
    """
    MAX_RETRIES = 3
    retry_delay = 1
    
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})
    
    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": response_format} if response_format == "json" else None
            )
            result = response.choices[0].message.content
            return result if response_format == "text" else eval(result)  # JSON格式自动转字典
        
        except (APIConnectionError, Timeout, RateLimitError) as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = retry_delay * (2 ** attempt)
                print(f"⚠️ [DeepSeek重试 {attempt+1}/{MAX_RETRIES}] 网络/限流异常: {str(e)}，等待{wait_time}秒后重试...")
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ [DeepSeek失败] 重试{MAX_RETRIES}次全部失败: {str(e)}")
                raise
        except APIError as e:
            if e.status_code >= 500 and attempt < MAX_RETRIES - 1:
                wait_time = retry_delay * (2 ** attempt)
                print(f"⚠️ [DeepSeek重试 {attempt+1}/{MAX_RETRIES}] 服务端错误: {str(e)}，等待{wait_time}秒后重试...")
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ [DeepSeek失败] 服务端错误: {str(e)}")
                raise
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = retry_delay * (2 ** attempt)
                print(f"⚠️ [DeepSeek重试 {attempt+1}/{MAX_RETRIES}] 未知异常: {str(e)}，等待{wait_time}秒后重试...")
                time.sleep(wait_time)
                continue
            else:
                print(f"❌ [DeepSeek失败] 重试{MAX_RETRIES}次全部失败: {str(e)}")
                raise

def deepseek_reasoner(content, model="deepseek-reasoner", temperature=0.7, max_tokens=8192, system_prompt=None):
    """
    调用DeepSeek R1思考版模型，适合复杂推理、逻辑推演场景
    :param content: 用户提问内容
    :param model: 模型名，默认deepseek-reasoner
    :param temperature: 温度，0-1
    :param max_tokens: 最大输出长度，默认8K
    :param system_prompt: 系统提示词，可选
    :return: 思考过程+最终结果
    """
    return deepseek_chat(
        content=content,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        system_prompt=system_prompt
    )
