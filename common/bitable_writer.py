#!/usr/bin/env python3
"""
飞书多维表格5表回写模块

功能：模拟盘运行后自动将数据写入以下5个飞书表格：
1. 股票池（BOlLb99JNaHZLZsjG9Zc4yzMnZg）
2. 模拟持仓（WHhNbqVe1a4VMwsZTovcqVfFn3e）
3. 持仓追踪（VPWrbsvUHazR3yscjN6c4fLQnbg）
4. 交易记录（CaJqb6ovNaPnBgsCgyFcpkGvnXd）
5. 绩效看板（VxAjbeRGta6ZgWsxZmhcBSsLnsE）

每个表格独立方法，调用方自行决定写入时机。
"""
import os, sys, json
from datetime import datetime
from typing import List, Dict, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.feishu_bitable import FeishuBitable
from common.config import config

# ============================================================
# 表格标识
# ============================================================
BITABLE = {
    "stock_pool": {
        "app_token": "VubNbUkLFa4PKGsv7b0cJMa4nDh",
        "table_id": "tblB912Cicmxs9Nj",
        "version": "v4.5.1",
    },
    "sim_position": {
        "app_token": "Jc1PbMKUIa9ADtsUeuhcDSrMnib",
        "table_id": "tblxk3xoADOVisJA",
        "version": "v4.5.1",
    },
    "holdings": {
        "app_token": "BgyUb7cslaTeSbsp42vcb45Vncd",
        "table_id": "tblRe2JnLXGzXSSE",
        "version": "v4.5.1",
    },
    "trade_history": {
        "app_token": "Eb3pbl9RqaWumFsl6D1c3rXCnae",
        "table_id": "tbl4Nlrf8wts1e5o",
        "version": "v4.5.1",
    },
    "performance": {
        "app_token": "Aw7qbPqQQaiC6Asap2ZcKJzMnTe",
        "table_id": "tblMe2DoKbEE9yyu",
        "version": "v4.5.1",
    },
}

# 单例
_feishu_bitable = FeishuBitable()


# ============================================================
# 1. 股票池 -> 全量替换（先删后增）
# ============================================================
def sync_stock_pool(stocks: List[Dict]) -> int:
    """将股票池数据全量写入飞书表格
    stocks: [{symbol, name, market, sector, date, reason, score, tier, price, signal}]
    """
    bs = BITABLE["stock_pool"]
    app_token, table_id = bs["app_token"], bs["table_id"]

    # 清空旧数据
    old_records = _feishu_bitable.list_records(app_token, table_id)
    old_ids = [r["record_id"] for r in old_records]
    if old_ids:
        _feishu_bitable.batch_delete_records(app_token, table_id, old_ids)

    # 写新数据
    records = []
    today = int(datetime.now().timestamp() * 1000)
    for s in stocks:
        fields = {
            "股票代码": s.get("symbol", ""),
            "股票名称": s.get("name", s.get("stock_name", "")),
            "市场": s.get("market", "A股"),
            "行业": s.get("sector", ""),
            "入选日期": today,
            "入选原因": s.get("reason", ""),
            "DSL评分": s.get("score", 0),
            "股价": s.get("price", 0),
            "更新时间": today,
        }
        if s.get("tier"):
            fields["分级"] = s["tier"]
        if s.get("signal"):
            fields["最新信号"] = s["signal"]
        records.append(fields)

    if records:
        return _feishu_bitable.batch_create_records(app_token, table_id, records)
    return 0


# ============================================================
# 2. 模拟持仓 -> 全量替换
# ============================================================
def sync_sim_positions(positions: List[Dict]) -> int:
    """将模拟持仓数据全量写入飞书表格
    positions: [{symbol, name, market, buy_date, avg_cost, qty,
                 current_price, market_value, unrealized_pnl,
                 unrealized_pnl_pct, position_ratio, total_cost, reason}]
    """
    bs = BITABLE["sim_position"]
    app_token, table_id = bs["app_token"], bs["table_id"]

    # 清空旧数据
    old_records = _feishu_bitable.list_records(app_token, table_id)
    old_ids = [r["record_id"] for r in old_records]
    if old_ids:
        _feishu_bitable.batch_delete_records(app_token, table_id, old_ids)

    # 写新数据
    records = []
    today_ts = int(datetime.now().timestamp() * 1000)
    for p in positions:
        buy_date_ts = today_ts
        if p.get("buy_date"):
            try:
                dt = datetime.strptime(str(p["buy_date"])[:10], "%Y-%m-%d")
                buy_date_ts = int(dt.timestamp() * 1000)
            except (ValueError, TypeError):
                pass

        fields = {
            "股票代码": p.get("symbol", ""),
            "股票名称": p.get("name", ""),
            "市场": p.get("market", "A股"),
            "买日期": buy_date_ts,
            "买入均价": p.get("avg_cost", 0),
            "持仓数量": int(p.get("qty", 0)),
            "当前价格": p.get("current_price", 0),
            "持仓市值": p.get("market_value", 0),
            "浮动盈亏": p.get("unrealized_pnl", 0),
            "浮动盈亏百分比": p.get("unrealized_pnl_pct", 0),
            "仓位比例": p.get("position_ratio", 0),
            "成本价总额": p.get("total_cost", 0),
            "买入理由": p.get("reason", ""),
            "最后更新": today_ts,
        }
        records.append(fields)

    if records:
        return _feishu_bitable.batch_create_records(app_token, table_id, records)
    return 0


