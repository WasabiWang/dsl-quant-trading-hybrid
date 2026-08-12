#!/usr/bin/env python3
"""
validate.py - 反馈验证脚本
验证建议结果、更新校准统计

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional

class FeedbackValidator:
    """反馈验证器"""
    
    def __init__(self, feedback_logger=None):
        """
        初始化反馈验证器
        
        Args:
            feedback_logger: FeedbackLogger实例
        """
        self.feedback_logger = feedback_logger
        
        # 校准数据目录
        self.calibration_dir = "calibration_data"
        os.makedirs(self.calibration_dir, exist_ok=True)
    
    def validate_strategy_suggestion(self, suggestion: Dict, market_data: pd.DataFrame) -> Dict:
        """
        验证策略建议
        
        Args:
            suggestion: 策略建议
            market_data: 市场数据
            
        Returns:
            验证结果
        """
        validation_result = {
            "valid": True,
            "score": 0.0,
            "issues": [],
            "confidence_level": "medium",
            "recommendation": "proceed"
        }
        
        try:
            # 检查基本字段
            required_fields = ["strategy", "symbol", "action", "confidence"]
            for field in required_fields:
                if field not in suggestion:
                    validation_result["valid"] = False
                    validation_result["issues"].append(f"缺少必要字段: {field}")
            
            # 检查置信度范围
            confidence = suggestion.get("confidence", 0)
            if not 0 <= confidence <= 1:
                validation_result["valid"] = False
                validation_result["issues"].append(f"置信度超出范围: {confidence}")
            
            # 检查市场数据
            if market_data is None or market_data.empty:
                validation_result["issues"].append("市场数据为空")
                validation_result["confidence_level"] = "low"
            
            # 计算验证分数
            score = self._calculate_validation_score(suggestion, market_data)
            validation_result["score"] = score
            
            # 确定置信等级
            if score >= 0.8:
                validation_result["confidence_level"] = "high"
                validation_result["recommendation"] = "strongly_recommend"
            elif score >= 0.6:
                validation_result["confidence_level"] = "medium"
                validation_result["recommendation"] = "proceed"
            elif score >= 0.4:
                validation_result["confidence_level"] = "low"
                validation_result["recommendation"] = "caution"
            else:
                validation_result["confidence_level"] = "very_low"
                validation_result["recommendation"] = "avoid"
            
            # 加载历史校准数据
            calibration_stats = self._load_calibration_stats()
            
            # 考虑历史准确率
            strategy_type = suggestion.get("strategy", "unknown")
            if strategy_type in calibration_stats.get("by_category", {}):
                hist_accuracy = calibration_stats["by_category"][strategy_type].get("accuracy", 0.5)
                # 调整分数基于历史表现
                adjusted_score = score * 0.7 + hist_accuracy * 0.3
                validation_result["score"] = adjusted_score
            
        except Exception as e:
            validation_result["valid"] = False
            validation_result["issues"].append(f"验证过程中出错: {str(e)}")
        
        return validation_result
    
    def _calculate_validation_score(self, suggestion: Dict, market_data: pd.DataFrame) -> float:
        """计算验证分数"""
        score = 0.0
        
        # 基础分数：置信度
        confidence = suggestion.get("confidence", 0)
        score += confidence * 0.3
        
        # 如果有市场数据，进行技术分析
        if market_data is not None and not market_data.empty:
            # 检查数据新鲜度
            if isinstance(market_data.index, pd.DatetimeIndex):
                latest_date = market_data.index[-1]
                days_old = (datetime.now() - latest_date).days
                if days_old <= 1:
                    score += 0.2  # 数据新鲜
                elif days_old <= 7:
                    score += 0.1  # 数据较新
                else:
                    score += 0.05  # 数据较旧
            
            # 检查数据完整性
            required_cols = ['open', 'high', 'low', 'close', 'volume']
            missing_cols = [col for col in required_cols if col not in market_data.columns]
            if not missing_cols:
                score += 0.2  # 数据完整
            else:
                score += 0.1  # 数据部分完整
        
        # 策略特定检查
        strategy_type = suggestion.get("strategy", "").lower()
        if "ma" in strategy_type or "cross" in strategy_type:
            # MA交叉策略额外检查
            if market_data is not None and len(market_data) >= 20:
                score += 0.1
        
        # 确保分数在0-1之间
        score = max(0.0, min(1.0, score))
        
        return score
    
    def _load_calibration_stats(self) -> Dict:
        """加载校准统计"""
        stats_file = os.path.join(self.calibration_dir, "calibration_stats.json")
        
        if not os.path.exists(stats_file):
            return {
                "total_suggestions": 0,
                "validated_suggestions": 0,
                "correct_predictions": 0,
                "accuracy_rate": 0.5,
                "by_category": {},
                "last_updated": datetime.now().isoformat()
            }
        
        with open(stats_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def _save_calibration_stats(self, stats: Dict):
        """保存校准统计"""
        stats_file = os.path.join(self.calibration_dir, "calibration_stats.json")
        
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
    
    def update_calibration(self, suggestion: Dict, actual_result: Dict):
        """
        更新校准统计
        
        Args:
            suggestion: 原始建议
            actual_result: 实际结果
        """
        # 加载现有统计
        stats = self._load_calibration_stats()
        
        # 更新总体统计
        stats["total_suggestions"] += 1
        stats["validated_suggestions"] += 1
        
        was_correct = actual_result.get("was_correct", False)
        if was_correct:
            stats["correct_predictions"] += 1
        
        # 计算准确率
        if stats["validated_suggestions"] > 0:
            stats["accuracy_rate"] = stats["correct_predictions"] / stats["validated_suggestions"]
        
        # 更新分类统计
        strategy_type = suggestion.get("strategy", "unknown")
        if strategy_type not in stats["by_category"]:
            stats["by_category"][strategy_type] = {
                "total": 0,
                "correct": 0,
                "accuracy": 0.5
            }
        
        cat_stats = stats["by_category"][strategy_type]
        cat_stats["total"] += 1
        if was_correct:
            cat_stats["correct"] += 1
        
        if cat_stats["total"] > 0:
            cat_stats["accuracy"] = cat_stats["correct"] / cat_stats["total"]
        
        # 更新最后修改时间
        stats["last_updated"] = datetime.now().isoformat()
        
        # 保存统计
        self._save_calibration_stats(stats)
        
        print(f"📈 更新校准统计: {strategy_type}, 正确: {was_correct}")
        
        # 如果反馈日志器存在，也更新它
        if self.feedback_logger:
            self.feedback_logger.update_calibration_stats(strategy_type, was_correct)
    
    def get_calibration_adjustment(self, strategy_type: str) -> float:
        """
        获取校准调整因子
        
        Args:
            strategy_type: 策略类型
            
        Returns:
            调整因子 (0.5-1.5)
        """
        stats = self._load_calibration_stats()
        
        if strategy_type in stats["by_category"]:
            accuracy = stats["by_category"][strategy_type].get("accuracy", 0.5)
            # 将准确率映射到调整因子
            # 准确率0.5 -> 调整因子1.0 (无调整)
            # 准确率1.0 -> 调整因子1.5 (增加置信度)
            # 准确率0.0 -> 调整因子0.5 (降低置信度)
            adjustment = 0.5 + accuracy  # 范围: 0.5-1.5
            return adjustment
        else:
            return 1.0  # 默认无调整
    
    def generate_validation_report(self, validation_results: List[Dict]) -> str:
        """生成验证报告"""
        if not validation_results:
            return "无验证结果"
        
        report = "# 策略验证报告\n\n"
        report += f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        report += f"**验证策略数**: {len(validation_results)}\n\n"
        
        report += "## 验证详情\n"
        
        valid_count = sum(1 for r in validation_results if r.get("valid", False))
        high_confidence = sum(1 for r in validation_results if r.get("confidence_level") == "high")
        avg_score = np.mean([r.get("score", 0) for r in validation_results])
        
        report += f"- **有效建议**: {valid_count}/{len(validation_results)}\n"
        report += f"- **高置信度**: {high_confidence}/{len(validation_results)}\n"
        report += f"- **平均分数**: {avg_score:.3f}\n\n"
        
        report += "## 详细结果\n"
        for i, result in enumerate(validation_results, 1):
            suggestion = result.get("suggestion_info", {})
            report += f"### 策略 {i}: {suggestion.get('strategy', '未知')}\n"
            report += f"- **标的**: {suggestion.get('symbol', '未知')}\n"
            report += f"- **建议动作**: {suggestion.get('action', '未知')}\n"
            report += f"- **验证结果**: {'✅ 有效' if result.get('valid') else '❌ 无效'}\n"
            report += f"- **置信等级**: {result.get('confidence_level', '未知')}\n"
            report += f"- **验证分数**: {result.get('score', 0):.3f}\n"
            report += f"- **推荐**: {result.get('recommendation', '未知')}\n"
            
            issues = result.get("issues", [])
            if issues:
                report += f"- **问题**:\n"
                for issue in issues:
                    report += f"  - {issue}\n"
            
            report += "\n"
        
        return report

def test_feedback_validator():
    """测试反馈验证器"""
    print("=" * 60)
    print("🧪 反馈验证器测试")
    print("=" * 60)
    
    validator = FeedbackValidator()
    
    # 创建测试市场数据
    dates = pd.date_range('2025-01-01', periods=50, freq='D')
    market_data = pd.DataFrame({
        'open': np.random.randn(50).cumsum() + 100,
        'high': np.random.randn(50).cumsum() + 105,
        'low': np.random.randn(50).cumsum() + 95,
        'close': np.random.randn(50).cumsum() + 100,
        'volume': np.random.randint(1000000, 10000000, 50)
    }, index=dates)
    
    # 测试验证策略建议
    suggestion = {
        "strategy": "MA交叉策略",
        "symbol": "600760",
        "action": "buy",
        "confidence": 0.75,
        "reason": "MA5上穿MA20"
    }
    
    validation_result = validator.validate_strategy_suggestion(suggestion, market_data)
    print(f"✅ 验证结果:")
    print(f"  有效: {validation_result['valid']}")
    print(f"  分数: {validation_result['score']:.3f}")
    print(f"  置信等级: {validation_result['confidence_level']}")
    print(f"  推荐: {validation_result['recommendation']}")
    
    # 测试更新校准
    actual_result = {
        "actual_action": "buy",
        "actual_return": 0.05,
        "was_correct": True
    }
    validator.update_calibration(suggestion, actual_result)
    
    # 测试获取校准调整
    adjustment = validator.get_calibration_adjustment("MA交叉策略")
    print(f"✅ 校准调整因子: {adjustment:.3f}")
    
    # 测试生成报告
    validation_results = [validation_result]
    report = validator.generate_validation_report(validation_results)
    print(f"\n📊 验证报告生成成功")
    
    print("\n" + "=" * 60)
    print("✅ 反馈验证器测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_feedback_validator()