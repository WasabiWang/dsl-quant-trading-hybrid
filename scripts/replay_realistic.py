#!/usr/bin/env python3
"""
scripts/replay_realistic.py — 交易现实回放器 (Phase 1.3)

按真实 A 股交易约束回放组合交易，为 B0/B1/B2/C1/C2 各赛道提供**统一、可执行的
成交与净值口径**。

与现有回测的核心口径差异（详见 audits/phase1-回放器设计.md）：
  1. 成交价 = 信号日 T 收盘后产生信号 → **T+1 开盘价或 VWAP 代理**成交；
     绝不使用 T 日收盘价或前收盘价。
  2. 涨跌停不可成交：涨停买不到、跌停卖不出（一字板一并拦截）。
  3. 停牌(零成交)不可成交：顺延 N 个交易日，超过则作废，均记录。
  4. T+1：当日买入不可当日卖出。
  5. 费用：佣金(万2.5, 5元保底) + 印花税(卖出千1) + 过户费(万0.1)，
     全部从 config/constants.py 读取（含过户费 TRANSFER_FEE_RATE，无兜底）。
  6. 逐日盯市 NAV 序列（现金 + 持仓市值），供回撤/波动/E6 使用。
  7. 成交失败/部分成交（成交量约束）处理与记录。
  8. 输出逐笔明细（滑点 bp = 成交价 vs 决策参考价的偏差）与逐日 NAV。

铁律：只新建本文件，不修改任何现有文件；费率不硬编码。

用法：
    .venv/bin/python scripts/replay_realistic.py                     # 默认 3 只标的烟测
    .venv/bin/python scripts/replay_realistic.py --mode vwap_proxy   # 换 VWAP 代理成交
    .venv/bin/python scripts/replay_realistic.py --capital 5000000 --interval 60
"""
from __future__ import annotations

import os
import re
import sys
import json
import glob
import argparse
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from config import constants as C
from config.constants import get_market_type, get_min_trade_unit, LIMIT_RATES
# FX-4: 缓存来源快照字段定义，与 common/cache.py 共用同一套口径
from common.cache import (CACHE_SCHEMA_VERSION as CACHE_META_SCHEMA_VERSION,
                          REQUIRED_META_FIELDS as CACHE_META_REQUIRED_FIELDS)

# ---------------------------------------------------------------------------
# 费率：一律从 config/constants.py 读取，不得硬编码
# ---------------------------------------------------------------------------
COMMISSION_RATE = C.COMMISSION_RATE          # 佣金（万2.5）
STAMP_TAX_RATE = C.STAMP_TAX_RATE            # 印花税（卖出千1）
MIN_COMMISSION = C.MIN_COMMISSION            # 最低佣金（5 元保底）
# 过户费：已统一到 config/constants.py（万0.1，双向）。
# v4.5.13 fix: 移除 getattr 兜底，改为直接读取，缺常量即报错（防止静默口径分叉）。
TRANSFER_FEE_RATE = C.TRANSFER_FEE_RATE
MIN_VOLUME_RATIO = getattr(C, "MIN_VOLUME_RATIO_FOR_FILL", 3.0)


@dataclass
class ReplayConfig:
    initial_capital: float = 1_000_000.0
    exec_mode: str = "open"                # "open" | "vwap_proxy"
    extra_slippage_bps: float = 0.0        # 额外执行滑点(默认0，开盘价/VWAP 本身即真实成交价)
    suspend_max_defer_days: int = 3        # 停牌顺延最大交易日数，超过作废
    enforce_t_plus_1: bool = True
    enforce_limit: bool = True
    enforce_volume: bool = True


@dataclass
class OrderIntent:
    """由策略在信号日 T 收盘后产生的一笔交易意图。"""
    symbol: str
    side: str            # "BUY" | "SELL"
    quantity: int        # 目标股数（引擎会做整手/成交量/资金修正）
    ref_price: float     # 决策参考价 = 信号日 T 收盘价（用于计算滑点）
    signal_date: Any
    reason: str = ""


