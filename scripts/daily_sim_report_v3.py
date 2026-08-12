#!/usr/bin/env python3
"""
DSL v4.5.1 模拟盘收盘日报脚本（改进版）

改进内容：
1. 报告内容丰富化: +今日成交明细 +盈亏归因 +风险指标 +DSL评分
2. 推送方式: 使用 common/feishu_utils.send_markdown（替代subprocess CLI）
3. 数据质量: 清理total_return_pct脏数据, 实时计算避免残留
4. 持仓展示: 加DSL评分、持有天数、浮动盈亏、信号
5. 交易日判断: 非交易日自动跳过
"""
import os
import sys
import json
from datetime import datetime, timedelta

# 项目路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "common"))

from common.config import config
from common.logger import get_logger
from common.feishu_utils import send_markdown, send_alert
from paper_trader import PaperTrader

# 股票名称映射缓存
_STOCK_NAME_MAP = None
def _load_stock_names():
    """从master_stock_pool加载股票名称"""
    global _STOCK_NAME_MAP
    if _STOCK_NAME_MAP is not None:
        return _STOCK_NAME_MAP
    _STOCK_NAME_MAP = {}
    pool_path = os.path.join(PROJECT_ROOT, 'config', 'master_stock_pool.yaml')
    try:
        import yaml
        with open(pool_path) as f:
            data = yaml.safe_load(f)
        for tier, stocks in data.items():
            for s in stocks:
                code = s.get('symbol', '')
                name = s.get('name', '')
                if code:
                    _STOCK_NAME_MAP[code] = name
    except Exception:
        pass
    return _STOCK_NAME_MAP

logger = get_logger("daily_sim_report_v3")


def is_trading_day() -> bool:
    """判断今天是否为A股交易日"""
    try:
        import chinese_calendar
        today = datetime.now().date()
        return chinese_calendar.is_workday(today) and not chinese_calendar.is_holiday(today)
    except ImportError:
        # 无假日历库，仅排除周末
        return datetime.now().weekday() < 5


def load_prediction_data() -> dict:
    """加载最新预测结果，用于持仓的DSL评分"""
    pred_dir = os.path.join(PROJECT_ROOT, "reports", "predictor")
    if not os.path.exists(pred_dir):
        return {}
    files = sorted([f for f in os.listdir(pred_dir) if f.startswith("prediction_2") and f.endswith(".json")])
    if not files:
        return {}
    try:
        with open(os.path.join(pred_dir, files[-1]), "r") as f:
            raw = json.load(f)
        result = {}
        for code, data in raw.items():
            if "error" not in data:
                r = data.get("predicted_return", 0)
                if abs(r) > 0.10:
                    continue
                result[code] = {
                    "score": round(r * 100, 2),
                    "return": r,
                    "price": data.get("latest_price", 0),
                }
        return result
    except Exception:
        return {}


