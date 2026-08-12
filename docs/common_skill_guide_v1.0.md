# 公共Skill库使用指南v1.0
## 概述
公共Skill库存放在`common/`目录下，是全系统通用的工具函数库，所有模块都可以直接调用，无需重复开发相同功能。目前包含5大类工具：日志、时间处理、配置读取、JSON校验、HTTP请求。
## 使用说明
### 1. 日志工具（common.logger）
统一日志格式，自动写入到指定目录，支持控制台输出+文件输出双模式，自动按日期分割日志文件。
#### 示例代码
```python
from common.logger import get_logger
# 初始化日志，name为模块名，level为日志级别：DEBUG/INFO/WARN/ERROR
logger = get_logger(name="decision_engine", level="INFO")
# 日志输出
logger.debug("调试信息")
logger.info("普通信息")
logger.warn("警告信息")
logger.error("错误信息")
logger.exception("异常信息，自动打印堆栈")
```
#### 日志格式
```
[2026-04-14 14:30:00] [INFO] [decision_engine] 日志内容 | request_id=xxx
```
#### 日志路径
- 普通日志：`logs/{module_name}/{date}.log`
- 错误日志：`logs/{module_name}/{date}_error.log`
### 2. 时间处理工具（common.time_utils）
统一时间处理函数，支持多格式转换、时区处理、交易日判断等。
#### 常用方法
```python
from common.time_utils import *
# 获取当前时间戳（毫秒）
now_ts = get_current_timestamp()
# 时间戳转日期字符串（YYYY-MM-DD）
date_str = timestamp_to_date(now_ts)
# 时间戳转时间字符串（YYYY-MM-DD HH:MM:SS）
datetime_str = timestamp_to_datetime(now_ts)
# 日期字符串转时间戳
ts = date_to_timestamp("2026-04-14")
# 判断是否为交易日
is_trade_day = is_trading_day("2026-04-14", market="CN")
# 获取下一个交易日
next_trade_day = get_next_trading_day("2026-04-14", market="CN")
# 获取当日开盘/收盘时间戳
open_ts = get_market_open_time(now_ts, market="CN")
close_ts = get_market_close_time(now_ts, market="CN")
```
### 3. 配置读取工具（common.config）
统一全局配置读取，支持多环境配置、动态刷新配置。
#### 示例代码
```python
from common.config import get_config, reload_config
# 获取配置，支持嵌套读取
redis_host = get_config("cache.redis.host")
redis_port = get_config("cache.redis.port", default=6379)
# 动态刷新配置（配置文件修改后无需重启服务）
reload_config()
```
#### 配置文件路径
- 全局配置文件：`config/config.yaml`
- 环境配置文件：`config/config.{env}.yaml`，env由环境变量`ENV`指定，默认dev
### 4. JSON校验工具（common.json_validator）
统一JSON Schema校验，自动校验接口输入输出格式是否符合规范。
#### 示例代码
```python
from common.json_validator import validate_json
# 定义Schema
schema = {
  "type": "object",
  "properties": {
    "symbol": {"type": "string"},
    "action": {"type": "string", "enum": ["buy", "sell"]},
    "quantity": {"type": "integer", "minimum": 100}
  },
  "required": ["symbol", "action", "quantity"]
}
# 校验数据
data = {"symbol": "600000", "action": "buy", "quantity": 100}
is_valid, error_msg = validate_json(data, schema)
if not is_valid:
  print(f"数据校验失败：{error_msg}")
```
### 5. HTTP请求工具（common.http_client）
统一HTTP请求客户端，自动处理重试、超时、限流、异常捕获，支持代理配置。
#### 示例代码
```python
from common.http_client import HttpClient
# 初始化客户端，默认重试3次，超时10秒
client = HttpClient(retry=3, timeout=10)
# GET请求
response = client.get("https://api.example.com/data", params={"param1": "value1"})
# POST请求（JSON格式）
response = client.post("https://api.example.com/submit", json={"key": "value"})
# POST请求（表单格式）
response = client.post("https://api.example.com/submit", data={"key": "value"})
# 响应处理
if response.status_code == 200:
  data = response.json()
else:
  print(f"请求失败：{response.status_code} {response.text}")
```
#### 配置说明
- 代理配置：在config.yaml中配置`http.proxy`，支持HTTP/HTTPS代理
- 重试配置：默认重试3次，重试间隔1秒，仅对GET请求和幂等POST请求重试
## 最佳实践
1. 所有模块必须使用公共Skill库，禁止自行开发相同功能的工具函数
2. 新增通用工具函数需要提交PR，经过评审后加入公共Skill库
3. 公共Skill库的修改必须保证向下兼容，避免影响现有模块
4. 工具函数的修改需要同步更新本文档和使用示例
## 版本信息
- 版本号：v1.0
- 生效日期：2026-04-14
- 维护人：DSL量化系统团队