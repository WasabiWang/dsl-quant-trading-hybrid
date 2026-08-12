#!/usr/bin/env python3
"""DSL v4.5.9 — 统一审计日志模块

所有关键操作记录到 audit_log 表，支持事后追溯和合规审计。
"""

import sqlite3
import json
import os
import threading
from datetime import datetime
from typing import Optional, Dict, Any

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "paper_trading.db")

_lock = threading.Lock()


def _ensure_table():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                source TEXT NOT NULL,
                action TEXT NOT NULL,
                target TEXT DEFAULT '',
                detail TEXT DEFAULT '{}',
                result TEXT DEFAULT 'success',
                operator TEXT DEFAULT 'system',
                trace_id TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_source ON audit_log(source, action)
        """)
        conn.commit()
        conn.close()
    except Exception:
        pass


def log(source: str, action: str, target: str = "",
        detail: Dict[str, Any] = None, result: str = "success",
        operator: str = "system", trace_id: str = "") -> bool:
    """写入审计日志

    Args:
        source: 来源模块 ('cron:batch_train', 'paper_trader', 'mcp:claude', 'manual:webui')
        action: 操作类型 ('trade:buy', 'trade:sell', 'model:retrain', 'config:update', 'system:health')
        target: 操作对象 ('688525', 'config/adaptive_params.yaml', '')
        detail: 附加数据 (dict, 自动JSON序列化)
        result: 'success' 或 'failed:<reason>'
        operator: 操作者标识
        trace_id: 链路追踪ID

    Returns:
        写入成功返回 True
    """
    try:
        with _lock:
            _ensure_table()
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO audit_log (source, action, target, detail, result, operator, trace_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, action, target,
                 json.dumps(detail or {}, ensure_ascii=False, default=str),
                 result, operator, trace_id)
            )
            conn.commit()
            conn.close()
            return True
    except Exception:
        return False  # 审计失败不影响主流程


def query(source: str = None, action: str = None, target: str = None,
          limit: int = 100, since_hours: int = None) -> list:
    """查询审计日志"""
    try:
        _ensure_table()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        sql = "SELECT * FROM audit_log WHERE 1=1"
        params = []

        if source:
            sql += " AND source = ?"
            params.append(source)
        if action:
            sql += " AND action = ?"
            params.append(action)
        if target:
            sql += " AND target = ?"
            params.append(target)
        if since_hours:
            sql += " AND timestamp >= datetime('now', 'localtime', ?)"
            params.append(f"-{since_hours} hours")

        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_summary(since_hours: int = 24) -> Dict[str, Any]:
    """获取审计摘要统计"""
    try:
        _ensure_table()
        conn = sqlite3.connect(DB_PATH)

        total = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE timestamp >= datetime('now', 'localtime', ?)",
            (f"-{since_hours} hours",)
        ).fetchone()[0]

        failed = conn.execute(
            "SELECT COUNT(*) FROM audit_log "
            "WHERE result != 'success' "
            "AND timestamp >= datetime('now', 'localtime', ?)",
            (f"-{since_hours} hours",)
        ).fetchone()[0]

        by_source = {}
        for row in conn.execute(
            "SELECT source, COUNT(*) as cnt FROM audit_log "
            "WHERE timestamp >= datetime('now', 'localtime', ?) "
            "GROUP BY source ORDER BY cnt DESC",
            (f"-{since_hours} hours",)
        ):
            by_source[row[0]] = row[1]

        by_action = {}
        for row in conn.execute(
            "SELECT action, COUNT(*) as cnt FROM audit_log "
            "WHERE timestamp >= datetime('now', 'localtime', ?) "
            "GROUP BY action ORDER BY cnt DESC",
            (f"-{since_hours} hours",)
        ):
            by_action[row[0]] = row[1]

        conn.close()
        return {
            "total": total,
            "failed": failed,
            "success_rate": round((total - failed) / total * 100, 1) if total else 100,
            "by_source": by_source,
            "by_action": by_action,
            "since_hours": since_hours,
        }
    except Exception:
        return {"total": 0, "failed": 0, "by_source": {}, "by_action": {}}