# ============================================================
# 3. 持仓追踪 -> 全量替换
# ============================================================
def sync_holdings(holdings: List[Dict]) -> int:
    """将持仓追踪数据全量写入飞书表格
    holdings: [{symbol, name, market, buy_date, avg_cost, qty,
                total_cost, current_price, market_value,
                unrealized_pnl, unrealized_pnl_pct, hold_days,
                stop_loss, target_price, trail_trigger,
                dsl_score, buy_reason, status}]
    """
    bs = BITABLE["holdings"]
    app_token, table_id = bs["app_token"], bs["table_id"]

    old_records = _feishu_bitable.list_records(app_token, table_id)
    old_ids = [r["record_id"] for r in old_records]
    if old_ids:
        _feishu_bitable.batch_delete_records(app_token, table_id, old_ids)

    records = []
    today_ts = int(datetime.now().timestamp() * 1000)
    for h in holdings:
        buy_date_ts = today_ts
        if h.get("buy_date"):
            try:
                dt = datetime.strptime(str(h["buy_date"])[:10], "%Y-%m-%d")
                buy_date_ts = int(dt.timestamp() * 1000)
            except (ValueError, TypeError):
                pass

        fields = {
            "股票代码": h.get("symbol", ""),
            "股票名称": h.get("name", ""),
            "市场": h.get("market", "A股"),
            "买入日期": buy_date_ts,
            "买入均价": h.get("avg_cost", 0),
            "持仓数量": int(h.get("qty", 0)),
            "成本总额": h.get("total_cost", 0),
            "当前价格": h.get("current_price", 0),
            "持仓市值": h.get("market_value", 0),
            "浮动盈亏": h.get("unrealized_pnl", 0),
            "盈亏百分比": h.get("unrealized_pnl_pct", 0),
            "持仓天数": int(h.get("hold_days", 0)),
            "止损价": h.get("stop_loss", 0),
            "目标价": h.get("target_price", 0),
            "移动止盈价": h.get("trail_trigger", 0),
            "DSL信号评分": h.get("dsl_score", 0),
            "买入理由": h.get("buy_reason", ""),
            "持仓状态": h.get("status", "持仓中"),
            "最后更新": today_ts,
        }
        records.append(fields)

    if records:
        return _feishu_bitable.batch_create_records(app_token, table_id, records)
    return 0


