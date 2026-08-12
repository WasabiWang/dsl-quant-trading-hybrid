# 全链路接口规范v1.0
## 通用规范
1. 所有接口输入输出均采用JSON格式，编码为UTF-8
2. 所有返回结果均包含基础字段：code（状态码，200=成功，非200=失败）、msg（状态描述）、data（返回数据）、request_id（请求唯一ID）
3. 所有时间字段均采用毫秒级时间戳，时区为Asia/Shanghai
4. 所有金额字段单位为人民币分，避免浮点数精度问题
## 各层接口规范
### 1_data_platform（数据层）
#### 获取行情数据接口
**输入**
```json
{
  "symbol": "股票/标的代码",
  "period": "周期：1m/5m/15m/30m/1h/1d/1w/1M",
  "start_time": "开始时间戳（毫秒）",
  "end_time": "结束时间戳（毫秒）",
  "limit": "返回条数限制，默认1000"
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": [
    {
      "time": "时间戳",
      "open": "开盘价",
      "high": "最高价",
      "low": "最低价",
      "close": "收盘价",
      "volume": "成交量",
      "amount": "成交额"
    }
  ],
  "request_id": "xxx"
}
```
#### 获取基本面数据接口
**输入**
```json
{
  "symbol": "股票代码",
  "fields": ["需要返回的字段列表：pe/pb/roe/eps/income/profit等"]
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "pe": 12.34,
    "pb": 1.56,
    "roe": 0.18,
    "eps": 2.34,
    "update_time": "更新时间戳"
  },
  "request_id": "xxx"
}
```
### 3_decision_engine（决策层）
#### 信号生成接口
**输入**
```json
{
  "symbol": "标的代码",
  "market": "市场：CN/HK/US",
  "context": "上下文信息，包含行情、基本面、宏观数据"
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "symbol": "标的代码",
    "direction": "方向：long/short/hold",
    "strength": "信号强度：0-100，越高可信度越高",
    "confidence": "置信度：0-1",
    "reason": "决策理由",
    "tags": ["标签列表：例如：高景气、低估值、突破信号等"],
    "generate_time": "生成时间戳"
  },
  "request_id": "xxx"
}
```
#### 交易决策接口
**输入**
```json
{
  "signals": ["信号列表"],
  "portfolio": "当前持仓信息",
  "max_position_ratio": "最大仓位上限，0-1"
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "orders": [
      {
        "symbol": "标的代码",
        "action": "动作：buy/sell/hold",
        "quantity": "数量",
        "price_range": ["价格下限", "价格上限"],
        "stop_loss": "止损价",
        "stop_profit": "止盈价",
        "reason": "决策理由",
        "confidence": "置信度"
      }
    ],
    "max_position_ratio": "最终仓位上限",
    "decision_time": "决策时间戳"
  },
  "request_id": "xxx"
}
```
### 4_execution_engine（执行层）
#### 下单接口
**输入**
```json
{
  "symbol": "标的代码",
  "action": "buy/sell",
  "quantity": "数量",
  "price": "价格，不传为市价单",
  "order_type": "limit/market",
  "account_type": "simulate/real"
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "order_id": "订单唯一ID",
    "status": "状态：pending/partially_filled/filled/cancelled/failed",
    "filled_quantity": "成交数量",
    "filled_price": "成交均价",
    "fee": "手续费",
    "slippage": "滑点",
    "execute_time": "执行时间戳"
  },
  "request_id": "xxx"
}
```
### 6_report_framework（输出层）
#### 生成报告接口
**输入**
```json
{
  "report_type": "报告类型：pre_market/intraday/post_market/risk/backtest",
  "market": "市场：CN/HK/ALL",
  "date": "报告日期：YYYY-MM-DD，默认当日"
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "report_id": "报告唯一ID",
    "title": "报告标题",
    "content": "报告内容（Markdown格式）",
    "generate_time": "生成时间戳"
  },
  "request_id": "xxx"
}
```
#### 发送报告接口
**输入**
```json
{
  "report_id": "报告ID",
  "channel": "发送渠道：feishu/email/dingtalk/wechat",
  "targets": ["接收人列表"]
}
```
**输出**
```json
{
  "code": 200,
  "msg": "success",
  "data": {
    "send_status": "success/failed",
    "send_time": "发送时间戳"
  },
  "request_id": "xxx"
}
```
## 跨层调用示例
### 盘前决策全链路调用流程
1. 数据层获取全市场行情、基本面数据
2. 策略层计算因子，生成候选标的列表
3. 决策层基于宏观、行业、个股数据生成交易信号，进行风险校验
4. 执行层根据信号执行交易（模拟/实盘）
5. 输出层生成盘前决策报告，发送到飞书
## 错误码规范
- 200：成功
- 400：参数错误
- 401：权限不足
- 404：资源不存在
- 429：请求过于频繁，触发限流
- 500：服务器内部错误
- 503：服务不可用，下游依赖故障
## 版本信息
- 版本号：v1.0
- 生效日期：2026-04-14
- 维护人：DSL量化系统团队