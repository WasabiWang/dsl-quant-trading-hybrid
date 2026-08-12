#!/usr/bin/env python3
"""
决策链路回溯模块 - 为每个决策生成唯一traceID，记录完整决策过程
"""
import time
import uuid
import json
import os
from typing import Dict, List, Any, Optional
from datetime import datetime
from common.logger import get_logger

logger = get_logger("decision_tracer")

class DecisionTracer:
    """
    决策链路回溯模块
    为每个决策生成全局唯一traceID，记录决策生成全流程的所有关键信息
    """
    
    def __init__(self, storage_path: str = None):
        if storage_path is None:
            storage_path = os.path.expanduser("~/.openclaw/logs/decisions")
        self.storage_path = storage_path
        os.makedirs(storage_path, exist_ok=True)
    
    def generate_trace_id(self, module: str = "DECISION") -> str:
        """生成全局唯一traceID: {时间戳}_{模块标识}_{随机字符串}"""
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        random_str = uuid.uuid4().hex[:8]
        return f"{timestamp}_{module}_{random_str}"
    
    def start_trace(self, trace_id: str, symbol: str, decision_type: str = "BUY") -> Dict:
        """开始记录决策链路"""
        trace = {
            "trace_id": trace_id,
            "symbol": symbol,
            "decision_type": decision_type,
            "start_time": int(time.time() * 1000),
            "nodes": [],
            "status": "PROCESSING"
        }
        self._save_trace(trace)
        logger.info(f"决策链路开始: {trace_id} | {symbol} | {decision_type}")
        return trace
    
    def add_node(self, trace_id: str, node_name: str, node_data: Dict):
        """添加决策节点"""
        trace = self._load_trace(trace_id)
        if not trace:
            logger.warning(f"trace不存在: {trace_id}")
            return
        
        node = {
            "node": node_name,
            "time": int(time.time() * 1000),
            "data": node_data
        }
        trace["nodes"].append(node)
        self._save_trace(trace)
        logger.debug(f"添加节点: {trace_id} | {node_name}")
    
    def finish_trace(self, trace_id: str, final_decision: Dict):
        """完成决策链路"""
        trace = self._load_trace(trace_id)
        if not trace:
            return
        
        trace["end_time"] = int(time.time() * 1000)
        trace["status"] = "COMPLETED"
        trace["final_decision"] = final_decision
        trace["duration_ms"] = trace["end_time"] - trace["start_time"]
        
        self._save_trace(trace)
        logger.info(f"决策链路完成: {trace_id} | 耗时: {trace['duration_ms']}ms")
    
    def get_trace(self, trace_id: str) -> Optional[Dict]:
        """获取单个决策的回溯信息"""
        return self._load_trace(trace_id)
    
    def query_traces(self, symbol: str = None, 
                    start_time: int = None, 
                    end_time: int = None,
                    limit: int = 100) -> List[Dict]:
        """批量查询决策"""
        traces = []
        try:
            for filename in os.listdir(self.storage_path):
                if filename.endswith('.json'):
                    filepath = os.path.join(self.storage_path, filename)
                    with open(filepath, 'r') as f:
                        trace = json.load(f)
                        
                        # 过滤条件
                        if symbol and trace.get("symbol") != symbol:
                            continue
                        if start_time and trace.get("start_time", 0) < start_time:
                            continue
                        if end_time and trace.get("start_time", 9999999999999) > end_time:
                            continue
                        
                        traces.append(trace)
                        
                        if len(traces) >= limit:
                            break
        except Exception as e:
            logger.error(f"查询决策失败: {e}")
        
        # 按时间倒序
        traces.sort(key=lambda x: x.get("start_time", 0), reverse=True)
        return traces
    
    def _save_trace(self, trace: Dict):
        """保存决策链路到文件"""
        filename = f"{trace['trace_id']}.json"
        filepath = os.path.join(self.storage_path, filename)
        with open(filepath, 'w') as f:
            json.dump(trace, f, ensure_ascii=False, indent=2)
    
    def _load_trace(self, trace_id: str) -> Optional[Dict]:
        """从文件加载决策链路"""
        filepath = os.path.join(self.storage_path, f"{trace_id}.json")
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return json.load(f)
        return None

# 全局实例
tracer = DecisionTracer()

if __name__ == "__main__":
    # 测试
    trace_id = tracer.generate_trace_id()
    print(f"生成traceID: {trace_id}")
    
    # 开始决策
    tracer.start_trace(trace_id, "600000", "BUY")
    
    # 添加节点
    tracer.add_node(trace_id, "data_fetch", {"data_source": "eastmoney", "records": 100})
    tracer.add_node(trace_id, "factor_calc", {"rs": 0.75, "momentum": 0.62})
    tracer.add_node(trace_id, "risk_check", {"passed": True, "checks": ["position_limit", "stop_loss"]})
    
    # 完成决策
    tracer.finish_trace(trace_id, {
        "action": "BUY",
        "quantity": 1000,
        "price": 12.50,
        "stop_loss": 11.25,
        "stop_profit": 14.00
    })
    
    # 回溯查询
    result = tracer.get_trace(trace_id)
    print(f"回溯结果: {json.dumps(result, indent=2, ensure_ascii=False)[:500]}")