# ============================================================
# 4. 交易记录 -> 增量追加
# ============================================================
def append_trade_history(trades: List[Dict]) -> int:
    """向交易记录表追加新交易，不删除旧数据（增量）
    trades: [{date, symbol, name, market, direction, price, qty,
              amount, fee, realized_pnl, hold_days, sell_reason, dsl_signal}]
    """
    bs = BITABLE["trade_history"]
    app_token, table_id = bs["app_token"], bs["table_id"]

    records = []
    for t in trades:
        trade_date_ts = int(datetime.now().timestamp() * 1000)
        if t.get("date"):
            try:
                dt = datetime.strptime(str(t["date"])[:10], "%Y-%m-%d")
                trade_date_ts = int(dt.timestamp() * 1000)
            except (ValueError, TypeError):
                pass

        # 映射方向
        direction = t.get("direction", "")
        dir_map = {"BUY": "买入", "SELL": "卖出止盈", "buy": "买入", "sell": "卖出止盈",
                   "买入": "买入", "卖出止盈": "卖出止盈", "卖出止损": "卖出止损"}
        if direction in dir_map:
            feishu_direction = dir_map[direction]
        else:
            feishu_direction = "买入"

        # 卖出原因
        sell_reason = t.get("sell_reason", "")
        reason_map = {
            "目标价止盈": "目标价止盈",
            "止损触发": "止损触发",
            "移动止盈": "移动止盈",
            "信号反转": "DSL信号反转",
            "人工干预": "人工干预",
            "手动": "人工干预",
        }
        if sell_reason in reason_map:
            feishu_reason = reason_map[sell_reason]
        else:
            feishu_reason = sell_reason if sell_reason else "DSL信号反转"

        fields = {
            "交易日期": trade_date_ts,
            "股票代码": t.get("symbol", ""),
            "股票名称": t.get("name", ""),
            "市场": t.get("market", "A股"),
            "交易方向": feishu_direction,
            "成交价格": t.get("price", 0),
            "成交量": int(t.get("qty", 0)),
            "成交金额": t.get("amount", 0),
            "手续费": t.get("fee", 0),
            "实现盈亏": t.get("realized_pnl", 0),
            "持仓天数": int(t.get("hold_days", 0)),
            "卖出原因": feishu_reason,
            "DSL触发信号": t.get("dsl_signal", ""),
        }
        records.append(fields)

    if records:
        return _feishu_bitable.batch_create_records(app_token, table_id, records)
    return 0


# ============================================================
# 5. 绩效看板 -> 追加（每日一条）
# ============================================================
def append_performance(metrics: Dict) -> bool:
    """向绩效看板追加一条每日统计
    metrics: {date, total_asset, market_value, cash,
              total_return_pct, max_drawdown_pct, win_rate,
              profit_factor, trade_count, win_count, loss_count,
              avg_hold_days, sharpe_ratio, cumulative_pnl, unrealized_pnl}
    """
    bs = BITABLE["performance"]
    app_token, table_id = bs["app_token"], bs["table_id"]

    date_ts = int(datetime.now().timestamp() * 1000)
    if metrics.get("date"):
        try:
            dt = datetime.strptime(str(metrics["date"])[:10], "%Y-%m-%d")
            date_ts = int(dt.timestamp() * 1000)
        except (ValueError, TypeError):
            pass

    fields = {
        "日期": date_ts,
        "总资金": metrics.get("total_asset", 0),
        "持仓市值": metrics.get("market_value", 0),
        "可用现金": metrics.get("cash", 0),
        "总收益率": metrics.get("total_return_pct", 0),
        "最大回撤": metrics.get("max_drawdown_pct", 0),
        "胜率": metrics.get("win_rate", 0),
        "盈亏比": metrics.get("profit_factor", 0),
        "交易次数": int(metrics.get("trade_count", 0)),
        "盈利次数": int(metrics.get("win_count", 0)),
        "亏损次数": int(metrics.get("loss_count", 0)),
        "平均持仓天数": metrics.get("avg_hold_days", 0),
        "夏普比率": metrics.get("sharpe_ratio", 0),
        "累计盈亏": metrics.get("cumulative_pnl", 0),
        "当前浮动盈亏": metrics.get("unrealized_pnl", 0),
    }

    return _feishu_bitable.create_record(app_token, table_id, fields)


# ============================================================
# 便捷方法：全表同步（模拟盘收盘后调用）
# ============================================================
def sync_all_tables(
    stock_pool: Optional[List[Dict]] = None,
    sim_positions: Optional[List[Dict]] = None,
    holdings: Optional[List[Dict]] = None,
    trades: Optional[List[Dict]] = None,
    performance: Optional[Dict] = None,
) -> Dict[str, int]:
    """一键同步全部5个表格"""
    result = {}
    if stock_pool is not None:
        result["stock_pool"] = sync_stock_pool(stock_pool)
    if sim_positions is not None:
        result["sim_position"] = sync_sim_positions(sim_positions)
    if holdings is not None:
        result["holdings"] = sync_holdings(holdings)
    if trades is not None:
        result["trade_history"] = append_trade_history(trades)
    if performance is not None:
        result["performance"] = 1 if append_performance(performance) else 0
    return result


# ============================================================
# 测试入口
# ============================================================
if __name__ == "__main__":
    print("🔍 bitable_writer.py - 飞书5表回写模块")
    print(f"   表格配置: {json.dumps({k: v['app_token'][:8]+'...' for k,v in BITABLE.items()}, ensure_ascii=False)}")
    print("   直接运行仅验证配置，不执行写入。请从业务流程脚本导入使用。")
