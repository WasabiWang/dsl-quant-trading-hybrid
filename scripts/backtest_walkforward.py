#!/usr/bin/env python3
"""
DSL v4.5.1 Walk-Forward回测 — 全量35只，无未来信息泄露

策略:
- 滚动窗口: 每3个月一个训练窗口，只用该窗口之前的数据
- 训练模型后预测下一季度的信号
- 40%仓位分给20d预测>2%的最佳5只
- 止损: 预测< -5% 或 持仓>30天

回测维度: 最近2年，按季度分割(8个窗口)
"""
import os, sys, json, yaml, gc, time
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import joblib

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from dsl_data_sdk_original import get_kline, normalize_symbol
from train_predictor_enhanced import build_features, fetch_fundamentals
from core.signal_generator import SignalGenerator, StockSignal  # v4.5.6 统一信号模块
from sklearn.preprocessing import StandardScaler
from sklearn.feature_selection import SelectFromModel
import lightgbm as lgb

# ====== 参数 ======
INITIAL_CAPITAL = 1_000_000
MAX_POSITIONS = 5
POSITION_SIZE = 0.10  # P0-3: 单标的仓位从20%降至10%
MIN_DAILY_AMOUNT = 100_000_000  # P0-3: 流动性过滤，日成交额≥1亿
COMMISSION = 0.00025  # v4.5.12: 与config/constants.py统一
SLIPPAGE = 0.0005     # v4.5.12: 5bps默认滑点，与config/constants.py统一
BUY_THRESHOLD = 0.02
# v4.5.7: 从adaptive_params读取实际阈值（若不可用则回退硬编码）
try:
    with open("config/adaptive_params.yaml") as f:
        import yaml
        _ap = yaml.safe_load(f)
    _trading = _ap.get("trading", {})
    # v4.6.9f P0-1: 回测对齐实盘 h5d 阈值(±0.5%), 不再用 h20d 阈值(±2.5%)
    # 实盘 batch_predict.py: H5D_SIGNAL_THRESHOLD=0.005
    BUY_THRESHOLD = _trading.get("buy_threshold_h5d", _ap.get("h5d", {}).get("buy_threshold", 0.005))
    _sell = _trading.get("sell_threshold_h5d", _ap.get("h5d", {}).get("sell_threshold", -0.005))
    SELL_THRESHOLD = -abs(_sell)  # 确保为负
    MIN_HOLD_DAYS = _trading.get("min_hold_days", 5)
    MAX_HOLD_DAYS = _trading.get("max_hold_days", 20)
    POSITION_SIZE = _trading.get("position_size", 0.08)
    MAX_POSITIONS = _trading.get("max_positions", 5)
    # v4.6.8: 回测成本校准参数
    _slip = _trading.get("slippage", {})
    BASE_BPS = _slip.get("base_bps", 4.0)
    IMPACT_FACTOR = _slip.get("impact_factor", 0.8)
    print(f"  📐 从adaptive_params读取: BUY>={BUY_THRESHOLD:.2%} SELL<={SELL_THRESHOLD:.2%} (h5d对齐)")
    print(f"  📐 max_pos={MAX_POSITIONS} pos_size={POSITION_SIZE:.0%} hold={MIN_HOLD_DAYS}-{MAX_HOLD_DAYS}d")
except Exception:
    BUY_THRESHOLD = 0.005
    SELL_THRESHOLD = -0.005
    MIN_HOLD_DAYS = 5
    MAX_HOLD_DAYS = 20
    POSITION_SIZE = 0.08
    MAX_POSITIONS = 5
    BASE_BPS = 4.0
    IMPACT_FACTOR = 0.8
    print(f"  📐 adaptive_params不可用，使用h5d对齐默认阈值")
# v4.6.9f P0-1: 回测对齐实盘 — 5日框架 (实盘 batch_predict 用 target_5d + h5d信号)
HORIZON = 5
BACKTEST_DAYS = 730  # 2年
WINDOW_DAYS = 90     # 每季度重新训练

