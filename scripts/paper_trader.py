#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模拟交易执行器 - SQLite版本
修复：JSON并发写入丢失 + 裸except + 无事务回滚
使用SQLite + WAL模式支持并发读写，事务保证ACID
"""

import sqlite3
import json
import os
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional
from contextlib import contextmanager

DB_PATH = os.path.expanduser(
    "~/.openclaw/workspace/dsl-quant-trading-hybrid/data/paper_trading.db"
)

# JSON同步文件路径 — SQLite权威源的镜像，供下游模块消费
LEDGER_JSON_PATH = os.path.join(os.path.dirname(DB_PATH), "paper_trading_ledger.json")
SIM_STATE_PATH = os.path.join(os.path.dirname(DB_PATH), "simulation_state.json")
SIM_PORTFOLIO_PATH = os.path.join(os.path.dirname(DB_PATH), "simulation_portfolio.json")

# 确保目录存在
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


class PaperTrader:
    """模拟交易执行器 - SQLite事务存储"""

    # ── API契约声明 ──
    # 用于 validate_api_contract.py 自动校验类方法完整性
    __all__methods__ = [
        "_apply_slippage",
        "_audit_trade",
        "_check_volume_limit",
        "_get_conn",
        "_get_float",
        "_init_db",
        "_is_live_mode",
        "_live_trade_gates",
        "_load_slippage_config",
        "_notify_reconciliation",
        "_record_execution",
        "_set_float",
        "_sync_to_json",
        "_try_load_from_json",
        "_update_metrics",
        "_update_peak_equity",
        "_validate_lot_size",
        "check_stop_losses",
        "execute_trade",
        "get_portfolio_summary",
        "load_ledger",
        "update_position_prices",
    ]

    DISABLE_HK_MARKET = True

    def __init__(self, test_mode: bool = False):
        """
        初始化纸交易系统
        
        Args:
            test_mode: 测试模式时使用内存数据库, 避免污染生产数据
        """
        self._test_mode = test_mode
        self._memory_conn = None  # 测试模式持有的内存DB引用
        # v4.5.6: 流动性约束参数
        self.MAX_VOLUME_RATIO = 0.10   # 单笔≤日成交量10%
        self.COMMISSION_RATE = 0.00025  # 佣金万2.5 (v4.5.12 fix: 与config/constants.py统一)
        self.STAMP_TAX_RATE = 0.001     # 印花税千1(仅卖出)
        self._init_db()

    def _init_db(self):
        """初始化数据库表结构"""
        with self._get_conn() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA busy_timeout=5000;
                CREATE TABLE IF NOT EXISTS ledger (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    avg_cost REAL NOT NULL,
                    current_price REAL NOT NULL,
                    UNIQUE(market, stock_code)
                );
                CREATE TABLE IF NOT EXISTS trade_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    market TEXT NOT NULL,
                    stock_code TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('BUY','SELL')),
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    commission REAL DEFAULT 0.0,
                    stamp_tax REAL DEFAULT 0.0,
                    total_fee REAL DEFAULT 0.0,
                    volume_ratio_pct REAL DEFAULT 0.0,
                    is_valid_for_metrics INTEGER DEFAULT 1,
                    quality_flag TEXT DEFAULT 'valid',
                    reason TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS performance_metrics (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                -- 插入默认账本
                INSERT OR IGNORE INTO ledger (key, value) VALUES ('version', '"v3.2.0-sqlite"');
                INSERT OR IGNORE INTO ledger (key, value) VALUES ('initial_capital', '1000000.0');
                INSERT OR IGNORE INTO ledger (key, value) VALUES ('current_cash', '1000000.0');
                INSERT OR IGNORE INTO ledger (key, value) VALUES ('peak_equity', '1000000.0');  -- P0-5: 峰值权益追踪
            """)
            # v4.5.6: 迁移旧DB缺少的列
            try:
                existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_history)")}
                for col, typ, default in [
                    ('commission', 'REAL', '0.0'),
                    ('stamp_tax', 'REAL', '0.0'),
                    ('total_fee', 'REAL', '0.0'),
                    ('volume_ratio_pct', 'REAL', '0.0'),
                    ('is_valid_for_metrics', 'INTEGER', '1'),
                    ('quality_flag', 'TEXT', "'valid'"),
                ]:
                    if col not in existing_cols:
                        conn.execute(f"ALTER TABLE trade_history ADD COLUMN {col} {typ} DEFAULT {default}")
            except Exception:
                pass

            # v4.5.12: JSON→SQLite 反向同步
            # SQLite为空时从JSON镜像文件加载历史持仓
            self._try_load_from_json()

    # ──────────────────────────────────────────────────────────
    # v4.5.12: JSON→SQLite 反向同步 — SQLite为空时从JSON加载
    # ──────────────────────────────────────────────────────────
    def _try_load_from_json(self):
        """
        当SQLite中无持仓记录时，尝试从JSON镜像文件加载历史持仓。
        
        解决三系统数据脱节问题：Trade执行若直接写JSON绕过SQLite，
        则PaperTrader启动时将检测到SQLite为空，自动从JSON导入。
        
        尝试顺序:
        1. paper_trading_ledger.json (最完整)
        2. simulation_portfolio.json (含stock names)
        3. simulation_state.json (最小镜像)
        """
        count = 0
        try:
            with self._get_conn() as conn:
                count = conn.execute("SELECT COUNT(*) as cnt FROM positions WHERE quantity>0").fetchone()["cnt"]
        except Exception:
            return  # DB尚不可用
        
        if count > 0:
            return  # SQLite已有数据，无需导入
        
        # ── 收集所有可用的JSON源 ──
        sources = []

        def _parse_ledger(jpath):
            if not os.path.exists(jpath):
                return None
            try:
                with open(jpath) as f:
                    d = json.load(f)
                entry_list = d.get("positions", [])
                if not isinstance(entry_list, list):
                    return None
                cash = float(d.get("current_cash", 1000000.0))
                initial = float(d.get("initial_capital", 1000000.0))
                parsed = [(str(e["code"]), int(e["shares"]), float(e["avg_price"]),
                           float(e["current_price"]), "A") for e in entry_list
                          if e.get("code") and int(e.get("shares", 0)) > 0]
                return (jpath, parsed, cash, initial)
            except Exception:
                return None

        def _parse_sim_portfolio(jpath):
            if not os.path.exists(jpath):
                return None
            try:
                with open(jpath) as f:
                    d = json.load(f)
                pos_dict = d.get("positions", {})
                if not isinstance(pos_dict, dict):
                    return None
                cash = float(d.get("current_capital", d.get("cash", 1000000.0)))
                initial = float(d.get("initial_capital", 1000000.0))
                parsed = []
                for code, e in pos_dict.items():
                    shares = int(e.get("shares", 0))
                    if shares <= 0:
                        continue
                    parsed.append((code, shares,
                                   float(e.get("avg_price", e.get("cost", 0))),
                                   float(e.get("current_price", 0)),
                                   str(e.get("market", "A")) or "A"))
                return (jpath, parsed, cash, initial)
            except Exception:
                return None

        def _parse_sim_state(jpath):
            if not os.path.exists(jpath):
                return None
            try:
                with open(jpath) as f:
                    d = json.load(f)
                entry_list = d.get("positions", [])
                if not isinstance(entry_list, list):
                    return None
                cash = float(d.get("current_cash", 1000000.0))
                initial = float(d.get("initial_capital", 1000000.0))
                parsed = [(str(e["symbol"]), int(e["quantity"]), float(e["avg_cost"]),
                           float(e["current_price"]), str(e.get("market", "A")) or "A")
                          for e in entry_list
                          if e.get("symbol") and int(e.get("quantity", 0)) > 0]
                return (jpath, parsed, cash, initial)
            except Exception:
                return None

        for parser in [_parse_ledger(LEDGER_JSON_PATH),
                       _parse_sim_portfolio(SIM_PORTFOLIO_PATH),
                       _parse_sim_state(SIM_STATE_PATH)]:
            if parser is not None:
                sources.append(parser)

        if not sources:
            return

        # ── 选持仓数最多的源 ──
        best = max(sources, key=lambda s: len(s[1]))
        jpath, entries, cash, initial = best

        if not entries:
            return

        imported = 0
        with self._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO ledger (key, value) VALUES ('initial_capital', ?)", (str(initial),))
            conn.execute("INSERT OR REPLACE INTO ledger (key, value) VALUES ('current_cash', ?)", (str(cash),))
            for code, shares, cost, price, mkt in entries:
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO positions (market, stock_code, quantity, avg_cost, current_price) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (mkt, code, shares, cost, price)
                    )
                    imported += 1
                except Exception:
                    pass

        if imported > 0:
            log = logging.getLogger(__name__)
            log.info(f"📂 JSON→SQLite反向同步: 从{os.path.basename(jpath)}导入{imported}个持仓, cash={cash:.0f}")
            print(f"📂 JSON→SQLite反向同步: 从{os.path.basename(jpath)}导入{imported}个持仓")
            self._sync_to_json()
            print(f"📂 JSON→SQLite反向同步: 从{os.path.basename(jpath)}导入{imported}个持仓")
            # 同步完后把最新状态写回JSON（同步格式增强）
            self._sync_to_json()

    @contextmanager
    def _get_conn(self):
        """获取数据库连接（上下文管理器自动提交/回滚）"""
        if self._test_mode:
            if self._memory_conn is None:
                self._memory_conn = sqlite3.connect(":memory:")
            conn = self._memory_conn
        else:
            conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            # 测试模式: 内存DB不关闭(保持引用), 生产模式: 正常关闭
            if not self._test_mode:
                conn.close()

    def _get_float(self, key: str, default: float = 0.0) -> float:
        with self._get_conn() as conn:
            row = conn.execute("SELECT value FROM ledger WHERE key=?", (key,)).fetchone()
            return float(row["value"]) if row else default

    def _set_float(self, key: str, value: float):
        with self._get_conn() as conn:
            conn.execute("INSERT OR REPLACE INTO ledger (key, value) VALUES (?, ?)",
                         (key, str(value)))

    def _update_peak_equity(self):
        """P0-5: 更新峰值权益 — 用于正确计算组合回撤
        每次交易成功后调用，若当前总权益超过历史峰值则更新
        """
        try:
            with self._get_conn() as conn:
                cash = float(conn.execute(
                    "SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"])
                positions = conn.execute(
                    "SELECT quantity, current_price FROM positions WHERE quantity > 0").fetchall()
                total_mv = sum(p["quantity"] * p["current_price"] for p in positions)
                current_equity = cash + total_mv
                peak_row = conn.execute(
                    "SELECT value FROM ledger WHERE key='peak_equity'").fetchone()
                peak = float(peak_row["value"]) if peak_row else 0.0
                if current_equity > peak:
                    conn.execute("UPDATE ledger SET value=? WHERE key='peak_equity'",
                                 (str(current_equity),))
        except Exception:
            pass  # 非关键路径，静默失败

    def run_portfolio_reconciliation(self) -> dict:
        """v4.6.9i(审计F1-5): 每日账目对账 — 验证 cash + Σ(持仓市值) 与账面权益一致。
        修复: 账目无对账路径、reconciliation表0行(审计P0-6)。
        返回 {ok, cash, pos_value, equity_implied, equity_stored, diff, positions}
        """
        result = {"ok": False, "cash": 0.0, "pos_value": 0.0,
                  "equity_implied": 0.0, "equity_stored": 0.0, "diff": 0.0, "positions": 0}
        try:
            with self._get_conn() as conn:
                cash = float(conn.execute(
                    "SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"])
                rows = conn.execute(
                    "SELECT quantity, current_price FROM positions WHERE quantity > 0").fetchall()
                pos_value = sum(float(r["quantity"]) * float(r["current_price"]) for r in rows)
                equity_implied = round(cash + pos_value, 2)
                try:
                    eq_row = conn.execute(
                        "SELECT value FROM performance_metrics WHERE key='current_equity'").fetchone()
                    equity_stored = float(eq_row["value"]) if eq_row else equity_implied
                except Exception:
                    equity_stored = equity_implied
                diff = round(equity_implied - equity_stored, 2)
                # 容差: 相对0.1%或绝对1元 — 容忍持仓价刷新时序噪音, 捕捉真实记账错误
                ok = abs(diff) <= max(1.0, equity_implied * 0.001)
                try:
                    conn.execute(
                        "INSERT INTO audit_log (timestamp, source, action, detail, result) "
                        "VALUES (datetime('now','localtime'), ?, ?, ?, ?)",
                        ("reconciliation", "portfolio" if ok else "portfolio:MISMATCH",
                         json.dumps({"cash": cash, "pos_value": round(pos_value, 2),
                                     "equity_implied": equity_implied,
                                     "equity_stored": equity_stored, "diff": diff,
                                     "positions": len(rows)}, ensure_ascii=False),
                         "success" if ok else "failed"))
                except Exception:
                    pass  # audit_log写入失败不阻断对账结果返回(兼容test_mode/旧库)
            result.update({"ok": ok, "cash": cash, "pos_value": round(pos_value, 2),
                           "equity_implied": equity_implied, "equity_stored": equity_stored,
                           "diff": diff, "positions": len(rows)})
        except Exception as e:
            print(f"⚠️ 账目对账失败: {e}")
        return result

    def load_ledger(self) -> dict:
        """加载账本摘要（兼容旧接口）"""
        with self._get_conn() as conn:
            cash = float(conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"])
            initial = float(conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()["value"])
            rows = conn.execute("SELECT * FROM positions").fetchall()
            trades = conn.execute("SELECT * FROM trade_history ORDER BY id").fetchall()
        positions = [dict(r) for r in rows]
        trade_list = [dict(t) for t in trades]
        return {
            "version": "v3.2.0-sqlite",
            "initial_capital": initial,
            "current_cash": cash,
            "positions": positions,
            "trade_history": trade_list,
        }

    def _audit_trade(self, stock_code: str, action: str, price: float,
                     quantity: int, success: bool, detail: str = ""):
        """v4.5.9: 审计日志 — 每笔交易可追溯"""
        try:
            from common.audit import log
            log(
                source="paper_trader",
                action=f"trade:{action.lower()}",
                target=stock_code,
                detail={"price": price, "quantity": quantity, "detail": detail},
                result="success" if success else f"failed:{detail[:80]}",
            )
        except Exception:
            pass

    # ═══════════════════ v4.6.x: 实盘/滑点基础设施 ═══════════════════

    def _is_live_mode(self) -> bool:
        """检测是否为实盘模式"""
        return os.environ.get("DSL_BROKER", "paper") != "paper"

    def _load_slippage_config(self) -> dict:
        try:
            import yaml
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "config", "adaptive_params.yaml")
            with open(path) as f:
                ap = yaml.safe_load(f)
            return ap.get("trading", {}).get("slippage", {})
        except Exception:
            return {}

    def _apply_slippage(self, symbol: str, price: float, quantity: int, action: str) -> float:
        """v4.6.x: 模拟交易滑点模型"""
        if self._test_mode:
            return price
        cfg = self._load_slippage_config()
        model = cfg.get("model", "volume_based")
        if model == "fixed":
            ticks = cfg.get("fixed_ticks", 2)
            tick_size = 0.01
            offset = ticks * tick_size
            return price + offset if action == "BUY" else price - offset
        elif model == "volume_based":
            base_bps = cfg.get("base_bps", 2.0) / 10000
            impact = cfg.get("impact_factor", 0.5)
            try:
                import akshare as ak
                import pandas as pd
                end = datetime.now().strftime("%Y%m%d")
                start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
                df = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust="qfq")
                avg_vol = 10000000
                if df is not None and not df.empty:
                    vol_col = [c for c in df.columns if "成交" in c or "volume" in c.lower()]
                    if vol_col:
                        avg_vol = float(df[vol_col[0]].tail(20).mean())
                vol_ratio = quantity / max(avg_vol, 1)
                slippage_pct = min(base_bps + vol_ratio * impact, 0.005)
                return price * (1 + slippage_pct) if action == "BUY" else price * (1 - slippage_pct)
            except Exception:
                return price * 1.001 if action == "BUY" else price * 0.999
        return price

    def _live_trade_gates(self, action: str, amount: float) -> tuple:
        """v4.6.x: 实盘安全闸门 Returns: (allowed: bool, reason: str)"""
        if not self._is_live_mode():
            return True, ""
        ack_path = os.path.join(os.path.dirname(__file__), "..", "data", "live_trading_ack")
        if not os.path.exists(ack_path):
            return False, "实盘未确认 — touch data/live_trading_ack 后重试"
        total = self._get_float("current_cash") + sum(
            p.get("market_value", 0) for p in self.get_portfolio_summary().get("positions", []))
        if amount > total * 0.15:
            return False, f"单笔金额超15%上限"
        today_amt = 0
        try:
            with self._get_conn() as conn:
                row = conn.execute("SELECT COALESCE(SUM(amount),0) FROM trade_history WHERE timestamp LIKE ?",
                                   (datetime.now().strftime("%Y-%m-%d") + "%",)).fetchone()
            today_amt = float(row[0]) if row else 0
        except Exception:
            pass
        if today_amt + amount > total * 0.50:
            return False, f"日累计超50%上限"
        return True, ""

    # ═══════════════════ 交易执行 ═══════════════════

    def execute_trade(self, market: str, stock_code: str, action: str,
                      price: float, quantity: int, reason: str = "",
                      order_id: str = "", business_date: str = "") -> Dict:
        """
        执行模拟交易（SQLite事务保证ACID）
        集成对账引擎 + 幂等性守卫 + 审计日志
        同一 order_id 仅执行一次，同一标的+方向+交易日仅执行一次
        
        :param market: 市场 (A/HK)
        :param stock_code: 股票代码
        :param action: BUY/SELL
        :param price: 交易价格
        :param quantity: 数量
        :param reason: 交易理由
        :param order_id: 对账订单ID（由TraderAgent生成）
        :param business_date: 交易日YYYYMMDD（默认当天）
        """
        # v4.5.14: 规范化 market/action, 调用方传什么大小写都行
        market = market.strip().upper()
        action = action.strip().upper()

        if self.DISABLE_HK_MARKET and market == "HK":
            self._notify_reconciliation(order_id, success=False, error="港股交易已暂停")
            return {"success": False, "error": "港股交易已暂停"}

        # v4.5.5+: A股仓位数量校验
        if market == "A":
            check, msg = self._validate_lot_size(stock_code, quantity, action)
            if not check:
                self._notify_reconciliation(order_id, success=False, error=msg)
                self._record_execution(stock_code, action, False)
                return {"success": False, "error": msg}

        # v4.5.6: 流动性约束 — 单笔≤日成交量10%
        # v4.5.12 fix: 集成部分成交逻辑
        volume_ratio_pct = 0.0
        filled_quantity = quantity
        avg_volume = 0.0
        liquidity_source = "not_checked"
        if market.upper() == "A" and not self._test_mode:
            vol_check, vol_msg, volume_ratio_pct, filled_quantity, avg_volume, liquidity_source = self._check_volume_limit(
                stock_code, price, quantity)
            if not vol_check:
                self._notify_reconciliation(order_id, success=False, error=vol_msg)
                self._record_execution(stock_code, action, False)
                return {
                    "success": False,
                    "error": vol_msg,
                    "avg_volume": avg_volume,
                    "volume_ratio_pct": volume_ratio_pct,
                    "filled_quantity": filled_quantity,
                    "liquidity_source": liquidity_source,
                }
            if filled_quantity < quantity:
                # 部分成交: 记录剩余量到待成交队列
                pending_qty = quantity - filled_quantity
                print(f"  ⚠️ 部分成交: {stock_code} {action} {filled_quantity}/{quantity}股 "
                      f"(剩余{pending_qty}股加入待成交队列)")
                quantity = filled_quantity  # 只执行可成交部分
                try:
                    from core.partial_fill import partial_fill_handler, FillResult
                    partial_fill_handler.history.append(
                        FillResult(
                            order_id=order_id or f"PF-{stock_code}-{datetime.now().strftime('%H%M%S')}",
                            symbol=stock_code, action=action,
                            target_qty=quantity + pending_qty,
                            filled_qty=quantity, avg_price=price,
                            status="PARTIAL",
                        )
                    )
                except Exception:
                    pass

        # v4.5.5: 非交易日禁止交易 (fail-safe, 不依赖外部模块)
        today = datetime.now().date()
        # 最低保障: 周末不交易
        if today.weekday() >= 5:
            return {"success": False, "error": f"周末({today})禁止交易"}
        try:
            from config.holiday_calendar import is_trading_day
            if not is_trading_day(check_date=today, market="A_SHARE" if market.upper() == "A" else "HK"):
                return {"success": False, "error": f"非交易日({today})禁止交易"}
        except ImportError:
            # 模块不可用时保守处理: 仅允许通过周末检查
            pass

        # v4.5.3d: 电路熔断器检查 (交易暂停/回撤/连续失败/日亏损)
        # v4.5.5 S6: 测试模式跳过熔断器(避免污染production circuit_breaker.json)
        self._cb = None
        if not self._test_mode:
            try:
                from core.circuit_breaker import CircuitBreaker
                self._cb = CircuitBreaker()
                allowed, reason = self._cb.is_trading_allowed()
                if not allowed:
                    return {"success": False, "error": f"熔断拦截: {reason}"}
            except ImportError:
                pass

        # === v4.5.12: 涨跌停价格拦截 (板块差异化) ===
        if market.upper() == "A":
            try:
                import requests
                from core.risk_manager import get_market_type
                from config.constants import LIMIT_RATES
                mairui_licence = os.environ.get("MAIRUI_LICENCE", "")
                resp = requests.get(
                    f"https://api.mairuiapi.com/hsrl/ssjy/{stock_code}/{mairui_licence}",
                    timeout=5
                ) if mairui_licence else None
                if resp and resp.status_code == 200:
                    d = resp.json()
                    prev_close = float(d.get("yc", 0) or 0)
                    if prev_close > 0:
                        mt = get_market_type(stock_code)
                        limit_rate = LIMIT_RATES.get(mt, 0.10)
                        limit_up = round(prev_close * (1 + limit_rate), 2)
                        limit_down = round(prev_close * (1 - limit_rate), 2)
                        price_val = price if isinstance(price, (int, float)) else float(price)
                        if action == "BUY" and price_val >= limit_up * 0.995:
                            return {"success": False, "error": f"涨停时禁止买入(prev_close={prev_close}, limit_up={limit_up}, board={mt})"}
                        if action == "SELL" and price_val <= limit_down * 1.005:
                            return {"success": False, "error": f"跌停时禁止卖出(prev_close={prev_close}, limit_down={limit_down}, board={mt})"}
            except Exception:
                pass  # 数据获取失败时不阻断交易

        # ---- 幂等性检查 ----
        from execution_engine.idempotency import get_idempotency_guard
        guard = get_idempotency_guard()
        
        # 1. order_id 幂等检查（严格防止重复执行）
        if order_id:
            if not guard.try_acquire(order_id):
                return {
                    "success": False,
                    "error": f"幂等拦截: order_id={order_id} 已存在",
                    "idempotent_skip": True
                }
        
        # 2. 业务键幂等检查（同一标的+方向+交易日唯一）
        bdate = business_date or datetime.now().strftime("%Y%m%d")
        existing_key = guard.check_business_key(stock_code, action, bdate)
        if existing_key:
            guard.mark_failed(order_id or "no-id",
                              f"业务键重复: {stock_code} {action} {bdate}")
            return {
                "success": False,
                "error": f"幂等拦截: 业务键已存在 ({stock_code} {action} {bdate})",
                "existing_order_id": existing_key,
                "idempotent_skip": True
            }
        # v4.6.x: business key推迟到事务成功后注册，防止ghost lock
        # ---- 幂等性检查结束 ----

        # v4.6.x: 实盘安全闸门
        amount_pre = price * quantity
        live_ok, live_reason = self._live_trade_gates(action, amount_pre)
        if not live_ok:
            return {"success": False, "error": f"实盘安全闸: {live_reason}"}

        # v4.6.x: 计算滑点调整后的执行价格
        exec_price = self._apply_slippage(stock_code, price, quantity, action)

        try:
            with self._get_conn() as conn:
                cash = float(
                    conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"]
                )
                amount = exec_price * quantity

                # v4.5.3d: 单票持仓限制检查 (买入前)
                if self._cb and action == "BUY":
                    pos_val = float(conn.execute(
                        "SELECT COALESCE(SUM(quantity*current_price),0) FROM positions"
                    ).fetchone()[0])
                    total_cap = cash + pos_val
                    allowed_pos, pos_reason = self._cb.check_position_limit(
                        stock_code, amount, max(total_cap, 1.0))
                    if not allowed_pos:
                        self._notify_reconciliation(order_id, success=False, error=pos_reason)
                        self._record_execution(stock_code, action, False)
                        return {"success": False, "error": f"熔断拦截: {pos_reason}"}

                if action == "BUY":
                    if amount > cash:
                        self._notify_reconciliation(order_id, success=False, error="资金不足")
                        self._record_execution(stock_code, action, False)
                        return {"success": False, "error": "资金不足"}
                    # 扣现金(含佣金)
                    buy_commission = max(5.0, round(amount * self.COMMISSION_RATE, 2))
                    conn.execute("UPDATE ledger SET value=? WHERE key='current_cash'", (str(cash - amount - buy_commission),))
                    # 更新持仓
                    existing = conn.execute(
                        "SELECT * FROM positions WHERE market=? AND stock_code=?",
                        (market, stock_code)
                    ).fetchone()
                    if existing:
                        total_cost = existing["avg_cost"] * existing["quantity"] + amount
                        new_qty = existing["quantity"] + quantity
                        new_avg = total_cost / new_qty
                        conn.execute(
                            "UPDATE positions SET quantity=?, avg_cost=?, current_price=? WHERE id=?",
                            (new_qty, new_avg, price, existing["id"])
                        )
                    else:
                        conn.execute(
                            "INSERT INTO positions (market, stock_code, quantity, avg_cost, current_price) VALUES (?,?,?,?,?)",
                            (market, stock_code, quantity, price, price)
                        )

                elif action == "SELL":
                    existing = conn.execute(
                        "SELECT * FROM positions WHERE market=? AND stock_code=?",
                        (market, stock_code)
                    ).fetchone()
                    if not existing or existing["quantity"] < quantity:
                        self._notify_reconciliation(order_id, success=False,
                            error=f"持仓不足 (持有{existing['quantity'] if existing else 0}股，需要{quantity}股)")
                        self._record_execution(stock_code, action, False)
                        return {"success": False, "error": f"持仓不足 (持有{existing['quantity'] if existing else 0}股，需要{quantity}股)"}
                    # 增现金(P0fix: 扣除佣金+印花税)
                    sell_commission = round(amount * self.COMMISSION_RATE, 2)
                    sell_stamp = round(amount * self.STAMP_TAX_RATE, 2)
                    sell_fee = max(5.0, sell_commission) + sell_stamp
                    conn.execute("UPDATE ledger SET value=? WHERE key='current_cash'", (str(cash + amount - sell_fee),))
                    new_qty = existing["quantity"] - quantity
                    if new_qty == 0:
                        conn.execute("DELETE FROM positions WHERE id=?", (existing["id"],))
                    else:
                        conn.execute("UPDATE positions SET quantity=?, current_price=? WHERE id=?",
                                     (new_qty, price, existing["id"]))

                else:
                    self._notify_reconciliation(order_id, success=False, error=f"未知操作类型: {action}")
                    self._record_execution(stock_code, action, False)
                    return {"success": False, "error": f"未知操作类型: {action}"}

                # v4.5.6: 计算交易费用
                commission = round(amount * self.COMMISSION_RATE, 2)
                stamp_tax = round(amount * self.STAMP_TAX_RATE, 2) if action == "SELL" else 0.0
                total_fee = commission + stamp_tax

                # 记录交易历史
                now = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO trade_history (timestamp, market, stock_code, action, price, quantity, amount, commission, stamp_tax, total_fee, volume_ratio_pct, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (now, market, stock_code, action, exec_price, quantity, amount, commission, stamp_tax, total_fee, volume_ratio_pct, reason)
                )
                trade_id = conn.execute("SELECT last_insert_rowid() as id").fetchone()["id"]

            # v4.6.x: 事务成功后注册业务键（防ghost lock — 之前在线469, 事务失败时key已占却无交易）
            if order_id:
                guard.register_business_key(stock_code, action, bdate, order_id)

            # 事务外更新绩效指标 + 对账回写 + 监控记录
            self._update_metrics()
            self._notify_reconciliation(order_id, success=True, trade_id=trade_id,
                                        exec_price=price, exec_qty=quantity)
            self._record_execution(stock_code, action, True, error="")

            # v4.5.3d: 熔断器更新 (交易成功)
            if self._cb:
                self._cb.update_trade_count()
                self._cb.record_trade_result(True)

            # 幂等性完成标记
            if order_id:
                try:
                    from execution_engine.idempotency import get_idempotency_guard
                    get_idempotency_guard().complete(order_id, f"trade_id={trade_id}")
                except Exception:
                    pass

            # 交易成功后自动同步JSON镜像（SQLite→Ledger+Sim）
            self._sync_to_json()
            # v4.5.9: 审计日志
            self._audit_trade(stock_code, action, price, quantity, True, reason)

            # P0-5: 更新峰值权益（用于正确计算回撤）
            self._update_peak_equity()

            return {"success": True, "trade": {
                "order_id": order_id,
                "trade_id": trade_id,
                "timestamp": now, "market": market, "stock_code": stock_code,
                "action": action, "price": exec_price, "quantity": quantity,
                "amount": amount, "reason": reason,
                "avg_volume": avg_volume,
                "volume_ratio_pct": volume_ratio_pct,
                "filled_quantity": quantity,
                "liquidity_source": liquidity_source,
            }}

        except Exception as e:
            self._notify_reconciliation(order_id, success=False, error=str(e))
            self._record_execution(stock_code or "unknown", action or "UNKNOWN", False, error=str(e))
            # v4.5.3d: 熔断器更新 (交易失败)
            if self._cb:
                self._cb.record_trade_result(False)
            return {"success": False, "error": f"交易执行异常: {e}"}

    def _check_volume_limit(self, stock_code: str, price: float, quantity: int) -> tuple:
        """v4.5.12: 流动性约束 — 单笔成交量不超过日成交量10%
        Returns: (pass, msg, volume_ratio_pct, filled_qty, avg_volume, liquidity_source)
        """
        try:
            import requests, os
            MAIRUI_LICENCE = os.environ.get("MAIRUI_LICENCE", "")
            if not MAIRUI_LICENCE:
                return True, "", 0.0, quantity, 0.0, "mairui_licence_missing_skip"
            resp = requests.get(
                f"https://api.mairuiapi.com/hsrl/ssjy/{stock_code}/{MAIRUI_LICENCE}",
                timeout=5
            )
            if resp.status_code != 200:
                return True, "", 0.0, quantity, 0.0, "mairui_realtime_unavailable"
            d = resp.json()
            vol = float(d.get("volume", 0) or d.get("cjl", 0) or 0)
            if vol <= 0:
                return True, "", 0.0, quantity, 0.0, "mairui_realtime_empty"
            trade_amount = price * quantity
            daily_amount = price * vol
            if daily_amount <= 0:
                return True, "", 0.0, quantity, vol, "mairui_realtime_volume"
            ratio = trade_amount / daily_amount
            if ratio > self.MAX_VOLUME_RATIO:
                # v4.5.12 fix: 部分成交 — 计算实际可成交量，剩余加入待成交队列
                max_qty = int(self.MAX_VOLUME_RATIO * daily_amount / price)
                max_qty = (max_qty // 100) * 100
                if max_qty >= 100:
                    return True, "", ratio, max_qty, vol, "mairui_realtime_volume"  # 部分成交
                return False, (
                    f"流动性约束: 交易金额({trade_amount:.0f})超过日成交{ratio:.1%} "
                    f"(上限{self.MAX_VOLUME_RATIO:.0%})"), ratio, 0, vol, "mairui_realtime_volume"
            return True, "", ratio, quantity, vol, "mairui_realtime_volume"  # 全部成交
        except Exception:
            return True, "", 0.0, quantity, 0.0, "liquidity_check_error_skip"

    @staticmethod
    def _validate_lot_size(stock_code: str, quantity: int, action: str) -> tuple:
        """验证A股交易数量是否符合最小单位和整数倍规则"""
        if action != "BUY":
            return True, ""
        code_prefix = stock_code[:3] if len(stock_code) >= 3 else ""
        is_kcb = code_prefix in ("688", "689")
        min_lot = 200 if is_kcb else 100
        if quantity < min_lot:
            suffix = "科创板最少200股起买" if is_kcb else "最少100股起买"
            return False, f"A股买入数量违规: {quantity}<{min_lot}股 ({suffix})"
        if not is_kcb and quantity % 100 != 0:
            return False, f"A股买入数量违规: {quantity}不是100的整数倍 (主板/创业板需100股整数倍)"
        return True, ""


    def _sync_to_json(self):
        """将SQLite权威数据同步到JSON镜像文件
        
        每次 execute_trade 成功后自动调用，确保:
        - paper_trading_ledger.json (morning_decision/daily_iteration/health_check/sync_bitable消费)
        - simulation_state.json (simulation模块消费)
        与SQLite保持一致
        
        test_mode下跳过同步, 避免测试数据污染生产文件
        """
        if self._test_mode:
            return
        try:
            with self._get_conn() as conn:
                cur = conn.execute("SELECT value FROM ledger WHERE key='current_cash'")
                row = cur.fetchone()
                cash = float(row["value"]) if row else 0.0
                
                cur = conn.execute("SELECT value FROM ledger WHERE key='initial_capital'")
                row = cur.fetchone()
                initial = float(row["value"]) if row else 0.0
                
                pos_rows = conn.execute(
                    "SELECT stock_code, quantity, avg_cost, current_price, market "
                    "FROM positions WHERE quantity > 0"
                ).fetchall()
                
                trade_rows = conn.execute(
                    "SELECT id, timestamp, market, stock_code, action, price, quantity, "
                    "amount, reason, commission, stamp_tax, total_fee, volume_ratio_pct, "
                    "is_valid_for_metrics, quality_flag "
                    "FROM trade_history ORDER BY id"
                ).fetchall()
            
            # === 1. paper_trading_ledger.json ===
            ledger_positions = []
            for p in pos_rows:
                mv = p["quantity"] * p["current_price"]
                ledger_positions.append({
                    "code": p["stock_code"],
                    "shares": p["quantity"],
                    "quantity": p["quantity"],  # P1-5: 双写 quantity 字段，兼容 risk_manager 读取
                    "avg_price": round(p["avg_cost"], 2),
                    "current_price": round(p["current_price"], 2),
                    "market_value": round(mv, 2),
                })
            
            total_mv = sum(lp["market_value"] for lp in ledger_positions)
            total_value = cash + total_mv
            
            ledger_trades = []
            for t in trade_rows:
                ledger_trades.append({
                    "id": t["id"], "timestamp": t["timestamp"], "market": t["market"],
                    "symbol": t["stock_code"], "action": t["action"],
                    "price": t["price"], "quantity": t["quantity"],
                    "amount": t["amount"], "reason": t["reason"],
                    "commission": t["commission"], "stamp_tax": t["stamp_tax"],
                    "total_fee": t["total_fee"],
                    "is_valid_for_metrics": t["is_valid_for_metrics"],
                    "quality_flag": t["quality_flag"],
                })
            
            ledger_data = {
                "version": "v3.2.0-sqlite",
                "initial_capital": initial,
                "current_cash": round(cash, 2),
                "total_value": round(total_value, 2),
                "positions": ledger_positions,
                "trade_history": ledger_trades,
                "performance_metrics": {
                    "total_return": round((total_value / initial - 1) * 100, 2) if initial > 0 else 0,
                    "total_pnl": round(total_value - initial, 2),
                    "position_count": len(ledger_positions),
                    "cash_ratio": round(cash / total_value * 100, 2) if total_value > 0 else 0,
                }
            }
            
            # 原子写入: 先写临时文件再rename
            import tempfile
            ledger_dir = os.path.dirname(LEDGER_JSON_PATH)
            fd, tmp_path = tempfile.mkstemp(dir=ledger_dir, suffix=".json")
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(ledger_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, LEDGER_JSON_PATH)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            
            # === 2. simulation_state.json ===
            sim_positions = []
            for p in pos_rows:
                sim_positions.append({
                    "symbol": p["stock_code"],
                    "quantity": p["quantity"],
                    "avg_cost": round(p["avg_cost"], 2),
                    "current_price": round(p["current_price"], 2),
                    "market": p["market"],
                })
            
            sim_data = {
                "version": "1.0",
                "initial_capital": initial,
                "current_cash": round(cash, 2),
                "total_value": round(total_value, 2),
                "positions": sim_positions,
                "last_updated": datetime.now().isoformat(),
            }
            
            fd, tmp_path = tempfile.mkstemp(dir=ledger_dir, suffix=".json")
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(sim_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, SIM_STATE_PATH)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            # === 3. simulation_portfolio.json (PortfolioManager格式) ===
            # 从SQLite权威源重写，确保与SQLite/Ledger/SimState四系统一致
            try:
                # 读取现有文件以保留name等字段
                existing_portfolio = {}
                if os.path.exists(SIM_PORTFOLIO_PATH):
                    try:
                        with open(SIM_PORTFOLIO_PATH) as ef:
                            existing_portfolio = json.load(ef)
                    except Exception:
                        pass
                existing_positions = existing_portfolio.get('positions', {})

                sp_positions = {}
                for p in pos_rows:
                    code = p['stock_code']
                    qty = p['quantity']
                    avg = round(p['avg_cost'], 2)
                    cur = round(p['current_price'], 2)
                    cost = round(qty * avg, 2)
                    # 保留已有的name字段，如无则从stock_pool读取
                    name = existing_positions.get(code, {}).get('name', '')
                    if not name:
                        # Lazy-load pool name map as class cache
                        if not hasattr(PaperTrader, '_pool_name_map'):
                            try:
                                import yaml as _yaml
                                _pool_path = os.path.normpath(os.path.join(os.path.dirname(DB_PATH), '..', 'config', 'master_stock_pool.yaml'))
                                if os.path.exists(_pool_path):
                                    with open(_pool_path) as _pf:
                                        _pool = _yaml.safe_load(_pf)
                                    PaperTrader._pool_name_map = {}
                                    for _tier, _stocks in _pool.items():
                                        for _s in _stocks:
                                            if _s.get('symbol'):
                                                PaperTrader._pool_name_map[_s['symbol']] = _s.get('name', '')
                            except Exception:
                                PaperTrader._pool_name_map = {}
                        name = getattr(PaperTrader, '_pool_name_map', {}).get(code, '')
                    market = p['market'] or 'A'
                    # 计算holding_days: 从trade_history查找首次买入日期
                    holding_days = 0
                    try:
                        buy_row = conn.execute(
                            "SELECT MIN(timestamp) as first_buy FROM trade_history "
                            "WHERE stock_code=? AND action='BUY'", (code,)
                        ).fetchone()
                        if buy_row and buy_row['first_buy']:
                            from datetime import datetime as _dt
                            buy_date = _dt.fromisoformat(buy_row['first_buy'].split('T')[0])
                            holding_days = (_dt.now() - buy_date).days
                    except Exception:
                        pass
                    
                    sp_positions[code] = {
                        'market': market,
                        'shares': qty,
                        'avg_price': avg,
                        'current_price': cur,
                        'cost': cost,
                        'name': name,
                        # PortfolioManager兼容字段
                        'quantity': qty,
                        'avg_cost': avg,
                        'holding_days': holding_days,
                    }

                # 保留trade_history和daily_stats
                total_mv = sum(qty * cur for qty, cur in [(p['quantity'], p['current_price']) for p in pos_rows])
                portfolio_value = cash + total_mv
                sp_data = {
                    'initial_capital': initial,
                    'current_capital': round(portfolio_value, 2),
                    'cash': round(cash, 2),
                    'positions': sp_positions,
                    'trade_history': existing_portfolio.get('trade_history', []),
                    'daily_stats': existing_portfolio.get('daily_stats', []),
                    'last_update': datetime.now().isoformat(),
                }

                fd, tmp_path = tempfile.mkstemp(dir=ledger_dir, suffix=".json")
                try:
                    with os.fdopen(fd, 'w') as f:
                        json.dump(sp_data, f, ensure_ascii=False, indent=2)
                    os.replace(tmp_path, SIM_PORTFOLIO_PATH)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise
            except Exception as sp_e:
                logging.getLogger(__name__).warning(f"simulation_portfolio.json同步失败(非致命): {sp_e}")

        except Exception as e:
            # 同步失败不影响交易主流程
            logging.getLogger(__name__).warning(f"JSON同步失败(非致命): {e}")

    def _record_execution(self, symbol: str, action: str, success: bool, error: str = ""):
        """记录执行到系统监控（静默失败）"""
        try:
            from execution_engine.system_monitor import get_system_monitor
            get_system_monitor().record_signal_result(symbol, action, success, error)
        except Exception:
            pass

    def _notify_reconciliation(self, order_id: str, success: bool,
                                trade_id: int = 0, exec_price: float = 0.0,
                                exec_qty: int = 0, error: str = ""):
        """通知对账引擎（静默失败不影响主流程）"""
        if not order_id:
            return
        try:
            from execution_engine.reconciliation import get_reconciliation_engine
            engine = get_reconciliation_engine()
            if success:
                engine.mark_executed(order_id, trade_id, exec_price, exec_qty)
            else:
                engine.mark_failed(order_id, error)
        except Exception as e:
            # 对账失败不影响交易主流程
            logger = logging.getLogger(__name__)
            logger.debug(f"对账通知失败（可忽略）: {e}")

    def update_position_prices(self, price_dict: Dict[str, float]):
        """更新持仓当前价格"""
        with self._get_conn() as conn:
            for code, price in price_dict.items():
                conn.execute(
                    "UPDATE positions SET current_price=? WHERE stock_code=?",
                    (price, code)
                )
        self._update_metrics()

    # ═══════════════════ 止损监控与自动执行 ═══════════════════

    def check_stop_losses(self, price_dict: Dict[str, float] = None) -> Dict:
        """扫描所有持仓，检查止损条件并自动执行

        在每个交易周期调用此方法:
        1. 获取所有持仓及成本价
        2. 用当前市价计算浮动盈亏
        3. 按板块差异化止损线判断是否触发
        4. 触发时自动执行 SELL 并记录原因

        Args:
            price_dict: {stock_code: current_price} 可选，不传则使用DB中current_price

        Returns:
            {"triggered": int, "executed": list, "errors": list}
        """
        triggered_count = 0
        executed = []
        errors = []

        # 先刷新价格
        if price_dict:
            self.update_position_prices(price_dict)

        with self._get_conn() as conn:
            positions = conn.execute("SELECT * FROM positions").fetchall()
            cash = float(
                conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"]
            )

        # 总浮动盈亏 + 总资产
        total_equity = cash + sum(p["quantity"] * p["current_price"] for p in positions)
        peak_equity = self._get_float("peak_equity", total_equity)
        portfolio_pct = (total_equity - peak_equity) / peak_equity if peak_equity > 0 else 0

        # ── 组合级别止损（portfolio-level stop-loss）──
        # 从config读取组合止损阈值
        config = _load_stop_loss_config()
        # 净值从peak回撤→硬止损
        sl_config = config.get('stop_loss', {}) if isinstance(config, dict) else {}
        portfolio_total = sl_config.get('portfolio_total', -0.20)
        portfolio_daily = sl_config.get('portfolio_daily', -0.05)

        if portfolio_pct <= portfolio_total:
            # 触发组合总止损: 清仓所有持仓
            executed_info = f"组合总止损: 回撤{portfolio_pct:.2%}"
            for p in positions:
                try:
                    qty = p["quantity"]
                    if qty <= 0:
                        continue
                    price = p["current_price"]
                    result = self.execute_trade(
                        market=p["market"], stock_code=p["stock_code"], action="SELL",
                        price=price, quantity=qty, reason="组合总止损触发"
                    )
                    if result.get("success"):
                        executed.append({"code": p["stock_code"], "qty": qty, "price": price,
                                        "reason": "组合总止损"})
                        triggered_count += 1
                except Exception as e:
                    errors.append({"code": p["stock_code"], "error": str(e)})
            return {
                "triggered": triggered_count,
                "executed": executed,
                "errors": errors,
                "portfolio_pnl_pct": round(portfolio_pct * 100, 2),
                "message": executed_info,
            }

        # ── 逐仓止损（per-position stop-loss）──
        for p in positions:
            code = p["stock_code"]
            qty = p["quantity"]
            cost = p["avg_cost"]
            current = p["current_price"]
            pnl_pct = (current - cost) / cost if cost > 0 else 0

            if qty <= 0 or cost <= 0:
                continue

            board = _get_board_type(code)
            stop_pct = get_stop_loss_pct(code)

            # 动态阈值引擎: 从adaptive_params读取动态阈值 (v4.5.5+)
            try:
                import yaml as _yaml
                _ap_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                       "config", "adaptive_params.yaml")
                if os.path.exists(_ap_path):
                    with open(_ap_path) as _f:
                        _ap = _yaml.safe_load(_f)
                    _sl = _ap.get("signals", {}).get("stop_loss", {})
                    _threshold = _sl.get("base_threshold", {}).get(board, None)
                    if _threshold is not None:
                        stop_pct = float(_threshold)
                        # v6: 方向精度调节
                        _acc = _sl.get("accuracy_adjust", 0.2)
                        _adj = _sl.get("accuracy_max_boost", 0.5)
                        stop_pct = stop_pct * (1 + _acc * _adj)
            except Exception:
                pass  # 使用默认止损线

            # 检查是否触发止损
            if pnl_pct <= stop_pct:
                try:
                    result = self.execute_trade(
                        market=p["market"], stock_code=code, action="SELL",
                        price=current, quantity=qty,
                        reason=f"止损触发: 浮动{pnl_pct:.2%}<={stop_pct:.2%}"
                    )
                    if result.get("success"):
                        print(f"  🔴 {code}: 止损执行 {qty}股 @{current:.2f} ({pnl_pct:.2%})")
                        executed.append({"code": code, "qty": qty, "price": current,
                                        "reason": f"止损"})
                        triggered_count += 1
                    else:
                        errors.append({"code": code, "error": result.get("error", "执行失败")})
                except Exception as e:
                    errors.append({"code": code, "error": str(e)})

        return {
            "triggered": triggered_count,
            "executed": executed,
            "errors": errors,
            "portfolio_pnl_pct": round(portfolio_pct * 100, 2),
        }

    def _update_metrics(self):
        """更新绩效指标"""
        try:
            with self._get_conn() as conn:
                cash = float(
                    conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"]
                )
                initial = float(
                    conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()["value"]
                )
                pos_rows = conn.execute("SELECT * FROM positions").fetchall()
                trade_rows = conn.execute("SELECT * FROM trade_history ORDER BY timestamp").fetchall()

                if initial <= 0:
                    return

                total_equity = cash + sum(p["quantity"] * p["current_price"] for p in pos_rows)
                total_return = (total_equity - initial) / initial if initial > 0 else 0

                # 日内收益
                if len(trade_rows) >= 2:
                    last = trade_rows[-1]
                    prev = trade_rows[-2]
                    try:
                        last_dict = dict(last)
                        prev_dict = dict(prev)
                        last_equity = float(last_dict.get("equity_after", total_equity))
                        prev_equity = float(prev_dict.get("equity_after", 0))
                        if prev_equity > 0:
                            intraday_return = (last_equity - prev_equity) / prev_equity
                        else:
                            intraday_return = 0.0
                    except (ValueError, TypeError, AttributeError):
                        intraday_return = 0.0
                else:
                    intraday_return = 0.0

                # 最新权益
                conn.execute(
                    "INSERT OR REPLACE INTO performance_metrics (key, value) VALUES ('current_equity', ?)",
                    (str(total_equity),)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO performance_metrics (key, value) VALUES ('total_return', ?)",
                    (str(total_return),)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO performance_metrics (key, value) VALUES ('total_return_pct', ?)",
                    (str(total_return * 100),)
                )
                conn.execute(
                    "INSERT OR REPLACE INTO performance_metrics (key, value) VALUES ('intraday_return', ?)",
                    (str(intraday_return),)
                )

                # 更新胜率
                wins = 0
                total_trades = 0
                for r in trade_rows:
                    try:
                        action = r["action"]
                        if action == "SELL":
                            total_trades += 1
                            pl = float(r["pnl"] if "pnl" in r.keys() else 0)
                            if pl > 0:
                                wins += 1
                    except (KeyError, ValueError, TypeError):
                        pass
                win_rate = wins / total_trades if total_trades > 0 else 0.0
                conn.execute(
                    "INSERT OR REPLACE INTO performance_metrics (key, value) VALUES ('win_rate', ?)",
                    (str(win_rate),)
                )

            # 更新峰值权益 (独立连接)
            self._update_peak_equity()

        except Exception as e:
            # 绩效指标更新失败不影响主流程
            import logging
            logging.getLogger(__name__).warning(f"绩效指标更新失败: {e}")

    def get_portfolio_summary(self) -> Dict:
        """获取投资组合摘要"""
        with self._get_conn() as conn:
            initial = float(conn.execute("SELECT value FROM ledger WHERE key='initial_capital'").fetchone()["value"])
            cash = float(conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()["value"])
            pos_rows = conn.execute("SELECT * FROM positions").fetchall()
            trade_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM trade_history"
            ).fetchone()["cnt"]
            perf_rows = conn.execute("SELECT * FROM performance_metrics").fetchall()

        perf = {}
        for r in perf_rows:
            k, v = r["key"], r["value"]
            try:
                perf[k] = float(v)
            except (ValueError, TypeError):
                perf[k] = v  # 保留原始值（如时间戳、状态字符串等）
        positions_value = sum(p["quantity"] * p["current_price"] for p in pos_rows)
        total_value = cash + positions_value

        # 过滤港股持仓
        filtered = []
        for p in pos_rows:
            if self.DISABLE_HK_MARKET and p["market"] == "HK":
                continue
            filtered.append(dict(p))

        if self.DISABLE_HK_MARKET:
            pos_value_a = sum(p["quantity"] * p["current_price"] for p in filtered)
            total_value_a = cash + pos_value_a
            return_pct = (total_value_a - initial) / initial * 100
        else:
            return_pct = (total_value - initial) / initial * 100

        return {
            "initial_capital": initial,
            "current_cash": cash,
            "positions_value": positions_value,
            "total_value": total_value,
            "total_return_pct": perf.get("total_return_pct", return_pct),
            "win_rate": perf.get("win_rate", 0.0),
            "positions": filtered,
            "trade_count": trade_count,
        }


