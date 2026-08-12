#!/usr/bin/env python3
"""core/partial_fill.py — 部分成交处理 v4.5.12

A股限价单常见场景：
- 卖一挂单200手，想买300手 → 只成交200手
- 针对剩余100手: 撤单重下/接受部分成交/转为市价单

策略:
1. 部分成交检测: 下单量 vs 实际成交量
2. 重试策略: 未成交部分 30秒/60秒/120秒 三次重试
3. 接受模式: 超过最小成交比例(默认70%)则接受
"""

import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass
class FillResult:
    """成交结果"""
    order_id: str
    symbol: str
    action: str  # BUY/SELL
    target_qty: int
    filled_qty: int = 0
    avg_price: float = 0.0
    status: str = "PENDING"  # PENDING/PARTIAL/FILLED/REJECTED/CANCELLED
    attempts: int = 0
    fill_ratio: float = 0.0
    error: str = ""


class PartialFillHandler:
    """部分成交处理策略"""

    def __init__(
        self,
        min_fill_ratio: float = 0.70,  # 最小接受比例
        max_retries: int = 3,
        retry_delays: List[float] = None,  # 秒
    ):
        self.min_fill_ratio = min_fill_ratio
        self.max_retries = max_retries
        self.retry_delays = retry_delays or [30, 60, 120]
        self.history: List[FillResult] = []

    def assess_fill(self, result: FillResult) -> str:
        """评估成交结果，返回建议动作: ACCEPT / RETRY / CANCEL"""
        result.fill_ratio = result.filled_qty / max(result.target_qty, 1)

        if result.fill_ratio >= 0.995:
            result.status = "FILLED"
            return "ACCEPT"

        result.status = "PARTIAL"

        if result.fill_ratio >= self.min_fill_ratio:
            return "ACCEPT"  # 超过最小比例，接受

        if result.attempts < self.max_retries:
            return "RETRY"  # 继续重试

        return "CANCEL"  # 超次，取消剩余

    def handle_partial_fill(
        self,
        order_id: str,
        symbol: str,
        action: str,
        target_qty: int,
        filled_qty: int,
        avg_price: float,
        exec_fn,  # callable(order_id, symbol, action, price, remaining_qty) -> FillResult
    ) -> FillResult:
        """处理部分成交：评估→重试/接受/取消

        Args:
            exec_fn: 执行函数，接收 (order_id, symbol, action, price, remaining_qty)
        """
        result = FillResult(
            order_id=order_id, symbol=symbol, action=action,
            target_qty=target_qty, filled_qty=filled_qty, avg_price=avg_price,
        )

        while True:
            result.attempts += 1
            decision = self.assess_fill(result)

            if decision == "ACCEPT":
                break
            elif decision == "CANCEL":
                result.status = "CANCELLED"
                result.error = f"重试{self.max_retries}次后取消，成交{result.filled_qty}/{target_qty}"
                break
            elif decision == "RETRY":
                remaining = target_qty - result.filled_qty
                delay = self.retry_delays[min(result.attempts - 1, len(self.retry_delays) - 1)]
                time.sleep(delay)

                retry_result = exec_fn(order_id, symbol, action, avg_price, remaining)
                if retry_result.status == "FILLED":
                    result.filled_qty += retry_result.filled_qty
                    result.fill_ratio = result.filled_qty / max(target_qty, 1)
                elif retry_result.status == "PARTIAL":
                    result.filled_qty += retry_result.filled_qty
                else:
                    break  # 重试失败，停止

        self.history.append(result)
        return result

    def get_stats(self) -> Dict:
        """获取部分成交统计"""
        total = len(self.history)
        if total == 0:
            return {"total": 0}
        filled = sum(1 for r in self.history if r.status == "FILLED")
        partial = sum(1 for r in self.history if r.status == "PARTIAL")
        cancelled = sum(1 for r in self.history if r.status == "CANCELLED")
        avg_fill = sum(r.fill_ratio for r in self.history) / total
        return {
            "total": total,
            "filled": filled,
            "partial_accepted": partial,
            "cancelled": cancelled,
            "avg_fill_ratio": round(avg_fill, 3),
            "accept_rate": round((filled + partial) / total, 3),
        }


# 全局单例
partial_fill_handler = PartialFillHandler()
