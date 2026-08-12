#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股持仓数量合规修正脚本

修正现有持仓到合规的最近交易单位，同时调整现金余额和均价。
用于 paper_trader.py 新增 _validate_lot_size 后的存量数据清洗。

规则:
- 科创板(688xxx/689xxx): ≥200股，超200后可以1股递增
- 主板/创业板: 100股整数倍
- 取整策略: 四舍五入到最近合规值

注意: 请在交易时段外执行，修正前自动备份数据库。
"""

import sqlite3
import shutil
import os
from datetime import datetime

DB_PATH = os.path.expanduser(
    "~/.openclaw/workspace/dsl-quant-trading-hybrid/data/paper_trading.db"
)


def get_lot_adjustment(stock_code: str, quantity: int) -> tuple:
    """
    计算数量调整方案。
    返回 (new_qty, delta_qty, description)
    delta_qty > 0 = 需要补买, < 0 = 需要卖出
    """
    code_prefix = stock_code[:3] if len(stock_code) >= 3 else ""
    is_kcb = code_prefix in ("688", "689")

    if is_kcb:
        # 科创板: 0 or ≥200
        if quantity >= 200:
            return quantity, 0, "合规"
        # 取最近值: 0 或 200
        new_qty = 200
        delta = new_qty - quantity
        return new_qty, delta, f"科创板最少200股, {quantity}→{new_qty} (+{delta}股)"
    else:
        # 主板/创业板: 100股整数倍, ≥100
        if quantity % 100 == 0:
            return quantity, 0, "合规"
        rounded = round(quantity / 100) * 100
        if rounded < 100:
            # 比如 40股→100股 (不足100取最近合规值=100)
            rounded = 100
        if rounded == 0:
            # 极小值, 全部卖出
            rounded = 0
        delta = rounded - quantity
        return rounded, delta, f"100股整数倍, {quantity}→{rounded} ({'+' if delta > 0 else ''}{delta}股)"


def main():
    # 1. 备份数据库
    bak_path = DB_PATH.replace(".db", f".bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, bak_path)
        print(f"✅ 已备份: {bak_path}")
    else:
        print(f"❌ 数据库不存在: {DB_PATH}")
        return

    # 2. 连接并读取当前持仓
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM positions").fetchall()

    if not rows:
        print("✅ 无持仓数据，无需修正")
        conn.close()
        return

    print(f"\n📋 共 {len(rows)} 个持仓需要检查\n")

    adjustments = []
    for row in rows:
        code = row["stock_code"]
        qty = row["quantity"]
        new_qty, delta, desc = get_lot_adjustment(code, qty)
        adjustments.append({
            "id": row["id"],
            "code": code,
            "market": row["market"],
            "old_qty": qty,
            "new_qty": new_qty,
            "delta": delta,
            "avg_cost": row["avg_cost"],
            "current_price": row["current_price"],
            "desc": desc,
        })
        if delta == 0:
            print(f"  ✅ {code:>6s}: {qty:>5d}股 -> {desc}")
        else:
            print(f"  ⚠️  {code:>6s}: {qty:>5d}股 -> {new_qty:>5d}股 ({desc})")

    # 计算总现金影响
    total_cash_impact = 0
    for a in adjustments:
        if a["delta"] == 0:
            continue
        # 修正时按当前价格买卖差额
        impact = -a["delta"] * a["current_price"]  # delta>0(买)扣现金, delta<0(卖)加现金
        total_cash_impact += impact

    print(f"\n💰 现金调整: ¥{total_cash_impact:+.2f}")
    print(f"   (正数=补买扣现金, 负数=卖出回补现金)")

    # 3. 确认继续？
    print(f"\n{'='*50}")
    print(f"即将执行修正，是否继续？ [y/N] ", end="")

    # 读取环境变量或用交互输入
    default_confirm = os.environ.get("FIX_CONFIRM", "")
    if default_confirm:
        confirm = default_confirm.lower()
    else:
        confirm = input().strip().lower()

    if confirm not in ("y", "yes"):
        print("已取消")
        conn.close()
        return

    # 4. 执行修正
    conn2 = sqlite3.connect(DB_PATH)
    cursor = conn2.cursor()

    try:
        for a in adjustments:
            if a["delta"] == 0:
                continue
            # 更新数量
            cursor.execute(
                "UPDATE positions SET quantity=? WHERE id=?",
                (a["new_qty"], a["id"])
            )
            # 如果补买，更新均价
            if a["delta"] > 0:
                total_cost = a["avg_cost"] * a["old_qty"] + a["delta"] * a["current_price"]
                new_avg = total_cost / a["new_qty"]
                cursor.execute(
                    "UPDATE positions SET avg_cost=? WHERE id=?",
                    (round(new_avg, 2), a["id"])
                )
                print(f"  🛒 {a['code']}: 补买 {a['delta']}股 @ ¥{a['current_price']}, 均价 {a['avg_cost']}→{round(new_avg,2)}")

        # 更新现金余额
        current_cash = float(cursor.execute(
            "SELECT value FROM ledger WHERE key='current_cash'"
        ).fetchone()[0])
        new_cash = current_cash + total_cash_impact  # total_cash_impact负=支出,正=收入
        cursor.execute(
            "INSERT OR REPLACE INTO ledger (key, value) VALUES ('current_cash', ?)",
            (str(round(new_cash, 2)),)
        )

        # 记录修正交易日志
        now = datetime.now().isoformat()
        for a in adjustments:
            if a["delta"] == 0:
                continue
            action = "BUY" if a["delta"] > 0 else "SELL"
            qty = abs(a["delta"])
            amount = round(qty * a["current_price"], 2)
            cursor.execute(
                "INSERT INTO trade_history (timestamp, market, stock_code, action, price, quantity, amount, reason) VALUES (?,?,?,?,?,?,?,?)",
                (now, a["market"], a["code"], action, a["current_price"], qty, amount,
                 f"数量合规修正: {a['old_qty']}→{a['new_qty']}")
            )

        # 更新绩效指标
        cursor.execute(
            "INSERT OR REPLACE INTO performance_metrics (key, value) VALUES ('last_lot_fix', ?)",
            (f'"{now}"',)
        )

        conn2.commit()
        print(f"\n✅ 修正完成")
        print(f"   💵 现金: {current_cash:.2f} → {round(new_cash,2):.2f} (变动: {total_cash_impact:+.2f})")
        print(f"   📂 备份: {bak_path}")

    except Exception as e:
        conn2.rollback()
        print(f"\n❌ 修正失败, 已回滚: {e}")
        print(f"   📂 原始数据: {bak_path} (可手动恢复)")
    finally:
        conn2.close()

    conn.close()


if __name__ == "__main__":
    main()
