#!/usr/bin/env python3
"""
DSL Data SDK 飞书告警集成模块
提供错误告警、性能监控和系统状态通知功能
"""

import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional, Union
from enum import Enum
from dataclasses import dataclass, asdict
import requests

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class AlertLevel(Enum):
    """告警级别"""
    INFO = "info"        # 信息
    WARNING = "warning"  # 警告
    ERROR = "error"      # 错误
    CRITICAL = "critical"  # 严重

class AlertType(Enum):
    """告警类型"""
    ERROR = "error"              # 错误告警
    PERFORMANCE = "performance"  # 性能告警
    AVAILABILITY = "availability"  # 可用性告警
    SECURITY = "security"        # 安全告警
    SYSTEM = "system"            # 系统告警

@dataclass
class AlertRecord:
    """告警记录"""
    timestamp: str
    level: str
    alert_type: str
    title: str
    message: str
    module: str
    metric: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    recipients: Optional[List[str]] = None
    acknowledged: bool = False
    resolved: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return asdict(self)

class FeishuAlertConfig:
    """飞书告警配置"""
    
    def __init__(self, webhook_url: Optional[str] = None):
        """
        初始化飞书告警配置
        
        Args:
            webhook_url: 飞书机器人Webhook URL
        """
        self._use_app_api = False  # 是否使用App API而非Webhook
        self.webhook_url = webhook_url or self._get_default_webhook()
        self.alert_history: List[AlertRecord] = []
        self.rate_limit = {
            "last_send_time": 0,
            "min_interval": 30  # 最小发送间隔(秒)
        }
    
    def _get_default_webhook(self) -> str:
        """获取默认Webhook URL"""
        import os
        webhook = os.getenv("FEISHU_WEBHOOK_URL", "")
        if not webhook:
            # 尝试使用App ID+Secret方式（通过common.feishu_utils）
            try:
                from common.feishu_utils import get_tenant_access_token, send_markdown
                self._use_app_api = True
                logger.info("未配置Webhook URL，改用飞书App API发送告警")
            except ImportError:
                logger.warning("未配置飞书Webhook URL，且飞书App模块不可用，告警功能将受限")
        return webhook
    
    def send_alert(self, alert: AlertRecord) -> bool:
        """
        发送飞书告警
        
        Args:
            alert: 告警记录
            
        Returns:
            bool: 是否发送成功
        """
        if not self.webhook_url and not self._use_app_api:
            logger.warning("未配置飞书Webhook URL，跳过告警发送")
            return False
        
        # 如果使用App API方式发送
        if self._use_app_api and not self.webhook_url:
            try:
                from common.feishu_utils import send_markdown
                level_emoji = {'info': 'ℹ️', 'warning': '⚠️', 'error': '🔴', 'critical': '⚫'}
                level_str = alert.level.value if hasattr(alert.level, 'value') else str(alert.level)
                emoji = level_emoji.get(level_str, '🔔')
                content = f"{emoji} **{alert.title}**\n\n{alert.message}\n\n时间: {alert.timestamp}"
                result = send_markdown(f"{emoji} {alert.title}", content)
                if result:
                    logger.info(f"飞书App API告警发送成功: {alert.title}")
                    return True
                else:
                    logger.warning("飞书App API告警发送失败")
                    return False
            except Exception as e:
                logger.warning(f"飞书App API告警异常: {e}")
                return False
        
        # 检查速率限制
        current_time = time.time()
        if current_time - self.rate_limit["last_send_time"] < self.rate_limit["min_interval"]:
            logger.info("速率限制，跳过告警发送")
            return False
        
        try:
            # 构建飞书消息卡片
            card = self._build_feishu_card(alert)
            
            # 发送请求
            response = requests.post(
                self.webhook_url,
                json=card,
                headers={"Content-Type": "application/json"},
                timeout=10
            )
            
            if response.status_code == 200:
                self.rate_limit["last_send_time"] = current_time
                logger.info(f"飞书告警发送成功: {alert.title}")
                return True
            else:
                logger.error(f"飞书告警发送失败: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"发送飞书告警异常: {e}")
            return False
    
    def _build_feishu_card(self, alert: AlertRecord) -> Dict[str, Any]:
        """构建飞书消息卡片"""
        
        # 根据告警级别设置颜色
        color_map = {
            AlertLevel.INFO.value: "blue",
            AlertLevel.WARNING.value: "orange",
            AlertLevel.ERROR.value: "red",
            AlertLevel.CRITICAL.value: "purple"
        }
        
        color = color_map.get(alert.level, "grey")
        
        # 构建卡片内容
        elements = []
        
        # 标题
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{alert.title}**"
            }
        })
        
        # 消息内容
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": alert.message
            }
        })
        
        # 详细信息
        details = []
        details.append(f"• **模块**: {alert.module}")
        details.append(f"• **时间**: {alert.timestamp}")
        details.append(f"• **级别**: {alert.level.upper()}")
        
        if alert.metric and alert.value is not None:
            details.append(f"• **指标**: {alert.metric} = {alert.value}")
            if alert.threshold is not None:
                details.append(f"• **阈值**: {alert.threshold}")
        
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(details)
            }
        })
        
        # 操作按钮
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "确认告警"
                    },
                    "type": "primary",
                    "value": json.dumps({
                        "alert_id": id(alert),
                        "action": "acknowledge"
                    })
                },
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "查看详情"
                    },
                    "type": "default",
                    "url": "https://your-dashboard.com/alerts"
                }
            ]
        })
        
        # 构建完整卡片
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {
                    "wide_screen_mode": True
                },
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🚨 DSL系统告警 - {alert.level.upper()}"
                    },
                    "template": color
                },
                "elements": elements
            }
        }
        
        return card
    
    def add_to_history(self, alert: AlertRecord) -> None:
        """添加到告警历史"""
        self.alert_history.append(alert)
        # 保留最近1000条记录
        if len(self.alert_history) > 1000:
            self.alert_history = self.alert_history[-1000:]
    
    def get_recent_alerts(self, limit: int = 50) -> List[AlertRecord]:
        """获取最近告警"""
        return sorted(
            self.alert_history,
            key=lambda x: x.timestamp,
            reverse=True
        )[:limit]

