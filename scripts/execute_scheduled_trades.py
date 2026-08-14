#!/usr/bin/env python3
"""
DSL v4.5.5 — 定时交易执行器
用途: 从 planned_trades.json 读取交易计划，通过 PaperTrader 执行
执行时机: 盘前决策后(09:25), 盘中(11:00复核), 尾盘(14:50)
"""
import os, sys, json, time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

PLANNED_TRADES_PATH = os.path.join(PROJECT_ROOT, "cache", "planned_trades.json")

# v4.5.10/🅰️: 早盘决策的融合推荐仓位, 由load_planned_trades()填充
_PLANNED_POSITION = None


def _trade_reference_price(trade: dict, default: float = 50.0) -> float:
    """Return a positive reference price for pre-execution risk sizing."""
    for key in ("price", "target_price", "open_price", "last_price"):
        try:
            price = float(trade.get(key, 0) or 0)
            if price > 0:
                return price
        except (TypeError, ValueError):
            continue
    return default


def _filter_buys_when_prediction_stale(trades: list) -> tuple[list, dict]:
    """Filter stale BUY orders while preserving SELL/reduction orders."""
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
        if not isinstance(trade, dict):
            continue
        action = str(trade.get("action", trade.get("signal", ""))).upper()
        if action == "BUY":
            blocked += 1
            code = trade.get("code") or trade.get("symbol") or "?"
            print(f"  ⛔ BUY {code}: ML预测不可买入 ({freshness.get('reason', freshness.get('status'))})")
            continue
        filtered.append(trade)
    if blocked:
        print(f"  ⛔ stale BUY门禁: 已过滤 {blocked} 笔BUY, 保留 {len(filtered)} 笔SELL/减仓")
    return filtered, freshness


def _expected_execution_date(now: datetime = None) -> str:
    """Return the A-share trading date a plan must target before execution."""
    now = now or datetime.now()
    today = now.date()
    try:
        from config.holiday_calendar import is_trading_day, get_next_trading_day
        if is_trading_day(check_date=today, market="A_SHARE"):
            return today.isoformat()
        return get_next_trading_day("A_SHARE", today).isoformat()
    except Exception:
        return today.isoformat()


def get_planned_position():
    """获取早盘决策写入的融合推荐仓位(fallback: adaptive_params black_swan_position_ratio)"""
    global _PLANNED_POSITION
    if _PLANNED_POSITION is not None:
        return _PLANNED_POSITION
    # fallback: 重新读取adaptive_params
    try:
        import yaml
        ap_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        with open(ap_path) as f:
            ap = yaml.safe_load(f)
        return float(ap.get("risk", {}).get("black_swan_position_ratio", 1.0))
    except Exception:
        return 1.0


