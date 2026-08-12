# 统一缓存机制接入手册v1.0
## 概述
统一缓存机制采用本地内存缓存+Redis分布式缓存双级架构，自动处理缓存失效、缓存击穿、缓存雪崩问题，大幅提升系统运行效率，避免重复计算和重复API请求。
## 架构设计
```
┌─────────────────┐    命中    ┌─────────────────┐
│  应用请求       │──────────▶│  本地内存缓存    │
└─────────────────┘            └─────────────────┘
         │                            │ 未命中
         │                            ▼
         │                     ┌─────────────────┐
         │                     │  Redis缓存      │
         │                     └─────────────────┘
         │                            │ 未命中
         │                            ▼
         │                     ┌─────────────────┐
         └────────────────────▶│  源数据查询     │
                               └─────────────────┘
                                         │ 写入缓存
                                         ▼
                                ┌─────────────────┐
                                │  同时写入两级缓存│
                                └─────────────────┘
```
## 特性
1. **双级缓存**：本地内存缓存（L1）+ Redis分布式缓存（L2），兼顾速度和分布式一致性
2. **自动过期**：每个缓存key支持自定义过期时间，自动清理过期数据
3. **缓存击穿防护**：热点key过期时，采用分布式锁保证只有一个请求回源查询
4. **缓存雪崩防护**：过期时间自动添加随机偏移，避免大量key同时过期
5. **缓存穿透防护**：空结果也会缓存，避免恶意请求穿透到数据源
6. **命中统计**：自动统计缓存命中率、回源率、平均耗时等指标
## 接入方法
### 基础使用
```python
from common.cache import cache
# 写入缓存，expire为过期时间（秒），默认3600秒
cache.set("key", "value", expire=3600)
# 读取缓存，不存在返回None
value = cache.get("key")
# 删除缓存
cache.delete("key")
# 判断缓存是否存在
exists = cache.exists("key")
# 批量写入
cache.batch_set({"key1": "value1", "key2": "value2"}, expire=3600)
# 批量读取
values = cache.batch_get(["key1", "key2"])
```
### 装饰器使用（自动缓存函数返回结果）
```python
from common.cache import cache
# 自动缓存函数返回结果，key为函数名+参数哈希，expire为过期时间
@cache.cacheable(expire=3600)
def get_stock_data(symbol, period):
  # 从数据源查询数据的逻辑
  return data
# 调用函数时，如果缓存中存在结果则直接返回，否则执行函数并缓存结果
data = get_stock_data("600000", "1d")
```
### 自定义缓存前缀
```python
from common.cache import Cache
# 创建自定义前缀的缓存实例，避免不同模块的key冲突
my_cache = Cache(prefix="decision_engine")
my_cache.set("key", "value")
value = my_cache.get("key")
```
## 配置说明
配置文件路径：`config/config.yaml`
```yaml
cache:
  # 本地内存缓存配置
  local:
    enable: true # 是否启用本地缓存，默认true
    max_size: 10000 # 最大缓存key数量，超过后自动清理最近最少使用的key
    default_expire: 3600 # 默认过期时间（秒）
  # Redis缓存配置
  redis:
    enable: true # 是否启用Redis缓存，默认true
    host: 127.0.0.1 # Redis地址
    port: 6379 # Redis端口
    password: "" # Redis密码，无则留空
    db: 0 # Redis数据库编号
    default_expire: 86400 # 默认过期时间（秒）
    max_connections: 100 # 最大连接数
  # 高级配置
  advanced:
    lock_timeout: 10 # 分布式锁超时时间（秒），防止死锁
    random_offset: 300 # 过期时间随机偏移范围（秒），避免缓存雪崩
    cache_null: true # 是否缓存空结果，防止缓存穿透
    null_expire: 60 # 空结果过期时间（秒）
```
## 缓存key命名规范
所有缓存key必须遵循以下命名规范，避免冲突：
```
{模块名}:{功能名}:{参数哈希/唯一标识}
```
示例：
- `data_platform:market_data:600000:1d:20260414`
- `decision_engine:signal:600000:20260414`
- `strategy_factory:backtest_result:strategy1:20260414`
## 性能指标
- 本地缓存读取耗时：<1ms
- Redis缓存读取耗时：<5ms
- 缓存命中率：平均>90%，数据层>95%
- 相同参数请求性能提升：10-100倍
## 最佳实践
1. **合理设置过期时间**：不经常变化的数据设置较长的过期时间（比如日线行情可以设置为24小时），实时数据设置较短的过期时间（比如分钟行情设置为1分钟）
2. **避免缓存大对象**：单个缓存value建议不要超过1MB，过大的对象会影响缓存性能
3. **热点key优化**：访问量特别大的热点key可以适当延长过期时间，或者添加预热机制
4. **禁止缓存敏感数据**：密码、密钥、用户隐私数据等禁止存入缓存
## 监控与统计
缓存统计数据会自动写入到`logs/cache_stats.log`，包含以下指标：
- 总请求次数
- 命中次数
- 命中率
- 回源次数
- 平均回源耗时
- 本地缓存命中率
- Redis缓存命中率
可以通过监控这些指标优化缓存配置和过期时间设置。
## 版本信息
- 版本号：v1.0
- 生效日期：2026-04-14
- 维护人：DSL量化系统团队