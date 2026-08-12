#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全链路幂等性控制 - Idempotency Guard

P2: 防止重复消费或重试导致重复下单
方案：
1. 每个order_id全局唯一（由ReconciliationEngine生成）
2. order_id 去重：同一order_id只执行一次
3. 信号级别的幂等：同一symbol+action+timestamp只执行一次（业务唯一键）
"""

import sqlite3
import os
import logging
from datetime import datetime
from typing import Optional, Dict
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.expanduser(
    "~/.openclaw/workspace/dsl-quant-trading-hybrid/data/idempotency.db"
)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


class IdempotencyGuard:
    """
    幂等性守卫
    
    核心规则：
    - 同一 order_id 仅执行一次（严格幂等）
    - 同一 symbol + action + business_date 仅执行一次（业务幂等）
    """

    CREATED = "CREATED"
    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    def __init__(self):
        self._init_db()

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS idempotency (
                    idempotency_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    result TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS business_keys (
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'CREATED',
                    executed_at TEXT,
                    PRIMARY KEY (symbol, action, business_date)
                );
            """)

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def try_acquire(self, order_id: str) -> bool:
        """
        尝试获取订单执行权（幂等检查）
        
        Returns:
            True = 首次执行，可以继续
            False = 已存在，跳过（重复请求）
        """
        try:
            with self._get_conn() as conn:
                existing = conn.execute(
                    "SELECT status FROM idempotency WHERE idempotency_key=?",
                    (order_id,)
                ).fetchone()
                if existing:
                    logger.warning(f"[幂等] 检测到重复order_id: {order_id} (status={existing['status']})")
                    return False
                
                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO idempotency (idempotency_key, status, created_at) VALUES (?, ?, ?)",
                    (order_id, self.IN_FLIGHT, now)
                )
                logger.info(f"[幂等] 获取执行权: {order_id}")
                return True
        except sqlite3.IntegrityError:
            # 并发插入冲突 - 说明另一个线程已插入
            logger.warning(f"[幂等] 并发冲突: {order_id}")
            return False
        except Exception as e:
            logger.error(f"[幂等] 检查异常: {e}")
            return False  # 幂等检查失败时保守起见拒绝执行

    def complete(self, order_id: str, result: str = ""):
        """标记订单已完成"""
        try:
            with self._get_conn() as conn:
                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE idempotency SET status=?, completed_at=?, result=? WHERE idempotency_key=?",
                    (self.COMPLETED, now, result, order_id)
                )
        except Exception as e:
            logger.error(f"[幂等] 完成标记失败: {e}")

    def mark_failed(self, order_id: str, error: str):
        """标记订单失败"""
        try:
            with self._get_conn() as conn:
                now = datetime.now().isoformat()
                conn.execute(
                    "UPDATE idempotency SET status=?, completed_at=?, error=? WHERE idempotency_key=?",
                    (self.FAILED, now, error, order_id)
                )
        except Exception as e:
            logger.error(f"[幂等] 失败标记异常: {e}")

    def check_business_key(self, symbol: str, action: str,
                           business_date: str) -> Optional[str]:
        """
        检查业务唯一键：同一标的+方向+交易日只执行一次
        
        Returns:
            None = 可执行
            order_id str = 已执行，返回上次的order_id
        """
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT order_id, status FROM business_keys WHERE symbol=? AND action=? AND business_date=?",
                    (symbol, action, business_date)
                ).fetchone()
                if row:
                    logger.info(f"[幂等] 业务键已存在: {symbol} {action} {business_date} -> {row['order_id']}")
                    return row["order_id"]
                return None
        except Exception as e:
            logger.error(f"[幂等] 业务键检查异常: {e}")
            return None  # 失败时允许执行（保守）

    def register_business_key(self, symbol: str, action: str,
                              business_date: str, order_id: str) -> bool:
        """注册业务唯一键"""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO business_keys (symbol, action, business_date, order_id) VALUES (?, ?, ?, ?)",
                    (symbol, action, business_date, order_id)
                )
                logger.info(f"[幂等] 注册业务键: {symbol} {action} {business_date}")
                return True
        except sqlite3.IntegrityError:
            logger.warning(f"[幂等] 业务键重复（已存在）: {symbol} {action} {business_date}")
            return False
        except Exception as e:
            logger.error(f"[幂等] 业务键注册异常: {e}")
            return False

    def get_status(self, order_id: str) -> Optional[Dict]:
        """查询订单状态"""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM idempotency WHERE idempotency_key=?",
                    (order_id,)
                ).fetchone()
                if row:
                    return dict(row)
                return None
        except Exception:
            return None


# 单例
_guard: Optional[IdempotencyGuard] = None


def get_idempotency_guard() -> IdempotencyGuard:
    global _guard
    if _guard is None:
        _guard = IdempotencyGuard()
    return _guard