# ── v4.6.x: 模块级止损函数（跨模块复用）──
_ATR_CACHE: Dict[str, Dict] = {}
_ATR_CACHE_TTL = 3600
_CONFIG_CACHE: Dict = {}
_CONFIG_CACHE_TIME = 0.0
_ATR_FAIL_ALERTED = False  # P4: ATR降级仅首次告警


def _get_board_type(stock_code: str) -> str:
    """根据股票代码前缀判断板块类型 (P1-6 fix: 支持ST检测)"""
    code = stock_code.lstrip("'\"")
    try:
        from config.constants import is_st_stock
        if is_st_stock(code):
            return "st"
    except ImportError:
        pass
    if code.startswith("688"):
        return "star"       # 科创板 20%涨跌幅
    elif code.startswith("300") or code.startswith("301"):
        return "gem"        # 创业板 20%涨跌幅
    elif code.startswith("8"):
        return "bse"        # 北交所 30%涨跌幅
    else:
        return "main_board" # 主板 10%涨跌幅


def _load_stop_loss_config() -> dict:
    """加载止损配置(带缓存, 每60秒刷新)"""
    global _CONFIG_CACHE, _CONFIG_CACHE_TIME
    now = time.time()
    if _CONFIG_CACHE and now - _CONFIG_CACHE_TIME < 60:
        return _CONFIG_CACHE
    try:
        import yaml
        ap_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "config", "adaptive_params.yaml")
        if os.path.exists(ap_path):
            with open(ap_path) as f:
                ap = yaml.safe_load(f)
            sl = ap.get('trading', {}).get('risk_management', {}).get('stop_loss', {})
            # 同时读取黑天鹅状态
            risk = ap.get('risk', {})
            sl['_bs_active'] = risk.get('black_swan_active', False)
            _CONFIG_CACHE = sl
            _CONFIG_CACHE_TIME = now
            return sl
    except Exception:
        pass
    return {}


