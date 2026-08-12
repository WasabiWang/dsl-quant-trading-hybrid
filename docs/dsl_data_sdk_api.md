# DSL Data SDK API Documentation
## 版本: v4.4.0 稳定性增强版
## 最后更新: 2026-04-19

## 📋 概述

DSL Data SDK 是一个统一的数据提供SDK，为量化交易系统提供稳定、可靠的数据访问接口。支持多源回退、缓存机制和熔断保护。

## 🏗️ 架构设计

```
┌─────────────────────────────────────────────┐
│              DSL Data SDK v4.4.0            │
├─────────────────────────────────────────────┤
│ 稳定性增强层 (stability_system)             │
│  • 熔断保护 (Circuit Breaker)               │
│  • 重试机制 (Retry with Backoff)           │
│  • 降级策略 (Fallback to Cache)            │
├─────────────────────────────────────────────┤
│ 核心数据层 (Core Data Functions)            │
│  • get_price() - 实时价格                   │
│  • get_kline() - K线数据                    │
│  • get_fundamentals() - 基本面数据          │
├─────────────────────────────────────────────┤
│ 数据源层 (Data Sources)                     │
│  • 新浪财经 (Sina Finance)                  │
│  • 腾讯财经 (Tencent Finance)               │
│  • 东方财富 (Eastmoney)                     │
│  • AKShare (开源数据)                       │
│  • Tushare (专业数据)                       │
├─────────────────────────────────────────────┤
│ 缓存层 (Cache System)                       │
│  • 内存缓存 (In-memory)                     │
│  • 磁盘缓存 (Disk Cache)                    │
│  • 缓存过期策略 (TTL)                       │
└─────────────────────────────────────────────┘
```

## 📊 API 参考

### 1. get_price() - 获取实时价格

**函数签名:**
```python
def get_price(symbol: str, market: str = 'cn') -> dict
```

**参数说明:**
- `symbol` (str): 股票代码，支持格式:
  - A股: `"000001.SZ"`, `"600519.SH"`
  - 港股: `"00700.HK"`
  - 美股: `"AAPL.US"`
- `market` (str): 市场标识，默认 `'cn'` (中国)

**返回值:**
```python
{
    "symbol": "000001.SZ",
    "name": "平安银行",
    "price": 11.01,           # 当前价格
    "prev_close": 10.80,      # 昨收价
    "change": 0.21,           # 涨跌额
    "change_pct": 1.94,       # 涨跌幅(%)
    "volume": 12345678,       # 成交量
    "amount": 987654321,      # 成交额
    "high": 11.20,            # 最高价
    "low": 10.90,             # 最低价
    "source": "sina",         # 数据源
    "update_time": "2026-04-19 23:45:00",  # 更新时间
    "note": "实时数据"        # 备注信息
}
```

**数据源优先级:**
1. 新浪财经 (Sina Finance) - 主数据源
2. 腾讯财经 (Tencent Finance) - 备用源1
3. 东方财富 (Eastmoney) - 备用源2
4. 缓存数据 (Cache) - 降级源

**缓存策略:**
- 实时数据: 缓存5分钟 (300秒)
- 失败时: 返回缓存数据并标记为降级模式

### 2. get_kline() - 获取K线数据

**函数签名:**
```python
def get_kline(symbol: str, start: str, end: str, freq: str = 'day') -> list
```

**参数说明:**
- `symbol` (str): 股票代码
- `start` (str): 开始日期，格式 `"YYYY-MM-DD"`
- `end` (str): 结束日期，格式 `"YYYY-MM-DD"`
- `freq` (str): K线频率，可选值:
  - `'day'` - 日线 (默认)
  - `'week'` - 周线
  - `'month'` - 月线

**返回值:**
```python
[
    {
        "date": "2026-04-18",
        "open": 10.80,
        "high": 11.20,
        "low": 10.70,
        "close": 11.01,
        "volume": 12345678,
        "source": "akshare"
    },
    # ... 更多K线数据
]
```