def _symbol_from_path(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"(\d{6})", base)
    return m.group(1) if m else base


class ReplayEngine:
    """统一成交与净值口径的回放引擎。"""

    def __init__(self, config: ReplayConfig, data: Dict[str, pd.DataFrame]):
        self.cfg = config
        self.data = data
        self.calendar = sorted(set().union(*[set(df.index) for df in data.values()]))
        self.cash = float(config.initial_capital)
        self.positions: Dict[str, int] = {}       # symbol -> 持仓股数
        self._buy_dates: Dict[str, Any] = {}      # symbol -> 最近一次买入成交日
        self._last_close: Dict[str, float] = {}   # symbol -> 最近已知收盘价
        self.trades: List[Dict] = []              # 逐笔明细
        self.daily_nav: List[Dict] = []           # 逐日 NAV
        self.pending: List[Dict] = []             # [{order, defer_left}]

    # --------------------------- 工具 ---------------------------
    def _row_info(self, symbol: str, d) -> Optional[Dict]:
        df = self.data.get(symbol)
        if df is None or d not in df.index:
            return None
        idx = df.index.get_loc(d)
        row = df.iloc[idx]
        prev_close = float(df.iloc[idx - 1]["close"]) if idx > 0 else float(row["close"])
        market = get_market_type(symbol)
        rate = LIMIT_RATES.get(market, 0.10)
        limit_up = round(prev_close * (1 + rate), 2)
        limit_down = round(prev_close * (1 - rate), 2)
        o = float(row["open"]); h = float(row["high"])
        l = float(row["low"]); c = float(row["close"]); v = float(row["volume"])
        avg_vol = float(df["volume"].iloc[max(0, idx - 20):idx].mean()) if idx > 0 else v
        return dict(
            idx=idx, prev_close=prev_close, market=market, rate=rate,
            limit_up=limit_up, limit_down=limit_down,
            open=o, high=h, low=l, close=c, volume=v, avg_vol=avg_vol,
            suspended=(v <= 0),
        )

    def _exec_base_price(self, info: Dict) -> float:
        if self.cfg.exec_mode == "vwap_proxy":
            # VWAP 代理：日内典型价 (H+L+C)/3（无分时数据下的近似，见审计文档局限）
            return (info["high"] + info["low"] + info["close"]) / 3.0
        return info["open"]

    def _limit_blocked(self, side: str, info: Dict) -> bool:
        if not self.cfg.enforce_limit:
            return False
        if side == "BUY":
            # 开盘即封涨停（含一字板）→ 买不到
            return info["open"] >= info["limit_up"] - 1e-9
        # 开盘即封跌停 → 卖不掉
        return info["open"] <= info["limit_down"] + 1e-9

    @staticmethod
    def _fees(side: str, price: float, qty: int) -> Dict[str, float]:
        value = price * qty
        commission = max(value * COMMISSION_RATE, MIN_COMMISSION)
        stamp = value * STAMP_TAX_RATE if side == "SELL" else 0.0
        transfer = value * TRANSFER_FEE_RATE
        return dict(
            value=round(value, 2),
            commission=round(commission, 2),
            stamp_tax=round(stamp, 2),
            transfer_fee=round(transfer, 2),
            total=round(commission + stamp + transfer, 2),
        )

    # --------------------------- 成交 ---------------------------
    def _try_fill(self, order: OrderIntent, d) -> None:
        symbol, side = order.symbol, order.side
        info = self._row_info(symbol, d)
        if info is None:
            return  # 无当日行情 → 由 run() 统一顺延
        if info["suspended"]:
            return  # 停牌 → 顺延，run() 处理
        if self._limit_blocked(side, info):
            self._record(order, d, side, symbol, qty=0, status="REJECTED",
                         note="涨停无法买入" if side == "BUY" else "跌停无法卖出")
            return
        if side == "SELL" and self.cfg.enforce_t_plus_1 and self._buy_dates.get(symbol) == d:
            self._record(order, d, side, symbol, qty=0, status="REJECTED",
                         note="T+1：当日买入不可当日卖出")
            return

        cur = self.positions.get(symbol, 0)
        if side == "SELL" and cur <= 0:
            self._record(order, d, side, symbol, qty=0, status="REJECTED", note="无持仓可卖")
            return

        lot = get_min_trade_unit(symbol)

        # 1) 目标股数 → 整手对齐（得到"有效请求量"；正常整手对齐不算部分成交）
        qty = order.quantity
        if side == "BUY":
            qty = (qty // lot) * lot
        else:
            qty = min(qty, cur)
            if qty < cur:                      # 部分卖出 → 向下取整手
                qty = (qty // lot) * lot
            # 全部清仓 → 允许卖出零股
        if qty <= 0:
            self._record(order, d, side, symbol, qty=0, status="REJECTED",
                         note="不足最小交易单位")
            return
        effective_request = qty

        # 2) 成交量约束（部分成交）：订单量 > 近20日均量 / MIN_VOLUME_RATIO 时封顶
        notes = []
        if self.cfg.enforce_volume and info["avg_vol"] > 0:
            max_shares = int(info["avg_vol"] / MIN_VOLUME_RATIO)
            if qty > max_shares:
                notes.append(f"成交量约束：{qty}→{max_shares}股")
                qty = max_shares
                if side == "BUY":
                    qty = (qty // lot) * lot
                elif qty < cur:
                    qty = (qty // lot) * lot
        if qty <= 0:
            self._record(order, d, side, symbol, qty=0, status="REJECTED",
                         note="成交量约束后不足1手")
            return

        # 3) 成交价
        base = self._exec_base_price(info)
        if side == "BUY":
            px = round(base * (1 + self.cfg.extra_slippage_bps / 10000.0), 4)
        else:
            px = round(base * (1 - self.cfg.extra_slippage_bps / 10000.0), 4)

        # 4) BUY 资金检查：逐步减到可负担整手（部分成交）
        if side == "BUY":
            while qty >= lot:
                f = self._fees("BUY", px, qty)
                if f["value"] + f["total"] <= self.cash + 1e-9:
                    break
                qty -= lot
            if qty < effective_request:
                notes.append("资金不足部分成交")
        if qty <= 0:
            self._record(order, d, side, symbol, qty=0, status="REJECTED", note="资金不足")
            return

        # 提交
        f = self._fees(side, px, qty)
        if side == "BUY":
            self.cash -= (f["value"] + f["total"])
            self.positions[symbol] = self.positions.get(symbol, 0) + qty
            self._buy_dates[symbol] = d
        else:
            self.cash += (f["value"] - f["total"])
            self.positions[symbol] = self.positions.get(symbol, 0) - qty
            if self.positions[symbol] <= 0:
                del self.positions[symbol]

        status = "FILLED" if qty == effective_request else "PARTIAL"
        self._record(order, d, side, symbol, qty=qty, px=px, status=status,
                     note="；".join(notes))

    def _record(self, order, d, side, symbol, qty, status, px: float = 0.0, note: str = ""):
        # 未成交(qty=0)无滑点可言，记 0.0；成交才有滑点 bp
        slippage_bp = 0.0
        if qty > 0 and order.ref_price:
            slippage_bp = round((px / order.ref_price - 1.0) * 10000.0, 2)
        f = self._fees(side, px, qty) if qty > 0 else dict(
            value=0, commission=0.0, stamp_tax=0.0, transfer_fee=0.0, total=0.0)
        self.trades.append(dict(
            signal_date=str(order.signal_date)[:10],
            exec_date=str(d)[:10],
            symbol=symbol, side=side, reason=order.reason,
            qty=qty, requested_qty=order.quantity,
            ref_price=round(order.ref_price, 4), exec_price=round(px, 4),
            slippage_bp=slippage_bp,
            status=status,
            fill_ratio=round(qty / max(order.quantity, 1), 4),
            value=f["value"], commission=f["commission"], stamp_tax=f["stamp_tax"],
            transfer_fee=f["transfer_fee"], fee_total=f["total"], note=note,
        ))

    # --------------------------- 主循环 ---------------------------
    def run(self, strategy: Callable) -> Dict:
        for d in self.calendar:
            # 1) 执行 pending（在 d 开盘成交）
            still_pending = []
            for item in self.pending:
                order: OrderIntent = item["order"]
                before = len(self.trades)
                self._try_fill(order, d)
                filled_now = len(self.trades) > before  # 本次是否产生了记录
                if not filled_now:
                    # 未成交：停牌或无当日行情 → 顺延
                    if item["defer_left"] > 1:
                        item["defer_left"] -= 1
                        still_pending.append(item)
                    else:
                        self._record(order, d, order.side, order.symbol, qty=0,
                                     status="CANCELLED", note=f"停牌/无行情顺延{self.cfg.suspend_max_defer_days}日未成交，作废")
            self.pending = still_pending

            # 2) 更新收盘价并盯市 NAV
            closes = {}
            for sym, df in self.data.items():
                if d in df.index:
                    c = float(df.loc[d, "close"])
                    closes[sym] = c
                    self._last_close[sym] = c
            pos_val = sum(
                sh * (closes.get(s, self._last_close.get(s, 0.0)))
                for s, sh in self.positions.items()
            )
            nav = self.cash + pos_val
            self.daily_nav.append(dict(
                date=str(d)[:10], nav=round(nav, 2), cash=round(self.cash, 2),
                position_value=round(pos_val, 2), n_positions=len(self.positions),
            ))

            # 3) T 收盘后生成新信号 → 次日开盘成交
            for o in strategy(d, closes, dict(self.positions), self.cash):
                self.pending.append({"order": o, "defer_left": self.cfg.suspend_max_defer_days})

        return self._summarize()

    # --------------------------- 汇总 ---------------------------
    def _summarize(self) -> Dict:
        navs = [x["nav"] for x in self.daily_nav]
        n = len(navs)
        total_ret = navs[-1] / navs[0] - 1 if n > 1 else 0.0
        years = n / C.BACKTEST_TRADING_DAYS
        annual = (1 + total_ret) ** (1 / years) - 1 if years > 0 and total_ret > -1 else 0.0

        peak = -np.inf
        mdd = 0.0
        for v in navs:
            peak = max(peak, v)
            if peak > 0:
                mdd = min(mdd, v / peak - 1)
        rets = pd.Series(navs).pct_change().dropna()
        vol = float(rets.std()) if len(rets) else 0.0
        sharpe = 0.0
        if vol > 0 and len(rets):
            rf_daily = C.BACKTEST_RISK_FREE_RATE / C.BACKTEST_TRADING_DAYS
            sharpe = float((rets.mean() - rf_daily) / vol * np.sqrt(C.BACKTEST_TRADING_DAYS))

        statuses: Dict[str, int] = {}
        for t in self.trades:
            statuses[t["status"]] = statuses.get(t["status"], 0) + 1

        return dict(
            config=dict(
                initial_capital=self.cfg.initial_capital,
                exec_mode=self.cfg.exec_mode,
                extra_slippage_bps=self.cfg.extra_slippage_bps,
                suspend_max_defer_days=self.cfg.suspend_max_defer_days,
                enforce_t_plus_1=self.cfg.enforce_t_plus_1,
                enforce_limit=self.cfg.enforce_limit,
                enforce_volume=self.cfg.enforce_volume,
            ),
            fees=dict(
                commission_rate=COMMISSION_RATE,
                stamp_tax_rate=STAMP_TAX_RATE,
                min_commission=MIN_COMMISSION,
                transfer_fee_rate=TRANSFER_FEE_RATE,
                transfer_fee_source="config/constants.py:TRANSFER_FEE_RATE",
            ),
            summary=dict(
                trading_days=n,
                final_nav=round(navs[-1], 2) if n else 0.0,
                total_return=round(total_ret, 6),
                annualized_return=round(annual, 6),
                max_drawdown=round(mdd, 6),
                daily_vol=round(vol, 6),
                sharpe=round(sharpe, 4),
                final_cash=round(self.cash, 2),
                final_positions={s: sh for s, sh in self.positions.items()},
                trade_status_count=statuses,
            ),
            trades=self.trades,
            daily_nav=self.daily_nav,
        )


# ---------------------------------------------------------------------------
# 示例策略：等权再平衡（每 interval 个交易日调仓一次）
# 仅用于烟测/演示；真实赛道用自己的策略生成 OrderIntent 列表即可。
# ---------------------------------------------------------------------------
def make_equal_weight_strategy(interval: int = 20) -> Callable:
    state = {"counter": 0}

    def strategy(signal_date, closes: Dict[str, float], positions: Dict[str, int], cash: float) -> List[OrderIntent]:
        state["counter"] += 1
        if state["counter"] == 1 or state["counter"] % interval == 1:
            pass
        else:
            return []
        if not closes:
            return []
        nav = cash + sum(sh * closes.get(s, 0.0) for s, sh in positions.items())
        target_value = nav / len(closes)
        orders: List[OrderIntent] = []
        for s, price in closes.items():
            cur = positions.get(s, 0)
            target_shares = int(target_value / price)
            diff = target_shares - cur
            if diff > 0:
                orders.append(OrderIntent(s, "BUY", diff, price, signal_date, "等权再平衡"))
            elif diff < 0:
                orders.append(OrderIntent(s, "SELL", -diff, price, signal_date, "等权再平衡"))
        return orders

    return strategy


def _read_cache_meta(csv_path: str) -> Optional[Dict[str, Any]]:
    """FX-4: 读取并校验 CSV 缓存 sidecar 来源快照 `<csv>.meta.json`

    校验 source / fetch_time / schema_version / adjust 四个字段。
    缺失或不合法时仅告警并返回 None（不阻断回放，也不静默篡改口径）。
    """
    mp = f"{csv_path}.meta.json"
    if not os.path.exists(mp):
        print(f"⚠️  缓存无来源快照: {csv_path} (缺少 {os.path.basename(mp)})")
        return None
    try:
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception as e:  # 损坏的元数据不得影响主流程
        print(f"⚠️  缓存来源快照解析失败 {mp}: {e}")
        return None
    missing = [f for f in CACHE_META_REQUIRED_FIELDS if f not in meta]
    if missing:
        print(f"⚠️  缓存来源快照缺字段 {missing}: {mp}")
        return None
    if meta.get("schema_version") != CACHE_META_SCHEMA_VERSION:
        print(f"⚠️  缓存来源快照 schema_version={meta.get('schema_version')} "
              f"(期望 {CACHE_META_SCHEMA_VERSION}): {mp}")
        return None
    print(f"✅ 缓存来源快照 {os.path.basename(csv_path)}: source={meta['source']} "
          f"fetch_time={meta['fetch_time']} adjust={meta['adjust']}")
    return meta


def load_data(paths: List[str]) -> Dict[str, pd.DataFrame]:
    """读取本地 CSV 行情缓存，并校验来源快照(FX-4)

    快照状态写入 df.attrs["cache_meta"/"cache_meta_status"]，最终随回放结果输出。
    """
    data = {}
    for p in paths:
        symbol = _symbol_from_path(p)
        df = pd.read_csv(p)
        df.columns = [c.lower() for c in df.columns]
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        meta = _read_cache_meta(p)
        df.attrs["cache_meta"] = meta or {}
        df.attrs["cache_meta_status"] = "ok" if meta else "missing_or_invalid"
        data[symbol] = df
    return data


def main() -> None:
    ap = argparse.ArgumentParser(description="交易现实回放器 (Phase 1.3)")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--mode", choices=["open", "vwap_proxy"], default="open")
    ap.add_argument("--interval", type=int, default=20, help="等权再平衡间隔(交易日)")
    ap.add_argument("--extra-slippage-bps", type=float, default=0.0)
    ap.add_argument("--paths", nargs="*", default=None, help="CSV 数据路径列表")
    args = ap.parse_args()

    if args.paths:
        paths = args.paths
    else:
        default = ["data/600519_realistic.csv", "data/300059_eastmoney.csv", "data/300750_catl.csv"]
        paths = [p for p in default if os.path.exists(p)] or sorted(glob.glob("data/*.csv"))[:5]
    if not paths:
        print("❌ 未找到数据文件")
        sys.exit(1)

    data = load_data(paths)
    cfg = ReplayConfig(initial_capital=args.capital, exec_mode=args.mode,
                       extra_slippage_bps=args.extra_slippage_bps)
    engine = ReplayEngine(cfg, data)
    result = engine.run(make_equal_weight_strategy(args.interval))
    # FX-4: 输出数据来源快照，供下游判断费用/行情口径来源
    result["data_provenance"] = {
        s: {"cache_meta": d.attrs.get("cache_meta", {}),
            "cache_meta_status": d.attrs.get("cache_meta_status", "unknown")}
        for s, d in data.items()
    }

    os.makedirs("reports", exist_ok=True)
    ts = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    out_path = f"reports/replay_realistic_{ts}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # ---- 控制台摘要 ----
    s = result["summary"]
    print("\n========== 交易现实回放器 · 烟测结果 ==========")
    print(f"标的: {list(data.keys())}  |  成交模式: {cfg.exec_mode}  |  交易日: {s['trading_days']}")
    print(f"费率: 佣金{COMMISSION_RATE:.5f}(最低{MIN_COMMISSION}元) 印花税{STAMP_TAX_RATE}(卖) 过户费{TRANSFER_FEE_RATE:.5f}")
    print(f"过户费来源: {result['fees']['transfer_fee_source']}")
    print(f"期初NAV: {result['daily_nav'][0]['nav']:,.2f}  期末NAV: {s['final_nav']:,.2f}")
    print(f"总收益: {s['total_return']*100:.2f}%  年化: {s['annualized_return']*100:.2f}%  "
          f"最大回撤: {s['max_drawdown']*100:.2f}%  日波动: {s['daily_vol']*100:.2f}%  Sharpe: {s['sharpe']}")
    print(f"成交状态统计: {s['trade_status_count']}")
    print(f"期末持仓: {s['final_positions']}  期末现金: {s['final_cash']:,.2f}")

    tr = result["trades"]
    print(f"\n--- 逐笔明细(共 {len(tr)} 笔，前 12 笔) ---")
    print("信号日      成交日      标的    方向  股数     决策参考价  成交价    滑点bp   状态    费用合计")
    for t in tr[:12]:
        print(f"{t['signal_date']} {t['exec_date']} {t['symbol']:>6} {t['side']:>4} "
              f"{t['qty']:>7} {t['ref_price']:>9.3f} {t['exec_price']:>9.3f} "
              f"{t['slippage_bp']:>7.1f} {t['status']:>8} {t['fee_total']:>9.2f}  {t['note']}")
    if len(tr) > 12:
        print(f"  ... 剩余 {len(tr)-12} 笔省略")

    dn = result["daily_nav"]
    print(f"\n--- 逐日 NAV(共 {len(dn)} 日，首尾各 3) ---")
    for x in dn[:3] + dn[-3:]:
        print(f"{x['date']}  NAV={x['nav']:,.2f}  现金={x['cash']:,.2f}  "
              f"市值={x['position_value']:,.2f}  持仓数={x['n_positions']}")

    print(f"\n✅ 输出 JSON: {out_path}")


if __name__ == "__main__":
    main()