def _get_atr_pct(stock_code: str) -> float:
    """获取个股ATR(20)百分比, 用于动态止损
    使用麦蕊K线数据(数据源优先级: 麦蕊 → 降级None)
    带1小时缓存
    """
    global _ATR_FAIL_ALERTED
    code = stock_code.lstrip("'\"")
    # 缓存命中
    now = time.time()
    cached = _ATR_CACHE.get(code)
    if cached and now - cached['time'] < _ATR_CACHE_TTL:
        return cached['value']
    try:
        from config.mairui_api_config import get_kline_history
        klines = get_kline_history(code, period="d", adjust="f", limit=25)
        if klines and len(klines) >= 2:
            closes = []
            for k in klines[-22:]:
                c = k.get('close') or k.get('close_price') or k.get('c')
                h = k.get('high') or k.get('h')
                lv = k.get('low') or k.get('l')
                if c is not None and h is not None and lv is not None:
                    closes.append({'h': float(h), 'l': float(lv), 'c': float(c)})
            if len(closes) >= 21:
                trs = []
                for i in range(1, len(closes)):
                    hl = closes[i]['h'] - closes[i]['l']
                    hcp = abs(closes[i]['h'] - closes[i-1]['c'])
                    lcp = abs(closes[i]['l'] - closes[i-1]['c'])
                    trs.append(max(hl, hcp, lcp))
                if trs:
                    atr = sum(trs) / len(trs)
                    price = closes[-1]['c']
                    result = atr / price if price > 0 else 0.0
                    # v4.6.x: 同时计算MA20/MA50存入缓存（用于智能移动止盈趋势确认）
                    close_prices = [x['c'] for x in closes]
                    ma20 = sum(close_prices[-20:]) / min(20, len(close_prices)) if close_prices else None
                    ma50 = sum(close_prices[-50:]) / min(50, len(close_prices)) if len(close_prices) >= 20 else None
                    _ATR_CACHE[code] = {'value': result, 'ma20': ma20, 'ma50': ma50, 'price': price, 'time': now}
                    return result
    except Exception:
        # P4: ATR降级告警（仅首次）
        if not _ATR_FAIL_ALERTED:
            _ATR_FAIL_ALERTED = True
            try:
                from common.feishu_utils import send_markdown
                send_markdown(
                    title="⚠️ ATR动态止损降级",
                    content=f"麦蕊K线API不可用, ATR动态止损已回退为固定板块止损。\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n影响: 所有持仓使用固定止损线。"
                )
            except Exception:
                pass
    return None