**数据源优先级:**
1. AKShare - 主数据源 (历史数据完整)
2. 新浪财经 - 备用源1 (近期数据)
3. 东方财富 - 备用源2
4. 模拟数据 - 降级源

**缓存策略:**
- 历史数据: 缓存24小时 (86400秒)
- 模拟数据: 基于基础价格生成

### 3. get_fundamentals() - 获取基本面数据

**函数签名:**
```python
def get_fundamentals(symbol: str) -> dict
```

**参数说明:**
- `symbol` (str): 股票代码

**返回值:**
```python
{
    "symbol": "000001.SZ",
    "pe": 15.5,               # 市盈率
    "pb": 2.1,                # 市净率
    "dividend_yield": 2.5,    # 股息率(%)
    "market_cap": 50000000000, # 市值(元)
    "update_time": "2026-04-19 23:45:00",
    "source": "tushare",
    "note": "专业基本面数据"
}
```

**数据源优先级:**
1. Tushare - 主数据源 (需要API Token)
2. AKShare - 备用源1
3. 新浪财经 - 备用源2 (有限字段)
4. 模拟数据 - 降级源

**缓存策略:**
- 基本面数据: 缓存24小时 (86400秒)

## 🔧 系统管理函数

### 4. get_system_status() - 获取系统状态

**函数签名:**
```python
def get_system_status() -> dict
```

**返回值:**
```python
{
    "data_fetch": {
        "circuit_state": "CLOSED",      # 熔断器状态: CLOSED/OPEN/HALF_OPEN
        "failure_count": 0,             # 失败次数
        "last_failure": None,           # 最后失败时间
        "success_rate": 1.0,            # 成功率
        "total_requests": 100           # 总请求数
    },
    "cache": {
        "hit_rate": 0.85,               # 缓存命中率
        "total_items": 50,              # 缓存项数
        "memory_usage": "12.5MB"        # 内存使用
    },
    "sources": {
        "sina": {"available": True, "latency": 0.12},
        "tencent": {"available": True, "latency": 0.15},
        "eastmoney": {"available": True, "latency": 0.18}
    }
}
```

### 5. reset_data_fetch_circuit() - 重置熔断器

**函数签名:**
```python
def reset_data_fetch_circuit() -> None
```

**功能说明:**
- 重置数据获取模块的熔断器状态
- 用于手动恢复故障状态

## ⚙️ 配置参数

### 环境变量
```bash
# Tushare API Token (用于基本面数据)
export TUSHARE_TOKEN="your_token_here"

# 缓存配置
export DSL_CACHE_TTL_PRICE=300        # 价格缓存时间(秒)
export DSL_CACHE_TTL_KLINE=86400      # K线缓存时间(秒)
export DSL_CACHE_TTL_FUND=86400       # 基本面缓存时间(秒)

# 熔断器配置
export DSL_CIRCUIT_FAILURE_THRESHOLD=10   # 失败阈值
export DSL_CIRCUIT_RESET_TIMEOUT=60       # 重置超时(秒)
```

### 配置文件
位置: `config/data_sources.yaml`
```yaml
data_sources:
  realtime:
    priority: ["sina", "tencent", "eastmoney"]
    timeout: 5
    retry: 2
  
  kline:
    priority: ["akshare", "sina", "eastmoney"]
    timeout: 10
    retry: 1
  
  fundamental:
    priority: ["tushare", "akshare", "sina"]
    timeout: 8
    retry: 1

cache:
  enabled: true
  ttl:
    price: 300
    kline: 86400
    fundamental: 86400
  max_size_mb: 100

circuit_breaker:
  failure_threshold: 10
  reset_timeout: 60
  half_open_max_requests: 3
```

## 🚀 使用示例

### 基础使用
```python
from dsl_data_sdk import get_price, get_kline, get_fundamentals

# 获取实时价格
price_data = get_price("000001.SZ")
print(f"{price_data['name']}: {price_data['price']}")

# 获取K线数据
kline_data = get_kline("000001.SZ", "2026-04-01", "2026-04-19")
print(f"获取到 {len(kline_data)} 条K线数据")

# 获取基本面数据
fund_data = get_fundamentals("000001.SZ")
print(f"市盈率: {fund_data['pe']}")
```

