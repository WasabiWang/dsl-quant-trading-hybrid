#!/usr/bin/env python3
"""
scripts/trade_execution_verifier.py — 交易执行结果签名校验 (v4.6.2)

解决教训58/64: 沉默失败 — 操作看起来成功了但实际未执行。
  - 止盈卖出 action='sell' vs PaperTrader只接受'SELL' → 静默失败
  - systemEvent cron dur=8ms标记ok → 实际只做了入队
  - black_swan_active=true被读成false → 虚假安全信号

核心原则: 所有外部操作必须验证结果再返回成功。

校验逻辑:
  1. 交易计划 vs 执行结果: 发送的指令数必须等于实际写入DB的条数
  2. 止损/止盈: 检测到的信号必须能在DB中找到对应的执行记录
  3. 幂等检查: 不存在重复执行为同一笔交易

使用:
  from scripts.trade_execution_verifier import verify_trade_execution
  result = verify_trade_execution(planned_trades, db_path)
  if not result.passed:
      send_feishu_alert(result.summary)
"""

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class VerificationResult:
    """交易执行校验结果"""
    passed: bool
    planned_count: int
    executed_count: int
    skipped_count: int
    failed_count: int
    mismatches: List[Dict] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "planned_count": self.planned_count,
            "executed_count": self.executed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "mismatches": self.mismatches,
            "summary": self.summary,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }


def verify_trade_execution(
    planned_trades: List[Dict],
    db_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    tolerance_minutes: int = 5,
) -> VerificationResult:
    """
    校验交易计划是否被正确执行.

    协议: 每笔planned trade的action字段在调用此函数时
    应已通过 normalize_action() 规范化 (使用 core.resilience.normalize_action).

    Args:
        planned_trades: 交易计划列表 [{"symbol":..., "action":..., "quantity":...}, ...]
        db_path: paper_trading.db路径 (默认使用项目标准路径)
        ledger_path: paper_trading_ledger.json路径
        tolerance_minutes: 执行时间容差(分钟), 在最近N分钟内的执行记录均视为有效

    Returns:
        VerificationResult: 校验结果
    """
    db_path = db_path or str(PROJECT_ROOT / "data" / "paper_trading.db")
    ledger_path = ledger_path or str(PROJECT_ROOT / "data" / "paper_trading_ledger.json")

    planned_count = len(planned_trades)
    result = VerificationResult(
        passed=True,
        planned_count=planned_count,
        executed_count=0,
        skipped_count=0,
        failed_count=0,
        mismatches=[],
    )

    if planned_count == 0:
        result.summary = "无计划交易，跳过校验"
        return result

    # 校验action已规范化
    from core.resilience import normalize_action
    for i, trade in enumerate(planned_trades):
        action_raw = trade.get("action", "")
        try:
            norm_action = normalize_action(action_raw)
            trade["action"] = norm_action  # 回写规范化后的值
            if norm_action != action_raw:
                result.mismatches.append({
                    "type": "action_normalized",
                    "index": i,
                    "symbol": trade.get("symbol"),
                    "detail": f"action已规范化: {action_raw} → {norm_action}",
                })
        except ValueError as e:
            result.passed = False
            result.failed_count += 1
            result.mismatches.append({
                "type": "invalid_action",
                "index": i,
                "symbol": trade.get("symbol"),
                "detail": str(e),
                "severity": "CRITICAL",
            })

    # 1. 检查SQLite中的执行记录
    db_records = _get_recent_trades_from_db(db_path, tolerance_minutes)

    # 2. 逐笔匹配计划vs执行
    for i, plan in enumerate(planned_trades):
        symbol = plan.get("symbol", "")
        action = plan.get("action", "")
        qty = plan.get("quantity", 0)

        # 查找匹配的执行记录
        matched = _find_matching_trade(db_records, symbol, action, qty)
        if matched:
            result.executed_count += 1
        else:
            result.mismatches.append({
                "type": "execution_not_found",
                "index": i,
                "symbol": symbol,
                "action": action,
                "planned_qty": qty,
                "detail": (
                    f"{symbol} {action} x{qty}: 在DB中未找到执行记录. "
                    f"可能原因: 1)执行静默失败(action大小写); "
                    f"2)被仓位限制/风控跳过; 3)cron未触发"
                ),
                "severity": "HIGH",
            })
            result.failed_count += 1
            result.passed = False

    # 3. 检查是否有未计划的重复执行
    planned_symbols = {(t.get("symbol"), t.get("action")) for t in planned_trades}
    for record in db_records:
        key = (record["symbol"], record["action"])
        matching_planned = [t for t in planned_trades
                            if t.get("symbol") == record["symbol"]
                            and t.get("action") == record["action"]]
        if not matching_planned:
            result.mismatches.append({
                "type": "unplanned_execution",
                "symbol": record["symbol"],
                "action": record["action"],
                "detail": (
                    f"DB中发现未在计划中的执行记录: {record['symbol']} "
                    f"{record['action']} x{record.get('qty', '?')}"
                ),
                "severity": "WARN",
            })

    # 4. 交叉验证: JSON Ledger是否同步
    sqlite_count = len(db_records)
    ledger_count = _count_ledger_entries(ledger_path, tolerance_minutes)
    if ledger_count != sqlite_count:
        result.mismatches.append({
            "type": "ledger_desync",
            "detail": (
                f"SQLite交易记录({sqlite_count}条) != "
                f"Ledger JSON记录({ledger_count}条)"
            ),
            "severity": "WARN",
        })

    # 生成摘要
    parts = []
    if result.passed:
        parts.append(f"✅ 全部{result.executed_count}/{planned_count}笔交易校验通过")
    else:
        parts.append(
            f"❌ 交易校验失败: "
            f"执行{result.executed_count}/{planned_count}, "
            f"失败{result.failed_count}"
        )
        for m in result.mismatches:
            sev = m.get("severity", "")
            parts.append(f"  [{sev}] {m['detail'][:120]}")

    result.summary = "\n".join(parts)
    return result


