#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
标准化决策报告框架 Framework v1.0
应用于所有重要决策报告：盘前决策、盘中预警、盘后复盘、风控报告、策略回测报告等
统一结构、样式、输出规范和分发渠道
"""

import os
import json
import subprocess
from datetime import datetime
from typing import Dict, List, Any, Optional

# 全局配置
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_CONFIG = {
    "default_timezone": "Asia/Shanghai",
    "log_dir": os.path.join(_PROJECT_ROOT, "logs", "reports"),
    "default_channel": "feishu",
    "default_target": os.environ.get("FEISHU_USER_OPEN_ID", "user:ou_xxx"),
    "emoji_map": {
        "success": "✅",
        "warning": "⚠️",
        "error": "❌",
        "info": "ℹ️",
        "buy": "🟢",
        "sell": "🔴",
        "hold": "🟡",
        "high_risk": "🔴",
        "medium_risk": "🟡",
        "low_risk": "🟢"
    }
}

# 支持的报告类型
REPORT_TYPES = {
    "pre_market_decision": {
        "title_template": "📊 {market}股盘前决策 | {date}",
        "default_modules": ["system_check", "macro_analysis", "decision_process", "final_decision", "portfolio_status"]
    },
    "intraday_alert": {
        "title_template": "🚨 盘中风险预警 | {alert_type} | {time}",
        "default_modules": ["alert_summary", "risk_indicators", "trigger_conditions", "suggested_actions", "current_portfolio"]
    },
    "post_market_review": {
        "title_template": "📝 盘后复盘报告 | {date}",
        "default_modules": ["market_summary", "trade_execution", "performance_review", "lessons_learned", "next_day_plan"]
    },
    "risk_control_report": {
        "title_template": "🛡️ 风控合规报告 | {report_period}",
        "default_modules": ["risk_overview", "violations", "exposure_analysis", "control_effectiveness", "improvement_suggestions"]
    },
    "backtest_report": {
        "title_template": "🧪 策略回测报告 | {strategy_name}",
        "default_modules": ["strategy_overview", "performance_metrics", "trade_analysis", "drawdown_analysis", "parameter_sensitivity", "conclusion"]
    }
}


class DecisionReportFramework:
    """标准化决策报告生成框架"""
    
    def __init__(self, report_type: str, **kwargs):
        """
        初始化报告
        Args:
            report_type: 报告类型，参考REPORT_TYPES
            **kwargs: 模板参数，用于填充标题
        """
        self.report_type = report_type
        self.config = REPORT_TYPES.get(report_type, REPORT_TYPES["pre_market_decision"])
        self.title = self.config["title_template"].format(date=datetime.now().strftime("%Y-%m-%d"), time=datetime.now().strftime("%H:%M"), **kwargs)
        self.modules = []
        self.report_id = f"{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # 创建目录
        os.makedirs(REPORT_CONFIG["log_dir"], exist_ok=True)
    
    def add_module(self, module_type: str, title: str, content: Any, **kwargs):
        """
        添加报告模块
        Args:
            module_type: 模块类型
            title: 模块标题
            content: 模块内容，支持字典、列表、字符串
            **kwargs: 额外参数
        """
        self.modules.append({
            "type": module_type,
            "title": title,
            "content": content,
            "kwargs": kwargs
        })
    
    def _render_module(self, module: Dict) -> str:
        """渲染单个模块为Markdown格式"""
        rendered = f"**{module['title']}**\n"
        
        content = module['content']
        if isinstance(content, dict):
            # 字典类型内容，按层级渲染
            for key, value in content.items():
                if isinstance(value, list):
                    rendered += f"- {key}:\n"
                    for item in value:
                        if isinstance(item, dict):
                            # 列表中的字典，渲染为 bullet point
                            item_str = "  • "
                            for k, v in item.items():
                                item_str += f"{k}: {v} | "
                            rendered += item_str.rstrip(" | ") + "\n"
                        else:
                            rendered += f"  • {item}\n"
                elif isinstance(value, dict):
                    rendered += f"- {key}:\n"
                    for k, v in value.items():
                        rendered += f"  • {k}: {v}\n"
                else:
                    # 普通键值对
                    rendered += f"- {key}: {value}\n"
                    
        elif isinstance(content, list):
            # 列表类型内容
            for item in content:
                if isinstance(item, dict):
                    # 带状态的列表项
                    status = item.get("status", "info")
                    emoji = REPORT_CONFIG["emoji_map"].get(status, "")
                    rendered += f"{emoji} {item.get('title', '')}: {item.get('message', '')}\n"
                else:
                    rendered += f"- {item}\n"
                    
        else:
            # 字符串类型内容
            rendered += f"{content}\n"
        
        rendered += "\n"
        return rendered
    
    def render(self) -> str:
        """渲染完整报告为Markdown格式"""
        report = f"**{self.title}**\n\n"
        
        # 渲染所有模块
        for module in self.modules:
            report += self._render_module(module)
        
        # 添加报告元信息
        report += "---\n"
        report += f"*报告ID: {self.report_id} | 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 框架版本: v1.0*\n"
        
        return report
    
    def save(self) -> str:
        """保存报告到文件"""
        report_content = self.render()
        file_path = os.path.join(REPORT_CONFIG["log_dir"], f"{self.report_id}.md")
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(report_content)
        
        # 同时保存结构化JSON数据
        json_path = os.path.join(REPORT_CONFIG["log_dir"], f"{self.report_id}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "report_id": self.report_id,
                "report_type": self.report_type,
                "title": self.title,
                "modules": self.modules,
                "generated_at": datetime.now().isoformat()
            }, f, ensure_ascii=False, indent=2)
        
        return file_path
    
    def send(self, channel: Optional[str] = None, target: Optional[str] = None) -> bool:
        """
        发送报告到指定渠道
        Args:
            channel: 发送渠道，默认feishu
            target: 发送目标，默认用户配置
        Returns:
            是否发送成功
        """
        channel = channel or REPORT_CONFIG["default_channel"]
        target = target or REPORT_CONFIG["default_target"]
        report_content = self.render()
        
        try:
            if channel == "feishu":
                subprocess.run([
                    'openclaw', 'message', 'send',
                    '--target', target,
                    '--message', report_content
                ], check=True, timeout=15)
                return True
            else:
                print(f"不支持的渠道: {channel}")
                return False
        except Exception as e:
            print(f"发送报告失败: {e}")
            return False
    
    def publish(self) -> Dict[str, Any]:
        """一键发布：渲染+保存+发送"""
        report_content = self.render()
        file_path = self.save()
        send_success = self.send()
        
        return {
            "report_id": self.report_id,
            "content": report_content,
            "file_path": file_path,
            "send_success": send_success,
            "generated_at": datetime.now().isoformat()
        }