def load_planned_trades(_allow_regenerate: bool = True) -> list:
    """加载最近的交易计划，返回新鲜度检查结果
    v4.5.10/🅰️: 同时提取final_position到全局_PLANNED_POSITION
    """
    global _PLANNED_POSITION
    if not os.path.exists(PLANNED_TRADES_PATH):
        print("📭 无交易计划文件")
        return []
    try:
        with open(PLANNED_TRADES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        trades = data.get("trades", []) if isinstance(data, dict) else data
        for trade in trades:
            if isinstance(trade, dict):
                code = trade.get("code") or trade.get("symbol") or ""
                if code:
                    trade["code"] = code
                    trade["symbol"] = code
        # v4.5.10/🅰️: 提取融合推荐仓位
        if isinstance(data, dict):
            fp = data.get("final_position")
            if fp is not None:
                _PLANNED_POSITION = float(fp)
        plan_time = data.get("generated_at", data.get("timestamp", "")) if isinstance(data, dict) else ""
        target_date = data.get("target_date", "") if isinstance(data, dict) else ""
        expected_date = _expected_execution_date()
        if target_date and target_date != expected_date:
            print(f"⛔ 交易计划目标日期{target_date}≠预期执行日{expected_date}，安全放弃")
            return []
        
        # 新鲜度: 超过24小时的计划作废
        if plan_time:
            try:
                plan_dt = datetime.fromisoformat(plan_time)
                age_hours = (datetime.now() - plan_dt).total_seconds() / 3600
                if age_hours > 24:
                    print(f"⛔ 交易计划过期({age_hours:.0f}h)，安全放弃；请由盘前/晚间流程重新生成")
                    return []
                print(f"📋 交易计划: {len(trades)}笔, 生成于{plan_dt.strftime('%H:%M')} ({age_hours:.1f}h前)")
            except:
                pass
        return trades
    except json.JSONDecodeError:
        print("❌ 交易计划JSON格式错误")
        return []


def execute_trades(trades: list, market: str = "A", enforce_prediction_freshness: bool = True):
    """通过 PaperTrader 执行交易
    v4.5.10/🅰️: 使用早盘决策融合推荐仓位(而非重新读取adaptive_params硬上限)
    """
    from paper_trader import PaperTrader
    import yaml

    if enforce_prediction_freshness:
        trades, freshness = _filter_buys_when_prediction_stale(trades)
        if not trades:
            print(f"ℹ️ stale门禁后无可执行交易 (freshness={freshness.get('status')})")
            return []
    
    # v4.5.10/🅰️: 使用早盘决策写入的融合推荐仓位作为执行上限
    position_ratio = get_planned_position()
    
    # 仍从adaptive_params读取bs_active状态(用于日志和黑天鹅模式识别)
    adaptive_path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
    with open(adaptive_path, "r", encoding="utf-8") as f:
        adaptive = yaml.safe_load(f)
    risk = adaptive.get("risk", {})
    bs_active = risk.get("black_swan_active", False)
    max_positions = adaptive.get("trading", {}).get("max_positions", 8)  # P1:默认8(与morning_decision一致)
    
    # v4.6.x P2: 分级仓位约束 — 按股票池tier限制资金分配比例
    tier_limits = {}
    try:
        pool_path = os.path.join(PROJECT_ROOT, 'config', 'master_stock_pool.yaml')
        if os.path.exists(pool_path):
            with open(pool_path) as f:
                master_pool = yaml.safe_load(f) or {}
            stock_pool_config = master_pool.get('master_pool', master_pool)
            # 如果是master_pool列表格式(非分组), 先归入tiers
            if isinstance(stock_pool_config, list):
                stock_tier_map = {}
                from scripts.dynamic_pool_manager import POOL_STRUCTURE
                tier_limits = {t: info.get('allocation', 0.10) for t, info in POOL_STRUCTURE.items()}
                for s in stock_pool_config:
                    if isinstance(s, dict):
                        code = s.get('symbol', '')
                        tier = s.get('tier', 'flex')
                        if code:
                            stock_tier_map[code] = tier
            else:
                # 分组格式: {bluechip: [...], core: [...]}
                tier_limits = {}
                stock_tier_map = {}
                from scripts.dynamic_pool_manager import POOL_STRUCTURE
                for tier_name, stocks in stock_pool_config.items():
                    tier_limits[tier_name] = POOL_STRUCTURE.get(tier_name, {}).get('allocation', 0.10)
                    if isinstance(stocks, list):
                        for s in stocks:
                            if isinstance(s, dict):
                                code = s.get('code', s.get('symbol', ''))
                                if code:
                                    stock_tier_map[code] = tier_name
                            elif isinstance(s, str):
                                stock_tier_map[s] = tier_name
                if not tier_limits:
                    tier_limits = {'core': 0.25, 'growth': 0.15, 'bluechip': 0.15, 'flex': 0.10, 'cyclical': 0.20}
    except Exception as e:
        print(f"  ⚠️ 加载tier分配失败: {e}")
        stock_tier_map = {}
    
    if bs_active and position_ratio < 0.5:
        print(f"🦢 融合推荐仓位{position_ratio*100:.0f}% (< 50%) — 风险约束生效")
    elif bs_active:
        print(f"🦢 黑天鹅活跃, 融合推荐仓位{position_ratio*100:.0f}%")
    
    trader = PaperTrader()
    results = []
    cash = 0.0
    try:
        summary = trader.get_portfolio_summary()
        cash = float(summary.get("current_cash", summary.get("cash", 0)) or 0)
    except Exception:
        pass
    
    # v4.6.x P0-2: 构建模型质量黑名单(精度<40%禁止买入)
    _quality_check = {}  # {code: True/False}
    _quality_accuracy = {}  # {code: accuracy}
    try:
        # 1) prediction_calibration.json
        _calib_path = os.path.join(PROJECT_ROOT, "confidence_data", "prediction_calibration.json")
        if os.path.exists(_calib_path):
            with open(_calib_path, "r") as _f:
                _calib = json.load(_f)
            _stock_acc = _calib.get("stock_accuracy", {})
            for _code, _info in _stock_acc.items():
                if isinstance(_info, dict):
                    _acc = _info.get("last_accuracy", _info.get("mean_accuracy", 0.5))
                else:
                    _acc = float(_info) if _info else 0.5
                _quality_accuracy[_code] = _acc
                _quality_check[_code] = _acc >= 0.40
        # 2) degraded_models.json (覆盖更高优先级)
        _degraded_path = os.path.join(PROJECT_ROOT, "confidence_data", "degraded_models.json")
        if os.path.exists(_degraded_path):
            with open(_degraded_path, "r") as _f:
                _degraded = json.load(_f)
            for _code, _info in _degraded.items():
                if isinstance(_info, dict):
                    _acc = _info.get("accuracy", 0)
                    _quality_accuracy[_code] = _acc
                    _quality_check[_code] = _acc >= 0.40
        _blocked_count = sum(1 for v in _quality_check.values() if not v)
        if _blocked_count:
            print(f"🛑 精度过滤: {_blocked_count}只<40%禁止买入")
    except Exception as _e:
        print(f"⚠️ 精度过滤加载失败(降级为全通过): {_e}")
    
    # ──────────── v4.6.9i(审计F1-2): 组合级风控前置检查 ────────────
    # 修复: 活跃执行器此前绕过 risk_manager.pre_execution_check → 行业配额/组合止损/RS止盈实盘不生效(审计P0-4)
    _trader = None
    _equity = None
    try:
        from core.risk_manager import RiskManager
        _risk = RiskManager()
        _pool_path2 = os.path.join(PROJECT_ROOT, 'config', 'master_stock_pool.yaml')
        with open(_pool_path2, 'r', encoding='utf-8') as _f:
            _stock_pool = yaml.safe_load(_f).get('master_pool', [])
        _trader = PaperTrader()
        _ledger = _trader.load_ledger()
        _positions = [{"code": p.get("stock_code", ""), "avg_cost": p.get("avg_cost", 0),
                       "current_price": p.get("current_price", 0), "quantity": p.get("quantity", 0)}
                      for p in _ledger.get("positions", [])]
        _init_cap = float(_ledger.get("initial_capital", 1000000))
        _cash = float(_ledger.get("current_cash", 0))
        _equity = _cash + sum(float(p["current_price"]) * int(p["quantity"]) for p in _positions)
        _risk_result = _risk.pre_execution_check(trades, _positions, _init_cap, _equity, _stock_pool)
        if not _risk_result.get("approved", True):
            print(f"🔴 组合风控拒绝: {_risk_result.get('portfolio_status', '不明')}")
            _blocked_codes = {b.get("code", "") for b in _risk_result.get("blocked_trades", [])}
            for _b in _risk_result.get("blocked_trades", []):
                print(f"  🚫 {_b.get('code','')} {_b.get('name','')}: {_b.get('reason','')}")
            trades = [t for t in trades if t.get("code", t.get("symbol", "")) not in _blocked_codes]
        for _sl in _risk_result.get("stop_losses", []):
            print(f"  ⚠️ 止损提示: {_sl.get('code','')} {_sl.get('name','')}: {_sl.get('reason','')}")
        for _cv in _risk_result.get("concentration_violations", []):
            print(f"  ⚠️ 集中度违规: {_cv.get('code','')} {_cv.get('sector','')}: {_cv.get('reason','')}")
    except Exception as _re:
        print(f"⚠️ 组合风控检查失败(降级放行, 需人工关注): {_re}")
    
    # ──────────── v4.6.9i(审计F1-4): 熔断器当日回撤回写 ────────────
    # 修复: update_drawdown 此前无生产调用者, 日内回撤熔断机制空转(审计P0-7)
    if _equity is not None:
        try:
            from core.circuit_breaker import CircuitBreaker
            _cb = CircuitBreaker()
            _dd = _cb.update_equity_drawdown(_equity)
            print(f"  📉 熔断回撤: 当日 {_dd:+.2%}")
        except Exception as _e2:
            print(f"⚠️ 熔断回撤更新失败: {_e2}")
    
    for trade in trades:
        code = trade.get("code", trade.get("symbol", ""))
        action = trade.get("action", trade.get("signal", "hold")).lower()
        reason = trade.get("reason", "scheduled")
        confidence = trade.get("confidence", 0)
        qty = int(trade.get("quantity", 100) or 100)
        ref_price = _trade_reference_price(trade)
        
        if action == "hold":
            continue
        
        # v4.6.x P0-2: 精度<40%禁入 — 从calibration/degraded_models读取
        if action == "buy" and not _quality_check.get(code, True):
            _acc = _quality_accuracy.get(code, 0)
            results.append({"code": code, "action": action, "qty": qty, "status": "skipped",
                          "detail": f"精度过低({_acc:.0%}<40%)", "reason": "quality_filter"})
            print(f"  🛑 {code}: 买入拦截(模型精度{_acc:.0%}<40%)")
            continue
        
        # 持仓校验: 卖出必须有持仓（直接查DB）
        if action == "sell":
            db_path = os.path.join(PROJECT_ROOT, "data", "paper_trading.db")
            held = {}
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                held = {r["stock_code"]: r["quantity"] for r in conn.execute("SELECT * FROM positions")}
                conn.close()
            if code not in held:
                results.append({"code": code, "action": action, "qty": qty, "status": "skipped", "detail": "无持仓"})
                print(f"  ⏭️ {code}: 卖出跳过(无持仓)")
                continue
            qty = min(qty, held[code])
        
        # ── 黑天鹅缩量逻辑 (v4.5.9 修复) ──
        # position_ratio 是持仓上限比例(如0.4=40%), 不是每笔缩量比例!
        # 原bug: qty = max(1, int(qty * 0.4)) → 200*0.4=80股 < 200(科创板最小委托) → 跳过
        # 但实际当前持仓23.4%, 远未到40%上限, 缩量毫无必要。
        # 
        # 正确逻辑: 检查买入后总持仓是否超过 position_ratio 上限, 超过则缩减至刚好不超限
        if bs_active and action == "buy" and position_ratio < 1.0:
            # 计算当前总资产和持仓
            try:
                import sqlite3 as _sq
                _db = _sq.connect(os.path.join(PROJECT_ROOT, "data", "paper_trading.db"))
                _positions = _db.execute("SELECT quantity, current_price FROM positions").fetchall()
                _current_pos_value = sum(p[0] * p[1] for p in _positions)
                _cash_row = _db.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
                _cash = float(_cash_row[0]) if _cash_row else 0
                _db.close()
                _total_value = _cash + _current_pos_value
                
                # 当前已持仓比例
                _current_ratio = _current_pos_value / _total_value if _total_value > 0 else 0
                
                # 本次买入后预计持仓比例
                _buy_price = ref_price
                _buy_value = qty * _buy_price
                _new_ratio = (_current_pos_value + _buy_value) / _total_value if _total_value > 0 else 0
                
                if _new_ratio > position_ratio:
                    # 需要缩减数量使总持仓恰好不超过上限
                    _allowable_value = position_ratio * _total_value - _current_pos_value
                    if _allowable_value > 0 and _buy_price > 0:
                        _max_qty = int(_allowable_value / _buy_price)
                        # A股整手
                        min_lot = 200 if code.startswith("688") else 100
                        _max_qty = (_max_qty // min_lot) * min_lot
                        if _max_qty >= min_lot:
                            print(f"  🦢 {code}: 缩量 {qty}→{_max_qty}股 (持仓{_current_ratio*100:.1f}%→上限{position_ratio*100:.0f}%)")
                            qty = _max_qty
                        else:
                            # 缩量后不足最小委托, 但总持仓未超限→仍买入最小委托
                            print(f"  🦢 {code}: 缩量后{_max_qty}股不足最小委托, 但持仓{_current_ratio*100:.1f}%<上限, 保持最小委托{min_lot}股")
                            qty = min_lot
                    else:
                        # 已满仓, 确实不能买入
                        results.append({"code": code, "action": action, "qty": qty, "status": "skipped",
                                       "detail": f"黑天鹅仓位已满: 持仓{_current_ratio*100:.1f}% >= 上限{position_ratio*100:.0f}%"})
                        print(f"  ⏭️ {code}: 跳过(黑天鹅持仓{_current_ratio*100:.1f}%已满)")
                        continue
                else:
                    # 买入后仍在上限内, 无需缩量
                    print(f"  🦢 {code}: 黑天鹅限制{position_ratio*100:.0f}%, 买入后持仓{_new_ratio*100:.1f}% ≤ 上限, 无需缩量")
            except Exception as e:
                print(f"  ⚠️ {code}: 缩量计算异常({e}), 保持原数量")
        
        # A股最小委托量校验: 科创板(688)200起, 主板100起, 必须整手
        if market.upper() == "A" and action == "buy":
            min_lot = 200 if code.startswith("688") else 100
            if qty < min_lot:
                # v4.5.9: 缩量后不足最小委托, 但只要持仓未满, 应买入最小委托而非跳过
                print(f"  ⚠️ {code}: 缩量后{qty}股 < 最小委托{min_lot}股, 向上取整为{min_lot}股")
                qty = min_lot
            # 整手校验: 确保100的整数倍(科创板首次200,后续1股递增)
            if not code.startswith("688"):
                qty = (qty // 100) * 100
                if qty < 100:
                    qty = 100  # v4.5.9: 向上取整而非跳过
        
        # v4.6.x P1-2: 单票仓位硬上限 — 买入后不得超过总资产的15%
        if action == "buy":
            try:
                _single_limit = 0.15  # 单票上限15%总资产
                _pos_db = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "paper_trading.db"))
                _pos_db.row_factory = sqlite3.Row
                # 获取当前该股票持仓
                _cur_pos = _pos_db.execute(
                    "SELECT quantity, current_price FROM positions WHERE stock_code=?", (code,)
                ).fetchone()
                _cur_qty = _cur_pos["quantity"] if _cur_pos else 0
                _cur_price = _cur_pos["current_price"] if _cur_pos else ref_price
                # 获取总资产
                _pos_rows = _pos_db.execute("SELECT quantity, current_price FROM positions").fetchall()
                _total_pos = sum(r["quantity"] * r["current_price"] for r in _pos_rows)
                _cash_row = _pos_db.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()
                _total_assets = _total_pos + float(_cash_row["value"]) if _cash_row else _total_pos
                _pos_db.close()
                # 计算买入后占比
                _existing_value = _cur_qty * _cur_price
                _buy_cost = qty * ref_price
                _new_single_ratio = (_existing_value + _buy_cost) / _total_assets if _total_assets > 0 else 0
                if _new_single_ratio > _single_limit:
                    # 缩量到不超过上限
                    _allowed = _single_limit * _total_assets - _existing_value
                    if _allowed > 0:
                        _buy_p = ref_price
                        _new_qty = int(_allowed / _buy_p) if _buy_p > 0 else 0
                        min_lot = 200 if code.startswith("688") else 100
                        _new_qty = (_new_qty // min_lot) * min_lot
                        if _new_qty >= min_lot:
                            print(f"  ⚠️ {code}: 单票上限拦截, {qty}→{_new_qty}股 (买入后{_new_single_ratio*100:.0f}%>上限{_single_limit*100:.0f}%)")
                            qty = _new_qty
                        else:
                            results.append({"code": code, "action": action, "qty": qty, "status": "skipped",
                                           "detail": f"单票上限{_single_limit*100:.0f}%, 缩量后不足最小委托"})
                            print(f"  🛑 {code}: 单票仓位上限, 缩量后不足{min_lot}股, 跳过买入")
                            continue
                    else:
                        results.append({"code": code, "action": action, "qty": qty, "status": "skipped",
                                       "detail": f"单票已超{_single_limit*100:.0f}%上限"})
                        print(f"  🛑 {code}: 单票持仓已超{_single_limit*100:.0f}%上限, 禁止追买")
                        continue
            except Exception as _e:
                print(f"  ⚠️ 单票仓位检查异常(降级通过): {_e}")
        
        try:
            # 获取执行价格: 优先今日开盘价, 退而昨收*1.005缓冲
            price = 0.0
            pass  # 用麦蕊API获取开盘价
            try:
                from config.mairui_api_config import get_stock_real
                # 麦蕊API获取当日开盘价（09:30后有数据）
                try:
                    real = get_stock_real(code, use_broker_source=False)
                    open_price = float(real.get("open", 0))
                    if open_price > 0 and open_price < 10000:  # 合理价格范围
                        price = open_price
                        print(f"    {code}: 开盘价 {open_price}")
                except Exception as e:
                    print(f"    {code}: 麦蕊API异常 {str(e)[:50]}, 回退缓冲价")
            except: pass
            if not price or price <= 0:
                # 退而使用预案价 + 缓冲
                buffer = 0.005  # 0.5%缓冲, 确保成交
                if action == "buy":
                    price = ref_price * (1 + buffer)
                else:
                    price = ref_price * (1 - buffer)
                price = round(price, 2)
                print(f"    {code}: 无开盘价, 用预案价{ref_price}*{1+buffer:.3f}={price}")
            if not price or price <= 0:
                price = trade.get("target_price", trade.get("price", 0))
            if not price or price <= 0:
                print(f"  ⚠️ {code}: 无法获取价格，跳过")
                continue
            
            # 滑点模型: 从config读取分级滑点 (v4.5.12 fix: 按市值分级)
            from config.constants import get_slippage_bps
            # 根据代码前缀估计市值层级：600/000=大盘, 002=中盘, 300/688=小盘
            if code.startswith(('600', '000')):
                _slippage_bps = get_slippage_bps(800e8)  # 估算大盘≥500亿
            elif code.startswith('002'):
                _slippage_bps = get_slippage_bps(200e8)  # 估算中盘100-500亿
            else:
                _slippage_bps = get_slippage_bps(50e8)   # 估算小盘<100亿
            slippage = _slippage_bps / 10000.0  # bp → 小数
            if action == "buy":
                price = price * (1 + slippage)
            else:
                price = price * (1 - slippage)
            price = round(price, 2)
            
            # v4.6.x P2: 分级仓位约束 — 按tier检查资金分配比例
            if action == "buy" and stock_tier_map:
                _tier = stock_tier_map.get(code, 'flex')
                _tier_limit = tier_limits.get(_tier, 0.10)
                if _tier_limit > 0:
                    try:
                        _db_tier = sqlite3.connect(os.path.join(PROJECT_ROOT, "data", "paper_trading.db"))
                        _pos_rows = _db_tier.execute("SELECT stock_code, quantity, current_price FROM positions").fetchall()
                        _cash_db = float(_db_tier.execute("SELECT value FROM ledger WHERE key='current_cash'").fetchone()[0])
                        _db_tier.close()
                        _total_cap = _cash_db + sum(p[1]*p[2] for p in _pos_rows)
                        # 同tier当前持仓市值
                        _tier_value = sum(p[1]*p[2] for p in _pos_rows if p[0] in {s for s,t in stock_tier_map.items() if t==_tier}.intersection({p[0] for p in _pos_rows}))
                        _current_tier_ratio = _tier_value / _total_cap if _total_cap > 0 else 0
                        _buy_cost = qty * price
                        _new_tier_ratio = (_tier_value + _buy_cost) / (_total_cap + _buy_cost) if (_total_cap + _buy_cost) > 0 else 0
                        if _new_tier_ratio > _tier_limit:
                            print(f"  ⏭️ {code} [{_tier}]: tier金额已达{_current_tier_ratio*100:.1f}%, 买入将达{_new_tier_ratio*100:.1f}%, 超限{_tier_limit*100:.0f}%")
                            results.append({"code": code, "action": action, "qty": qty, "status": "skipped",
                                           "detail": f"tier[{_tier}]超限: {_new_tier_ratio*100:.1f}% > {_tier_limit*100:.0f}%"})
                            print(f"  ⏭️ {code}: 跳过(分级仓位[{_tier}]上限{_tier_limit*100:.0f}%)")
                            continue
                    except Exception as e_tier:
                        print(f"  ⚠️ {code}: tier检查失败: {e_tier}")
            
            order_id = f"SCH-{datetime.now().strftime('%Y%m%d%H%M%S')}-{code}"
            result = trader.execute_trade(
                market=market, stock_code=code, action=action.upper(),
                price=price, quantity=qty, reason=reason, order_id=order_id
            )
            status = "executed" if result.get("success") else "failed"
            results.append({
                "code": code, "action": action, "qty": qty, "price": price,
                "status": status,
                "detail": result.get("error", result.get("message", str(result)))[:200],
            })
            status = "✅" if result else "❌"
            print(f"  {status} {code}: {action} {qty}股 ({reason})")
        except Exception as e:
            results.append({"code": code, "action": action, "qty": qty, "status": "error", "detail": str(e)[:200]})
            print(f"  ❌ {code}: {str(e)[:80]}")
    
    # 写入 task_logs 供 Dashboard 回溯 (v4.6: execution_log.json 已移除, paper_trading.db 为唯一真相源)
    try:
        from log_task import log_task
        executed = [r for r in results if r["status"] == "executed"]
        log_task("trade_execution_0930", "completed",
                 f"执行{len(executed)}/{len(results)}笔交易, {cash:,.0f}现金",
                 {"executed": len(executed), "total": len(results), "skipped": len(results)-len(executed)})
    except Exception:
        pass
    
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DSL定时交易执行器")
    parser.add_argument("--market", default="A", help="A=沪深, HK=港股 (默认A)")
    parser.add_argument("--force", action="store_true", help="跳过新鲜度检查")
    args = parser.parse_args()
    
    # 检查是否是交易日（创建tracker之前，避免非交易日产生垃圾progress文件）
    from datetime import date
    from config.holiday_calendar import is_trading_day
    today = date.today()
    if not is_trading_day(today, market="A_SHARE"):
        print(f"📅 {today} 非交易日，跳过")
        return
    
    # v4.6.x: ProgressTracker for Dashboard（交易日确认后才创建）
    _tracker = None
    try:
        from common.progress_tracker import ProgressTracker
        _tracker = ProgressTracker("trade_execution_0930", total_steps=3)
        _tracker.step(1, "加载交易计划")
    except Exception:
        pass
    
    print(f"\n{'='*60}")
    print(f"  🚀 DSL 定时交易执行器 — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*60}")
    
    
    trades = load_planned_trades()
    if not trades:
        if _tracker:
            _tracker.complete("无可执行交易")
        return
    
    if _tracker:
        _tracker.step(2, f"执行 {len(trades)} 笔交易")
    
    results = execute_trades(
        trades,
        args.market,
        enforce_prediction_freshness=not args.force,
    )
    
    if _tracker:
        _exec = sum(1 for r in results if r["status"] == "executed")
        _fail = sum(1 for r in results if r["status"] in ("failed", "error"))
    
    executed = sum(1 for r in results if r["status"] == "executed")
    failed = sum(1 for r in results if r["status"] in ("failed", "error"))
    total = len(results)
    print(f"\n📊 执行结果: {executed}成功 / {failed}失败 / {total}总计")
    
    # v4.5.13: 飞书自报告
    try:
        from common.feishu_utils import send_markdown
        detail_lines = []
        for r in results[:10]:  # 最多显示10笔
            code = r.get("code", "?")
            action = r.get("action", "?")
            status = "✅" if r.get("status") == "executed" else "❌"
            qty = r.get("qty", r.get("quantity", 0))
            price = r.get("price", 0)
            detail_lines.append(f"{status} {code} {action} {qty}股 @{price:.2f}")
        detail_str = '\n'.join(detail_lines) if detail_lines else '     无交易记录'
        report = f"""**💹 交易执行 | {datetime.now().strftime('%Y-%m-%d %H:%M')}**
**结果**: {executed}成功 / {failed}失败 / {total}总计
{detail_str}"""
        send_markdown(title=f"💹 交易执行 | {datetime.now().strftime('%Y-%m-%d')}", content=report)
    except Exception:
        pass
    
    # v4.6.x: ProgressTracker complete
    if _tracker:
        _tracker.step(3, f"完成: {_exec}成功/{_fail}失败/{len(results)}总计")
        _tracker.complete(f"{_exec}笔执行, {_fail}笔失败")
    
    # ──────────── v4.6.9i(审计F1-5): 每日账目对账 ────────────
    if _trader is not None:
        try:
            _recon = _trader.run_portfolio_reconciliation()
            _status = "✅" if _recon.get("ok") else "🔴"
            print(f"{_status} 账目对账: 现金{_recon.get('cash', 0):,.0f} + 持仓{_recon.get('pos_value', 0):,.0f} "
                  f"= {_recon.get('equity_implied', 0):,.0f} vs 账面{_recon.get('equity_stored', 0):,.0f} "
                  f"(差异{_recon.get('diff', 0):+,.2f})")
            if not _recon.get("ok"):
                try:
                    from common.feishu_utils import send_markdown as _fs
                    _fs(f"🔴 **DSL账目对账异常**\n\n现金+持仓 ≠ 账面权益, 差异 {_recon.get('diff', 0):+,.2f} 元\n\n"
                        f"请检查 paper_trading.db ledger/positions/performance_metrics")
                except Exception:
                    pass
        except Exception as _re2:
            print(f"⚠️ 账目对账失败: {_re2}")
    
    return results


if __name__ == "__main__":
    # v4.6.x: ProgressTracker from main()
    _r = main()
