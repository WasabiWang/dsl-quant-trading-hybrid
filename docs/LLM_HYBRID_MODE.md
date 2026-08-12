# LLM混合模式使用指南

## 🎯 概述

系统实现了**主备自动故障转移**的LLM混合模式：

- **主**: Ollama本地模型 (`qwen3.5:9b`)
- **备**: OpenRouter云端模型 (`openrouter/auto`)

## 📊 工作流程

```
用户请求
   │
   ▼
┌─────────────┐
│ 尝试Ollama   │ ──── 成功 ────► 返回结果
│ (本地优先)   │
└──────┬──────┘
       │ 失败
       ▼
┌─────────────┐
│ 切换到       │ ──── 成功 ────► 返回结果
│ OpenRouter  │
│ (云端备用)   │
└──────┬──────┘
       │ 失败
       ▼
   抛出异常
```

## 🔧 使用示例

### 1. 基本使用

```python
from agents.analysts.sentiment_analyst import SentimentAnalyst

# 创建情绪分析师（自动使用混合LLM）
analyst = SentimentAnalyst()

# 执行分析（自动故障转移）
result = analyst.analyze("600519.SH", {})

print(result["recommendation"])  # BUY/SELL/HOLD
print(result["sentiment_score"])  # -1.0 to 1.0
```

### 2. 查看LLM统计信息

```python
# 获取调用统计
stats = analyst.get_llm_stats()
print(stats)
# 输出:
# {
#   "total_calls": 10,
#   "primary_success": 8,      # Ollama成功8次
#   "fallback_used": 2,        # OpenRouter备用2次
#   "total_failures": 0        # 总失败0次
# }
```

### 3. 自定义模型配置

```python
# 使用不同的模型
analyst = SentimentAnalyst(config={
    "ollama_model": "deepseek-r1:14b",  # 主模型
    "openrouter_model": "qwen/qwen3.6-plus:free"  # 备用模型
})
```

## ⚙️ 配置选项

### config/agents_config.yaml

```yaml
llm:
  # 主提供商: Ollama
  ollama:
    model: "qwen3.5:9b"
    base_url: "http://localhost:11434"
  
  # 备用提供商: OpenRouter
  openrouter:
    model: "openrouter/auto"
    api_key_env: "OPENROUTER_API_KEY"  # 环境变量名
```

## 🔍 日志示例

### 正常情况（Ollama可用）
```
✅ Ollama服务可用: qwen3.5:9b
尝试主提供商: ollama/qwen3.5:9b
✅ 主提供商成功
```

### 故障转移（Ollama不可用）
```
❌ Ollama服务不可用: Connection refused
⚠️ 主提供商失败: Ollama服务不可用
🔄 切换到备用提供商: openrouter/openrouter/auto
✅ 备用提供商成功
```

## 🚀 启动Ollama服务

```bash
# 1. 启动Ollama
ollama serve

# 2. 拉取模型（如果还没有）
ollama pull qwen3.5:9b

# 3. 验证服务
curl http://localhost:11434/api/tags
```

## 🔑 配置OpenRouter API密钥（可选）

如果希望备用模式可用：

```bash
# 方法1: 环境变量
export OPENROUTER_API_KEY="sk-or-xxxxxxxx"

# 方法2: .env文件
echo "OPENROUTER_API_KEY=sk-or-xxxxxxxx" >> .env
```

获取API密钥: https://openrouter.ai/keys

## 📊 性能对比

| 场景 | Ollama (qwen3.5:9b) | OpenRouter (auto) |
|------|---------------------|-------------------|
| 响应时间 | ~500ms | ~2000ms |
| 成本 | ¥0 | 免费额度内¥0 |
| 可用性 | 依赖本地服务 | 依赖网络 |
| 隐私性 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ |

## 💡 最佳实践

1. **开发环境**: 优先使用Ollama，快速迭代
2. **生产环境**: 配置OpenRouter备用，保障可用性
3. **监控统计**: 定期检查`get_llm_stats()`，了解切换频率
4. **成本控制**: OpenRouter设置使用限额

## ⚠️ 注意事项

1. **Ollama服务必须先启动**
   ```bash
   ollama serve  # 后台运行
   ```

2. **内存需求**
   - qwen3.5:9b 需要约6GB RAM
   - deepseek-r1:14b 需要约10GB RAM

3. **网络要求**
   - Ollama: 无需外网
   - OpenRouter: 需要访问api.openrouter.ai

## 🐛 故障排查

### Ollama连接失败
```bash
# 检查服务状态
ollama list

# 重启服务
ollama serve

# 检查端口
lsof -i :11434
```

### OpenRouter失败
```bash
# 检查API密钥
echo $OPENROUTER_API_KEY

# 测试API
curl -H "Authorization: Bearer $OPENROUTER_API_KEY" \
     https://openrouter.ai/api/v1/auth/key
```
