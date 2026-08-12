#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统级可观测性监控 - System Monitor

P2: 采集和上报关键业务指标
监控指标：
1. 端到端信号延迟（创建→分发→执行→确认）
2. 信号丢失率（发出但未执行）
3. 信号执行成功率
4. 对账不一致数量
5. 系统健康状态聚合报告
"""

import os
import json
import time
import logging
from datetime import datetime, date
from typing import Dict, List, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)

STORAGE_PATH = os.path.expanduser(
    "~/.openclaw/workspace/dsl-quant-trading-hybrid/data/monitoring"
)
os.makedirs(STORAGE_PATH, exist_ok=True)


class SystemMonitor:
    """
    系统监控器
    
    采集端到端信号延迟、丢失率、成功率、
    对账不一致等关键业务指标，生成监控报告。
    """

    def __init__(self):
        self._metrics: Dict[str, List] = defaultdict(list)
        self._load_state()

    def _get_file_path(self) -> str:
        return os.path.join(STORAGE_PATH, "metrics_daily.json")

    def _load_state(self):
        """加载今日已有指标"""
        path = self._get_file_path()
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                for key, values in data.items():
                    self._metrics[key] = values
            except (json.JSONDecodeError, Exception):
                pass

    def _save_state(self):
        """持久化指标（累计存储）"""
        path = self._get_file_path()
        try:
            with open(path, "w") as f:
                json.dump(dict(self._metrics), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def record_signal_latency(self, source: str, latency_ms: float):
        """记录信号延迟"""
        self._metrics.setdefault("signal_latencies", []).append({
            "source": source,
            "latency_ms": round(latency_ms, 1),
            "time": datetime.now().isoformat()
        })
        # 只保留最近200条
        if len(self._metrics["signal_latencies"]) > 200:
            self._metrics["signal_latencies"] = self._metrics["signal_latencies"][-200:]
        self._save_state()

    def record_signal_result(self, symbol: str, action: str, success: bool,
                             error: str = ""):
        """记录信号执行结果"""
        self._metrics.setdefault("signal_results", []).append({
            "symbol": symbol,
            "action": action,
            "success": success,
            "error": error[:50] if error else "",
            "time": datetime.now().isoformat()
        })
        if len(self._metrics["signal_results"]) > 500:
            self._metrics["signal_results"] = self._metrics["signal_results"][-500:]
        self._save_state()

    def record_reconciliation_result(self, total: int, mismatches: int,
                                     orphans: int):
        """记录对账结果"""
        self._metrics.setdefault("reconciliation_results", []).append({
            "total": total,
            "mismatches": mismatches,
            "orphans": orphans,
            "time": datetime.now().isoformat()
        })
        self._save_state()

    def get_snapshot(self) -> Dict:
        """
        获取系统状态快照
        
        Returns:
            {
                "overall_health": "healthy"/"warning"/"critical",
                "time_period": "2026-04-24",
                "signal_stats": {...},
                "reconciliation_stats": {...},
                "alerts": [...]
            }
        """
        alerts = []
        
        # 信号统计
        results = self._metrics.get("signal_results", [])
        total_signals = len(results)
        successes = sum(1 for r in results if r.get("success"))
        failures = total_signals - successes
        success_rate = (successes / total_signals * 100) if total_signals > 0 else 100.0
        
        # 延迟统计
        latencies = self._metrics.get("signal_latencies", [])
        avg_latency = (
            sum(l["latency_ms"] for l in latencies) / len(latencies)
            if latencies else 0
        )
        max_latency = max((l["latency_ms"] for l in latencies), default=0)
        
        # 对账统计
        reconciles = self._metrics.get("reconciliation_results", [])
        recent_recon = reconciles[-1] if reconciles else {}
        total_mismatches = sum(r.get("mismatches", 0) for r in reconciles)
        total_orphans = sum(r.get("orphans", 0) for r in reconciles)
        
        # 告警判定
        if failures > 0:
            alerts.append({
                "severity": "warning",
                "message": f"有{failures}个信号执行失败（成功率{success_rate:.0f}%）"
            })
        if total_mismatches > 0:
            alerts.append({
                "severity": "warning",
                "message": f"对账发现{total_mismatches}个不一致"
            })
        if total_orphans > 0:
            alerts.append({
                "severity": "warning",
                "message": f"对账发现{total_orphans}个孤儿信号"
            })
        if avg_latency > 10000:  # 10秒
            alerts.append({
                "severity": "warning",
                "message": f"信号平均延迟{avg_latency:.0f}ms，可能超时"
            })
        
        # 整体健康度
        if failures > 2 or total_mismatches > 3:
            health = "critical"
        elif failures > 0 or total_mismatches > 0:
            health = "warning"
        else:
            health = "healthy"
        
        return {
            "overall_health": health,
            "time_period": date.today().isoformat(),
            "report_time": datetime.now().isoformat(),
            "signal_stats": {
                "total": total_signals,
                "success": successes,
                "failure": failures,
                "success_rate_pct": round(success_rate, 1),
                "avg_latency_ms": round(avg_latency, 1),
                "max_latency_ms": round(max_latency, 1),
            },
            "reconciliation_stats": {
                "total_runs": len(reconciles),
                "total_mismatches": total_mismatches,
                "total_orphans": total_orphans,
                "last_run": recent_recon.get("time", ""),
            },
            "alerts": alerts,
        }

    def reset_daily(self):
        """重置今日指标（跨日调用）"""
        today = date.today().isoformat()
        # 备份到历史文件
        hist_path = os.path.join(STORAGE_PATH, f"metrics_{today}.json")
        if not os.path.exists(hist_path):
            self._save_to(hist_path)
        # 清空内存
        self._metrics.clear()
        self._save_state()

    def _save_to(self, path: str):
        try:
            with open(path, "w") as f:
                json.dump(dict(self._metrics), f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# 单例
_monitor: Optional[SystemMonitor] = None


def get_system_monitor() -> SystemMonitor:
    global _monitor
    if _monitor is None:
        _monitor = SystemMonitor()
    return _monitor


def get_health_report() -> Dict:
    """获取系统健康报告"""
    monitor = get_system_monitor()
    return monitor.get_snapshot()