### 错误处理
```python
from dsl_data_sdk import get_price, get_system_status

try:
    data = get_price("INVALID.SYMBOL")
except Exception as e:
    print(f"数据获取失败: {e}")
    
    # 检查系统状态
    status = get_system_status()
    if status["data_fetch"]["circuit_state"] == "OPEN":
        print("熔断器已打开，请稍后重试")
```

### 批量处理
```python
import concurrent.futures
from dsl_data_sdk import get_price

symbols = ["000001.SZ", "600519.SH", "000858.SZ", "002415.SZ"]

def fetch_price(symbol):
    try:
        return get_price(symbol)
    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

# 并行获取
with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    results = list(executor.map(fetch_price, symbols))

for result in results:
    if "error" not in result:
        print(f"{result['symbol']}: {result['price']}")
```

## 🛡️ 稳定性特性

### 1. 熔断保护 (Circuit Breaker)
- 状态: CLOSED(正常) → OPEN(熔断) → HALF_OPEN(半开) → CLOSED
- 阈值: 连续10次失败触发熔断
- 恢复: 60秒后自动进入半开状态

### 2. 重试机制 (Retry with Backoff)
- 价格获取: 最多重试2次
- K线获取: 最多重试1次
- 退避策略: 指数退避 (1s, 2s, 4s)

### 3. 降级策略 (Fallback)
- 实时数据失败 → 返回缓存数据
- 缓存数据过期 → 返回模拟数据
- 模拟数据: 基于历史模式生成

### 4. 缓存系统
- 内存缓存: 快速访问
- 磁盘缓存: 持久化存储
- TTL策略: 不同数据类型不同过期时间

## 📈 性能指标

### 响应时间
- 实时价格: < 200ms (缓存命中), < 1000ms (网络获取)
- K线数据: < 500ms (缓存命中), < 3000ms (网络获取)
- 基本面: < 300ms (缓存命中), < 2000ms (网络获取)

### 可靠性
- 可用性: > 99.9%
- 缓存命中率: > 85%
- 错误率: < 0.1%

## 🔍 调试与监控

### 日志输出
```python
import logging

# 启用详细日志
logging.basicConfig(level=logging.DEBUG)

# 查看SDK内部日志
from dsl_data_sdk import get_price
data = get_price("000001.SZ")
```

### 监控端点
```bash
# 检查系统状态
curl http://localhost:8080/api/system/status

# 查看缓存统计
curl http://localhost:8080/api/cache/stats

# 重置熔断器
curl -X POST http://localhost:8080/api/circuit/reset
```

## 🐛 常见问题

### Q1: 获取数据返回缓存数据而不是实时数据？
**A:** 检查缓存TTL设置，默认实时数据缓存5分钟。可以设置环境变量 `DSL_CACHE_TTL_PRICE=0` 禁用缓存。

### Q2: Tushare基本面数据获取失败？
**A:** 确保设置了正确的 `TUSHARE_TOKEN` 环境变量，并且Token有足够的权限。

### Q3: 熔断器一直处于OPEN状态？
**A:** 使用 `reset_data_fetch_circuit()` 手动重置，或检查网络连接和数据源可用性。

### Q4: 并行请求时性能下降？
**A:** 默认线程池大小为5，可以通过环境变量 `DSL_MAX_WORKERS=10` 调整。

## 📞 支持与反馈

### 问题报告
1. 查看错误日志: `logs/dsl_errors.log`
2. 收集系统状态: `get_system_status()`
3. 提交Issue: [GitHub Issues](https://github.com/your-repo/issues)

### 性能优化建议
- 增加缓存大小提高命中率
- 调整数据源优先级
- 优化网络连接池

---

**文档版本**: v1.0.0  
**最后更新**: 2026-04-19  
**维护者**: DSL量化团队  
**联系方式**: team@dsl-quant.com