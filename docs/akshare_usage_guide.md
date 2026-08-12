# akshare 使用指南

## 问题回顾
在2026-04-20的训练中，遇到了以下akshare接口问题：
1. `stock_hsgt_north_net_flow_in_em` 接口不存在
2. `sw_index_daily` 接口不存在  
3. `stock_notice_report` 参数错误（用了`start_time/end_time`而不是`start_date/end_date`）
4. `stock_individual_fund_flow` 参数格式问题（`market`参数应该小写）

## 如何避免akshare接口问题

### 1. 查看akshare版本和可用接口
```python
import akshare as ak
import inspect

# 查看版本
print(f"akshare版本: {ak.__version__}")

# 查看所有可用接口
ak_members = [name for name in dir(ak) if not name.startswith('_')]
print(f"共有{len(ak_members)}个接口")

# 查找特定功能的接口
fund_flow_funcs = [name for name in ak_members if 'fund' in name.lower() or 'flow' in name.lower()]
north_funcs = [name for name in ak_members if 'north' in name.lower() or 'hsgt' in name.lower()]
sector_funcs = [name for name in ak_members if 'sw' in name.lower() or 'index' in name.lower()]
```

### 2. 查看函数签名和参数
```python
# 查看函数签名
func = getattr(ak, 'stock_individual_fund_flow')
sig = inspect.signature(func)
print(f"参数: {sig}")

# 输出示例： (stock: str = '600094', market: str = 'sh') -> pandas.core.frame.DataFrame
```

### 3. 正确的接口调用方式（基于akshare 1.18.55）

#### 资金流数据
```python
# 正确：market参数小写，股票代码不带市场前缀
df = ak.stock_individual_fund_flow(stock='000001', market='sz')
# 错误：market大写或股票代码带前缀
df = ak.stock_individual_fund_flow(stock='sz000001', market='SZ')
```

#### 北向资金数据
```python
# 获取北向资金历史数据
df = ak.stock_hsgt_hist_em(symbol='北向资金')

# 获取个股北向资金详情
df = ak.stock_hsgt_individual_em(symbol='002008')

# 获取北向资金持股统计
df = ak.stock_hsgt_hold_stock_em(market='沪股通', indicator='5日排行')
```

#### 行业指数数据
```python
# 获取申万行业指数
df = ak.index_hist_sw(symbol='801780')

# 获取普通指数
df = ak.stock_zh_index_daily(symbol='sh000922')
df = ak.stock_zh_index_daily_em(symbol='csi931151', start_date='20240101', end_date='20240420')
```

#### 公告数据
```python
# 正确：使用start_date/end_date参数
df = ak.stock_notice_report(symbol='000001', start_date='20240101', end_date='20240420')
# 错误：使用start_time/end_time
df = ak.stock_notice_report(symbol='000001', start_time='20240101', end_time='20240420')
```

#### 龙虎榜数据
```python
df = ak.stock_lhb_detail_em(start_date='20240401', end_date='20240420')
```

### 4. 错误处理和降级逻辑
```python
def safe_akshare_call(func_name, *args, **kwargs):
    """安全的akshare接口调用，带错误处理和降级"""
    try:
        func = getattr(ak, func_name)
        result = func(*args, **kwargs)
        if result is not None and len(result) > 0:
            return result
        else:
            raise ValueError(f"{func_name}返回空数据")
    except Exception as e:
        print(f"⚠️ akshare接口{func_name}调用失败: {e}")
        # 返回模拟数据或None
        return None

# 使用示例
df = safe_akshare_call('stock_individual_fund_flow', stock='000001', market='sz')
if df is None:
    # 使用模拟数据或备用数据源
    df = generate_mock_fund_flow_data()
```

### 5. 多数据源优先级配置
```python
DATA_SOURCE_PRIORITY = {
    'fund_flow': ['baostock', 'akshare', 'tushare', 'mock'],
    'northbound': ['akshare', 'tushare', 'mock'],
    'sector_index': ['baostock', 'akshare', 'mock'],
    'announcement': ['akshare', 'mock']
}

def get_data_with_fallback(data_type, symbol, **kwargs):
    """多数据源自动回退"""
    sources = DATA_SOURCE_PRIORITY.get(data_type, ['mock'])
    
    for source in sources:
        try:
            if source == 'akshare':
                return get_akshare_data(data_type, symbol, **kwargs)
            elif source == 'baostock':
                return get_baostock_data(data_type, symbol, **kwargs)
            elif source == 'tushare':
                return get_tushare_data(data_type, symbol, **kwargs)
            elif source == 'mock':
                return get_mock_data(data_type, symbol, **kwargs)
        except Exception as e:
            print(f"⚠️ {source}数据源失败: {e}")
            continue
    
    return None
```

### 6. 定期检查接口更新
akshare更新频繁（约每月更新），需要：
1. 定期更新akshare：`pip install akshare --upgrade`
2. 测试核心接口是否仍然可用
3. 更新接口调用文档

### 7. 推荐的数据源策略
基于本次经验，推荐以下数据源策略：

| 数据类型 | 首选数据源 | 备用数据源 | 说明 |
|---------|-----------|-----------|------|
| 日线数据 | baostock | akshare | baostock免费稳定，akshare可能有接口变化 |
| 资金流 | akshare | 模拟数据 | akshare的资金流接口相对稳定 |
| 北向资金 | akshare | 模拟数据 | 使用`stock_hsgt_*`系列接口 |
| 行业指数 | baostock | akshare | baostock的指数数据更稳定 |
| 公告数据 | akshare | 模拟数据 | 注意参数名是`start_date/end_date` |

### 8. 最佳实践总结
1. **先测试后使用**：新接口先写测试脚本验证
2. **添加错误处理**：所有akshare调用都要有try-catch
3. **实现降级逻辑**：当akshare失败时自动切换到备用数据源
4. **定期更新**：每月检查akshare更新和接口变化
5. **本地缓存**：下载历史数据到本地，减少对实时接口的依赖
6. **监控告警**：监控接口成功率，失败时发送告警

## 参考链接
- GitHub仓库：https://github.com/akfamily/akshare
- 官方文档：https://akshare.akfamily.xyz/
- 问题反馈：https://github.com/akfamily/akshare/issues

---
*最后更新：2026-04-20*
*基于akshare 1.18.55版本*