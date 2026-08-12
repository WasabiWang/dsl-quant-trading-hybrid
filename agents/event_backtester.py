#!/usr/bin/env python3
"""
事件回测模块 - 统计事件历史胜率、盈亏比，动态调整事件优先级
"""
import os
import sys
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.data_adapter import data_adapter
try:
    import akshare as ak
except ImportError:
    ak = None

class EventBacktester:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
    
    def _init(self):
        self.data_dir = f"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}/data/events"
        os.makedirs(self.data_dir, exist_ok=True)
        self.history_file = f"{self.data_dir}/event_history.json"
        self.stats_file = f"{self.data_dir}/event_stats.json"
        
        # 加载历史数据
        self.event_history = self._load_json(self.history_file, [])
        self.event_stats = self._load_json(self.stats_file, {})
    
    def _load_json(self, file_path: str, default: Any) -> Any:
        """加载JSON文件"""
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return default
        return default
    
    def _save_json(self, data: Any, file_path: str):
        """保存JSON文件"""
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def record_event_result(self, event: Dict, actual_return: float, holding_days: int = 5):
        """记录事件的实际收益结果"""
        event_record = {
            'event_id': event.get('event_id', f"{event['type']}_{event['symbol']}_{event['publish_time']}"),
            'type': event['type'],
            'symbol': event['symbol'],
            'stock_name': event['stock_name'],
            'publish_time': event['publish_time'],
            'predict_win_rate': event.get('win_rate', 60),
            'actual_return': actual_return,
            'holding_days': holding_days,
            'is_win': actual_return > 0,
            'record_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        self.event_history.append(event_record)
        self._save_json(self.event_history, self.history_file)
        
        # 更新统计数据
        self._update_event_stats(event['type'])
    
    def _update_event_stats(self, event_type: str):
        """更新事件类型的统计数据"""
        type_events = [e for e in self.event_history if e['type'] == event_type]
        if not type_events:
            return
        
        total_count = len(type_events)
        win_count = sum([1 for e in type_events if e['is_win']])
        win_rate = win_count / total_count * 100
        avg_return = np.mean([e['actual_return'] for e in type_events]) * 100
        win_avg_return = np.mean([e['actual_return'] for e in type_events if e['is_win']]) * 100
        loss_avg_return = np.mean([e['actual_return'] for e in type_events if not e['is_win']]) * 100
        profit_loss_ratio = abs(win_avg_return / loss_avg_return) if loss_avg_return != 0 else 0
        
        self.event_stats[event_type] = {
            'total_count': total_count,
            'win_count': win_count,
            'win_rate': round(win_rate, 2),
            'avg_return': round(avg_return, 2),
            'win_avg_return': round(win_avg_return, 2),
            'loss_avg_return': round(loss_avg_return, 2),
            'profit_loss_ratio': round(profit_loss_ratio, 2),
            'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        self._save_json(self.event_stats, self.stats_file)
    
    def get_event_win_rate(self, event_type: str, symbol: str = None) -> float:
        """获取事件类型的历史胜率"""
        # 优先返回具体股票的事件胜率，如果没有返回类型平均胜率
        if symbol:
            symbol_events = [e for e in self.event_history if e['type'] == event_type and e['symbol'] == symbol]
            if len(symbol_events) >= 10:
                win_count = sum([1 for e in symbol_events if e['is_win']])
                return round(win_count / len(symbol_events) * 100, 2)
        
        # 返回类型平均胜率
        if event_type in self.event_stats:
            return self.event_stats[event_type]['win_rate']
        
        # 默认初始胜率
        default_win_rates = {
            'earning_surprise': 72,
            'policy_benefit': 68,
            'increase_repurchase': 65,
            'merger_reorganization': 75,
            'major_contract': 63
        }
        return default_win_rates.get(event_type, 60)
    
    def get_event_priority_level(self, event: Dict) -> str:
        """动态计算事件优先级：高/中/低"""
        win_rate = self.get_event_win_rate(event['type'], event['symbol'])
        
        # 高优先级：胜率>=70%，或者胜率>=65%且盈亏比>=2
        if win_rate >=70:
            return 'high_priority'
        elif win_rate >=65:
            event_type_stats = self.event_stats.get(event['type'], {})
            if event_type_stats.get('profit_loss_ratio', 1) >=2:
                return 'high_priority'
            else:
                return 'medium_priority'
        elif win_rate >=55:
            return 'medium_priority'
        else:
            return 'low_priority'
    
    def filter_high_quality_events(self, events: List[Dict], min_win_rate: float = 60) -> List[Dict]:
        """过滤出高质量事件，仅保留胜率>=min_win_rate的"""
        high_quality = []
        for event in events:
            win_rate = self.get_event_win_rate(event['type'], event['symbol'])
            if win_rate >= min_win_rate:
                event['actual_win_rate'] = win_rate
                event['priority'] = self.get_event_priority_level(event)
                high_quality.append(event)
        
        # 按胜率排序
        high_quality.sort(key=lambda x: x['actual_win_rate'], reverse=True)
        return high_quality
    
    def get_backtest_report(self) -> Dict:
        """生成事件回测报告"""
        report = {
            'total_events': len(self.event_history),
            'event_types': list(self.event_stats.keys()),
            'type_stats': self.event_stats,
            'overall_win_rate': round(sum([1 for e in self.event_history if e['is_win']]) / len(self.event_history) * 100, 2) if self.event_history else 0,
            'overall_avg_return': round(np.mean([e['actual_return'] for e in self.event_history]) * 100, 2) if self.event_history else 0
        }
        return report

# 全局实例
event_backtester = EventBacktester()
