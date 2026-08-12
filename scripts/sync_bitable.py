#!/usr/bin/env python3
"""
飞书5表收盘同步脚本 — 模拟盘收盘后调用
从 SQLite 读取 PaperTrader 数据，一键写入飞书5个多维表格

集成方式：在 daily_sim_report_v3.py 生成报告之前调用本脚本
"""
import os
import sys
import json
from datetime import datetime
from typing import Dict, List, Optional

# 项目路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from common.bitable_writer import sync_all_tables, sync_stock_pool as _sync_stock_pool
from common.config import config
from common.logger import get_logger

logger = get_logger("bitable_sync")

# 模拟盘数据路径 — 兼容两种存储方式
SIM_PORTFOLIO_PATH = os.path.join(PROJECT_ROOT, "data", "simulation_portfolio.json")
PAPER_LEDGER_PATH = os.path.join(PROJECT_ROOT, "data", "paper_trading_ledger.json")


def _load_simulation_portfolio() -> Optional[Dict]:
    """从 simulation_portfolio.json 读取模拟盘数据"""
    if not os.path.exists(SIM_PORTFOLIO_PATH):
        logger.warning(f"模拟盘数据文件不存在: {SIM_PORTFOLIO_PATH}")
        return None
    try:
        with open(SIM_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取 simulation_portfolio.json 失败: {e}")
        return None


def _load_paper_trading_ledger() -> Optional[Dict]:
    """从 paper_trading_ledger.json 读取模拟盘数据"""
    if not os.path.exists(PAPER_LEDGER_PATH):
        logger.warning(f"模拟盘账本文件不存在: {PAPER_LEDGER_PATH}")
        return None
    try:
        with open(PAPER_LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"读取 paper_trading_ledger.json 失败: {e}")
        return None


def _load_paper_trader_sqlite() -> Optional[Dict]:
    """从 SQLite PaperTrader 直接读取"""
    try:
        sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
        from paper_trader import PaperTrader
        trader = PaperTrader()
        return trader.load_ledger()
    except Exception as e:
        logger.warning(f"SQLite PaperTrader 读取失败: {e}")
        return None


def _determine_market(symbol: str) -> str:
    """根据股票代码判断市场"""
    s = str(symbol).upper()
    if s.startswith("HK") or s.startswith("07") or s.startswith("08") or s.startswith("09"):
        return "港股"
    if s.startswith("6") or s.startswith("00") or s.startswith("30") or s.startswith("68"):
        return "A股"
    return "A股"


def _load_predictions() -> Dict[str, Dict]:
    """从最近一次预测结果中加载预测数据（含股价和评分）
    v4.5.1+ 优先使用20d预测（主信号），5d做确认。
    自动兼容旧格式（flat predicted_return）和新格式（嵌套h20d/h5d/h1d）。
    """
    reports_dir = os.path.join(PROJECT_ROOT, "reports", "predictor")
    if not os.path.exists(reports_dir):
        return {}
    files = [f for f in os.listdir(reports_dir) if f.startswith("prediction_") and f.endswith(".json")]
    if not files:
        return {}
    # 取最新的
    latest = sorted(files)[-1]
    path = os.path.join(reports_dir, latest)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        predictions = {}
        for code, data in raw.items():
            if "error" in data:
                continue

            # 🔑 v4.5.1+ 增强格式：优先取20d预测做为主信号
            # h20d.direction_accuracy 平均50.12%，远高于5d的47.81%
            h20d = data.get("h20d", {})
            h5d = data.get("h5d", {})

            if h20d and h20d.get("predicted_return") is not None:
                predicted_return_20d = h20d["predicted_return"]
                predicted_return_5d = h5d.get("predicted_return", 0)
                # 综合：20d权重60% + 5d权重40%
                predicted_return = predicted_return_20d * 0.6 + predicted_return_5d * 0.4
            else:
                # 旧格式fallback
                predicted_return = data.get("predicted_return", 0)

            # ⚠️ 异常值过滤: predicted_return 超过 ±30% 视为异常
            if abs(predicted_return) > 0.30:
                print(f"    ⚠️ 跳过异常预测 {code}: return={predicted_return*100:+.2f}%")
                continue

            # 20日预测需要缩放：20日收益率→等效日评分的经验系数
            score = round(predicted_return * 100, 2)
            price = data.get("latest_price", 0)

            # 信号判断（20d阈值放宽，因为20d方向精度更高）
            if score > 5:
                signal = "买入"
            elif score < -5:
                signal = "卖出"
            elif score > 1.0:
                signal = "持有"
            else:
                signal = "观望"

            predictions[code] = {
                "price": price,
                "score": score,
                "signal": signal,
            }
        return predictions
    except Exception as e:
        logger.warning(f"加载预测结果失败: {e}")
        return {}


def _fetch_realtime_prices(stock_codes: List[str]) -> Dict[str, float]:
    """从新浪行情批量获取实时股价"""
    if not stock_codes:
        return {}
    import requests
    sina_codes = []
    for s in stock_codes:
        s = s.strip()
        if s.startswith("6") or s.startswith("68"):
            sina_codes.append(f"sh{s}")
        else:
            sina_codes.append(f"sz{s}")
    query = ",".join(sina_codes)
    url = f"https://hq.sinajs.cn/list={query}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    prices = {}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.encoding = "gbk"
        for line in resp.text.split("\n"):
            if line.startswith("var hq_str_"):
                try:
                    parts = line.split('"')
                    if len(parts) > 1:
                        data = parts[1].split(",")
                        code_key = line.split("_")[2].replace("sh", "").replace("sz", "")
                        raw = data[3] if len(data) > 3 else "0"
                        try:
                            prices[code_key] = float(raw)
                        except (ValueError, TypeError):
                            prices[code_key] = 0.0
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"获取实时行情失败: {e}")
    return prices


