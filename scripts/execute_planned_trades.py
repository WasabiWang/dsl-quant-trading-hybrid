#!/usr/bin/env python3
"""
execute_planned_trades.py — DSL v4.5.7 信号执行桥接 (改进版)

v4.5.7改进:
  1. 执行前获取实际开盘价（麦蕊API优先，akshare备用）
  2. 价格偏离阈值检测：与实际价偏离>3%则跳过该笔交易
  3. 数量自动调整：按目标金额+实际价格重算
  4. 防双重执行：检查今日是否已执行过

调用方式:
  python3 scripts/execute_planned_trades.py
    读取 cache/planned_trades.json（由 09:20 morning_decision 生成）
    获取实际开盘价后执行

依赖:
  - 大盘开盘时间: 09:30
  - planned_trades.json 应在 09:20 已就绪
  - cron: A股交易执行(09:30) → 本脚本
"""
import os, sys, json
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

TRADES_CACHE = os.path.join(PROJECT_ROOT, "cache", "planned_trades.json")
PORTFOLIO_LOG = os.path.join(PROJECT_ROOT, "data", "execution_log.jsonl")

# 价格偏离阈值
PRICE_DEVIATION_CANCEL = 0.03   # 偏离>3% → 跳过该笔
PRICE_DEVIATION_WARN = 0.015    # 偏离>1.5% → 仅告警

# 流动性风险参数 (P2-13)
MAX_POSITION_VOLUME_RATIO = 0.10  # 持仓市值 ≤ 日均成交额的10%
AVG_VOLUME_DAYS = 20              # 计算均量的窗口

# 强制代理绕过
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost'


def fetch_actual_prices(codes: list) -> dict:
    """获取股票实际开盘价（麦蕊API优先，akshare备用）

    Returns:
        {code: {"price": float, "change_pct": float}, ...}
    """
    prices = {}

    # 主数据源: 麦蕊API get_stock_real（逐只获取，闭市后返回收盘价）
    try:
        from config.mairui_api_config import get_stock_real
        from common.retry_decorator import retry_call
        import time

        for code in codes:
            try:
                data = retry_call(get_stock_real, code, max_attempts=2, backoff=1.0)
                if data and data.get("current_price", 0) > 0:
                    prices[code] = {
                        "price": float(data["current_price"]),
                        "change_pct": float(data.get("change_percent", 0) or 0),
                    }
            except Exception:
                continue
            time.sleep(0.05)  # 麦蕊限速

        if prices:
            print(f"  ✅ 麦蕊API: {len(prices)}只实时价")
            return prices
    except Exception as e:
        print(f"  ⚠️ 麦蕊API失败: {e}")

    # 降级: akshare 全市场实时行情
    try:
        import akshare as ak
        import pandas as pd
        df = ak.stock_zh_a_spot()  # v4.6.9i(审计F1-3): 东财→新浪(东财push2实测封锁, 新浪实测可用)
        if df is not None and len(df) > 0:
            df["code"] = df["代码"].str.replace(r"^(sh|sz|bj)", "", regex=True)
            for code in codes:
                row = df[df["code"] == code]
                if len(row) > 0:
                    r = row.iloc[0]
                    prices[code] = {
                        "price": float(r["最新价"]),
                        "change_pct": float(r.get("涨跌幅", 0) or 0),
                    }
            print(f"  ✅ akshare: {len(prices)}只实时价")
            return prices
    except Exception as e:
        print(f"  ⚠️ akshare行情失败: {e}")

    print(f"  ⚠️ 所有行情源失败，使用计划价")
    return {}


def _check_idempotent(today: str) -> bool:
    """检查今日是否已执行过（防双重执行）

    Args:
        today: YYYYMMDD

    Returns:
        True=今日已执行过（跳过）
    """
    if not os.path.exists(PORTFOLIO_LOG):
        return False
    try:
        with open(PORTFOLIO_LOG, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp", "")
                    if ts.startswith(today) and entry.get("source") == "execute_planned_trades":
                        print(f"  ⏭️ 今日{today}已执行过，跳过(防双重执行)")
                        return True
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return False


def _get_avg_daily_volume(code: str, days: int = AVG_VOLUME_DAYS) -> float:
    """获取股票日均成交量(股数), 用于流动性风险检查

    v4.5.12 P2-13: 使用akshare获取近days个交易日的历史成交量计算均值
    Returns:
        日均成交量(股数), 获取失败返回0(保守跳过检查)
    """
    try:
        import akshare as ak
        import pandas as pd
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now().replace(year=datetime.now().year - 1)).strftime("%Y%m%d")
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq")
        if df is None or df.empty:
            return 0.0
        # 取最近days行(排除今日可能的不完整数据)
        recent = df.tail(days + 5).head(days)  # 多取5行跳过可能的不完整日
        vol_col = [c for c in recent.columns if '成交' in c or 'volume' in c.lower() or 'vol' in c.lower()]
        if vol_col:
            return float(recent[vol_col[0]].mean())
        return 0.0
    except Exception as e:
        print(f"  ⚠️ 获取{code}均量失败: {e}")
        return 0.0


