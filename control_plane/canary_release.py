#!/usr/bin/env python3
"""
灰度发布系统 - 支持新策略/规则的小流量测试
"""
import json
import time
import random
from typing import Dict, List, Any, Optional, Callable
from common.logger import get_logger
from common.id_generator import IDGenerator

logger = get_logger("canary_release")

class CanaryReleaseManager:
    """灰度发布管理器"""
    
    def __init__(self):
        self.active_experiments = {}  # experiment_id -> config
        self.experiment_results = {}  # experiment_id -> results
    
    def create_experiment(self,
                         name: str,
                         description: str,
                         traffic_percentage: float,  # 0.0 - 1.0
                         duration_hours: float = 24,
                         success_criteria: Dict = None) -> str:
        """创建灰度发布实验"""
        experiment_id = IDGenerator.generate_short_id(f"EXP_{name.upper()}_")
        
        self.active_experiments[experiment_id] = {
            "experiment_id": experiment_id,
            "name": name,
            "description": description,
            "traffic_percentage": max(0.0, min(1.0, traffic_percentage)),
            "duration_hours": duration_hours,
            "start_time": time.time(),
            "end_time": time.time() + (duration_hours * 3600),
            "success_criteria": success_criteria or {},
            "status": "ACTIVE",
            "stats": {
                "total_requests": 0,
                "canary_requests": 0,
                "control_requests": 0,
                "canary_success": 0,
                "control_success": 0
            }
        }
        
        logger.info(f"创建灰度实验: {name} [{experiment_id}] "
                   f"流量: {traffic_percentage*100:.1f}% "
                   f"时长: {duration_hours}h")
        return experiment_id
    
    def should_use_canary(self, experiment_id: str) -> bool:
        """判断当前请求是否应该走灰度版本"""
        if experiment_id not in self.active_experiments:
            return False
            
        exp = self.active_experiments[experiment_id]
        if exp["status"] != "ACTIVE":
            return False
            
        # 检查是否过期
        if time.time() > exp["end_time"]:
            exp["status"] = "EXPIRED"
            logger.info(f"实验已过期: {experiment_id}")
            return False
        
        # 按流量比例判断
        return random.random() < exp["traffic_percentage"]
    
    def record_result(self,
                     experiment_id: str,
                     variant: str,  # "canary" or "control"
                     success: bool,
                     metrics: Dict = None):
        """记录实验结果"""
        if experiment_id not in self.active_experiments:
            return
            
        exp = self.active_experiments[experiment_id]
        exp["stats"]["total_requests"] += 1
        
        if variant == "canary":
            exp["stats"]["canary_requests"] += 1
            if success:
                exp["stats"]["canary_success"] += 1
        else:  # control
            exp["stats"]["control_requests"] += 1
            if success:
                exp["stats"]["control_success"] += 1
        
        # 保存详细结果
        if experiment_id not in self.experiment_results:
            self.experiment_results[experiment_id] = []
            
        self.experiment_results[experiment_id].append({
            "timestamp": time.time(),
            "variant": variant,
            "success": success,
            "metrics": metrics or {}
        })
    
    def get_experiment_status(self, experiment_id: str) -> Optional[Dict]:
        """获取实验状态"""
        if experiment_id not in self.active_experiments:
            return None
            
        exp = self.active_experiments[experiment_id].copy()
        
        # 计算成功率
        stats = exp["stats"]
        if stats["canary_requests"] > 0:
            exp["canary_success_rate"] = stats["canary_success"] / stats["canary_requests"]
        else:
            exp["canary_success_rate"] = 0.0
            
        if stats["control_requests"] > 0:
            exp["control_success_rate"] = stats["control_success"] / stats["control_requests"]
        else:
            exp["control_success_rate"] = 0.0
        
        # 计算提升幅度
        if stats["control_requests"] > 0 and stats["canary_requests"] > 0:
            control_rate = exp["control_success_rate"]
            canary_rate = exp["canary_success_rate"]
            if control_rate > 0:
                exp["improvement"] = (canary_rate - control_rate) / control_rate
            else:
                exp["improvement"] = 0.0 if canary_rate == 0 else float('inf')
        else:
            exp["improvement"] = 0.0
        
        return exp
    
    def stop_experiment(self, experiment_id: str, 
                       promote: bool = False) -> bool:
        """停止实验，可选是否提升为正式版本"""
        if experiment_id not in self.active_experiments:
            return False
            
        exp = self.active_experiments[experiment_id]
        exp["status"] = "STOPPED"
        exp["stop_time"] = time.time()
        
        logger.info(f"实验已停止: {experiment_id}")
        
        if promote:
            # TODO: 实现提升逻辑（需要根据具体系统集成）
            logger.info(f"实验 {experiment_id} 已标记为待提升")
            return True
            
        return False
    
    def get_all_experiments(self) -> Dict:
        """获取所有实验状态"""
        result = {}
        for exp_id, exp in self.active_experiments.items():
            result[exp_id] = self.get_experiment_status(exp_id)
        return result

# 全局实例
canary_manager = CanaryReleaseManager()