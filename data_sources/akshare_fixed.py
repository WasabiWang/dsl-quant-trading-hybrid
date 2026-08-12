#!/usr/bin/env python3
"""
akshare修复包装器 - 绕过代理问题
"""

import os
import sys

# 永久禁用代理
os.environ['no_proxy'] = '*'
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)

# 导入原始akshare
import akshare as ak_original

class AkshareFixed:
    """修复后的akshare"""
    
    def __init__(self):
        self.session = None
        self._setup_session()
    
    def _setup_session(self):
        """配置requests session绕过代理"""
        import requests
        
        self.session = requests.Session()
        self.session.trust_env = False  # 不信任环境变量代理
        self.session.verify = False     # 临时禁用SSL验证
        
        # 这里需要修改akshare内部使用requests的方式
        # 由于akshare是闭源，我们只能通过猴子补丁
        
    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        """修复的历史数据接口"""
        try:
            # 使用原始接口，但环境已修复
            return ak_original.stock_zh_a_hist(
                symbol=symbol,
                period=period,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust
            )
        except Exception as e:
            print(f"akshare历史数据失败: {e}")
            return None
    
    def stock_zh_a_spot(self):
        """修复的实时数据接口"""
        try:
            return ak_original.stock_zh_a_spot()
        except Exception as e:
            print(f"akshare实时数据失败: {e}")
            return None

# 创建全局实例
ak = AkshareFixed()

# 导出常用函数
def stock_zh_a_hist(symbol, period, start_date, end_date, adjust):
    return ak.stock_zh_a_hist(symbol, period, start_date, end_date, adjust)

def stock_zh_a_spot():
    return ak.stock_zh_a_spot()

if __name__ == "__main__":
    # 测试
    df = stock_zh_a_hist('000001', 'daily', '20260401', '20260410', 'qfq')
    if df is not None:
        print(f"✅ 修复成功: {len(df)} 条记录")
    else:
        print("❌ 修复失败")