# v4.6.8: 回测执行价格校准 — 使用次日开盘价模拟实盘
# 回测用信号日收盘价生成信号，次日开盘价执行(避免look-ahead bias)
USE_NEXT_DAY_OPEN = True  # True=次日开盘价执行, False=当日收盘价(旧行为)
EXECUTION_SLIPPAGE_BUY = 0.002   # 买入时开盘价溢价20bps
EXECUTION_SLIPPAGE_SELL = 0.002  # 卖出时开盘价折价20bps

# 加载股票池
with open("config/master_stock_pool.yaml") as f:
    pool = [s for s in yaml.safe_load(f)["master_pool"] if "." not in s["symbol"]]
CODES = [s["symbol"] for s in pool]
print(f"🚀 Walk-Forward回测: {len(CODES)}只 × {BACKTEST_DAYS//WINDOW_DAYS}窗口")

# ====== Step 1: 预加载全量历史数据 ======
print("⏳ [1/3] 拉取历史数据...")
end_date = datetime.now().strftime("%Y-%m-%d")
start_date = (datetime.now() - timedelta(days=BACKTEST_DAYS + 365)).strftime("%Y-%m-%d")
efunds = {k: 0.0 for k in ["roe","eps","bps","cfps","revenue_growth","gross_margin","capex_ratio"]}

all_data = {}
for code in CODES:
    try:
        kline = get_kline(normalize_symbol(code), start_date, end_date)
        if not kline or len(kline) < 300:
            continue
        df = pd.DataFrame(kline)
        for c in ["close","volume","high","low","open"]:
            df[c] = df[c].astype(float)
        # P0-3: 流动性过滤 — 排除日成交额<1亿的标的
        if 'amount' in df.columns:
            avg_amount = df['amount'].tail(60).mean()
            if avg_amount < MIN_DAILY_AMOUNT:
                print(f"  ⏭️ {code} 日均成交额{avg_amount/1e8:.1f}亿<1亿，流动性不足，排除")
                continue
        else:
            # 没有amount列时用 close*volume 估算
            df['amount'] = df['close'] * df['volume']
            avg_amount = df['amount'].tail(60).mean()
            if avg_amount < MIN_DAILY_AMOUNT:
                print(f"  ⏭️ {code} 日均成交额{avg_amount/1e8:.1f}亿<1亿，流动性不足，排除")
                continue
        feats = build_features(df, efunds).replace([np.inf, -np.inf], np.nan)
        # 保留日期为索引
        all_data[code] = {"df": df.set_index(pd.RangeIndex(len(df))), "feats": feats, "raw_dates": [r.get("date", "") for r in kline]}
    except Exception as e:
        pass

print(f"   数据加载: {len(all_data)}只")

# ====== Step 2: 滚动窗口回测 ======
print(f"⏳ [2/3] Walk-Forward回测 ({BACKTEST_DAYS//WINDOW_DAYS}个窗口)...")

cash = INITIAL_CAPITAL
positions = {}  # {code: {"shares": N, "cost": price, "hold_start": idx}}
trades = []
daily_equity = []

# 建立公共时间轴
common_dates = []
for code, d in all_data.items():
    common_dates.append(d["feats"].index.tolist())

# 找所有日期的并集，排序
all_day_indices = sorted(set(sum(common_dates, [])))
# 取回测期: 最后 BACKTEST_DAYS 天
bt_start = max(0, len(all_day_indices) - BACKTEST_DAYS)
bt_indices = all_day_indices[bt_start:]

# 按窗口分割
windows = []
for w_start in range(bt_start, len(all_day_indices), WINDOW_DAYS):
    w_end = min(w_start + WINDOW_DAYS, len(all_day_indices))
    # 训练数据截止到 w_start 之前
    train_end = all_day_indices[w_start] if w_start < len(all_day_indices) else all_day_indices[-1]
    windows.append({
        "train_end_idx": train_end,
        "test_start": w_start,
        "test_end": w_end,
    })

