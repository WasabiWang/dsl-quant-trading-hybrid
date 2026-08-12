#!/usr/bin/env python3
"""
统一管控中心 - 阶段3核心产出
整合版本管理、灰度发布、成本核算、自动迭代、
任务调度、消息网关、监控告警、自动重试等功能
"""
import os
import json
import time
import requests
from typing import Dict, Any, List, Callable, Optional
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control_plane.version_manager import VersionManager, version_manager
from control_plane.canary_release import CanaryReleaseManager, canary_manager
from control_plane.cost_calculator import CostCalculator, cost_calculator
from review_engine.auto_iteration import AutoIterationEngine, auto_iteration_engine
from common.id_generator import IDGenerator, id_gen
from common.logger import get_logger

logger = get_logger("control_center")

# 飞书配置 — 必须通过环境变量设置，不提供硬编码默认值
FEISHU_CONFIG = {
    "app_id": os.getenv("FEISHU_APP_ID", ""),
    "app_secret": os.getenv("FEISHU_APP_SECRET", ""),
    "default_receive_user": os.getenv("FEISHU_RECEIVE_USER", "ou_xxx")
}

class ControlCenter:
    """统一管控中心 - 阶段3核心产出"""
    
    def __init__(self):
        self.start_time = time.time()
        self.feishu_token_cache = None
        self.feishu_token_expire_time = 0
        self.task_retry_config = {  # 失败重试配置
            "max_retries": 3,
            "retry_delay": 2,  # 初始延迟秒数，指数退避
            "retryable_errors": ["timeout", "connection error", "500", "502", "503", "504"]
        }
        logger.info("统一管控中心已初始化")
    
    # ==================== 统一消息网关模块 ====================
    def _get_feishu_access_token(self) -> Optional[str]:
        """获取飞书访问令牌，带缓存"""
        if not FEISHU_CONFIG["app_id"] or not FEISHU_CONFIG["app_secret"]:
            logger.error("飞书凭据未配置: 请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET 环境变量")
            return None

        now = time.time()
        if self.feishu_token_cache and now < self.feishu_token_expire_time - 60:
            return self.feishu_token_cache

        try:
            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            resp = requests.post(url, json={
                "app_id": FEISHU_CONFIG["app_id"],
                "app_secret": FEISHU_CONFIG["app_secret"]
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("code") == 0:
                self.feishu_token_cache = data["tenant_access_token"]
                self.feishu_token_expire_time = now + data["expire"]
                return self.feishu_token_cache
            else:
                logger.error(f"获取飞书Token失败: {data.get('msg')}")
                return None
        except Exception as e:
            logger.error(f"获取飞书Token异常: {e}")
            return None
    
    def send_feishu_message(self,
                          content: str,
                          receive_user: Optional[str] = None,
                          msg_type: str = "text",
                          title: Optional[str] = None) -> bool:
        """发送飞书消息，带自动重试"""
        receive_user = receive_user or FEISHU_CONFIG["default_receive_user"]
        token = self._get_feishu_access_token()
        
        if not token:
            logger.error("无法获取飞书Token，消息发送失败")
            return False
        
        # 构造消息内容
        if msg_type == "text":
            content_data = {"text": content}
        elif msg_type == "post":
            content_data = {
                "post": {
                    "zh_cn": {
                        "title": title or "系统通知",
                        "content": [[{"tag": "text", "text": content}]]
                    }
                }
            }
        else:
            content_data = {"text": content}
        
        # 重试发送
        for retry in range(self.task_retry_config["max_retries"]):
            try:
                url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id"
                headers = {
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8"
                }
                payload = {
                    "receive_id": receive_user,
                    "msg_type": msg_type,
                    "content": json.dumps(content_data, ensure_ascii=False)
                }
                
                resp = requests.post(url, headers=headers, json=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                
                if data.get("code") == 0:
                    logger.info(f"飞书消息发送成功，接收人: {receive_user}")
                    return True
                else:
                    logger.error(f"飞书消息发送失败: {data.get('msg')}，重试 {retry+1}/{self.task_retry_config['max_retries']}")
                    
            except Exception as e:
                logger.error(f"飞书消息发送异常: {e}，重试 {retry+1}/{self.task_retry_config['max_retries']}")
            
            # 指数退避等待
            time.sleep(self.task_retry_config["retry_delay"] * (2 ** retry))
        
        logger.error(f"飞书消息发送最终失败，已重试 {self.task_retry_config['max_retries']} 次")
        return False
    
    def send_alert(self, alert_level: str, title: str, content: str) -> bool:
        """发送告警消息，自动按级别格式化"""
        level_emoji = {
            "CRITICAL": "🔴",
            "ERROR": "🟠",
            "WARNING": "🟡",
            "INFO": "🟢"
        }
        emoji = level_emoji.get(alert_level, "⚪")
        formatted_content = f"{emoji} [{alert_level}] {title}\n\n{content}\n\n⏰ 发送时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        return self.send_feishu_message(formatted_content, msg_type="post", title=f"{emoji} {alert_level} 告警")
    
    def get_system_status(self) -> Dict[str, Any]:
        """获取系统整体状态"""
        uptime = time.time() - self.start_time
        
        return {
            "control_center": {
                "status": "OPERATIONAL",
                "uptime_seconds": round(uptime, 2),
                "uptime_formatted": f"{int(uptime//3600)}h {int((uptime%3600)//60)}m {int(uptime%60)}s",
                "version": "3.0.0"
            },
            "version_manager": {
                "strategies": len(version_manager.list_versions("strategies")),
                "models": len(version_manager.list_versions("models")),
                "rules": len(version_manager.list_versions("rules")),
                "releases": len(os.listdir(version_manager.releases_dir)) if os.path.exists(version_manager.releases_dir) else 0
            },
            "canary_release": {
                "active_experiments": len([e for e in canary_manager.active_experiments.values() if e.get("status") == "ACTIVE"]),
                "total_experiments": len(canary_manager.active_experiments)
            },
            "cost_calculator": cost_calculator.get_daily_costs(),
            "auto_iteration": {
                "recorded_trades": len(auto_iteration_engine.performance_history),
                "recent_suggestions": len(auto_iteration_engine.get_recent_suggestions(limit=10))
            },
            "id_generator": {
                "total_ids_generated": "N/A"  # 实际应该有计数器
            }
        }
    
    def execute_with_monitoring(self,
                               task_name: str,
                               func: Callable,
                               *args,
                               **kwargs) -> Any:
        """执行任务并自动监控成本和性能"""
        task_id = IDGenerator.generate_short_id(f"TASK_{task_name.upper()}_")
        start_time = time.time()
        
        logger.info(f"开始执行任务: {task_name} [{task_id}]")
        
        try:
            # 执行任务
            result = func(*args, **kwargs)
            
            # 记录成功
            execution_time = (time.time() - start_time) * 1000  # 转换为毫秒
            cost_calculator.calculate_task_cost(
                task_name=task_name,
                execution_time_ms=execution_time,
                metadata={"task_id": task_id, "status": "SUCCESS"}
            )
            
            logger.info(f"任务完成: {task_name} [{task_id}] "
                       f"耗时: {execution_time:.1f}ms")
            return result
            
        except Exception as e:
            # 记录失败
            execution_time = (time.time() - start_time) * 1000
            cost_calculator.calculate_task_cost(
                task_name=task_name,
                execution_time_ms=execution_time,
                metadata={"task_id": task_id, "status": "FAILED", "error": str(e)}
            )
            
            logger.error(f"任务失败: {task_name} [{task_id}] "
                        f"耗时: {execution_time:.1f}ms | 错误: {e}")
            raise
    
    # ==================== 任务调度管理模块 ====================
    def list_all_cron_jobs(self) -> List[Dict]:
        """列出所有定时任务"""
        try:
            import subprocess
            result = subprocess.run(
                ["openclaw", "cron", "list", "--json"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                logger.error(f"获取定时任务列表失败: {result.stderr}")
                return []
        except Exception as e:
            logger.error(f"获取定时任务列表异常: {e}")
            return []
    
    def enable_cron_job(self, job_id: str) -> bool:
        """启用定时任务"""
        try:
            import subprocess
            result = subprocess.run(
                ["openclaw", "cron", "enable", job_id],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                logger.info(f"定时任务已启用: {job_id}")
                return True
            else:
                logger.error(f"启用定时任务失败: {job_id}, 错误: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"启用定时任务异常: {job_id}, 错误: {e}")
            return False
    
    def disable_cron_job(self, job_id: str) -> bool:
        """禁用定时任务"""
        try:
            import subprocess
            result = subprocess.run(
                ["openclaw", "cron", "disable", job_id],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                logger.info(f"定时任务已禁用: {job_id}")
                return True
            else:
                logger.error(f"禁用定时任务失败: {job_id}, 错误: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"禁用定时任务异常: {job_id}, 错误: {e}")
            return False
    
    def run_cron_job_now(self, job_id: str) -> bool:
        """立即执行一次定时任务"""
        try:
            import subprocess
            result = subprocess.run(
                ["openclaw", "cron", "run", job_id],
                capture_output=True,
                text=True,
                timeout=300  # 给任务足够的执行时间
            )
            if result.returncode == 0:
                logger.info(f"定时任务已手动执行: {job_id}")
                return True
            else:
                logger.error(f"手动执行定时任务失败: {job_id}, 错误: {result.stderr}")
                return False
        except Exception as e:
            logger.error(f"手动执行定时任务异常: {job_id}, 错误: {e}")
            return False
    
    def get_cron_job_status(self, job_id: str) -> Optional[Dict]:
        """获取定时任务状态"""
        jobs = self.list_all_cron_jobs()
        for job in jobs:
            if job.get("id") == job_id:
                return job
        return None
    
    # ==================== 系统状态与优化建议 ====================
    def get_system_status(self) -> Dict[str, Any]:
        """获取系统整体状态"""
        uptime = time.time() - self.start_time
        cron_jobs = self.list_all_cron_jobs()
        
        # 统计任务状态
        enabled_jobs = [j for j in cron_jobs if j.get("enabled")]
        failed_jobs = [j for j in cron_jobs if j.get("state", {}).get("lastRunStatus") == "ERROR"]
        success_jobs = [j for j in cron_jobs if j.get("state", {}).get("lastRunStatus") == "OK"]
        
        return {
            "control_center": {
                "status": "OPERATIONAL",
                "uptime_seconds": round(uptime, 2),
                "uptime_formatted": f"{int(uptime//3600)}h {int((uptime%3600)//60)}m {int(uptime%60)}s",
                "version": "3.0.0"
            },
            "task_scheduler": {
                "total_jobs": len(cron_jobs),
                "enabled_jobs": len(enabled_jobs),
                "disabled_jobs": len(cron_jobs) - len(enabled_jobs),
                "last_run_success": len(success_jobs),
                "last_run_failed": len(failed_jobs),
                "jobs": cron_jobs
            },
            "version_manager": {
                "strategies": len(version_manager.list_versions("strategies")),
                "models": len(version_manager.list_versions("models")),
                "rules": len(version_manager.list_versions("rules")),
                "releases": len(os.listdir(version_manager.releases_dir)) if os.path.exists(version_manager.releases_dir) else 0
            },
            "canary_release": {
                "active_experiments": len([e for e in canary_manager.active_experiments.values() if e.get("status") == "ACTIVE"]),
                "total_experiments": len(canary_manager.active_experiments)
            },
            "cost_calculator": cost_calculator.get_daily_costs(),
            "auto_iteration": {
                "recorded_trades": len(auto_iteration_engine.performance_history),
                "recent_suggestions": len(auto_iteration_engine.get_recent_suggestions(limit=10))
            },
            "message_gateway": {
                "feishu_token_status": "VALID" if self.feishu_token_cache else "INVALID",
                "token_expire_in": max(0, int(self.feishu_token_expire_time - time.time())) if self.feishu_token_cache else 0
            }
        }
    
    def suggest_optimizations(self) -> List[Dict]:
        """获取系统优化建议"""
        suggestions = []
        
        # 从自动迭代引擎获取建议
        iteration_suggestions = auto_iteration_engine.get_recent_suggestions(limit=5)
        for sug in iteration_suggestions:
            suggestions.append({
                "source": "auto_iteration",
                "type": sug.get("type", "unknown"),
                "priority": sug.get("priority", "MEDIUM"),
                "title": sug.get("suggestion", "无建议"),
                "description": sug.get("issue", ""),
                "data": sug.get("data", {})
            })
        
        # 从成本分析获取建议
        daily_costs = cost_calculator.get_daily_costs()
        if daily_costs["total_cost_usd"] > 10.0:  # 日成本超过10美元
            suggestions.append({
                "source": "cost_calculator",
                "type": "cost_optimization",
                "priority": "HIGH",
                "title": "日运行成本较高",
                "description": f"今日已产生 ${daily_costs['total_cost_usd']:.2f} 成本",
                "data": {"daily_cost": daily_costs["total_cost_usd"], "breakdown": daily_costs["breakdown"]}
            })
        
        # 从任务调度获取建议
        cron_jobs = self.list_all_cron_jobs()
        failed_jobs = [j for j in cron_jobs if j.get("state", {}).get("lastRunStatus") == "ERROR"]
        if failed_jobs:
            suggestions.append({
                "source": "task_scheduler",
                "type": "task_failure",
                "priority": "HIGH",
                "title": f"有 {len(failed_jobs)} 个定时任务最近执行失败",
                "description": f"失败任务: {', '.join([j.get('name', str(j.get('id'))) for j in failed_jobs])}",
                "data": {"failed_jobs": failed_jobs}
            })
        
        # 检查全量任务禁用
        enabled_jobs = [j for j in cron_jobs if j.get("enabled")]
        if len(enabled_jobs) == 0 and len(cron_jobs) > 0:
            suggestions.append({
                "source": "task_scheduler",
                "type": "all_jobs_disabled",
                "priority": "MEDIUM",
                "title": "所有定时任务处于禁用状态",
                "description": f"共 {len(cron_jobs)} 个定时任务全部被禁用，系统无自动化运行能力",
                "data": {"total_jobs": len(cron_jobs)}
            })
        
        return suggestions

# 全局实例
control_center = ControlCenter()