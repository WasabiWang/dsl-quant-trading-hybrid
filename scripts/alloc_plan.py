import json, os, yaml, sqlite3
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 1. 读取黑天鹅参数
ap = yaml.safe_load(open(os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")))
risk = ap.get("risk", {})
bs_ratio = risk.get("black_swan_position_ratio", 1.0)
max_positions = ap.get("trading", {}).get("max_positions", 5)

# 2. 读取账户
db = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
held = {}
available_cash = 1000000.0
if os.path.exists(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    held = {r["stock_code"]: r["quantity"] for r in conn.execute("SELECT * FROM positions")}
    cash_row = conn.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
    if cash_row: available_cash = float(cash_row["value"])
    conn.close()

# 3. 风控计算
total_capital = available_cash + sum(p.get("latest_price", 0) * q for q, p in [(held.get(c, 0), next((x for x in [] if x.get("symbol")==c), {})) for c in held])  # simplified
effective_cash = available_cash * bs_ratio   # 黑天鹅限仓
remaining = effective_cash

print(f"账户: ¥{available_cash:,.0f} | 黑天鹅限仓: {bs_ratio*100:.0f}% | 可用: ¥{effective_cash:,.0f}")
print(f"持仓: {len(held)}只 | 风控上限: {max_positions}只")
print()

# 4. 加载信号，按信号强度排序
daily = json.load(open(os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")))
signals = []
for p in daily["predictions"]:
    sig = p.get("signal", "hold")
    if sig not in ("buy", "sell"): continue
    signals.append(p)

# 排序: 先按信号(buy>sell)，再按置信度*精度得分降序
signals.sort(key=lambda x: (
    0 if x["signal"] == "buy" else 1,
    -(x.get("confidence", 0) or 0) * (x.get("direction_accuracy", 0) or 0),
))

# 5. 按优先级分配仓位
trades = []
skipped = []
positions_budget = len(held)

for p in signals:
    code = p["symbol"]
    sig = p.get("signal")

    # 卖出手动持仓检查
    if sig == "sell" and code not in held:
        skipped.append(f"SELL {code}: 无持仓")
        continue
    if sig == "buy":
        # 持仓上限
        if positions_budget >= max_positions:
            skipped.append(f"BUY {code}: 已达{max_positions}只上限")
            continue
        if remaining < 50000:  # 最低5万
            skipped.append(f"BUY {code}: 资金不足(剩{remaining:,.0f})")
            continue

    # 获取价格: 优先用latest_price, 否则动态K线查询最新交易日
    price = p.get("latest_price", 0) or 0
    if not price:
        try:
            from dsl_data_sdk_original import get_kline
            from datetime import datetime, timedelta
            end_date = datetime.now().strftime("%Y-%m-%d")
            start_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
            kline = get_kline(code, start_date, end_date)
            if kline and len(kline) > 0:
                price = float(kline[-1].get("close", kline[-1].get("c", 0)))
            if price <= 0:
                # 降级: 麦蕊API获取实时价格
                try:
                    from config.mairui_api_config import get_stock_real
                    real = get_stock_real(code)
                    if real and real.get("current_price", 0) > 0:
                        price = float(real["current_price"])
                except:
                    pass
            if price <= 0:
                raise ValueError(f"无法获取{code}价格")
        except Exception as e:
            print(f"  ⚠️ {code} 价格获取失败: {e}, 跳过")
            skipped.append(f"{code}: 价格获取失败")
            continue

    # 单只分配: 剩余资金 / 剩余仓位位置数，但不超过10万
    slots_left = max_positions - positions_budget + (0 if sig == "sell" else 1)
    if slots_left <= 0: slots_left = 1
    alloc = min(remaining / max(slots_left, 1), 100000)
    qty = max(100, int(alloc / price / 100) * 100)
    cost = price * qty

    # 卖出: 不超持仓
    if sig == "sell":
        qty = min(qty, held.get(code, 0))
        cost = 0  # 卖出释放资金
        remaining += price * qty
        positions_budget -= 1
    else:
        if cost > remaining:
            qty = max(100, int(remaining / price / 100) * 100)
            cost = price * qty
            if cost <= 0:
                skipped.append(f"BUY {code}: 资金不够1手")
                continue
        remaining -= cost
        positions_budget += 1

    trades.append(dict(
        code=code, name=p.get("name", ""), action=sig.upper(),
        price=round(price, 2), quantity=qty,
        reason=f'{p.get("source","?")}: conf={p.get("confidence",0)*100:.0f}%',
        confidence=round(p.get("confidence", 0) * 100, 0),
    ))

# 6. 保存
plan = dict(
    generated_at=datetime.now().isoformat(),
    market="A", target_date="2026-05-06",
    trades=trades, skipped=skipped,
    capital=available_cash, effective_capital=effective_cash,
    position_ratio=bs_ratio, remaining_cash=round(remaining, 2),
)
path = os.path.join(PROJECT_ROOT, "cache", "planned_trades.json")
with open(path, "w") as f:
    json.dump(plan, f, ensure_ascii=False, indent=2)

total_cost = sum(t["price"] * t["quantity"] for t in trades if t["action"] == "BUY")
buys = sum(1 for t in trades if t["action"] == "BUY")

print(f"✅ 预案: {len(trades)}笔 ({buys}买入, {len(trades)-buys}卖出)")
print(f"💰 买入金额: ¥{total_cost:,.0f} / 可用¥{effective_cash:,.0f} ({(total_cost/effective_cash*100):.0f}%)")
print(f"💵 剩余现金: ¥{remaining:,.0f}")
print(f"📊 持仓数: {positions_budget}/{max_positions}只")
print(f"⏭️ 跳过: {len(skipped)}笔")
for s in skipped: print(f"  {s}")
print()
for t in trades:
    amt = t["price"] * t["quantity"]
    pct = amt/effective_cash*100 if effective_cash > 0 else 0
    print(f"  {t['action']:4s} {t['code']} {t['name']:6s} | ¥{t['price']:.2f} x {t['quantity']:4d} = ¥{amt:,.0f} ({pct:.0f}%)")