def _liquidity_fields(avg_volume: float = 0.0, volume_ratio_pct: float = 0.0,
                      filled_quantity: int = 0, source: str = "not_checked") -> dict:
    return {
        "avg_volume": float(avg_volume or 0.0),
        "volume_ratio_pct": float(volume_ratio_pct or 0.0),
        "filled_quantity": int(filled_quantity or 0),
        "liquidity_source": source,
    }


def execute_trades(trades: list, market: str = "A", dry_run: bool = False) -> list:
    """执行计划交易列表

    v4.5.7: 执行前风控检查(止损+集中度+组合风控)
    v4.5.7: 执行前获取实际开盘价，偏离>3%跳过

    Args:
        trades: [{"code": "...", "action": "BUY", "price": 50.0, "quantity": 100, "reason": "ML"}]
        market: A/HK
        dry_run: 仅打印不执行

    Returns:
        [{"code": str, "action": str, "success": bool, "trade_id": int, "error": str, "adjusted": bool}, ...]
    """
    # v4.5.12 P1-8: 午休检查 (11:30-13:00 禁止交易)
    from config.constants import is_lunch_break
    if is_lunch_break():
        logger.warning("⏸️ 当前为A股午休时段(11:30-13:00)，跳过交易执行")
        return [{"code": t.get("code", ""), "action": t.get("action", ""),
                 "success": False, "error": "午休时段禁止交易"} for t in trades]

    from paper_trader import PaperTrader
    from execution_engine.reconciliation import get_reconciliation_engine

    trader = PaperTrader()
    recon = get_reconciliation_engine()

    # ──────────── v4.5.7: 风控前置检查 ────────────
    from core.risk_manager import RiskManager
    risk = RiskManager()
    # 加载股票池映射
    import yaml as _yaml
    _pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    with open(_pool_path) as _f:
        _stock_pool = _yaml.safe_load(_f).get("master_pool", [])
    # 获取当前持仓和资产
    positions = trader.list_positions() if hasattr(trader, 'list_positions') else []
    init_cap = trader.get_initial_capital() if hasattr(trader, 'get_initial_capital') else 1000000
    equity = trader.get_total_equity() if hasattr(trader, 'get_total_equity') else init_cap
    
    risk_result = risk.pre_execution_check(trades, positions, init_cap, equity, _stock_pool)
    if not risk_result.get("approved", True):
        logger.critical(f"🔴 风控拒绝: {risk_result.get('portfolio_status', '不明')}")
        for b in risk_result.get("blocked_trades", []):
            logger.critical(f"  🚫 {b['code']} {b.get('name','')}: {b.get('reason','')}")
        # 过滤被阻止的交易
        blocked_codes = {b["code"] for b in risk_result.get("blocked_trades", [])}
        trades = [t for t in trades if t.get("code", "") not in blocked_codes]
        logger.info(f"  剩余有效交易: {len(trades)}笔")
    # 止损警告
    for sl in risk_result.get("stop_losses", []):
        logger.warning(f"  ⚠️ 止损: {sl.get('reason','')}")

    # v4.5.12 P1-7: 止盈触发 → 自动生成卖出计划
    take_profits = risk_result.get("take_profits", [])
    if take_profits:
        logger.info(f"🟢 止盈触发 {len(take_profits)}只 → 生成卖出计划")
        # 获取当前持仓详细信息
        tp_positions = {p.get("stock_code", p.get("symbol", "")): p for p in positions}
        for tp in take_profits:
            sym = tp.get("symbol", "")
            if not sym:
                # 从reason中提取code
                for pcode, pinfo in tp_positions.items():
                    if pcode in str(tp):
                        sym = pcode
                        break
            if not sym:
                continue
            pos_info = tp_positions.get(sym, {})
            qty = pos_info.get("quantity", pos_info.get("shares", 0))
            cur_price = tp.get("current_price", pos_info.get("current_price", 0))
            if qty >= 100 and cur_price > 0:
                new_trade = {
                    "code": sym,
                    "name": pos_info.get("name", sym),
                    "action": "SELL",
                    "price": cur_price,
                    "quantity": qty,
                    "reason": f"止盈自动卖出: {tp.get('reason', '')}",
                }
                trades.append(new_trade)
                logger.info(f"  ➕ 止盈卖出 {sym} {qty}股 @{cur_price:.2f} [{tp.get('reason','')}]")

    results = []
    executed = 0
    skipped = 0

    # ──────────── v4.5.7: 获取实际开盘价 ────────────
    all_codes = list(set(t["code"] for t in trades if "code" in t))
    actual_prices = fetch_actual_prices(all_codes) if not dry_run else {}

    # 统计偏离情况
    deviations = []
    for t in trades:
        code = t.get("code", "")
        planned_price = t.get("price", 0)
        actual_price_info = actual_prices.get(code, {})
        actual_price = actual_price_info.get("price", 0)

        if planned_price > 0 and actual_price > 0:
            deviation = abs(actual_price - planned_price) / planned_price
            deviations.append((code, deviation, planned_price, actual_price))
            if deviation > PRICE_DEVIATION_CANCEL:
                t["_skip"] = True
                t["_skip_reason"] = f"价差{deviation:.1%}>(>{PRICE_DEVIATION_CANCEL:.0%})"
            elif deviation > PRICE_DEVIATION_WARN:
                print(f"  ⚠️ {code}: 计划价{planned_price:.2f}→实际{actual_price:.2f} (偏离{deviation:.1%})")
                t["_price"] = actual_price  # 使用实际价
            else:
                t["_price"] = actual_price  # 使用实际价
        else:
            t["_price"] = planned_price or actual_price or 0
            t["_skip"] = False

    cancelled = [d for d in deviations if d[1] > PRICE_DEVIATION_CANCEL]
    if cancelled:
        print(f"  🚫 {len(cancelled)}笔因价差>{PRICE_DEVIATION_CANCEL:.0%}跳过:")
        for code, dev, planned, actual in cancelled:
            print(f"    {code}: 计划{planned:.2f}→实际{actual:.2f} (偏离{dev:.1%})")

    # ──────────── 获取组合状态 ────────────
    summary = trader.get_portfolio_summary()
    current_positions = {p["stock_code"]: p for p in summary.get("positions", [])}

    try:
        import yaml
        config_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        with open(config_path) as f:
            params = yaml.safe_load(f)
        # v4.7.6 修复#4: 默认值由 5 改为 3, 与 config:trading.max_positions 对齐(缺失/非法时 fail-closed)
        max_positions = params.get("trading", {}).get("max_positions", 3)
    except Exception:
        max_positions = 3

    # 1️⃣  先执行卖出
    sell_trades = [t for t in trades if t.get("action", "").upper() == "SELL"]
    buy_trades = [t for t in trades if t.get("action", "").upper() == "BUY"]

    for t in sell_trades:
        code = t["code"]
        price = t.get("_price", t.get("price", 0))
        quantity = t.get("quantity", 0)
        reason = t.get("reason", "ML信号")

        if dry_run:
            print(f"  [DRY-RUN] SELL {code} {quantity}股 @{price:.2f} [{reason}]")
            results.append({
                "code": code, "action": "SELL", "success": True, "dry_run": True,
                **_liquidity_fields(filled_quantity=quantity, source="not_checked_dry_run"),
            })
            continue

        if code not in current_positions:
            print(f"  ⏭️ SELL {code}: 无持仓，跳过")
            skipped += 1
            continue

        order_id = recon.record_signal(
            symbol=code, action="SELL", quantity=quantity,
            price=price, confidence=0.6, source="ml_signal", reasoning=reason
        )

        result = trader.execute_trade(
            market=market, stock_code=code, action="SELL",
            price=price, quantity=quantity,
            reason=reason, order_id=order_id,
            business_date=datetime.now().strftime("%Y%m%d")
        )

        if result.get("success"):
            executed += 1
            print(f"  ✅ SELL {code} {quantity}股 @{price:.2f} → trade_id={result['trade']['trade_id']}")
        else:
            print(f"  ❌ SELL {code}: {result.get('error', '未知')}")

        results.append({
            "code": code, "action": "SELL", "success": result.get("success", False),
            "error": result.get("error", ""), "order_id": order_id,
            "planned_price": t.get("price", 0), "actual_price": price,
            **_liquidity_fields(
                avg_volume=result.get("trade", {}).get("avg_volume", 0.0),
                volume_ratio_pct=result.get("trade", {}).get("volume_ratio_pct", 0.0),
                filled_quantity=result.get("trade", {}).get("filled_quantity", quantity),
                source=result.get("trade", {}).get("liquidity_source", "paper_trader"),
            ),
        })

    # 2️⃣  再执行买入（含价格偏离跳过）
    current_position_count = len([p for p in trader.get_portfolio_summary().get("positions", [])
                                   if p.get("market", "A") == market])

    for t in buy_trades:
        code = t["code"]
        planned_price = t.get("price", 0)
        price = t.get("_price", planned_price)
        quantity = t.get("quantity", 0)
        reason = t.get("reason", "ML信号")
        skip = t.get("_skip", False)

        if skip:
            reason_skip = t.get("_skip_reason", "价格偏离")
            print(f"  🚫 BUY {code}: {reason_skip}, 跳过")
            skipped += 1
            results.append({"code": code, "action": "BUY", "success": False,
                           "error": reason_skip, "planned_price": planned_price, "actual_price": price,
                           **_liquidity_fields(filled_quantity=0, source="price_deviation_skip")})
            continue

        if current_position_count >= max_positions:
            print(f"  ⏭️ BUY {code}: 已达最大持仓({max_positions})")
            skipped += 1
            continue

        if dry_run:
            print(f"  [DRY-RUN] BUY {code} {quantity}股 @{price:.2f} [{reason}]")
            results.append({
                "code": code, "action": "BUY", "success": True, "dry_run": True,
                **_liquidity_fields(filled_quantity=quantity, source="not_checked_dry_run"),
            })
            continue

        # v4.5.7: 按实际价格调整数量（保持目标金额不变）
        target_amount = planned_price * quantity
        adjusted_quantity = int(target_amount / price / 100) * 100 if price > 0 else quantity
        adjusted_quantity = max(adjusted_quantity, 100)  # 最少1手
        _avg_vol = 0.0
        _volume_ratio_pct = 0.0
        _liquidity_source = "unavailable"

        # ── v4.5.12 P2-13: 流动性风险检查: 持仓市值 ≤ 日均成交额10% ──
        _avg_vol = _get_avg_daily_volume(code, AVG_VOLUME_DAYS)
        if _avg_vol > 0:
            _liquidity_source = f"akshare_hist_{AVG_VOLUME_DAYS}d"
            # 计算当前持仓股数
            _held = 0
            if isinstance(positions, list):
                for _p in positions:
                    _pc = _p.get("stock_code", _p.get("code", ""))
                    if _pc == code:
                        _held = int(_p.get("quantity", _p.get("shares", 0)))
                        break
            if _held == 0:
                # 直接查DB兜底
                try:
                    import sqlite3 as _sq
                    _db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
                    if os.path.exists(_db_path):
                        _conn = _sq.connect(_db_path)
                        _row = _conn.execute("SELECT quantity FROM positions WHERE stock_code=?", (code,)).fetchone()
                        if _row:
                            _held = int(_row[0])
                        _conn.close()
                except Exception:
                    pass
            _new_held = _held + adjusted_quantity
            _volume_ratio_pct = (_new_held / _avg_vol) * 100 if _avg_vol > 0 else 0.0
            _pos_value = _new_held * price
            _max_vol_value = _avg_vol * price * MAX_POSITION_VOLUME_RATIO
            if _pos_value > _max_vol_value:
                _max_qty_by_vol = int(_avg_vol * MAX_POSITION_VOLUME_RATIO) - _held
                _min_lot = 200 if code.startswith("688") else 100
                _max_qty_by_vol = max(0, (_max_qty_by_vol // _min_lot) * _min_lot)
                if _max_qty_by_vol < _min_lot:
                    print(f"  ⏭️ {code}: 流动性限制跳过(持仓{_held}股+新{adjusted_quantity}股={_new_held}股 "
                          f"市值{_pos_value:.0f} > 日均成交额10%={_max_vol_value:.0f}, 且无法缩量)")
                    skipped += 1
                    results.append({"code": code, "action": "BUY", "success": False,
                                   "error": "流动性不足(持仓超日均成交额10%)", "planned_price": planned_price,
                                   "actual_price": price, "planned_qty": quantity, "adjusted_qty": adjusted_quantity,
                                   **_liquidity_fields(_avg_vol, _volume_ratio_pct, 0, _liquidity_source)})
                    continue
                else:
                    _old_qty = adjusted_quantity
                    adjusted_quantity = _max_qty_by_vol
                    _new_held = _held + adjusted_quantity
                    _volume_ratio_pct = (_new_held / _avg_vol) * 100 if _avg_vol > 0 else 0.0
                    print(f"  🫗 {code}: 流动性缩量 {_old_qty}→{adjusted_quantity}股 "
                          f"(持仓{_held}+新{adjusted_quantity}={_new_held}股 市值≤日均成交额10%)")

        order_id = recon.record_signal(
            symbol=code, action="BUY", quantity=adjusted_quantity,
            price=price, confidence=0.6, source="ml_signal", reasoning=reason
        )

        result = trader.execute_trade(
            market=market, stock_code=code, action="BUY",
            price=price, quantity=adjusted_quantity,
            reason=reason, order_id=order_id,
            business_date=datetime.now().strftime("%Y%m%d")
        )

        if result.get("success"):
            executed += 1
            current_position_count += 1
            qty_note = f"(原{quantity}→{adjusted_quantity})" if adjusted_quantity != quantity else ""
            print(f"  ✅ BUY {code} {adjusted_quantity}股 @{price:.2f}{qty_note} → trade_id={result['trade']['trade_id']}")
        else:
            print(f"  ❌ BUY {code}: {result.get('error', '未知')}")

        results.append({
            "code": code, "action": "BUY", "success": result.get("success", False),
            "error": result.get("error", ""), "order_id": order_id,
            "planned_price": planned_price, "actual_price": price,
            "planned_qty": quantity, "adjusted_qty": adjusted_quantity,
            **_liquidity_fields(
                avg_volume=result.get("trade", {}).get("avg_volume", _avg_vol),
                volume_ratio_pct=result.get("trade", {}).get("volume_ratio_pct", _volume_ratio_pct),
                filled_quantity=result.get("trade", {}).get("filled_quantity", adjusted_quantity if result.get("success") else 0),
                source=result.get("trade", {}).get("liquidity_source", _liquidity_source),
            ),
        })

    # 3️⃣  更新持仓价格
    success_codes = [r["code"] for r in results if r.get("success")]
    if success_codes and not dry_run:
        try:
            from dsl_data_sdk_original import get_price
            price_dict = {}
            for code in success_codes:
                try:
                    p = get_price(code)
                    if p and p.get("price"):
                        price_dict[code] = float(p["price"])
                except Exception:
                    pass
            if price_dict:
                trader.update_position_prices(price_dict)
        except Exception:
            pass

    # 4️⃣  记录执行日志
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "source": "execute_planned_trades",
        "market": market,
        "dry_run": dry_run,
        "total_planned": len(trades),
        "executed": executed,
        "skipped": skipped,
        "cancelled_due_to_price": cancelled,
        "results": results,
    }
    if not dry_run:
        os.makedirs(os.path.dirname(PORTFOLIO_LOG), exist_ok=True)
        with open(PORTFOLIO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
    else:
        print("  [DRY-RUN] 不写入执行日志")

    # 5️⃣  组合摘要
    summary = trader.get_portfolio_summary()
    print(f"\n📊 执行后组合状态:")
    print(f"  总资产: ¥{summary['total_value']:,.0f} | 收益: {summary['total_return_pct']:.2f}%")
    print(f"  现金: ¥{summary['current_cash']:,.0f} | 持仓: {len(summary['positions'])}只")
    for p in summary.get("positions", []):
        print(f"    {p['stock_code']} {p['quantity']}股 成本={p['avg_cost']:.2f} 现价={p['current_price']:.2f}")

    return results


def _filter_buys_when_prediction_stale(trades: list) -> tuple:
    """Return (filtered_trades, freshness) with stale BUY orders removed."""
    try:
        from core.data_freshness import assess_daily_predict_freshness
        freshness = assess_daily_predict_freshness()
    except Exception as e:
        freshness = {
            "status": "invalid",
            "allow_buy": False,
            "allow_sell": True,
            "reason": f"freshness检查失败: {e}",
        }

    if freshness.get("allow_buy", False):
        return trades, freshness

    filtered = []
    blocked = 0
    for trade in trades:
        action = str(trade.get("action", "")).upper()
        if action == "BUY":
            blocked += 1
            print(f"  ⛔ BUY {trade.get('code', trade.get('symbol', '?'))}: ML预测不可买入 ({freshness.get('reason', freshness.get('status'))})")
            continue
        filtered.append(trade)
    if blocked:
        print(f"  ⛔ stale BUY门禁: 已过滤 {blocked} 笔BUY, 保留 {len(filtered)} 笔SELL/减仓")
    return filtered, freshness


def _expected_trade_target_date(now: datetime = None) -> str:
    """Expected target date for planned trades: today if trading, else next A-share trading day."""
    now = now or datetime.now()
    today = now.date()
    try:
        from config.holiday_calendar import is_trading_day, get_next_trading_day
        if is_trading_day(check_date=today, market="A_SHARE"):
            return today.isoformat()
        return get_next_trading_day("A_SHARE", today).isoformat()
    except Exception:
        return today.isoformat()


def main(trades_path: str = None, market: str = "A", dry_run: bool = False):
    """执行已规划的交易

    Args:
        trades_path: 计划交易JSON路径，None时使用默认路径
    """
    # 假日检查：非交易日跳过（Harness L5要求）
    try:
        from config.holiday_calendar import is_trading_day
        today = datetime.now().date()
        if not dry_run and not is_trading_day(check_date=today, market="A_SHARE"):
            print(f"⏸️ 今日({today})非A股交易日，跳过交易执行")
            return
    except ImportError:
        print("  ⚠️ 假日日历不可用，继续执行")

    # 进度追踪
    try:
        from common.progress_tracker import ProgressTracker
        tracker = ProgressTracker("trade_execution_0930", total_steps=1)
    except ImportError:
        tracker = None

    date_str = datetime.now().strftime("%Y%m%d")
    print(f"{'='*60}")
    print(f"🚀 DSL交易执行 [{date_str}]")
    print(f"{'='*60}")

    trades_file = trades_path or TRADES_CACHE

    # v4.5.7: 防双重执行检查
    if _check_idempotent(date_str):
        if tracker:
            tracker.complete("已执行过，跳过")
        return

    if not os.path.exists(trades_file):
        print(f"❌ 找不到计划交易文件: {trades_file}")
        if tracker:
            tracker.fail("找不到计划交易文件")
        return

    with open(trades_file, encoding="utf-8") as f:
        raw = json.load(f)

    # 支持 [trades] 和 {"trades": [...], "target_date": "..."} 两种格式
    if isinstance(raw, dict):
        trades = raw.get("trades", [])
        target_date = raw.get("target_date", "")
        expected_date = _expected_trade_target_date()
        if target_date and target_date != expected_date:
            print(f"⚠️ 计划交易目标日期{target_date}≠预期执行日{expected_date}，跳过")
            return
    else:
        trades = raw

    for trade in trades:
        if isinstance(trade, dict):
            code = trade.get("code") or trade.get("symbol") or ""
            if code:
                trade["code"] = code
                trade["symbol"] = code

    if not trades:
        print("ℹ️ 无计划交易")
        if tracker:
            tracker.complete("无计划交易")
        return

    trades, freshness = _filter_buys_when_prediction_stale(trades)
    if not trades:
        print(f"ℹ️ stale门禁后无可执行交易 (freshness={freshness.get('status')})")
        if tracker:
            tracker.complete("stale门禁后无可执行交易")
        return

    print(f"📋 加载 {len(trades)} 笔计划交易")
    if tracker:
        tracker.step(1, f"执行{len(trades)}笔交易")

    # 执行（使用开盘实际价格）
    results = execute_trades(trades, market=market, dry_run=dry_run)
    success = sum(1 for r in results if r.get("success"))
    print(f"\n✅ 执行完成: {success}/{len(results)} 成功 ({len(results)-success}失败/跳过)")
    if tracker:
        tracker.complete(f"{success}/{len(results)}笔执行成功", total=len(results), success=success)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DSL信号执行桥接")
    parser.add_argument("--trades", default=None, help="计划交易JSON路径")
    parser.add_argument("--trades-path", default=None, help="计划交易JSON路径（--trades别名）")
    parser.add_argument("--market", default="A", help="市场代码 A/HK")
    parser.add_argument("--dry-run", action="store_true", help="仅打印不执行")
    args = parser.parse_args()
    main(trades_path=args.trades_path or args.trades, market=args.market, dry_run=args.dry_run)