class AlertManager:
    """告警管理器"""
    
    def __init__(self, feishu_config: Optional[FeishuAlertConfig] = None):
        self.feishu_config = feishu_config or FeishuAlertConfig()
        self.alert_rules = self._load_alert_rules()
        self.active_alerts: Dict[str, AlertRecord] = {}  # 活跃告警
    
    def _load_alert_rules(self) -> Dict[str, Any]:
        """加载告警规则"""
        # 默认告警规则
        return {
            "error_rate": {
                "threshold": 0.1,  # 错误率阈值
                "window_minutes": 60,
                "level": AlertLevel.ERROR
            },
            "response_time": {
                "threshold": 10000,  # 响应时间阈值(ms) — v4.6.6: 10s(并行2源+串行2源各3s)
                "window_minutes": 30,
                "level": AlertLevel.WARNING
            },
            "availability": {
                "threshold": 0.95,  # 可用性阈值
                "window_minutes": 60,
                "level": AlertLevel.CRITICAL
            },
            "cache_hit_rate": {
                "threshold": 0.7,  # 缓存命中率阈值
                "window_minutes": 30,
                "level": AlertLevel.WARNING
            }
        }
    
    def create_alert(
        self,
        level: Union[str, AlertLevel],
        alert_type: Union[str, AlertType],
        title: str,
        message: str,
        module: str,
        metric: Optional[str] = None,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
        recipients: Optional[List[str]] = None,
        send_to_feishu: bool = True
    ) -> AlertRecord:
        """
        创建告警
        
        Args:
            level: 告警级别
            alert_type: 告警类型
            title: 告警标题
            message: 告警消息
            module: 模块名称
            metric: 指标名称
            value: 指标值
            threshold: 阈值
            recipients: 接收人列表
            send_to_feishu: 是否发送到飞书
            
        Returns:
            AlertRecord: 告警记录
        """
        # 转换枚举类型
        if isinstance(level, AlertLevel):
            level = level.value
        if isinstance(alert_type, AlertType):
            alert_type = alert_type.value
        
        # 创建告警记录
        alert = AlertRecord(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            level=level,
            alert_type=alert_type,
            title=title,
            message=message,
            module=module,
            metric=metric,
            value=value,
            threshold=threshold,
            recipients=recipients
        )
        
        # 添加到历史
        self.feishu_config.add_to_history(alert)
        
        # 发送到飞书
        if send_to_feishu and level in [AlertLevel.WARNING.value, AlertLevel.ERROR.value, AlertLevel.CRITICAL.value]:
            success = self.feishu_config.send_alert(alert)
            if success:
                logger.info(f"告警已发送到飞书: {title}")
            else:
                logger.warning(f"告警发送到飞书失败: {title}")
        
        # 记录活跃告警
        alert_key = f"{module}_{metric}" if metric else module
        self.active_alerts[alert_key] = alert
        
        return alert
    
    def check_error_rate(self, error_count: int, total_requests: int, module: str) -> Optional[AlertRecord]:
        """检查错误率"""
        if total_requests == 0:
            return None
        
        error_rate = error_count / total_requests
        rule = self.alert_rules["error_rate"]
        
        if error_rate > rule["threshold"]:
            return self.create_alert(
                level=rule["level"],
                alert_type=AlertType.ERROR,
                title=f"{module} 错误率过高",
                message=f"{module} 模块在最近{rule['window_minutes']}分钟内错误率达到{error_rate:.2%}，超过阈值{rule['threshold']:.2%}",
                module=module,
                metric="error_rate",
                value=error_rate,
                threshold=rule["threshold"]
            )
        
        return None
    
    def check_response_time(self, response_time_ms: float, module: str, operation: str) -> Optional[AlertRecord]:
        """检查响应时间"""
        rule = self.alert_rules["response_time"]
        
        if response_time_ms > rule["threshold"]:
            return self.create_alert(
                level=rule["level"],
                alert_type=AlertType.PERFORMANCE,
                title=f"{module} 响应时间过长",
                message=f"{module}.{operation} 响应时间{response_time_ms:.0f}ms，超过阈值{rule['threshold']}ms",
                module=module,
                metric="response_time",
                value=response_time_ms,
                threshold=rule["threshold"]
            )
        
        return None
    
    def check_availability(self, success_count: int, total_requests: int, module: str) -> Optional[AlertRecord]:
        """检查可用性"""
        if total_requests == 0:
            return None
        
        availability = success_count / total_requests
        rule = self.alert_rules["availability"]
        
        if availability < rule["threshold"]:
            return self.create_alert(
                level=rule["level"],
                alert_type=AlertType.AVAILABILITY,
                title=f"{module} 可用性下降",
                message=f"{module} 模块在最近{rule['window_minutes']}分钟内可用性为{availability:.2%}，低于阈值{rule['threshold']:.2%}",
                module=module,
                metric="availability",
                value=availability,
                threshold=rule["threshold"]
            )
        
        return None
    
    def check_cache_hit_rate(self, hit_rate: float, module: str) -> Optional[AlertRecord]:
        """检查缓存命中率"""
        rule = self.alert_rules["cache_hit_rate"]
        
        if hit_rate < rule["threshold"]:
            return self.create_alert(
                level=rule["level"],
                alert_type=AlertType.PERFORMANCE,
                title=f"{module} 缓存命中率低",
                message=f"{module} 缓存命中率为{hit_rate:.2%}，低于阈值{rule['threshold']:.2%}",
                module=module,
                metric="cache_hit_rate",
                value=hit_rate,
                threshold=rule["threshold"]
            )
        
        return None
    
    def acknowledge_alert(self, alert_key: str, user: str) -> bool:
        """确认告警"""
        if alert_key in self.active_alerts:
            alert = self.active_alerts[alert_key]
            alert.acknowledged = True
            
            # 发送确认通知
            self.create_alert(
                level=AlertLevel.INFO,
                alert_type=AlertType.SYSTEM,
                title=f"告警已确认",
                message=f"用户 {user} 已确认告警: {alert.title}",
                module=alert.module,
                send_to_feishu=True
            )
            
            logger.info(f"告警已确认: {alert_key} by {user}")
            return True
        
        return False
    
    def resolve_alert(self, alert_key: str) -> bool:
        """解决告警"""
        if alert_key in self.active_alerts:
            alert = self.active_alerts[alert_key]
            alert.resolved = True
            
            # 发送解决通知
            self.create_alert(
                level=AlertLevel.INFO,
                alert_type=AlertType.SYSTEM,
                title=f"告警已解决",
                message=f"告警已解决: {alert.title}",
                module=alert.module,
                send_to_feishu=True
            )
            
            # 从活跃告警中移除
            del self.active_alerts[alert_key]
            
            logger.info(f"告警已解决: {alert_key}")
            return True
        
        return False
    
    def get_active_alerts(self) -> List[AlertRecord]:
        """获取活跃告警"""
        return list(self.active_alerts.values())
    
    def get_alert_summary(self) -> Dict[str, Any]:
        """获取告警摘要"""
        total_alerts = len(self.feishu_config.alert_history)
        active_alerts = len(self.active_alerts)
        
        # 按级别统计
        level_counts = {}
        for alert in self.feishu_config.alert_history[-1000:]:  # 最近1000条
            level_counts[alert.level] = level_counts.get(alert.level, 0) + 1
        
        # 按类型统计
        type_counts = {}
        for alert in self.feishu_config.alert_history[-1000:]:
            type_counts[alert.alert_type] = type_counts.get(alert.alert_type, 0) + 1
        
        return {
            "summary": {
                "total_alerts": total_alerts,
                "active_alerts": active_alerts,
                "resolved_alerts": total_alerts - active_alerts,
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "level_distribution": level_counts,
            "type_distribution": type_counts,
            "recent_alerts": [alert.to_dict() for alert in self.feishu_config.get_recent_alerts(10)]
        }

# 全局告警管理器实例
_alert_manager = AlertManager()

def send_alert(
    level: Union[str, AlertLevel],
    alert_type: Union[str, AlertType],
    title: str,
    message: str,
    module: str,
    **kwargs
) -> AlertRecord:
    """发送告警"""
    return _alert_manager.create_alert(level, alert_type, title, message, module, **kwargs)

def check_error_rate(error_count: int, total_requests: int, module: str) -> Optional[AlertRecord]:
    """检查错误率"""
    return _alert_manager.check_error_rate(error_count, total_requests, module)

def check_response_time(response_time_ms: float, module: str, operation: str) -> Optional[AlertRecord]:
    """检查响应时间"""
    return _alert_manager.check_response_time(response_time_ms, module, operation)

def get_alert_summary() -> Dict[str, Any]:
    """获取告警摘要"""
    return _alert_manager.get_alert_summary()

def get_active_alerts() -> List[AlertRecord]:
    """获取活跃告警"""
    return _alert_manager.get_active_alerts()

# 使用示例
if __name__ == "__main__":
    # 测试告警功能
    print("🧪 测试飞书告警功能")
    print("=" * 60)
    
    # 创建测试告警
    alert = send_alert(
        level=AlertLevel.WARNING,
        alert_type=AlertType.PERFORMANCE,
        title="测试告警 - 响应时间过长",
        message="数据获取模块响应时间超过阈值",
        module="data_fetch",
        metric="response_time",
        value=5500,
        threshold=5000,
        send_to_feishu=False  # 测试时不实际发送
    )
    
    print(f"✅ 告警创建成功: {alert.title}")
    print(f"   级别: {alert.level}")
    print(f"   模块: {alert.module}")
    
    # 获取告警摘要
    summary = get_alert_summary()
    print(f"\n📊 告警摘要:")
    print(f"   总告警数: {summary['summary']['total_alerts']}")
    print(f"   活跃告警: {summary['summary']['active_alerts']}")
    
    # 检查错误率告警
    error_alert = check_error_rate(15, 100, "data_fetch")
    if error_alert:
        print(f"\n⚠️  错误率告警触发: {error_alert.title}")
    
    print("\n✅ 飞书告警模块测试完成")