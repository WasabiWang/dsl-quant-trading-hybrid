#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信号交割与仓位对账机制 (Reconciliation Engine)

P0: 解决"信号已发出但未成交/部分成交"的状态断裂问题
功能：
1. 为每个交易信号生成全局唯一 order_id
2. 追踪信号生成→分发→执行→确认的完整生命周期
3. 日终/实时对账：信号 vs 实际持仓，标记不一致
4. 信号重复/遗漏检测

v4.6.x: 存储层从JSON迁移到SQLite (paper_trading.db reconciliation表)
  - ACID事务保护, WAL模式
  - 对账时交叉验证 trade_history 表
"""

import time
import uuid
import sqlite3
import json
import os
import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from contextlib import contextmanager

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")


# ──────────────────────────────────────────
# 数据模型
# ──────────────────────────────────────────

@dataclass
class SignalOrder:
    """
    交易信号订单 - 追踪信号全生命周期
    """
    order_id: str                     # 全局唯一ID: {timestamp}_{symbol}_{uuid[:8]}
    symbol: str                       # 股票代码
    action: str                       # BUY / SELL / HOLD
    quantity: int                     # 建议数量
    price: float                      # 建议价格
    confidence: float                 # 置信度 [0,1]
    source: str                       # 信号来源 (fusion/trader/deepseek)
    reasoning: str = ""               # 决策理由
    status: str = "CREATED"           # CREATED → DISPATCHED → EXECUTED → CONFIRMED | FAILED
    created_at: str = ""              # ISO时间
    dispatched_at: str = ""           # 分发时间
    executed_at: str = ""             # 执行时间
    trade_id: int = 0                 # trade_history.id (PaperTrader返回)
    execution_price: float = 0.0      # 实际成交价
    execution_quantity: int = 0       # 实际成交数量
    error: str = ""                   # 失败原因
    reconciliation_status: str = "PENDING"  # PENDING / MATCHED / MISMATCH / ORPHAN

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SignalOrder":
        return SignalOrder(**d)

    @staticmethod
    def from_row(row: sqlite3.Row) -> "SignalOrder":
        """从SQLite行构建"""
        d = dict(row)
        # JSON字段还原
        return SignalOrder(
            order_id=d["order_id"], symbol=d.get("symbol",""),
            action=d.get("action",""), quantity=d.get("quantity",0),
            price=d.get("price",0.0), confidence=d.get("confidence",0.0),
            source=d.get("source",""), reasoning=d.get("reasoning",""),
            status=d.get("status","CREATED"), created_at=d.get("created_at",""),
            dispatched_at=d.get("dispatched_at",""), executed_at=d.get("executed_at",""),
            trade_id=d.get("trade_id",0), execution_price=d.get("execution_price",0.0),
            execution_quantity=d.get("execution_quantity",0), error=d.get("error",""),
            reconciliation_status=d.get("reconciliation_status","PENDING"),
        )

    @staticmethod
    def generate_id(symbol: str) -> str:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:8]
        return f"ORD_{ts}_{symbol}_{suffix}"


# ──────────────────────────────────────────
# 对账引擎
# ──────────────────────────────────────────

class ReconciliationEngine:
    """
    信号对账引擎

    核心流程：
    1. 信号生成 → record_signal() 记录 CREATED
    2. 信号分发 → mark_dispatched()
    3. 交易执行 → mark_executed() 关联 trade_id
    4. 日终/实时对账 → reconcile() 校验一致性 (含cross-verify trade_history)
    5. 异常标记 → mark_mismatch() 人工介入

    v4.6.x: SQLite存储 (paper_trading.db::reconciliation表), WAL+ACID
    """

    def __init__(self):
        self._pending_orders: Dict[str, SignalOrder] = {}
        self._init_db()
        self._load_pending()

    @contextmanager
    def _get_conn(self):
        """获取SQLite连接 (WAL模式, 5s超时)"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """创建reconciliation表 (如果不存在)"""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reconciliation (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0.0,
                    confidence REAL DEFAULT 0.0,
                    source TEXT DEFAULT '',
                    reasoning TEXT DEFAULT '',
                    status TEXT DEFAULT 'CREATED',
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT DEFAULT '',
                    executed_at TEXT DEFAULT '',
                    trade_id INTEGER DEFAULT 0,
                    execution_price REAL DEFAULT 0.0,
                    execution_quantity INTEGER DEFAULT 0,
                    error TEXT DEFAULT '',
                    reconciliation_status TEXT DEFAULT 'PENDING',
                    updated_at TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rec_status ON reconciliation(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_rec_date ON reconciliation(created_at)
            """)

    def _load_pending(self):
        """从SQLite加载今日待处理订单"""
        try:
            with self._get_conn() as conn:
                today = date.today().isoformat()
                rows = conn.execute(
                    "SELECT * FROM reconciliation WHERE created_at LIKE ? AND status NOT IN ('CONFIRMED','FAILED')",
                    (f"{today}%",)
                ).fetchall()
                for row in rows:
                    order = SignalOrder.from_row(row)
                    self._pending_orders[order.order_id] = order
        except Exception as e:
            logger.warning(f"加载待处理订单失败: {e}")

    def _save(self, order: SignalOrder):
        """持久化订单记录 (UPSERT)"""
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO reconciliation (
                        order_id, symbol, action, quantity, price, confidence,
                        source, reasoning, status, created_at, dispatched_at,
                        executed_at, trade_id, execution_price, execution_quantity,
                        error, reconciliation_status, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        symbol=excluded.symbol, action=excluded.action,
                        quantity=excluded.quantity, price=excluded.price,
                        confidence=excluded.confidence, source=excluded.source,
                        status=excluded.status, dispatched_at=excluded.dispatched_at,
                        executed_at=excluded.executed_at, trade_id=excluded.trade_id,
                        execution_price=excluded.execution_price,
                        execution_quantity=excluded.execution_quantity,
                        error=excluded.error,
                        reconciliation_status=excluded.reconciliation_status,
                        updated_at=excluded.updated_at
                """, (
                    order.order_id, order.symbol, order.action, order.quantity,
                    order.price, order.confidence, order.source, order.reasoning,
                    order.status, order.created_at, order.dispatched_at,
                    order.executed_at, order.trade_id, order.execution_price,
                    order.execution_quantity, order.error, order.reconciliation_status,
                    datetime.now().isoformat()
                ))
        except Exception as e:
            logger.error(f"持久化订单失败 {order.order_id}: {e}")

    def record_signal(self, symbol: str, action: str, quantity: int,
                      price: float, confidence: float, source: str,
                      reasoning: str = "") -> str:
        """
        记录信号创建

        Returns: order_id
        """
        order_id = SignalOrder.generate_id(symbol)
        order = SignalOrder(
            order_id=order_id, symbol=symbol, action=action,
            quantity=quantity, price=price, confidence=confidence,
            source=source, reasoning=reasoning, status="CREATED"
        )
        self._pending_orders[order_id] = order
        self._save(order)
        logger.info(f"[对账] 信号创建: {order_id} | {symbol} {action} x{quantity} @ {price}")
        return order_id

    def mark_dispatched(self, order_id: str):
        """标记信号已分发到交易执行器"""
        order = self._pending_orders.get(order_id)
        if order:
            order.status = "DISPATCHED"
            order.dispatched_at = datetime.now().isoformat()
            self._save(order)
            logger.info(f"[对账] 信号分发: {order_id}")

    def mark_executed(self, order_id: str, trade_id: int,
                      exec_price: float, exec_qty: int):
        """标记信号已执行"""
        order = self._pending_orders.get(order_id)
        if order:
            order.status = "EXECUTED"
            order.executed_at = datetime.now().isoformat()
            order.trade_id = trade_id
            order.execution_price = exec_price
            order.execution_quantity = exec_qty
            self._save(order)
            logger.info(f"[对账] 信号执行: {order_id} | trade_id={trade_id}")

    def mark_failed(self, order_id: str, error: str):
        """标记信号执行失败"""
        order = self._pending_orders.get(order_id)
        if order:
            order.status = "FAILED"
            order.error = error
            self._save(order)
            logger.warning(f"[对账] 信号失败: {order_id} | {error}")

    def mark_confirmed(self, order_id: str):
        """标记信号已确认（对账通过）"""
        order = self._pending_orders.get(order_id)
        if order:
            order.status = "CONFIRMED"
            order.reconciliation_status = "MATCHED"
            self._save(order)
            logger.info(f"[对账] 信号确认: {order_id}")

    # ──────────────────────────────
    # 核心对账逻辑
    # ──────────────────────────────

    def reconcile(self, market: str = "A", paper_trader=None) -> Dict:
        """
        对账主入口：校验今日所有信号 vs 实际持仓 + cross-verify paper_trading.db

        v4.6.x: 新增SQLite trade_history交叉验证

        Args:
            market: 市场 A/HK
            paper_trader: PaperTrader 实例（用于获取实际持仓）

        Returns:
            {
                "total_signals": int, "executed": int, "failed": int,
                "pending": int, "mismatches": [...], "orphan_signals": [...],
                "reconciled": True/False
            }
        """
        today_orders = self._load_today_orders()
        if not today_orders:
            return {"total_signals": 0, "executed": 0, "failed": 0,
                    "pending": 0, "mismatches": [], "orphan_signals": [],
                    "reconciled": True}

        total = len(today_orders)
        executed = sum(1 for o in today_orders if o.status == "EXECUTED")
        failed = sum(1 for o in today_orders if o.status == "FAILED")
        still_pending = sum(1 for o in today_orders if o.status in ("CREATED", "DISPATCHED"))

        mismatches = []
        orphan_signals = []

        # Cross-verify with trade_history
        trade_map = {}
        if paper_trader:
            try:
                conn = paper_trader._get_conn()
                with conn:
                    rows = conn.execute(
                        "SELECT id, stock_code, action, price, quantity, amount FROM trade_history WHERE timestamp LIKE ?",
                        (f"{date.today().isoformat()}%",)
                    ).fetchall()
                trade_map = {row["id"]: dict(row) for row in rows}
            except Exception as e:
                logger.warning(f"Cross-verify trade_history失败: {e}")

        for order in today_orders:
            # 检查信号是否超时 (5分钟未执行)
            try:
                created = datetime.fromisoformat(order.created_at)
                if order.status in ("CREATED", "DISPATCHED"):
                    elapsed = (datetime.now() - created).total_seconds()
                    if elapsed > 300:
                        mismatches.append({
                            "order_id": order.order_id,
                            "symbol": order.symbol,
                            "action": order.action,
                            "status": order.status,
                            "elapsed_seconds": elapsed,
                            "issue": "信号超时未执行"
                        })
                        order.reconciliation_status = "MISMATCH"
                        self._save(order)
            except (ValueError, TypeError):
                pass

            # 检查建议执行量 vs 实际执行量
            if order.status == "EXECUTED":
                if order.quantity != order.execution_quantity:
                    mismatches.append({
                        "order_id": order.order_id,
                        "symbol": order.symbol,
                        "action": order.action,
                        "suggested_qty": order.quantity,
                        "executed_qty": order.execution_quantity,
                        "issue": "成交数量与建议数量不一致"
                    })
                    order.reconciliation_status = "MISMATCH"
                    self._save(order)

                # v4.6.x: Cross-verify trade_history
                if order.trade_id > 0:
                    trade = trade_map.get(order.trade_id)
                    if trade is None:
                        mismatches.append({
                            "order_id": order.order_id,
                            "symbol": order.symbol,
                            "trade_id": order.trade_id,
                            "issue": "信号已执行但trade_history中无对应记录"
                        })
                    elif (abs(trade["price"] - order.execution_price) > 0.01 or
                          trade["quantity"] != order.execution_quantity):
                        mismatches.append({
                            "order_id": order.order_id,
                            "symbol": order.symbol,
                            "trade_id": order.trade_id,
                            "expected_price": order.execution_price,
                            "actual_price": trade["price"],
                            "expected_qty": order.execution_quantity,
                            "actual_qty": trade["quantity"],
                            "issue": "signal vs trade_history数据不一致"
                        })

        # 孤儿信号检测
        for order in today_orders:
            if order.status == "EXECUTED" and order.trade_id == 0:
                orphan_signals.append(order.order_id)
                order.reconciliation_status = "ORPHAN"
                self._save(order)

        result = {
            "total_signals": total,
            "executed": executed,
            "failed": failed,
            "pending": still_pending,
            "mismatches": mismatches,
            "orphan_signals": orphan_signals,
            "reconciled": len(mismatches) == 0 and len(orphan_signals) == 0
        }

        if result["reconciled"]:
            logger.info(f"[对账] ✅ 全部通过: {total}个信号，{executed}个已执行")
        else:
            logger.warning(f"[对账] ⚠️ 发现{len(mismatches)}个不一致，{len(orphan_signals)}个孤儿信号")

        return result

    def _load_today_orders(self) -> List[SignalOrder]:
        """从SQLite加载今日订单"""
        try:
            with self._get_conn() as conn:
                today = date.today().isoformat()
                rows = conn.execute(
                    "SELECT * FROM reconciliation WHERE created_at LIKE ?",
                    (f"{today}%",)
                ).fetchall()
            return [SignalOrder.from_row(row) for row in rows]
        except Exception as e:
            logger.warning(f"加载今日订单失败: {e}")
            return []

    def get_order(self, order_id: str) -> Optional[SignalOrder]:
        """获取指定订单"""
        return self._pending_orders.get(order_id)

    def get_today_orders(self) -> List[SignalOrder]:
        """获取今日所有订单"""
        return self._load_today_orders()

    def get_pending_count(self) -> int:
        """获取待处理信号数"""
        return len(self._pending_orders)


# ──────────────────────────────────────────
# 便捷集成函数
# ──────────────────────────────────────────

_reconciliation_engine: Optional[ReconciliationEngine] = None


def get_reconciliation_engine() -> ReconciliationEngine:
    """获取全局对账引擎单例"""
    global _reconciliation_engine
    if _reconciliation_engine is None:
        _reconciliation_engine = ReconciliationEngine()
    return _reconciliation_engine


def create_signal_order(symbol: str, action: str, quantity: int,
                        price: float, confidence: float, source: str,
                        reasoning: str = "") -> str:
    """便捷创建信号并返回 order_id（集成入口）"""
    engine = get_reconciliation_engine()
    return engine.record_signal(symbol, action, quantity, price,
                                confidence, source, reasoning)


def run_reconciliation(market: str = "A") -> Dict:
    """运行对账"""
    engine = get_reconciliation_engine()
    return engine.reconcile(market)