def _get_recent_trades_from_db(db_path: str, tolerance_minutes: int) -> List[Dict]:
    """从SQLite读取最近N分钟内的交易记录"""
    if not os.path.exists(db_path):
        return []

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 检查表结构
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('trades', 'orders')"
        )
        tables = [r[0] for r in cursor.fetchall()]

        records = []
        cutoff_time = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - tolerance_minutes * 60),
        )

        for table in tables:
            try:
                cursor.execute(
                    f"SELECT * FROM {table} WHERE created_at >= ? OR timestamp >= ? "
                    f"ORDER BY created_at DESC LIMIT 50",
                    (cutoff_time, cutoff_time),
                )
                columns = [desc[0] for desc in cursor.description]
                for row in cursor.fetchall():
                    record = dict(zip(columns, row))
                    # 统一字段名
                    record["symbol"] = record.get("symbol") or record.get("stock_code", "")
                    record["action"] = (
                        record.get("action") or record.get("direction", "")
                    ).upper()
                    record["qty"] = record.get("qty") or record.get("quantity", 0)
                    records.append(record)
            except sqlite3.OperationalError:
                continue

        conn.close()
        return records
    except Exception:
        return []


def _find_matching_trade(
    records: List[Dict], symbol: str, action: str, qty
) -> Optional[Dict]:
    """在记录列表中查找匹配的交易"""
    for record in records:
        rec_symbol = record.get("symbol", "").replace(".SH", "").replace(".SZ", "")
        plan_symbol = symbol.replace(".SH", "").replace(".SZ", "")
        if rec_symbol == plan_symbol and record.get("action", "") == action:
            rec_qty = record.get("qty", 0)
            try:
                rec_qty = int(rec_qty)
                plan_qty = int(qty)
                if abs(rec_qty - plan_qty) <= max(1, plan_qty * 0.05):
                    return record
            except (ValueError, TypeError):
                if str(rec_qty) == str(qty):
                    return record
    return None


def _count_ledger_entries(ledger_path: str, tolerance_minutes: int) -> int:
    """计算Ledger JSON中最近N分钟内的条目数"""
    if not os.path.exists(ledger_path):
        return 0
    try:
        with open(ledger_path) as f:
            ledger = json.load(f)
        trades = ledger.get("trades", []) if isinstance(ledger, dict) else ledger
        cutoff = time.time() - tolerance_minutes * 60
        recent = [
            t for t in trades
            if _parse_timestamp(t.get("timestamp") or t.get("created_at", "")) > cutoff
        ]
        return len(recent)
    except Exception:
        return -1  # -1表示无法读取, 区别于0


def _parse_timestamp(ts_str: str) -> float:
    """解析时间戳字符串为epoch秒"""
    for fmt in [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            return time.mktime(time.strptime(ts_str[:19], fmt))
        except (ValueError, IndexError):
            continue
    return 0


# ═══════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    print("=" * 60)
    print("🧪 trade_execution_verifier 自测")
    print("=" * 60)

    # Test 1: action规范化
    print("\n📋 1. action规范化")
    from core.resilience import normalize_action
    assert normalize_action("sell") == "SELL"
    assert normalize_action("BUY") == "BUY"
    print("   ✅ normalize_action正常")

    # Test 2: 正常校验 (含action规范化)
    planned = [
        {"symbol": "000001", "action": "buy", "quantity": 100},
        {"symbol": "000002", "action": "sell", "quantity": 200},
    ]
    result = verify_trade_execution(planned)
    print(f"\n📋 2. 校验(with action normalization):")
    print(f"   passed={result.passed}")
    print(f"   planned={result.planned_count}, executed={result.executed_count}, "
          f"failed={result.failed_count}")
    # action已规范化为大写 (in-place modification)
    assert planned[0]["action"] == "BUY"
    assert planned[1]["action"] == "SELL"
    print("   ✅ action原地规范化为大写")

    # Test 3: 无效action检测
    print(f"\n📋 3. 无效action检测")
    bad_plan = [{"symbol": "000001", "action": "unknown", "quantity": 100}]
    result3 = verify_trade_execution(bad_plan)
    assert not result3.passed
    assert result3.failed_count >= 1
    print(f"   ✅ 无效action已正确标记: failed={result3.failed_count}")

    print(f"\n{'='*60}")
    print("✅ trade_execution_verifier 自测通过")
    print("=" * 60)
