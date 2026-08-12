#!/usr/bin/env python3
"""
统一ID生成系统 - 为所有对象生成全局唯一标识
"""
import uuid
import time
import hashlib
from typing import Optional
from .logger import get_logger

logger = get_logger("id_generator")

class IDGenerator:
    """全局唯一ID生成器"""
    
    @staticmethod
    def generate_uuid() -> str:
        """生成标准UUID v4"""
        return str(uuid.uuid4())
    
    @staticmethod
    def generate_short_id(prefix: str = "") -> str:
        """生成短ID（用于日志、调试）"""
        # 时间戳 + 随机数
        timestamp = int(time.time() * 1000)  # 毫秒级时间戳
        random_part = uuid.uuid4().hex[:8]
        short_id = f"{timestamp:x}_{random_part}"
        return f"{prefix}{short_id}" if prefix else short_id
    
    @staticmethod
    def generate_trace_id(module: str = "SYSTEM") -> str:
        """生成追踪ID（与决策追踪兼容）"""
        timestamp = time.strftime("%Y%m%d%H%M%S")
        random_str = uuid.uuid4().hex[:6]
        return f"{timestamp}_{module}_{random_str}"
    
    @staticmethod
    def generate_content_id(content: str) -> str:
        """根据内容生成确定性ID（用于去重、缓存键）"""
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    
    @staticmethod
    def generate_order_id() -> str:
        """生成订单ID"""
        return f"ORD_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_report_id(report_type: str = "DAILY") -> str:
        """生成报告ID"""
        return f"{report_type}_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_skill_id(skill_name: str) -> str:
        """生成Skill ID"""
        return f"SKL_{IDGenerator.generate_content_id(skill_name)}"
    
    @staticmethod
    def generate_model_id(model_name: str) -> str:
        """生成模型ID"""
        return f"MDL_{IDGenerator.generate_content_id(model_name)}"
    
    @staticmethod
    def generate_decision_id(strategy_id: str = "UNKNOWN") -> str:
        """生成决策ID"""
        return f"DEC_{strategy_id}_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_signal_id(signal_type: str = "UNKNOWN") -> str:
        """生成交易信号ID"""
        return f"SIG_{signal_type}_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_strategy_id(strategy_name: str) -> str:
        """生成策略ID"""
        return f"STR_{IDGenerator.generate_content_id(strategy_name)}"
    
    @staticmethod
    def generate_rule_id(rule_name: str) -> str:
        """生成规则ID"""
        return f"RUL_{IDGenerator.generate_content_id(rule_name)}"
    
    @staticmethod
    def generate_version_id(resource_type: str = "STRATEGY") -> str:
        """生成版本ID"""
        return f"VER_{resource_type}_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_experiment_id(experiment_name: str) -> str:
        """生成灰度实验ID"""
        return f"EXP_{IDGenerator.generate_content_id(experiment_name)}"
    
    @staticmethod
    def generate_cost_id(task_name: str) -> str:
        """生成成本记录ID"""
        return f"COST_{IDGenerator.generate_content_id(task_name)}_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_alert_id(alert_level: str = "INFO") -> str:
        """生成告警ID"""
        return f"ALT_{alert_level}_{IDGenerator.generate_short_id()}"
    
    @staticmethod
    def generate_trace_id(module: str = "SYSTEM") -> str:
        """生成全链路追踪ID（上下文传递用）"""
        timestamp = time.strftime("%Y%m%d%H%M%S")
        random_str = uuid.uuid4().hex[:8]
        return f"TRC_{timestamp}_{module}_{random_str}"

# 全局实例
id_gen = IDGenerator()

# 导出所有常用ID生成函数
__all__ = ["id_gen", "IDGenerator"]

if __name__ == "__main__":
    print("=== ID生成器测试 ===")
    print(f"UUID: {IDGenerator.generate_uuid()}")
    print(f"短ID: {IDGenerator.generate_short_id('TASK_')}")
    print(f"追踪ID: {IDGenerator.generate_trace_id('DECISION')}")
    print(f"内容ID: {IDGenerator.generate_content_id('test content')}")
    print(f"订单ID: {IDGenerator.generate_order_id()}")
    print(f"报告ID: {IDGenerator.generate_report_id('WEEKLY')}")
    print(f"Skill ID: {IDGenerator.generate_skill_id('sentiment_analysis')}")
    print(f"模型 ID: {IDGenerator.generate_model_id('nemotron-3-super')}")