total_days = 0
for wi, win in enumerate(windows):
    train_end = win["train_end_idx"]
    test_range = all_day_indices[win["test_start"]:win["test_end"]]

    if len(test_range) < 10:
        continue

    print(f"  [{wi+1}/{len(windows)}] 训练截止={train_end}, 预测={len(test_range)}天")

    # 为每只股票训练模型（只用train_end之前的数据）
    signals = {}  # {code: [list of (date_idx, predicted_return)]}
    for code in CODES:
        d = all_data.get(code)
        if not d:
            continue

        # 只取训练截止日期前的数据
        train_mask = d["feats"].index <= train_end
        if train_mask.sum() < 200:
            continue

        feats = d["feats"].loc[train_mask]
        tcol = f"target_{HORIZON}d"
        fcols = [c for c in feats.columns if not c.startswith("target_")]

        if tcol not in feats.columns:
            continue

        # 分割训练/验证
        split = int(len(feats) * 0.8)
        X_tr = feats.iloc[:split][fcols].values
        y_tr = feats.iloc[:split][tcol].values

        try:
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X_tr)

            # 特征选择
            selector = SelectFromModel(
                lgb.LGBMRegressor(n_estimators=50, random_state=42, verbose=-1),
                threshold="median", max_features=40
            )
            X_sel = selector.fit_transform(X_scaled, y_tr)

            # 训练
            model = lgb.LGBMRegressor(
                n_estimators=200, max_depth=6, learning_rate=0.03,
                random_state=42, verbose=-1, n_jobs=-1,
            )
            model.fit(X_sel, y_tr)

            # 预测测试区间
            code_signals = []
            for test_idx in test_range:
                d_feats = d["feats"]
                if test_idx not in d_feats.index:
                    continue
                try:
                    X_test = scaler.transform([d_feats.loc[test_idx, fcols].values])
                    X_test_sel = selector.transform(X_test)
                    pred = model.predict(X_test_sel)[0]
                    code_signals.append((test_idx, pred))
                except:
                    pass

            if code_signals:
                signals[code] = code_signals

        except Exception as e:
            pass

        gc.collect()

    # 模拟交易
    for date_idx in test_range:
        # 卖出
        for code in list(positions.keys()):
            p = positions[code]
            d = all_data.get(code)
            if not d or date_idx not in d["df"].index:
                continue

            hold_days = p["hold_days"] + 1
            positions[code]["hold_days"] = hold_days
            cp = d["df"].loc[date_idx, "close"]

            force_sell = hold_days >= MAX_HOLD_DAYS
            if not force_sell and code in signals:
                # 找当前日期的信号
                sig = next((s[1] for s in signals[code] if s[0] == date_idx), None)
                if sig is not None and sig < SELL_THRESHOLD:
                    force_sell = True

            if hold_days >= MIN_HOLD_DAYS and force_sell:
                # v4.6.8: 使用次日开盘价执行(若启用), 否则用当日收盘价
                if USE_NEXT_DAY_OPEN and date_idx in d["df"].index:
                    next_dates = d["df"].index[d["df"].index.get_loc(date_idx)+1:]
                    if len(next_dates) > 0:
                        next_open = d["df"].loc[next_dates[0], "open"]
                        sp = next_open * (1 - EXECUTION_SLIPPAGE_SELL)
                    else:
                        sp = cp * (1 - SLIPPAGE)
                else:
                    sp = cp * (1 - SLIPPAGE)
                # v4.6.8: 成本模型 — 成交量加权滑点 + 印花税
                # v4.6.9f P2-2: 修复印花税缺失 — A股卖出需缴纳0.1%印花税
                dynamic_slippage = SLIPPAGE + BASE_BPS/10000 * IMPACT_FACTOR
                sp = sp * (1 - dynamic_slippage)
                # v4.6.9f: 显式扣印花税(仅卖出) — 与config/constants.py STAMP_TAX_RATE=0.001统一
                try:
                    from config.constants import STAMP_TAX_RATE, MIN_COMMISSION
                except Exception:
                    STAMP_TAX_RATE, MIN_COMMISSION = 0.001, 5.0
                sp = sp * (1 - STAMP_TAX_RATE)  # 印花税(卖出)
                proceeds = sp * p["shares"] * (1 - COMMISSION)
                # 最低佣金保护 (单边≥5元)
                _comm = max(proceeds * COMMISSION, MIN_COMMISSION) if proceeds > 0 else 0
                proceeds = sp * p["shares"] - _comm
                pnl = proceeds - p["cost"] * p["shares"]
                cash += proceeds
                trades.append({"code": code, "action": "SELL", "pnl": pnl,
                               "pnl_pct": (sp-p["cost"])/p["cost"], "hold": hold_days})
                del positions[code]

        # 买入
        candidates = []
        for code in signals:
            if code in positions:
                continue
            d = all_data.get(code)
            if not d or date_idx not in d["df"].index:
                continue
            sig = next((s[1] for s in signals[code] if s[0] == date_idx), None)
            if sig is not None and sig > BUY_THRESHOLD:
                candidates.append((code, sig))

        candidates.sort(key=lambda x: x[1], reverse=True)
        slots = MAX_POSITIONS - len(positions)
        for code, _ in candidates[:slots]:
            cp = all_data[code]["df"].loc[date_idx, "close"]
            # v4.6.8: 使用次日开盘价执行(模拟实盘), 否则用当日收盘价
            if USE_NEXT_DAY_OPEN:
                d = all_data[code]["df"]
                if date_idx in d.index:
                    next_dates = d.index[d.index.get_loc(date_idx)+1:]
                    if len(next_dates) > 0:
                        next_open = d.loc[next_dates[0], "open"]
                        bp = next_open * (1 + EXECUTION_SLIPPAGE_BUY)
                    else:
                        bp = cp * (1 + SLIPPAGE)
                else:
                    bp = cp * (1 + SLIPPAGE)
            else:
                bp = cp * (1 + SLIPPAGE)
            # v4.6.8: 叠加动态成本模型
            dynamic_slippage = SLIPPAGE + BASE_BPS/10000 * IMPACT_FACTOR
            bp = bp * (1 + dynamic_slippage)
            shares = int(INITIAL_CAPITAL * POSITION_SIZE / bp / 100) * 100
            if shares == 0:
                continue
            # v4.6.9f P2-2: 买入佣金+最低佣金保护 (与卖出侧一致)
            try:
                from config.constants import MIN_COMMISSION as _MIN_COMM
            except Exception:
                _MIN_COMM = 5.0
            _buy_comm = max(shares * bp * COMMISSION, _MIN_COMM)
            cost = shares * bp + _buy_comm
            if cost > cash:
                continue
            cash -= cost
            positions[code] = {"shares": shares, "cost": bp, "hold_days": 0}
            trades.append({"code": code, "action": "BUY", "pnl": 0, "pnl_pct": 0, "hold": 0})

        # 每日估值
        pv = sum(p["shares"] * all_data[c]["df"].loc[date_idx, "close"]
                 for c, p in positions.items() if c in all_data and date_idx in all_data[c]["df"].index)
        daily_equity.append(float(cash + pv))
        total_days += 1

    r = (cash + sum(p["shares"] * all_data[c]["df"]["close"].iloc[-1] for c, p in positions.items() if c in all_data) - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100
    print(f"      累计收益: {r:+.2f}% 持仓{len(positions)}只")

# ====== Step 3: 绩效汇总 ======
final_pv = sum(p["shares"] * all_data[c]["df"].iloc[-1]["close"]
               for c, p in positions.items() if c in all_data)
final_value = cash + final_pv
total_return = (final_value - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100

daily_returns = np.array(np.diff(daily_equity) / np.array(daily_equity[:-1]) if len(daily_equity) > 1 else [0])
annual_vol = np.std(daily_returns) * np.sqrt(252) * 100 if np.std(daily_returns) > 0 else 0
sharpe = (np.mean(daily_returns) * 252) / (np.std(daily_returns) * np.sqrt(252)) if np.std(daily_returns) > 0 else 0
# v4.5.5 S6: 新增索提诺比率 — 只惩罚下行波动(更真实反映不对称策略风险)
downside_returns = daily_returns[daily_returns < 0]
downside_std = np.std(downside_returns) if len(downside_returns) > 0 else 0
sortino = (np.mean(daily_returns) * 252) / (downside_std * np.sqrt(252)) if downside_std > 0 else 0

peak = INITIAL_CAPITAL
max_dd = 0
for v in daily_equity:
    peak = max(peak, v)
    dd = (v - peak) / peak * 100
    if dd < max_dd:
        max_dd = dd

sells = [t for t in trades if t["action"] == "SELL"]
wins = [t for t in sells if t["pnl"] > 0]
losses = [t for t in sells if t["pnl"] <= 0]
win_rate = len(wins) / len(sells) * 100 if sells else 0
total_profit = sum(t["pnl"] for t in wins) if wins else 0
total_loss = abs(sum(t["pnl"] for t in losses)) if losses else 0
profit_factor = total_profit / total_loss if total_loss > 0 else 0

print(f"\n{'='*60}")
print(f"📊 DSL v4.5.1 Walk-Forward 回测（全量，无未来信息泄露）")
print(f"{'='*60}")
print(f"  回测窗口: {len(windows)}个季度 × {len(CODES)}只")
print(f"  交易天数: {total_days}天")
print(f"  初始资金: ¥{INITIAL_CAPITAL:,.0f}")
print(f"  最终资金: ¥{final_value:,.0f}")
print(f"  总收益率: {total_return:+.2f}%")
print(f"  年化收益: {total_return/(BACKTEST_DAYS/365):.2f}%/年")
print(f"  最大回撤: {max_dd:.2f}%")
print(f"  年化波动: {annual_vol:.2f}%")
print(f"  夏普比率: {sharpe:.2f}")
print(f"  索提诺比率: {sortino:.2f} (仅惩罚下行波动)")
print(f"")
print(f"  总交易: {len(trades)}笔 ({len([t for t in trades if t['action']=='BUY'])}买/{len(sells)}卖)")
print(f"  胜率: {win_rate:.1f}% ({len(wins)}赢/{len(losses)}输)")
print(f"  盈亏比: {profit_factor:.2f}")
if wins:
    print(f"  平均盈利: ¥{np.mean([t['pnl'] for t in wins]):,.0f}")
if losses:
    print(f"  平均亏损: ¥{np.mean([t['pnl'] for t in losses]):,.0f}")
if sells:
    print(f"  平均持仓: {np.mean([t['hold'] for t in sells]):.1f}天")

# 保存报告
report = {
    "backtest_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    "method": "walk_forward_no_lookahead",
    "params": {
        "stocks": len(CODES), "windows": len(windows), "horizon": HORIZON,
        "buy_threshold": BUY_THRESHOLD, "sell_threshold": SELL_THRESHOLD,
        "initial_capital": INITIAL_CAPITAL, "max_positions": MAX_POSITIONS,
    },
    "performance": {
        "total_return_pct": round(total_return, 2),
        "annual_return_pct": round(total_return / (BACKTEST_DAYS / 365), 2),
        "max_drawdown_pct": round(max_dd, 2),
        "annual_volatility_pct": round(annual_vol, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(trades),
        "avg_hold_days": round(np.mean([t["hold"] for t in sells]), 1) if sells else 0,
    },
    "trades": trades[-100:],
}
os.makedirs("reports/backtest", exist_ok=True)
out_path = f"reports/backtest/walkforward_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
with open(out_path, "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"\n  📁 {out_path}")