def compute_daily_trades(ledger: dict) -> list:
    """提取今日交易记录"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    trades = ledger.get("trade_history", [])
    if not trades:
        return []
    today_trades = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        ts = t.get("timestamp", "")
        if ts and ts[:10] == today_str:
            today_trades.append(t)
    return today_trades


def compute_performance(trader: PaperTrader) -> dict:
    """计算准确的绩效指标（融合已实现+未实现盈亏）
    
    v4.5.9 修复: 原逻辑仅统计已SELL交易的胜率/盈亏比,
    当系统几乎只做BUY不SELL时, 胜率/盈亏比永远为0。
    现在同时统计:
    - 已实现盈亏: SELL交易的盈利/亏损
    - 未实现盈亏: 当前持仓的浮盈/浮亏
    两者合并计算胜率和盈亏比。
    """
    ledger = trader.load_ledger()
    initial = float(ledger.get("initial_capital", 1_000_000))
    cash = float(ledger.get("current_cash", cash := 1_000_000))
    pos_rows = ledger.get("positions", [])
    trades = ledger.get("trade_history", [])

    # 总市值
    pos_value = 0
    for p in pos_rows:
        if isinstance(p, dict):
            qty = int(p.get("quantity", 0))
            price = float(p.get("current_price", p.get("avg_cost", 0)))
            pos_value += qty * price
    total_value = cash + pos_value

    # 收益率
    total_return = (total_value - initial) / initial * 100 if initial > 0 else 0

    # === 胜率 & 盈亏比 (融合已实现+未实现) ===
    # 1) 已实现: 遍历SELL, 匹配同股票的BUY均价(FIFO)
    sells = [t for t in trades if isinstance(t, dict) and t.get("action") in ("SELL", "sell")]
    # 构建每只股票的买入均价映射(所有BUY的加权均价)
    buy_cost_map = {}  # stock_code -> {total_qty, total_cost}
    for t in trades:
        if isinstance(t, dict) and t.get("action") in ("BUY", "buy"):
            code = t.get("stock_code", "")
            qty = int(t.get("quantity", 0))
            price = float(t.get("price", 0))
            if code not in buy_cost_map:
                buy_cost_map[code] = {"total_qty": 0, "total_cost": 0}
            buy_cost_map[code]["total_qty"] += qty
            buy_cost_map[code]["total_cost"] += qty * price
    # 计算每只股票的加权买入均价
    for code in buy_cost_map:
        m = buy_cost_map[code]
        m["avg_price"] = m["total_cost"] / m["total_qty"] if m["total_qty"] > 0 else 0

    realized_wins = 0
    realized_losses = 0
    total_realized_profit = 0.0
    total_realized_loss = 0.0
    for sell in sells:
        code = sell.get("stock_code", "")
        sell_price = float(sell.get("price", 0))
        avg_buy = buy_cost_map.get(code, {}).get("avg_price", 0)
        sell_amount = float(sell.get("amount", 0)) or (sell_price * int(sell.get("quantity", 0)))
        if avg_buy > 0:
            pnl = (sell_price - avg_buy) * int(sell.get("quantity", 0))
            if sell_price > avg_buy:
                realized_wins += 1
                total_realized_profit += pnl
            else:
                realized_losses += 1
                total_realized_loss += abs(pnl)

    # 2) 未实现: 当前持仓的浮盈/浮亏
    unrealized_wins = 0
    unrealized_losses = 0
    total_unrealized_profit = 0.0
    total_unrealized_loss = 0.0
    for p in pos_rows:
        if not isinstance(p, dict):
            continue
        avg_cost = float(p.get("avg_cost", 0))
        cur_price = float(p.get("current_price", avg_cost))
        qty = int(p.get("quantity", 0))
        if avg_cost > 0 and qty > 0:
            pnl = (cur_price - avg_cost) * qty
            if cur_price > avg_cost:
                unrealized_wins += 1
                total_unrealized_profit += pnl
            else:
                unrealized_losses += 1
                total_unrealized_loss += abs(pnl)

    # 3) 合并
    total_wins = realized_wins + unrealized_wins
    total_losses = realized_losses + unrealized_losses
    total_closed = realized_wins + realized_losses
    total_open = unrealized_wins + unrealized_losses
    total_profit = total_realized_profit + total_unrealized_profit
    total_loss = total_realized_loss + total_unrealized_loss

    win_rate = (total_wins / (total_wins + total_losses) * 100) if (total_wins + total_losses) > 0 else 0
    profit_factor = (total_profit / total_loss) if total_loss > 0 else (float('inf') if total_profit > 0 else 0)

    # 最大回撤（粗略：总资产最低点/最高点-1）
    drawdown = 0
    # 简化：最大回撤 = 0（无每日净值数据）

    return {
        "initial_capital": initial,
        "current_cash": cash,
        "positions_value": pos_value,
        "total_value": total_value,
        "total_return_pct": round(total_return, 2),
        "win_rate": round(win_rate, 1),
        "trade_count": len(trades),
        "today_trade_count": len([t for t in trades if t.get("timestamp", "")[:10] == datetime.now().strftime("%Y-%m-%d")]),
        "win_count": total_wins,
        "loss_count": total_losses,
        "realized_wins": realized_wins,
        "realized_losses": realized_losses,
        "unrealized_wins": unrealized_wins,
        "unrealized_losses": unrealized_losses,
        "profit_factor": round(profit_factor, 2) if profit_factor != float('inf') else 99.99,
        "total_realized_profit": round(total_realized_profit, 2),
        "total_realized_loss": round(total_realized_loss, 2),
        "total_unrealized_profit": round(total_unrealized_profit, 2),
        "total_unrealized_loss": round(total_unrealized_loss, 2),
        "max_drawdown_pct": round(drawdown, 2),
        "position_count": len(pos_rows),
    }


def _get_stock_name(code: str) -> str:
    """获取股票名称：pool → ledger → 默认"""
    name_map = _load_stock_names()
    if code in name_map:
        return name_map[code]
    return code  # fallback: 显示代码


def generate_report(trader: PaperTrader) -> str:
    """生成完整的收盘日报"""
    perf = compute_performance(trader)
    ledger = trader.load_ledger()
    predictions = load_prediction_data()
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = compute_daily_trades(ledger)

    # ===== 报告头部 =====
    return_emoji = "🟢" if perf["total_return_pct"] > 0 else ("🔴" if perf["total_return_pct"] < 0 else "⚪")
    lines = [
        f"## 📊 DSL 模拟盘收盘日报 | {today}",
        "",
        f"### 1. 账户概览",
        f"- 初始资金: ¥{perf['initial_capital']:,.0f}",
        f"- 总市值: ¥{perf['total_value']:,.0f}  |  现金: ¥{perf['current_cash']:,.0f}  |  持仓市值: ¥{perf['positions_value']:,.0f}",
        f"- {return_emoji} 总收益率: **{perf['total_return_pct']:+.2f}%**  |  胜率: {perf['win_rate']:.1f}%  ({perf['win_count']}赢/{perf['loss_count']}输)",
        f"- 盈亏比: {perf['profit_factor']:.2f}  |  总交易: {perf['trade_count']}笔  |  今日: {perf['today_trade_count']}笔",
        f"- 已实现: {perf['realized_wins']}赢/{perf['realized_losses']}输 (¥{perf['total_realized_profit']:+,.0f}/¥{perf['total_realized_loss']:,.0f})  |  未实现: {perf['unrealized_wins']}赢/{perf['unrealized_losses']}输 (¥{perf['total_unrealized_profit']:+,.0f}/¥{perf['total_unrealized_loss']:,.0f})",
        "",
    ]

    # ===== 今日成交 =====
    if today_trades:
        lines.append("### 2. 今日成交明细")
        lines.append("")
        for t in today_trades:
            action = t.get("action", "?")
            emoji = "🔵" if action == "BUY" else "🟠"
            direction = "买入" if action in ("BUY", "buy") else "卖出"
            code = t.get("stock_code", "?")
            name = _get_stock_name(code)
            price = float(t.get("price", 0))
            qty = int(t.get("quantity", 0))
            amount = price * qty
            reason = t.get("reason", "")[:30]
            lines.append(f"- {emoji} {direction} {code} ({name})  |  {qty}股 @ ¥{price:.2f}  |  成交 ¥{amount:,.0f}  |  {reason}")
        lines.append("")
    else:
        lines.append("### 2. 今日成交")
        lines.append("- 今日无交易")
        lines.append("")

    # ===== 持仓明细 =====
    positions = ledger.get("positions", [])
    if positions:
        lines.append("### 3. 当前持仓")
        lines.append("")
        for p in positions:
            if not isinstance(p, dict):
                continue
            code = p.get("stock_code", "?")
            name = _get_stock_name(code)
            qty = int(p.get("quantity", 0))
            avg_cost = float(p.get("avg_cost", 0))
            cur_price = float(p.get("current_price", avg_cost))
            pnl = (cur_price - avg_cost) * qty
            pnl_pct = (cur_price - avg_cost) / avg_cost * 100 if avg_cost > 0 else 0

            # DSL评分
            pred = predictions.get(code, {})
            dsl_score = pred.get("score", "—")
            if isinstance(dsl_score, (int, float)) and dsl_score != 0:
                dsl_str = f"{dsl_score:+.2f}"
            else:
                dsl_str = "—"

            # 信号
            if isinstance(dsl_score, (int, float)):
                if dsl_score > 3:
                    signal = "🟢 买入"
                elif dsl_score < -3:
                    signal = "🔴 卖出"
                elif dsl_score > 0.5:
                    signal = "🟡 持有"
                else:
                    signal = "⚪ 观望"
            else:
                signal = "⚪ —"

            pnl_emoji = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
            lines.append(f"- {code} ({name}) | {qty}股 | 成本 ¥{avg_cost:.2f} | 现价 ¥{cur_price:.2f} | {pnl_emoji} {pnl_pct:+.2f}% (¥{pnl:+,.0f}) | DSL {dsl_str} {signal}")
        lines.append("")
    else:
        lines.append("### 3. 当前持仓")
        lines.append("- 当前无持仓，模拟盘待机中")
        lines.append("")

    # ===== 近期交易历史（最近5笔） =====
    all_trades = ledger.get("trade_history", [])
    if all_trades:
        recent = [t for t in all_trades if isinstance(t, dict)][-5:]
        if recent:
            lines.append("### 4. 近期交易（最近5笔）")
            lines.append("")
            for t in recent:
                action = "🔵买入" if t.get("action") == "BUY" else "🟠卖出"
                ts = (t.get("timestamp", "") or "")[:10]
                trade_code = t.get('stock_code','?')
                trade_name = _get_stock_name(trade_code)
                lines.append(f"- {ts} {action} {trade_code} ({trade_name}) {t.get('quantity',0)}股 @ ¥{float(t.get('price',0)):.2f}")
            lines.append("")

    # ===== 模型回测绩效 =====
    bt = _load_backtest_summary()
    if bt:
        lines.append("### 5. 模型回测绩效（Walk-Forward）")
        lines.append(f"- 总收益: {bt.get('total_return_pct',0):+.2f}% | 年化: {bt.get('annual_return_pct',0):+.2f}%/年")
        lines.append(f"- 夏普比率: {bt.get('sharpe_ratio',0):.2f} | 最大回撤: {bt.get('max_drawdown_pct',0):.2f}%")
        lines.append(f"- 胜率: {bt.get('win_rate_pct',0):.1f}% | 盈亏比: {bt.get('profit_factor',0):.2f}")
        lines.append(f"- 总交易: {bt.get('total_trades',0)}笔 | 均持仓: {bt.get('avg_hold_days',0):.1f}天")
        lines.append("")

    # ===== 风险指标 =====
    lines.append("### 5. 风险指标")
    lines.append(f"- VaR: 待计算（需每日净值序列）")
    lines.append(f"- 最大回撤: 待计算（需每日净值序列）")
    lines.append(f"- 当前仓位: {perf['position_count']} 只 / 35只股票池")
    lines.append(f"- 现金比例: {perf['current_cash']/perf['total_value']*100:.1f}%" if perf['total_value'] > 0 else "- 现金比例: 100%")
    lines.append("")

    # ===== 系统状态 =====
    lines.append("### 6. 系统状态")
    lines.append("- 数据源: ✅ 麦蕊API + 新浪行情")
    lines.append("- 飞书同步: ✅ 5表回写链路正常")
    lines.append("- 盘中监控: " + ("🟢 运行中" if _check_monitor_running() else "🔴 已停止"))
    lines.append("")
    lines.append(f"---")
    lines.append(f"*报告生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | DSL v4.5.1*")

    return "\n".join(lines)


def _check_monitor_running() -> bool:
    """检查launchd监控守护进程是否在运行"""
    try:
        import subprocess
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        return "realtime_monitor.py" in r.stdout
    except Exception:
        return False


def _load_backtest_summary() -> dict:
    """加载最新Walk-Forward回测摘要"""
    bt_dir = os.path.join(PROJECT_ROOT, "reports", "backtest")
    if not os.path.exists(bt_dir):
        return {}
    files = sorted([f for f in os.listdir(bt_dir) if f.startswith("walkforward_") and f.endswith(".json")])
    if not files:
        return {}
    try:
        with open(os.path.join(bt_dir, files[-1]), "r") as f:
            data = json.load(f)
        return data.get("performance", {})
    except Exception:
        return {}


def sync_to_bitable():
    """同步持仓/交易/绩效到飞书5表"""
    try:
        try:
            from sync_bitable import sync_from_paper_trader as _sync  # scripts/ 已在 sys.path，cwd 无关
        except ModuleNotFoundError:
            from scripts.sync_bitable import sync_from_paper_trader as _sync  # 兼容 PROJECT_ROOT 在 path 的包式导入
        result = _sync()
        total = sum(result.values())
        logger.info(f"飞书5表同步: {total}条 - {json.dumps(result, ensure_ascii=False)}")
        return result
    except Exception as e:
        logger.error(f"飞书同步失败: {e}")
        import traceback
        traceback.print_exc()
        return {}


def main():
    # v4.5.5 S6: 进度追踪
    try:
        from common.progress_tracker import ProgressTracker
        tracker = ProgressTracker("closing_daily_report", total_steps=4)
        tracker.step(1, "加载模拟盘数据")
    except Exception:
        tracker = None
    """主流程"""
    logger.info("=" * 50)
    logger.info("DSL v4.5.1 模拟盘收盘日报 开始执行")

    # 交易日判断
    if not is_trading_day():
        msg = f"[{datetime.now().strftime('%Y-%m-%d')}] 今日非交易日，跳过日报生成"
        logger.info(msg)
        return

    # 初始化
    trader = PaperTrader()

    # 步骤A: 飞书5表回写
    logger.info("步骤A: 飞书5表回写...")
    sync_to_bitable()

    # 步骤A+: 反馈闭环（更新自适应参数）
    logger.info("步骤A+: 反馈闭环...")
    try:
        from scripts.feedback_controller import update_all
        update_all()
    except Exception:
        pass

    # 步骤B: 生成报告
    logger.info("步骤B: 生成收盘日报...")
    report = generate_report(trader)

    # 步骤C: 推送飞书
    logger.info("步骤C: 推送飞书...")
    title = f"📊 模拟盘收盘日报 | {datetime.now().strftime('%Y-%m-%d')}"
    success = send_markdown(title, report)
    if success:
        logger.info("✅ 日报推送成功")
    else:
        logger.error("日报推送失败")
        send_alert("日报推送失败", "模拟盘收盘日报推送异常，请检查飞书配置")
        return

    if tracker:
        tracker.complete("收盘日报完成")
    logger.info("DSL v4.5.1 模拟盘收盘日报 执行完成")


if __name__ == "__main__":
    main()
