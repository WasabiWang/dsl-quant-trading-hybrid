#!/usr/bin/env python3
"""
feedback_log.py - 反馈日志系统
记录每次策略建议和实际结果

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import json
import pandas as pd
from datetime import datetime
from typing import Dict, List, Any, Optional

class FeedbackLogger:
    """反馈日志记录器"""
    
    def __init__(self, log_dir: str = "feedback_logs"):
        """
        初始化反馈日志记录器
        
        Args:
            log_dir: 日志目录
        """
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        
        # 日志文件路径
        self.log_file = os.path.join(log_dir, "feedback_log.json")
        self.stats_file = os.path.join(log_dir, "feedback_stats.json")
        
        # 初始化日志文件
        if not os.path.exists(self.log_file):
            self._init_log_file()
    
    def _init_log_file(self):
        """初始化日志文件"""
        initial_data = {
            "version": "1.0",
            "created_at": datetime.now().isoformat(),
            "entries": []
        }
        self._save_json(self.log_file, initial_data)
    
    def _save_json(self, filepath: str, data: Dict):
        """保存JSON数据"""
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    def _load_json(self, filepath: str) -> Dict:
        """加载JSON数据"""
        if not os.path.exists(filepath):
            return {}
        
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def log_strategy_suggestion(self, suggestion: Dict) -> str:
        """
        记录策略建议
        
        Args:
            suggestion: 策略建议字典
            
        Returns:
            日志条目ID
        """
        # 加载现有日志
        log_data = self._load_json(self.log_file)
        if "entries" not in log_data:
            log_data["entries"] = []
        
        # 创建日志条目
        entry_id = f"entry_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{len(log_data['entries'])}"
        
        entry = {
            "id": entry_id,
            "timestamp": datetime.now().isoformat(),
            "type": "strategy_suggestion",
            "data": suggestion,
            "validation_result": None,
            "actual_result": None,
            "calibration_updated": False
        }
        
        # 添加条目
        log_data["entries"].append(entry)
        
        # 保存日志
        self._save_json(self.log_file, log_data)
        
        print(f"📝 记录策略建议: {entry_id}")
        return entry_id
    
    def log_validation_result(self, entry_id: str, validation_result: Dict):
        """
        记录验证结果
        
        Args:
            entry_id: 日志条目ID
            validation_result: 验证结果
        """
        log_data = self._load_json(self.log_file)
        
        # 查找并更新条目
        for entry in log_data.get("entries", []):
            if entry["id"] == entry_id:
                entry["validation_result"] = validation_result
                entry["updated_at"] = datetime.now().isoformat()
                break
        
        self._save_json(self.log_file, log_data)
        print(f"✅ 记录验证结果: {entry_id}")
    
    def log_actual_result(self, entry_id: str, actual_result: Dict):
        """
        记录实际结果
        
        Args:
            entry_id: 日志条目ID
            actual_result: 实际结果
        """
        log_data = self._load_json(self.log_file)
        
        # 查找并更新条目
        for entry in log_data.get("entries", []):
            if entry["id"] == entry_id:
                entry["actual_result"] = actual_result
                entry["updated_at"] = datetime.now().isoformat()
                break
        
        self._save_json(self.log_file, log_data)
        print(f"📊 记录实际结果: {entry_id}")
    
    def get_calibration_stats(self) -> Dict:
        """
        获取校准统计
        
        Returns:
            校准统计字典
        """
        stats_data = self._load_json(self.stats_file)
        
        if not stats_data:
            # 初始化统计
            stats_data = {
                "total_suggestions": 0,
                "validated_suggestions": 0,
                "correct_predictions": 0,
                "accuracy_rate": 0.0,
                "by_category": {},
                "last_updated": datetime.now().isoformat()
            }
        
        return stats_data
    
    def update_calibration_stats(self, category: str, was_correct: bool):
        """
        更新校准统计
        
        Args:
            category: 策略类别
            was_correct: 预测是否正确
        """
        stats_data = self.get_calibration_stats()
        
        # 更新总体统计
        stats_data["total_suggestions"] += 1
        stats_data["validated_suggestions"] += 1
        
        if was_correct:
            stats_data["correct_predictions"] += 1
        
        # 更新准确率
        if stats_data["validated_suggestions"] > 0:
            stats_data["accuracy_rate"] = (
                stats_data["correct_predictions"] / stats_data["validated_suggestions"]
            )
        
        # 更新分类统计
        if category not in stats_data["by_category"]:
            stats_data["by_category"][category] = {
                "total": 0,
                "correct": 0,
                "accuracy": 0.0
            }
        
        cat_stats = stats_data["by_category"][category]
        cat_stats["total"] += 1
        if was_correct:
            cat_stats["correct"] += 1
        
        if cat_stats["total"] > 0:
            cat_stats["accuracy"] = cat_stats["correct"] / cat_stats["total"]
        
        # 更新最后修改时间
        stats_data["last_updated"] = datetime.now().isoformat()
        
        # 保存统计
        self._save_json(self.stats_file, stats_data)
        
        print(f"📈 更新校准统计: {category}, 正确: {was_correct}")
    
    def generate_report(self) -> str:
        """生成反馈报告"""
        stats = self.get_calibration_stats()
        
        report = "# 反馈系统报告\n\n"
        report += f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        report += f"**总建议数**: {stats['total_suggestions']}\n"
        report += f"**已验证建议**: {stats['validated_suggestions']}\n"
        report += f"**正确预测**: {stats['correct_predictions']}\n"
        report += f"**准确率**: {stats['accuracy_rate']:.2%}\n\n"
        
        report += "## 分类统计\n"
        for category, cat_stats in stats.get('by_category', {}).items():
            report += f"- **{category}**: {cat_stats['correct']}/{cat_stats['total']} ({cat_stats['accuracy']:.2%})\n"
        
        return report

def test_feedback_logger():
    """测试反馈日志系统"""
    print("=" * 60)
    print("🧪 反馈日志系统测试")
    print("=" * 60)
    
    logger = FeedbackLogger(log_dir="test_feedback_logs")
    
    # 测试记录策略建议
    suggestion = {
        "strategy": "MA交叉策略",
        "symbol": "600760",
        "action": "buy",
        "confidence": 0.75,
        "reason": "MA5上穿MA20"
    }
    
    entry_id = logger.log_strategy_suggestion(suggestion)
    print(f"✅ 记录策略建议: {entry_id}")
    
    # 测试记录验证结果
    validation_result = {
        "valid": True,
        "score": 0.8,
        "issues": []
    }
    logger.log_validation_result(entry_id, validation_result)
    
    # 测试记录实际结果
    actual_result = {
        "actual_action": "buy",
        "actual_return": 0.05,
        "was_correct": True
    }
    logger.log_actual_result(entry_id, actual_result)
    
    # 测试更新校准统计
    logger.update_calibration_stats("MA交叉", True)
    logger.update_calibration_stats("RSI超卖", False)
    
    # 测试生成报告
    report = logger.generate_report()
    print("\n📊 反馈报告:")
    print(report)
    
    # 清理测试文件
    import shutil
    if os.path.exists("test_feedback_logs"):
        shutil.rmtree("test_feedback_logs")
    
    print("\n" + "=" * 60)
    print("✅ 反馈日志系统测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_feedback_logger()