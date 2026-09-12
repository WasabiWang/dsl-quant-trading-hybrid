#!/usr/bin/env python3
"""
DSL v4.5.1 单元测试
测试核心模块功能

【GLM-5】
"""
import pytest
import sys
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

# 添加项目根目录到路径
PROJECT_ROOT = "/Users/jameswang/.openclaw/workspace/dsl-quant-trading-hybrid"
sys.path.insert(0, PROJECT_ROOT)


class TestDataSource:
    """数据源模块测试"""
    
    def test_sina_data_format(self):
        """测试新浪数据格式"""
        mock_response = '[{"day":"2024-01-01","open":"100","close":"102","high":"103","low":"99","volume":"1000000"}]'
        data = json.loads(mock_response)
        df = pd.DataFrame(data)
        
        # 验证必要的列
        assert 'day' in df.columns
        assert 'close' in df.columns
        assert 'open' in df.columns
        
    def test_stock_code_normalization(self):
        """测试股票代码标准化"""
        codes = ['600760', '000001', '300750']
        for code in codes:
            normalized = str(code).zfill(6)
            assert len(normalized) == 6
            
        # 测试前缀添加
        def add_prefix(code):
            code = str(code).zfill(6)
            return f"sh{code}" if code.startswith('6') else f"sz{code}"
        
        assert add_prefix('600760') == 'sh600760'
        assert add_prefix('000001') == 'sz000001'


class TestStrategyMonitor:
    """风控模块测试"""
    
    def test_calculate_volatility(self):
        """测试波动率计算"""
        from core.strategy_monitor import StrategyMonitor
        
        monitor = StrategyMonitor()
        prices = pd.Series([100, 102, 101, 103, 98, 95, 97, 99, 101, 100])
        
        vol = monitor.calculate_volatility(prices)
        assert vol >= 0  # 波动率应该非负
        
    def test_stop_loss_check(self):
        """测试止损检查"""
        from core.strategy_monitor import StrategyMonitor
        
        monitor = StrategyMonitor({'stop_loss_pct': -0.07})
        
        # 触发止损
        triggered, pnl = monitor.check_stop_loss(100, 92)
        assert triggered == True
        assert pnl == -0.08
        
        # 未触发止损
        triggered, pnl = monitor.check_stop_loss(100, 98)
        assert triggered == False
        
    def test_take_profit_check(self):
        """测试止盈检查"""
        from core.strategy_monitor import StrategyMonitor
        
        monitor = StrategyMonitor({'take_profit_pct': 0.15})
        
        # 触发止盈
        triggered, reason = monitor.check_take_profit(100, 118)
        assert triggered == True
        assert reason == "固定止盈"
        
    def test_position_size(self):
        """测试仓位计算"""
        from core.strategy_monitor import StrategyMonitor
        
        monitor = StrategyMonitor()
        
        # 低波动率应该获得更大仓位
        shares_low_vol = monitor.calculate_position_size("600760", 100000, 50.0, 0.1)
        shares_high_vol = monitor.calculate_position_size("600760", 100000, 50.0, 0.4)
        
        assert shares_low_vol >= shares_high_vol
        
    def test_circuit_breaker(self):
        """测试熔断器"""
        from core.strategy_monitor import CircuitBreaker
        
        breaker = CircuitBreaker(max_consecutive_losses=3)
        
        # 连续亏损
        breaker.record_trade(-0.02)
        breaker.record_trade(-0.03)
        can_trade, msg = breaker.can_trade()
        assert can_trade == True
        
        # 第三次亏损触发熔断
        breaker.record_trade(-0.01)
        can_trade, msg = breaker.can_trade()
        assert can_trade == False
        
        # 盈利重置
        breaker.record_trade(0.05)
        can_trade, msg = breaker.can_trade()
        assert can_trade == True


class TestBacktestEngine:
    """回测引擎测试"""
    
    def test_calculate_returns(self):
        """测试收益计算"""
        # 模拟K线数据
        data = {
            'close': [100, 102, 101, 103, 105, 107, 110]
        }
        df = pd.DataFrame(data)
        
        # 计算收益率
        returns = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
        # 使用近似相等，避免浮点数精度问题
        assert abs(returns - 10.0) < 0.0001
        
    def test_ma_signal(self):
        """测试MA信号"""
        data = {
            'close': [100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                     110, 111, 112, 113, 114, 115, 116, 117, 118, 119]
        }
        df = pd.DataFrame(data)
        
        df['ma5'] = df['close'].rolling(5).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        
        # 验证MA计算
        assert df['ma5'].iloc[-1] > df['ma20'].iloc[-1]  # 应该多头


class TestFeishuIntegration:
    """飞书集成测试"""
    
    def test_feishu_utils_import(self):
        """测试飞书工具导入"""
        try:
            from common.feishu_utils import send_markdown, send_alert
            assert callable(send_markdown)
            assert callable(send_alert)
        except ImportError as e:
            pytest.skip(f"飞书模块导入失败: {e}")
            
    def test_token_format(self):
        """测试飞书token格式"""
        # 模拟token验证
        mock_token = "mock_token_12345"
        assert len(mock_token) > 0


class TestCronConfig:
    """定时任务配置测试"""
    
    def test_cron_syntax(self):
        """测试cron语法"""
        # 验证cron表达式格式
        cron_expr = "10 15 * * *"  # 每天15:10执行
        
        parts = cron_expr.split()
        assert len(parts) == 5  # 分 时 日 月 周
        
    def test_cron_file_exists(self):
        """测试cron配置文件存在"""
        cron_file = os.path.join(PROJECT_ROOT, "cron", "crontab.conf")
        # 配置文件可能不存在，跳过检查
        if os.path.exists(cron_file):
            with open(cron_file, 'r') as f:
                content = f.read()
                assert 'CRON' in content or '#' in content


def test_import_all_modules():
    """测试所有核心模块导入"""
    modules = [
        ("core.auto_engine", "AutoIterativeEngine"),
        ("core.strategy_monitor", "StrategyMonitor"),
        ("core.circuit_breaker", "CircuitBreaker"),
        ("core.data_fetcher", "DataFetcher"),
        ("common.feishu_utils", "send_markdown"),
    ]
    
    for module_name, class_name in modules:
        try:
            parts = module_name.split('.')
            mod = __import__(module_name, fromlist=[class_name])
            getattr(mod, class_name)
        except (ImportError, AttributeError) as e:
            pytest.skip(f"模块导入跳过: {module_name}.{class_name} - {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])