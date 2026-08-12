#!/usr/bin/env python3
"""
calibration_system.py - 黑天鹅校准系统
7大类事件校准+预测准确率追踪

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

class BlackSwanCalibration:
    """黑天鹅事件校准系统"""
    
    def __init__(self, data_dir: str = "confidence_data"):
        """
        初始化校准系统
        
        Args:
            data_dir: 数据目录
        """
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        
        # 校准文件路径
        self.confidence_file = os.path.join(data_dir, "confidence_calibration.json")
        self.prediction_file = os.path.join(data_dir, "prediction_calibration.json")
        self.event_history_file = os.path.join(data_dir, "event_history.json")
        
        # 7大类事件类别
        self.event_categories = [
            "geopolitical_military",      # 地缘军事
            "financial_system",           # 金融系统
            "public_health",              # 公共卫生
            "natural_disasters",          # 自然灾害
            "tech_breakthrough_accident", # 技术突破/事故
            "policy_sudden_change",       # 政策突变
            "social_unrest"               # 社会动荡
        ]
        
        # 初始化校准数据
        self._init_calibration_data()
    
    def _init_calibration_data(self):
        """初始化校准数据"""
        # 置信度校准数据
        if not os.path.exists(self.confidence_file):
            confidence_data = {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "category_adjustments": {},
                "event_history": []
            }
            
            # 初始化各类别调整因子
            for category in self.event_categories:
                confidence_data["category_adjustments"][category] = {
                    "base_confidence": 0.5,
                    "adjustment_factor": 1.0,
                    "historical_accuracy": 0.5,
                    "total_predictions": 0,
                    "correct_predictions": 0,
                    "last_updated": datetime.now().isoformat()
                }
            
            self._save_json(self.confidence_file, confidence_data)
        
        # 预测校准数据
        if not os.path.exists(self.prediction_file):
            prediction_data = {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "overall_stats": {
                    "total_predictions": 0,
                    "correct_predictions": 0,
                    "accuracy_rate": 0.5,
                    "avg_confidence": 0.5,
                    "calibration_error": 0.0
                },
                "predictions": []
            }
            
            self._save_json(self.prediction_file, prediction_data)
        
        # 事件历史数据
        if not os.path.exists(self.event_history_file):
            event_history = {
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "events": []
            }
            
            self._save_json(self.event_history_file, event_history)
    
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
    
    def calibrate_confidence(self, category: str, predicted_severity: int, 
                           actual_severity: int, confidence: float) -> float:
        """
        校准置信度
        
        Args:
            category: 事件类别
            predicted_severity: 预测严重等级 (1-5)
            actual_severity: 实际严重等级 (1-5)
            confidence: 原始置信度
            
        Returns:
            校准后的置信度
        """
        # 加载置信度校准数据
        confidence_data = self._load_json(self.confidence_file)
        
        if category not in confidence_data["category_adjustments"]:
            # 如果类别不存在，添加到校准数据
            confidence_data["category_adjustments"][category] = {
                "base_confidence": 0.5,
                "adjustment_factor": 1.0,
                "historical_accuracy": 0.5,
                "total_predictions": 0,
                "correct_predictions": 0,
                "last_updated": datetime.now().isoformat()
            }
        
        cat_data = confidence_data["category_adjustments"][category]
        
        # 更新统计
        cat_data["total_predictions"] += 1
        
        # 检查预测是否正确（严重等级误差在1以内视为正确）
        was_correct = abs(predicted_severity - actual_severity) <= 1
        
        if was_correct:
            cat_data["correct_predictions"] += 1
        
        # 计算历史准确率
        if cat_data["total_predictions"] > 0:
            cat_data["historical_accuracy"] = (
                cat_data["correct_predictions"] / cat_data["total_predictions"]
            )
        
        # 计算调整因子
        # 准确率越高，调整因子越大（增加置信度）
        # 准确率越低，调整因子越小（降低置信度）
        accuracy = cat_data["historical_accuracy"]
        adjustment_factor = 0.5 + accuracy  # 范围: 0.5-1.5
        
        cat_data["adjustment_factor"] = adjustment_factor
        cat_data["last_updated"] = datetime.now().isoformat()
        
        # 保存更新后的数据
        self._save_json(self.confidence_file, confidence_data)
        
        # 应用校准
        calibrated_confidence = confidence * adjustment_factor
        calibrated_confidence = max(0.0, min(1.0, calibrated_confidence))
        
        print(f"📈 校准置信度: {category}, 原始: {confidence:.3f}, 校准: {calibrated_confidence:.3f}")
        
        return calibrated_confidence
    
    def record_prediction(self, prediction: Dict):
        """
        记录预测
        
        Args:
            prediction: 预测数据
        """
        # 加载预测数据
        prediction_data = self._load_json(self.prediction_file)
        
        # 添加预测记录
        prediction_entry = {
            "id": f"pred_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "timestamp": datetime.now().isoformat(),
            **prediction
        }
        
        prediction_data["predictions"].append(prediction_entry)
        
        # 更新总体统计
        stats = prediction_data["overall_stats"]
        stats["total_predictions"] += 1
        
        # 如果有实际结果，更新正确预测数
        if "actual_severity" in prediction:
            predicted = prediction.get("predicted_severity", 0)
            actual = prediction.get("actual_severity", 0)
            
            if abs(predicted - actual) <= 1:
                stats["correct_predictions"] += 1
            
            # 计算准确率
            if stats["total_predictions"] > 0:
                stats["accuracy_rate"] = stats["correct_predictions"] / stats["total_predictions"]
            
            # 计算平均置信度
            all_confidences = [p.get("confidence", 0.5) for p in prediction_data["predictions"] 
                              if "confidence" in p]
            if all_confidences:
                stats["avg_confidence"] = np.mean(all_confidences)
            
            # 计算校准误差（置信度与准确率之差）
            stats["calibration_error"] = abs(stats["avg_confidence"] - stats["accuracy_rate"])
        
        # 保存数据
        self._save_json(self.prediction_file, prediction_data)
        
        print(f"📝 记录预测: {prediction_entry['id']}")
    
    def record_event(self, event: Dict):
        """
        记录事件
        
        Args:
            event: 事件数据
        """
        # 加载事件历史
        event_history = self._load_json(self.event_history_file)
        
        # 添加事件记录
        event_entry = {
            "id": f"event_{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "timestamp": datetime.now().isoformat(),
            **event
        }
        
        event_history["events"].append(event_entry)
        
        # 保存数据
        self._save_json(self.event_history_file, event_history)
        
        print(f"📋 记录事件: {event_entry['id']}")
    
    def get_category_adjustment(self, category: str) -> float:
        """
        获取类别调整因子
        
        Args:
            category: 事件类别
            
        Returns:
            调整因子
        """
        confidence_data = self._load_json(self.confidence_file)
        
        if category in confidence_data["category_adjustments"]:
            return confidence_data["category_adjustments"][category].get("adjustment_factor", 1.0)
        else:
            return 1.0
    
    def get_overall_stats(self) -> Dict:
        """获取总体统计"""
        prediction_data = self._load_json(self.prediction_file)
        return prediction_data.get("overall_stats", {})
    
    def generate_calibration_report(self) -> str:
        """生成校准报告"""
        confidence_data = self._load_json(self.confidence_file)
        prediction_data = self._load_json(self.prediction_file)
        
        report = "# 黑天鹅校准系统报告\n\n"
        report += f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        # 总体统计
        overall_stats = prediction_data.get("overall_stats", {})
        report += "## 📊 总体统计\n"
        report += f"- **总预测数**: {overall_stats.get('total_predictions', 0)}\n"
        report += f"- **正确预测**: {overall_stats.get('correct_predictions', 0)}\n"
        report += f"- **准确率**: {overall_stats.get('accuracy_rate', 0):.2%}\n"
        report += f"- **平均置信度**: {overall_stats.get('avg_confidence', 0):.3f}\n"
        report += f"- **校准误差**: {overall_stats.get('calibration_error', 0):.3f}\n\n"
        
        # 分类统计
        report += "## 📈 分类校准\n"
        category_adjustments = confidence_data.get("category_adjustments", {})
        
        for category, cat_data in category_adjustments.items():
            # 转换类别名称
            category_name = self._get_category_name(category)
            
            report += f"### {category_name}\n"
            report += f"- **历史准确率**: {cat_data.get('historical_accuracy', 0):.2%}\n"
            report += f"- **总预测数**: {cat_data.get('total_predictions', 0)}\n"
            report += f"- **正确预测**: {cat_data.get('correct_predictions', 0)}\n"
            report += f"- **调整因子**: {cat_data.get('adjustment_factor', 1.0):.3f}\n"
            report += f"- **最后更新**: {cat_data.get('last_updated', '未知')}\n\n"
        
        # 近期预测
        report += "## 🔮 近期预测\n"
        predictions = prediction_data.get("predictions", [])
        recent_predictions = predictions[-5:]  # 最近5个预测
        
        for pred in recent_predictions:
            category_name = self._get_category_name(pred.get("category", "unknown"))
            report += f"- **{pred.get('id', '未知')}**: {category_name}, "
            report += f"预测等级: {pred.get('predicted_severity', '未知')}, "
            report += f"置信度: {pred.get('confidence', 0):.3f}\n"
        
        return report
    
    def _get_category_name(self, category_key: str) -> str:
        """获取类别显示名称"""
        category_names = {
            "geopolitical_military": "地缘军事",
            "financial_system": "金融系统",
            "public_health": "公共卫生",
            "natural_disasters": "自然灾害",
            "tech_breakthrough_accident": "技术突破/事故",
            "policy_sudden_change": "政策突变",
            "social_unrest": "社会动荡"
        }
        
        return category_names.get(category_key, category_key)

def test_calibration_system():
    """测试校准系统"""
    print("=" * 60)
    print("🧪 黑天鹅校准系统测试")
    print("=" * 60)
    
    # 使用测试数据目录
    calibration = BlackSwanCalibration(data_dir="test_calibration_data")
    
    # 测试校准置信度
    test_cases = [
        ("geopolitical_military", 4, 4, 0.8),   # 正确预测
        ("financial_system", 3, 5, 0.7),        # 错误预测（误差2）
        ("public_health", 2, 2, 0.6),           # 正确预测
    ]
    
    print("📊 测试置信度校准:")
    for category, pred_sev, actual_sev, confidence in test_cases:
        calibrated = calibration.calibrate_confidence(category, pred_sev, actual_sev, confidence)
        print(f"  {category}: {confidence:.3f} → {calibrated:.3f}")
    
    # 测试记录预测
    print("\n📝 测试记录预测:")
    for i, (category, pred_sev, actual_sev, confidence) in enumerate(test_cases):
        prediction = {
            "category": category,
            "predicted_severity": pred_sev,
            "actual_severity": actual_sev,
            "confidence": confidence,
            "description": f"测试预测 {i+1}"
        }
        calibration.record_prediction(prediction)
    
    # 测试记录事件
    print("\n📋 测试记录事件:")
    test_event = {
        "category": "geopolitical_military",
        "severity": 4,
        "description": "测试地缘军事事件",
        "location": "测试地区",
        "impact_score": 0.75
    }
    calibration.record_event(test_event)
    
    # 测试获取调整因子
    print("\n📈 测试获取调整因子:")
    for category in ["geopolitical_military", "financial_system", "public_health"]:
        adjustment = calibration.get_category_adjustment(category)
        print(f"  {category}: {adjustment:.3f}")
    
    # 测试生成报告
    print("\n📊 测试生成报告:")
    report = calibration.generate_calibration_report()
    print("  报告生成成功")
    
    # 清理测试数据
    import shutil
    if os.path.exists("test_calibration_data"):
        shutil.rmtree("test_calibration_data")
    
    print("\n" + "=" * 60)
    print("✅ 黑天鹅校准系统测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_calibration_system()