#!/usr/bin/env python3
"""
成本核算系统 - 自动统计每个任务/每个Skill的运行成本
"""
import time
import json
import os
from typing import Dict, List, Any, Optional
from collections import defaultdict
from common.logger import get_logger
from common.id_generator import IDGenerator

logger = get_logger("cost_calculator")

class CostCalculator:
    """成本计算器"""
    
    # 成本模型（单位：美元）
    # 实际成本应从提供商API获取，这里使用估算值
    COST_PER_1K_TOKENS = {
        # OpenRouter 免费模型
        "openrouter/nvidia/nemotron-3-super-120b-a12b:free": {"input": 0.0, "output": 0.0},
        "openrouter/nvidia/nemotron-3-super-120b-a12b": {"input": 0.09, "output": 0.5},
        "openrouter/qwen/qwen3.5-122b-a10b:free": {"input": 0.0, "output": 0.0},
        "openrouter/meta-llama/llama-3.3-70b-instruct:free": {"input": 0.0, "output": 0.0},
        
        # 其他常见模型（参考价格）
        "openai/gpt-4o": {"input": 2.50, "output": 10.00},
        "openai/gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "openai/gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
        "anthropic/claude-3-5-sonnet-20241022": {"input": 3.00, "output": 15.00},
        "anthropic/claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
        "google/gemini-1.5-pro": {"input": 1.25, "output": 5.00},
        "google/gemini-1.5-flash": {"input": 0.075, "output": 0.30},
        
        # 默认值
        "default": {"input": 1.00, "output": 2.00}
    }
    
    def __init__(self, storage_dir: str = None):
        if storage_dir is None:
            storage_dir = os.path.expanduser("~/.openclaw/costs")
        self.storage_dir = storage_dir
        self.daily_costs_file = os.path.join(storage_dir, "daily_costs.json")
        self.task_costs_file = os.path.join(storage_dir, "task_costs.json")
        
        os.makedirs(storage_dir, exist_ok=True)
        self._ensure_files()
    
    def _ensure_files(self):
        """确保存储文件存在"""
        for file_path in [self.daily_costs_file, self.task_costs_file]:
            if not os.path.exists(file_path):
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump({}, f)
    
    def _load_daily_costs(self) -> Dict:
        """加载今日成本数据"""
        try:
            with open(self.daily_costs_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    
    def _save_daily_costs(self, data: Dict):
        """保存今日成本数据"""
        with open(self.daily_costs_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _load_task_costs(self) -> Dict:
        """加载任务成本数据"""
        try:
            with open(self.task_costs_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    
    def _save_task_costs(self, data: Dict):
        """保存任务成本数据"""
        with open(self.task_costs_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def _get_model_cost(self, model_name: str) -> Dict[str, float]:
        """获取模型的单token成本"""
        # 直接匹配
        if model_name in self.COST_PER_1K_TOKENS:
            return self.COST_PER_1K_TOKENS[model_name]
        
        # 前缀匹配（处理带:free等后缀）
        for known_model, cost in self.COST_PER_1K_TOKENS.items():
            if model_name.startswith(known_model.split(':')[0]):
                return cost
        
        return self.COST_PER_1K_TOKENS["default"]
    
    def calculate_llm_cost(self,
                          model_name: str,
                          input_tokens: int,
                          output_tokens: int) -> Dict[str, Any]:
        """计算LLM调用成本"""
        cost_model = self._get_model_cost(model_name)
        
        input_cost = (input_tokens / 1000) * cost_model["input"]
        output_cost = (output_tokens / 1000) * cost_model["output"]
        total_cost = input_cost + output_cost
        
        cost_info = {
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost_usd": round(input_cost, 6),
            "output_cost_usd": round(output_cost, 6),
            "total_cost_usd": round(total_cost, 6),
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d")
        }
        
        # 记录到任务成本
        self._record_task_cost("llm_call", cost_info)
        
        # 更新今日累计成本
        self._update_daily_cost("llm_call", total_cost)
        
        logger.debug(f"LLM成本: {model_name} "
                    f"in:{input_tokens} out:{output_tokens} "
                    f"cost:${total_cost:.6f}")
        
        return cost_info
    
    def calculate_task_cost(self,
                           task_name: str,
                           execution_time_ms: float,
                           cpu_usage_percent: float = None,
                           memory_usage_mb: float = None,
                           custom_costs: Dict = None) -> Dict[str, Any]:
        """计算任务执行成本"""
        # 基础成本模型（可以根据实际情况调整）
        # 这里简化为：基于执行时间的线性成本
        base_cost_per_second = 0.001  # $0.001/秒作为基础计算成本
        time_cost = (execution_time_ms / 1000) * base_cost_per_second
        
        # CPU成本（可选）
        cpu_cost = 0.0
        if cpu_usage_percent is not None:
            # 假设100% CPU使用额外成本为0.0005/秒
            cpu_cost = (execution_time_ms / 1000) * (cpu_usage_percent / 100) * 0.0005
        
        # 内存成本（可选）
        memory_cost = 0.0
        if memory_usage_mb is not None:
            # 假设1GB内存额外成本为0.0001/秒
            memory_cost = (execution_time_ms / 1000) * (memory_usage_mb / 1024) * 0.0001
        
        total_cost = time_cost + cpu_cost + memory_cost
        if custom_costs:
            total_cost += sum(custom_costs.values())
        
        cost_info = {
            "task": task_name,
            "execution_time_ms": execution_time_ms,
            "cpu_usage_percent": cpu_usage_percent,
            "memory_usage_mb": memory_usage_mb,
            "compute_cost_usd": round(time_cost, 6),
            "cpu_cost_usd": round(cpu_cost, 6),
            "memory_cost_usd": round(memory_cost, 6),
            "custom_cost_usd": round(sum(custom_costs.values()) if custom_costs else 0, 6),
            "total_cost_usd": round(total_cost, 6),
            "timestamp": time.time(),
            "date": time.strftime("%Y-%m-%d")
        }
        
        # 记录到任务成本
        self._record_task_cost("task_execution", cost_info)
        
        # 更新今日累计成本
        self._update_daily_cost("task_execution", total_cost)
        
        logger.debug(f"任务成本: {task_name} "
                    f"time:{execution_time_ms}ms "
                    f"cost:${total_cost:.6f}")
        
        return cost_info
    
    def _record_task_cost(self, cost_type: str, cost_info: Dict):
        """记录单个任务的成本"""
        try:
            task_costs = self._load_task_costs()
            date_key = cost_info["date"]
            
            if date_key not in task_costs:
                task_costs[date_key] = {}
            if cost_type not in task_costs[date_key]:
                task_costs[date_key][cost_type] = []
            
            task_costs[date_key][cost_type].append(cost_info)
            
            # 保留最近1000条记录以防文件过大
            if len(task_costs[date_key][cost_type]) > 1000:
                task_costs[date_key][cost_type] = task_costs[date_key][cost_type][-1000:]
            
            self._save_task_costs(task_costs)
        except Exception as e:
            logger.warning(f"记录任务成本失败: {e}")
    
    def _update_daily_cost(self, cost_type: str, amount: float):
        """更新今日累计成本"""
        try:
            daily_costs = self._load_daily_costs()
            today = time.strftime("%Y-%m-%d")
            
            if today not in daily_costs:
                daily_costs[today] = {}
            if cost_type not in daily_costs[today]:
                daily_costs[today][cost_type] = 0.0
            
            daily_costs[today][cost_type] += amount
            
            self._save_daily_costs(daily_costs)
        except Exception as e:
            logger.warning(f"更新每日成本失败: {e}")
    
    def get_daily_costs(self, date: str = None) -> Dict[str, Any]:
        """获取指定日期的成本统计"""
        if date is None:
            date = time.strftime("%Y-%m-%d")
            
        daily_costs = self._load_daily_costs()
        day_costs = daily_costs.get(date, {})
        
        # 计算总计
        total = sum(day_costs.values()) if day_costs else 0.0
        
        return {
            "date": date,
            "breakdown": day_costs.copy(),
            "total_cost_usd": round(total, 6),
            "total_tasks": sum(
                len(v) if isinstance(v, list) else 1 
                for v in day_costs.values() if isinstance(v, (list, dict))
            ) if day_costs else 0
        }
    
    def get_task_cost_summary(self, 
                             task_name: str = None,
                             date: str = None) -> Dict[str, Any]:
        """获取特定任务的成本摘要"""
        if date is None:
            date = time.strftime("%Y-%m-%d")
            
        task_costs = self._load_task_costs()
        day_costs = task_costs.get(date, {})
        
        results = {}
        for cost_type, cost_list in day_costs.items():
            if not isinstance(cost_list, list):
                continue
                
            # 过滤特定任务
            if task_name:
                filtered = [c for c in cost_list if c.get("task") == task_name]
                if not filtered:
                    continue
                cost_list = filtered
            
            if not cost_list:
                continue
                
            # 计算统计量
            total_cost = sum(c.get("total_cost_usd", 0) for c in cost_list)
            count = len(cost_list)
            avg_cost = total_cost / count if count > 0 else 0
            
            results[cost_type] = {
                "count": count,
                "total_cost_usd": round(total_cost, 6),
                "avg_cost_per_call_usd": round(avg_cost, 6),
                "min_cost_usd": round(min(c.get("total_cost_usd", 0) for c in cost_list), 6),
                "max_cost_usd": round(max(c.get("total_cost_usd", 0) for c in cost_list), 6)
            }
        
        return {
            "date": date,
            "task_filter": task_name,
            "breakdown": results
        }
    
    def get_cost_trends(self, days: int = 7) -> Dict[str, Any]:
        """获取成本趋势（最近N天）"""
        daily_costs = self._load_daily_costs()
        
        # 获取最近N天的日期
        from datetime import datetime, timedelta
        today = datetime.now().date()
        dates = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        dates.reverse()  # 从旧到新
        
        trend_data = []
        for date in dates:
            day_cost = daily_costs.get(date, {})
            total = sum(day_costs.values()) if day_costs else 0.0
            trend_data.append({
                "date": date,
                "total_cost_usd": round(total, 6),
                "breakdown": day_costs.copy()
            })
        
        return {
            "period_days": days,
            "dates": [d["date"] for d in trend_data],
            "daily_totals": [d["total_cost_usd"] for d in trend_data],
            "breakdown_by_type": self._aggregate_breakdown_by_type(trend_data)
        }
    
    def _aggregate_breakdown_by_type(self, trend_data: List[Dict]) -> Dict[str, List[float]]:
        """按成本类型聚合趋势数据"""
        if not trend_data:
            return {}
            
        # 获取所有成本类型
        cost_types = set()
        for day in trend_data:
            cost_types.update(day.get("breakdown", {}).keys())
        
        # 按类型聚合
        result = {}
        for cost_type in cost_types:
            result[cost_type] = [
                day.get("breakdown", {}).get(cost_type, 0.0) 
                for day in trend_data
            ]
        
        return result

# 全局实例
cost_calculator = CostCalculator()