def sync_stock_pool_from_config() -> int:
    """从 master_stock_pool.yaml 读取股票池并写入飞书股票池表（带实时股价和DSL评分）"""
    import yaml
    pool_path = os.path.join(PROJECT_ROOT, "config", "master_stock_pool.yaml")
    if not os.path.exists(pool_path):
        logger.warning(f"master_stock_pool.yaml 不存在: {pool_path}")
        return 0
    try:
        with open(pool_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        stocks_raw = data.get("master_pool", [])
        if not stocks_raw:
            logger.warning("master_stock_pool.yaml 中没有股票数据")
            return 0

        # 加载预测数据（含评分和最新价格）
        predictions = _load_predictions()
        # 获取实时行情（查漏补缺）
        all_codes = [s["symbol"] for s in stocks_raw]
        realtime_prices = _fetch_realtime_prices(all_codes)

        stocks = []
        for s in stocks_raw:
            code = s["symbol"]
            tier_raw = s.get("tier", "")
            tier_map = {
                "core": "T1-核心",
                "cyclical": "T2-观察",
                "growth": "T3-备选",
                "bluechip": "T1-核心",
            }
            feishu_tier = tier_map.get(tier_raw, "T2-观察")

            # 从预测结果获取股价和评分（优先），否则从实时行情补
            pred = predictions.get(code, {})
            price = pred.get("price", 0) or realtime_prices.get(code, 0)
            score = pred.get("score", 0)
            signal = pred.get("signal", "观望")

            stocks.append({
                "symbol": code,
                "name": s.get("name", ""),
                "market": "A股",
                "sector": s.get("sector", ""),
                "reason": s.get("note", ""),
                "score": score,
                "tier": feishu_tier,
                "price": price,
                "signal": signal,
            })

        result = _sync_stock_pool(stocks)
        logger.info(f"股票池同步完成: {result} 条 (有评分: {sum(1 for s in stocks if s['score'] != 0)}, 有股价: {sum(1 for s in stocks if s['price'] > 0)})")
        return result
    except Exception as e:
        logger.error(f"股票池同步失败: {e}")
        import traceback
        traceback.print_exc()
        return 0


def sync_stock_pool_from_session(stocks: List[Dict]) -> int:
    """从当天的选股结果同步股票池
    stocks: [{'symbol':'000001','name':'平安银行','sector':'银行','score':8.5,'tier':'T1-核心',...}]
    """
    from common.bitable_writer import sync_stock_pool as _sync_pool
    return _sync_pool(stocks)


def sync_from_paper_trader() -> Dict[str, int]:
    """从 PaperTrader 读取全部数据并回写飞书5表"""
    # 优先从 SQLite 读取（数据最新）
    ledger = _load_paper_trader_sqlite()
    if not ledger:
        ledger = _load_simulation_portfolio()
    if not ledger:
        ledger = _load_paper_trading_ledger()
    if not ledger:
        logger.error("所有数据源均不可用，无法同步")
        return {}

    positions_raw = ledger.get("positions", [])
    trades_raw = ledger.get("trade_history", [])
    cash = ledger.get("current_cash", ledger.get("cash", 0))
    initial = ledger.get("initial_capital", 1_000_000)
    perf_metrics = ledger.get("performance_metrics", {})

    # === 构造模拟持仓数据 ===
    sim_positions = []
    # 处理 SQLite 格式的 positions (dict)
    if positions_raw and isinstance(positions_raw, list):
        for p in positions_raw:
            if isinstance(p, dict):
                qty = int(p.get("quantity", p.get("shares", 0)))
                avg_cost = float(p.get("avg_cost", p.get("cost", p.get("cost_price", 0))))
                current_price = float(p.get("current_price", avg_cost))
                market_value = qty * current_price
                total_cost = qty * avg_cost
                symbol = p.get("stock_code", p.get("symbol", ""))
                sim_positions.append({
                    "symbol": symbol,
                    "name": p.get("stock_name", p.get("name", "")),
                    "market": _determine_market(symbol),
                    "buy_date": p.get("buy_date", ""),
                    "avg_cost": avg_cost,
                    "qty": qty,
                    "current_price": current_price,
                    "market_value": market_value,
                    "unrealized_pnl": market_value - total_cost,
                    "unrealized_pnl_pct": ((current_price - avg_cost) / avg_cost * 100) if avg_cost > 0 else 0,
                    "position_ratio": (market_value / (cash + market_value) * 100) if (cash + market_value) > 0 else 0,
                    "total_cost": total_cost,
                    "reason": p.get("reason", p.get("buy_reason", "")),
                })

    # === 构造持仓追踪数据 ===
    holdings = []
    for p in sim_positions:
        holdings.append({
            "symbol": p["symbol"],
            "name": p["name"],
            "market": p["market"],
            "buy_date": p["buy_date"],
            "avg_cost": p["avg_cost"],
            "qty": p["qty"],
            "total_cost": p["total_cost"],
            "current_price": p["current_price"],
            "market_value": p["market_value"],
            "unrealized_pnl": p["unrealized_pnl"],
            "unrealized_pnl_pct": p["unrealized_pnl_pct"],
            "hold_days": 0,
            "stop_loss": 0,
            "target_price": 0,
            "trail_trigger": 0,
            "dsl_score": 0,
            "buy_reason": p["reason"],
            "status": "持仓中",
        })

    # === 构造交易记录数据 ===
    trade_records = []
    if trades_raw and isinstance(trades_raw, list):
        for t in trades_raw:
            if isinstance(t, dict):
                symbol = t.get("stock_code", t.get("symbol", ""))
                action = t.get("action", t.get("trade_type", ""))
                price = float(t.get("price", t.get("exec_price", 0)))
                qty = int(t.get("quantity", t.get("volume", t.get("exec_qty", 0))))
                amount = qty * price
                # 判断卖出原因
                sell_reason = ""
                if action in ("SELL", "sell", "卖出"):
                    sell_reason = t.get("reason", "")
                    if "止损" in str(sell_reason):
                        sell_reason = "止损触发"
                    elif "止盈" in str(sell_reason) or "目标" in str(sell_reason):
                        sell_reason = "目标价止盈"
                    elif "移动" in str(sell_reason):
                        sell_reason = "移动止盈"
                    elif "信号" in str(sell_reason):
                        sell_reason = "DSL信号反转"
                    else:
                        sell_reason = "信号反转"

                trade_records.append({
                    "date": t.get("timestamp", t.get("date", t.get("trade_date", "")))[:10],
                    "symbol": symbol,
                    "name": t.get("stock_name", t.get("name", "")),
                    "market": _determine_market(symbol),
                    "direction": action,
                    "price": price,
                    "qty": qty,
                    "amount": amount,
                    "fee": float(t.get("fee", t.get("手续费", 0))),
                    "realized_pnl": float(t.get("profit", t.get("realized_pnl", 0))),
                    "hold_days": int(t.get("hold_days", 0)),
                    "sell_reason": sell_reason,
                    "dsl_signal": t.get("reason", t.get("dsl_signal", "")),
                })

    # === 构造绩效看板数据 ===
    total_value = cash + sum(p["market_value"] for p in sim_positions)
    total_return_pct = ((total_value - initial) / initial * 100) if initial > 0 else 0
    total_sells = [t for t in trade_records if t["direction"] in ("SELL", "sell", "卖出")]
    wins = [t for t in total_sells if t["realized_pnl"] > 0]
    losses = [t for t in total_sells if t["realized_pnl"] <= 0]
    win_rate = (len(wins) / len(total_sells) * 100) if total_sells else 0

    perf = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_asset": total_value,
        "market_value": sum(p["market_value"] for p in sim_positions),
        "cash": cash,
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": perf_metrics.get("max_drawdown_pct", 0),
        "win_rate": round(win_rate, 2),
        "profit_factor": perf_metrics.get("profit_factor", 0),
        "trade_count": len(trades_raw) if trades_raw else 0,
        "win_count": len(wins),
        "loss_count": len(losses),
        "avg_hold_days": perf_metrics.get("avg_hold_days", 0),
        "sharpe_ratio": perf_metrics.get("sharpe_ratio", 0),
        "cumulative_pnl": total_value - initial,
        "unrealized_pnl": sum(p["unrealized_pnl"] for p in sim_positions),
    }

    # === 股票池同步（从master_stock_pool.yaml读取） ===
    pool_count = sync_stock_pool_from_config()

    # === 执行同步 ===
    result = sync_all_tables(
        sim_positions=sim_positions,
        holdings=holdings,
        trades=trade_records,
        performance=perf,
    )
    result["stock_pool"] = pool_count
    logger.info(f"飞书同步完成: {json.dumps(result, ensure_ascii=False)}")
    return result


# ============================================================
# 主入口
# ============================================================
if __name__ == "__main__":
    print(f"🔄 开始同步飞书5表 ({datetime.now().strftime('%Y-%m-%d %H:%M')})...")
    result = sync_from_paper_trader()
    total = sum(result.values())
    print(f"✅ 同步完成: {total} 条记录")
    for k, v in result.items():
        print(f"   {k}: {v} 条")
