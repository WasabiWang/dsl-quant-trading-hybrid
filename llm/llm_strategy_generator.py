#!/usr/bin/env python3
"""
llm_strategy_generator.py - LLM策略生成器
将自然语言转换为DSL JSON策略定义

功能：
1. 自然语言描述 → DSL JSON
2. 策略参数验证
3. 策略模板生成
4. 策略优化建议

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import json
import re
import os
import sys
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from llm.provider import get_default_provider
    HAS_LLM = True
except ImportError:
    HAS_LLM = False
    print("⚠️ LLM提供商不可用，将使用模板模式")

class LLMStrategyGenerator:
    """LLM策略生成器"""
    
    def __init__(self, llm_provider=None):
        """
        初始化策略生成器
        
        Args:
            llm_provider: LLM提供商实例，如果为None则使用默认提供商
        """
        if llm_provider:
            self.llm = llm_provider
        elif HAS_LLM:
            try:
                self.llm = get_default_provider()
            except:
                self.llm = None
        else:
            self.llm = None
        
        self.strategy_templates = self._load_strategy_templates()
        
    def _load_strategy_templates(self) -> Dict[str, Any]:
        """加载策略模板"""
        templates = {
            "trend_following": {
                "name": "趋势跟踪策略",
                "description": "基于移动平均线的趋势跟踪策略",
                "template": {
                    "name": "MA_Trend_Following",
                    "description": "基于双移动平均线的趋势跟踪策略",
                    "author": "LLM_Generator",
                    "version": "1.0.0",
                    "created_at": datetime.now().isoformat(),
                    "parameters": {
                        "fast_ma": {"type": "int", "default": 5, "min": 1, "max": 50, "description": "快速移动平均线周期"},
                        "slow_ma": {"type": "int", "default": 20, "min": 5, "max": 200, "description": "慢速移动平均线周期"},
                        "stop_loss": {"type": "float", "default": 0.05, "min": 0.01, "max": 0.2, "description": "止损比例"},
                        "take_profit": {"type": "float", "default": 0.1, "min": 0.02, "max": 0.3, "description": "止盈比例"}
                    },
                    "signals": [
                        {
                            "name": "buy_signal",
                            "condition": "fast_ma > slow_ma",
                            "action": "BUY",
                            "weight": 1.0
                        },
                        {
                            "name": "sell_signal",
                            "condition": "fast_ma < slow_ma",
                            "action": "SELL",
                            "weight": 1.0
                        }
                    ],
                    "filters": [
                        {
                            "name": "volume_filter",
                            "condition": "volume > volume.ma(20) * 0.8",
                            "description": "成交量过滤"
                        }
                    ]
                }
            },
            "mean_reversion": {
                "name": "均值回归策略",
                "description": "基于布林带的均值回归策略",
                "template": {
                    "name": "Bollinger_Mean_Reversion",
                    "description": "基于布林带的均值回归策略",
                    "author": "LLM_Generator",
                    "version": "1.0.0",
                    "created_at": datetime.now().isoformat(),
                    "parameters": {
                        "bb_period": {"type": "int", "default": 20, "min": 10, "max": 50, "description": "布林带周期"},
                        "bb_std": {"type": "float", "default": 2.0, "min": 1.0, "max": 3.0, "description": "布林带标准差倍数"},
                        "rsi_period": {"type": "int", "default": 14, "min": 7, "max": 30, "description": "RSI周期"},
                        "oversold": {"type": "float", "default": 30.0, "min": 10.0, "max": 40.0, "description": "超卖阈值"},
                        "overbought": {"type": "float", "default": 70.0, "min": 60.0, "max": 90.0, "description": "超买阈值"}
                    },
                    "signals": [
                        {
                            "name": "buy_signal",
                            "condition": "close < bb_lower and rsi < oversold",
                            "action": "BUY",
                            "weight": 1.0
                        },
                        {
                            "name": "sell_signal",
                            "condition": "close > bb_upper and rsi > overbought",
                            "action": "SELL",
                            "weight": 1.0
                        }
                    ]
                }
            },
            "breakout": {
                "name": "突破策略",
                "description": "基于价格突破的策略",
                "template": {
                    "name": "Price_Breakout",
                    "description": "基于价格高低点突破的策略",
                    "author": "LLM_Generator",
                    "version": "1.0.0",
                    "created_at": datetime.now().isoformat(),
                    "parameters": {
                        "lookback": {"type": "int", "default": 20, "min": 5, "max": 100, "description": "回顾周期"},
                        "breakout_threshold": {"type": "float", "default": 0.02, "min": 0.005, "max": 0.05, "description": "突破阈值"},
                        "confirmation_bars": {"type": "int", "default": 2, "min": 1, "max": 5, "description": "确认K线数"}
                    },
                    "signals": [
                        {
                            "name": "breakout_buy",
                            "condition": "close > high.max(lookback) * (1 + breakout_threshold)",
                            "action": "BUY",
                            "weight": 1.0
                        },
                        {
                            "name": "breakdown_sell",
                            "condition": "close < low.min(lookback) * (1 - breakout_threshold)",
                            "action": "SELL",
                            "weight": 1.0
                        }
                    ]
                }
            }
        }
        return templates
    
    def generate_from_natural_language(self, description: str, strategy_type: str = None) -> Dict[str, Any]:
        """
        从自然语言描述生成策略
        
        Args:
            description: 自然语言策略描述
            strategy_type: 策略类型（可选），如"trend_following", "mean_reversion", "breakout"
            
        Returns:
            DSL JSON策略定义
        """
        print(f"🔍 分析策略描述: {description[:100]}...")
        
        # 如果指定了策略类型，使用对应模板
        if strategy_type and strategy_type in self.strategy_templates:
            template = self.strategy_templates[strategy_type]["template"]
            print(f"📋 使用模板: {self.strategy_templates[strategy_type]['name']}")
            return self._customize_template(template, description)
        
        # 否则让LLM分析并生成
        return self._generate_with_llm(description)
    
    def _customize_template(self, template: Dict[str, Any], description: str) -> Dict[str, Any]:
        """基于模板和描述自定义策略"""
        # 这里可以添加基于描述的模板定制逻辑
        # 目前先返回模板
        return template.copy()
    
    def _generate_with_llm(self, description: str) -> Dict[str, Any]:
        """使用LLM生成策略"""
        prompt = f"""
        你是一个量化交易策略专家。请根据以下描述生成一个DSL JSON格式的交易策略。

        策略描述：
        {description}

        请生成一个完整的DSL JSON策略定义，包括：
        1. 策略名称（英文，使用下划线分隔）
        2. 策略描述
        3. 作者（设置为"LLM_Generator"）
        4. 版本号（"1.0.0"）
        5. 创建时间（当前ISO时间）
        6. 参数定义（类型、默认值、范围、描述）
        7. 信号定义（买入/卖出条件、动作、权重）
        8. 过滤器（可选）
        9. 风险控制参数（可选）

        DSL JSON格式示例：
        {{
            "name": "Strategy_Name",
            "description": "策略描述",
            "author": "LLM_Generator",
            "version": "1.0.0",
            "created_at": "2026-04-19T14:30:00",
            "parameters": {{
                "param1": {{"type": "int", "default": 10, "min": 1, "max": 50, "description": "参数描述"}}
            }},
            "signals": [
                {{
                    "name": "buy_signal",
                    "condition": "close > ma(close, 20)",
                    "action": "BUY",
                    "weight": 1.0
                }}
            ],
            "filters": [
                {{
                    "name": "volume_filter",
                    "condition": "volume > 1000000",
                    "description": "成交量过滤"
                }}
            ]
        }}

        请只返回JSON格式，不要有其他文本。
        """
        
        try:
            response = self.llm.chat(prompt)
            # 提取JSON部分
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                strategy_json = json.loads(json_match.group())
                print("✅ LLM策略生成成功")
                return strategy_json
            else:
                print("⚠️ 无法从LLM响应中提取JSON")
                return self._get_fallback_strategy(description)
        except Exception as e:
            print(f"❌ LLM策略生成失败: {e}")
            return self._get_fallback_strategy(description)
    
    def _get_fallback_strategy(self, description: str) -> Dict[str, Any]:
        """获取回退策略"""
        # 根据描述关键词选择模板
        description_lower = description.lower()
        
        if any(word in description_lower for word in ['趋势', 'moving average', 'ma', '均线']):
            template_type = "trend_following"
        elif any(word in description_lower for word in ['均值回归', 'bollinger', '布林', 'rsi']):
            template_type = "mean_reversion"
        elif any(word in description_lower for word in ['突破', 'breakout', '高低点']):
            template_type = "breakout"
        else:
            template_type = "trend_following"  # 默认
        
        template = self.strategy_templates[template_type]["template"]
        template["description"] = f"基于描述的策略: {description[:50]}..."
        return template
    
    def validate_strategy(self, strategy: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        验证策略定义
        
        Args:
            strategy: 策略定义
            
        Returns:
            (是否有效, 错误消息列表)
        """
        errors = []
        
        # 检查必需字段
        required_fields = ["name", "description", "author", "version", "created_at", "parameters", "signals"]
        for field in required_fields:
            if field not in strategy:
                errors.append(f"缺少必需字段: {field}")
        
        # 检查参数定义
        if "parameters" in strategy:
            for param_name, param_def in strategy["parameters"].items():
                if "type" not in param_def:
                    errors.append(f"参数 {param_name} 缺少类型定义")
                if "default" not in param_def:
                    errors.append(f"参数 {param_name} 缺少默认值")
        
        # 检查信号定义
        if "signals" in strategy:
            for i, signal in enumerate(strategy["signals"]):
                if "name" not in signal:
                    errors.append(f"信号 #{i+1} 缺少名称")
                if "condition" not in signal:
                    errors.append(f"信号 {signal.get('name', f'#{i+1}')} 缺少条件")
                if "action" not in signal:
                    errors.append(f"信号 {signal.get('name', f'#{i+1}')} 缺少动作")
        
        return len(errors) == 0, errors
    
    def save_strategy(self, strategy: Dict[str, Any], output_dir: str = "strategies/generated") -> str:
        """
        保存策略到文件
        
        Args:
            strategy: 策略定义
            output_dir: 输出目录
            
        Returns:
            保存的文件路径
        """
        # 创建目录
        os.makedirs(output_dir, exist_ok=True)
        
        # 生成文件名
        strategy_name = strategy.get("name", "unnamed_strategy").replace(" ", "_").lower()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{strategy_name}_{timestamp}.json"
        filepath = os.path.join(output_dir, filename)
        
        # 保存文件
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(strategy, f, ensure_ascii=False, indent=2)
        
        print(f"💾 策略已保存: {filepath}")
        return filepath
    
    def generate_strategy_report(self, strategy: Dict[str, Any]) -> str:
        """生成策略报告"""
        report = []
        report.append("=" * 60)
        report.append("📊 策略生成报告")
        report.append("=" * 60)
        report.append(f"策略名称: {strategy.get('name', 'N/A')}")
        report.append(f"策略描述: {strategy.get('description', 'N/A')}")
        report.append(f"作　　者: {strategy.get('author', 'N/A')}")
        report.append(f"版　　本: {strategy.get('version', 'N/A')}")
        report.append(f"创建时间: {strategy.get('created_at', 'N/A')}")
        
        # 参数统计
        params = strategy.get('parameters', {})
        report.append(f"参数数量: {len(params)}")
        if params:
            report.append("参数列表:")
            for param_name, param_def in params.items():
                param_type = param_def.get('type', 'unknown')
                default = param_def.get('default', 'N/A')
                desc = param_def.get('description', '')
                report.append(f"  • {param_name}: {param_type} = {default} ({desc})")
        
        # 信号统计
        signals = strategy.get('signals', [])
        report.append(f"信号数量: {len(signals)}")
        if signals:
            report.append("信号列表:")
            for signal in signals:
                name = signal.get('name', 'unnamed')
                action = signal.get('action', 'N/A')
                condition = signal.get('condition', 'N/A')[:50]
                report.append(f"  • {name}: {action} when {condition}...")
        
        report.append("=" * 60)
        return "\n".join(report)


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='LLM策略生成器')
    parser.add_argument('--description', type=str, required=True, help='策略描述（自然语言）')
    parser.add_argument('--type', type=str, choices=['trend_following', 'mean_reversion', 'breakout'], 
                       help='策略类型（可选）')
    parser.add_argument('--output', type=str, default='strategies/generated', help='输出目录')
    parser.add_argument('--test', action='store_true', help='测试模式（不使用真实LLM）')
    
    args = parser.parse_args()
    
    # 创建生成器
    if args.test:
        # 测试模式使用模拟LLM
        class MockLLM:
            def chat(self, prompt):
                return json.dumps({
                    "name": "Test_Strategy",
                    "description": args.description[:100],
                    "author": "LLM_Generator",
                    "version": "1.0.0",
                    "created_at": datetime.now().isoformat(),
                    "parameters": {
                        "ma_period": {"type": "int", "default": 20, "min": 5, "max": 50, "description": "移动平均线周期"}
                    },
                    "signals": [
                        {
                            "name": "buy_signal",
                            "condition": "close > ma(close, ma_period)",
                            "action": "BUY",
                            "weight": 1.0
                        }
                    ]
                })
        
        generator = LLMStrategyGenerator(MockLLM())
    else:
        generator = LLMStrategyGenerator()
    
    # 生成策略
    strategy = generator.generate_from_natural_language(args.description, args.type)
    
    # 验证策略
    is_valid, errors = generator.validate_strategy(strategy)
    if not is_valid:
        print("❌ 策略验证失败:")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    
    # 生成报告
    report = generator.generate_strategy_report(strategy)
    print(report)
    
    # 保存策略
    filepath = generator.save_strategy(strategy, args.output)
    print(f"✅ 策略已保存到: {filepath}")


if __name__ == "__main__":
    main()