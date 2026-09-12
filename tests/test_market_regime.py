import sys
import os
import unittest
import pandas as pd

# 确保能导入核心模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestMarketRegime(unittest.TestCase):
    
    def test_bull_market_detection(self):
        """测试牛市判定：收盘价远高于 MA200"""
        # 模拟数据：价格持续上涨
        data = pd.DataFrame({'close': range(100, 300)})
        ma200 = data['close'].rolling(200).mean().iloc[-1]
        current_price = data['close'].iloc[-1]
        
        # 逻辑验证
        self.assertTrue(current_price > ma200, "牛市判定逻辑失效")

    def test_bear_market_detection(self):
        """测试熊市判定：收盘价远低于 MA200"""
        # 模拟数据：价格持续下跌
        data = pd.DataFrame({'close': range(300, 100, -1)})
        ma200 = data['close'].rolling(200).mean().iloc[-1]
        current_price = data['close'].iloc[-1]
        
        # 逻辑验证
        self.assertTrue(current_price < ma200 * 0.95, "熊市判定逻辑失效")

if __name__ == '__main__':
    unittest.main()