def _get_ma_prices(stock_code: str):
    """v4.6.x: 获取MA20/MA50价格，复用ATR K线缓存
    Returns: (ma20: float|None, ma50: float|None, latest_price: float|None)
    """
    # 先尝试从缓存读取
    cached = _ATR_CACHE.get(stock_code.lstrip("'\""))
    if cached and time.time() - cached.get('time', 0) < _ATR_CACHE_TTL:
        return cached.get('ma20'), cached.get('ma50'), cached.get('price')
    # 缓存未命中 → 触发ATR计算（附带MA缓存）
    _get_atr_pct(stock_code)
    cached = _ATR_CACHE.get(stock_code.lstrip("'\""))
    if cached:
        return cached.get('ma20'), cached.get('ma50'), cached.get('price')
    return None, None, None


def get_stop_loss_pct(stock_code: str) -> float:
    """获取ATR动态止损线(v4.6.x: 跨模块统一入口), fallback to 固定比例"""
    board = _get_board_type(stock_code)
    atr_config = _load_stop_loss_config()

    # ATR动态止损
    use_atr = atr_config.get('atr_based', True)
    if use_atr:
        atr_pct = _get_atr_pct(stock_code)
        if atr_pct is not None and atr_pct > 0:
            atr_mult = atr_config.get('atr_multiplier', {}).get(board, 2.5)
            atr_min = atr_config.get('atr_min_stop', -0.04)
            atr_max = atr_config.get('atr_max_stop', -0.15)

            # 黑天鹅收紧: 乘数打折
            tighten = atr_config.get('black_swan_tighten', False)
            bs_active = atr_config.get('_bs_active', False)
            if tighten and bs_active:
                adjust = atr_config.get('black_swan_atr_adjust', 0.8)
                atr_mult = atr_mult * adjust

            stop_pct = -(atr_pct * atr_mult)
            # 限幅: atr_max是上限(最严格), atr_min是下限(最宽松)
            stop_pct = max(stop_pct, atr_max)   # 不低于-15%
            stop_pct = min(stop_pct, atr_min)   # 不高于-4%
            return round(stop_pct, 4)

    # fallback: 固定比例(从config读取或硬编码)
    fixed = atr_config.get('per_market', {}).get(board, None)
    if fixed is not None:
        return float(fixed)
    return {
        "main_board": -0.08,
        "gem": -0.15,
        "star": -0.15,
        "bse": -0.22,
        "st": -0.05,   # P1-6: ST股5%涨跌幅→止损5%
    }.get(board, -0.08)

    print(json.dumps(trader.get_portfolio_summary(), indent=2, ensure_ascii=False))



if __name__ == "__main__":
    trader = PaperTrader()
    print(json.dumps(trader.get_portfolio_summary(), indent=2, ensure_ascii=False))
