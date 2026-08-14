#!/usr/bin/env python3
"""
DSL v4.5.14 — ATR动态止损 + 三层止盈 监控执行器

用途: 盘中定期扫描持仓, 检查ATR动态止损 + 固定/RS动态/移动止盈, 自动执行卖出
执行时机: 09:50 / 11:05 / 14:05 (紧跟盘中信号监控)
数据流: 麦蕊获取持仓实时价 → 止损检查 + 止盈检查 → 自动执行 + 飞书告警

v4.5.14 修复: 新增盘中止盈检查(固定止盈/RS动态止盈/移动止盈),
         填补 stop_loss_monitor 只查止损不查止盈的缺口
"""
import os, sys, json
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'scripts'))

# 飞书通知
HAS_FEISHU = False
try:
    from common.feishu_utils import send_markdown as _feishu_send
    HAS_FEISHU = True
except ImportError:
    pass


def _send_alert(title: str, content: str):
    """发送飞书告警"""
    if HAS_FEISHU:
        try:
            _feishu_send(title=title, content=content)
        except Exception as e:
            print(f"  ⚠️ 飞书发送失败: {e}")
    print(f"  📢 {title}: {content[:200]}")


def main():
    print(f"\n{'='*50}")
    print(f" 🛡️ DSL 止损+止盈 监控 — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*50}")

    from paper_trader import PaperTrader
    import yaml

    # 1. 获取所有持仓
    trader = PaperTrader()
    summary = trader.get_portfolio_summary()
    positions = summary.get('positions', []) if summary else []

    if not positions:
        print("📭 无持仓, 跳过检查")
        return

    print(f"📊 当前持仓: {len(positions)}只")

    # 2. 获取实时价格(麦蕊优先, akshare fallback)
    price_dict = {}
    all_codes = [pos.get('stock_code', '') for pos in positions if pos.get('stock_code')]

    # 麦蕊实时行情(批量, 最多20只)
    try:
        from config.mairui_api_config import get_multi_stock_real
        real_list = get_multi_stock_real(all_codes[:20])
        if isinstance(real_list, list):
            for item in real_list:
                dm = str(item.get('dm', ''))
                p = item.get('p') or item.get('current')
                if dm and p:
                    price_dict[dm] = float(p)
    except Exception as e:
        print(f"  ⚠️ 麦蕊批量行情失败: {e}")

    missing = [c for c in all_codes if c not in price_dict]
    if missing:
        print(f"⚠️ {len(missing)}只缺失麦蕊行情, 尝试akshare...")
        try:
            import akshare as ak
            df_all = ak.stock_zh_a_spot()  # v4.6.9i(审计F1-3): 降级源东财→新浪(东财push2实测封锁)
            if df_all is not None and not df_all.empty:
                df_all['代码6'] = df_all['代码'].astype(str).str[-6:]  # 新浪格式sh600519→6位
                for code in missing:
                    row = df_all[df_all['代码6'] == code]
                    if not row.empty:
                        price_dict[code] = float(row.iloc[0].get('最新价', 0))
        except Exception as e:
            print(f"  ⚠️ akshare拉取失败: {e}")

    if not price_dict:
        # v4.6.9i(审计F1-3): 数据全空时暂停交易+飞书告警(替代静默跳过, 审计P0-3)
        print("❌ 无法获取任何实时价格 — 暂停交易并告警")
        try:
            from core.circuit_breaker import CircuitBreaker
            CircuitBreaker().pause("止损监控数据源全空(麦蕊+降级均失败), 自动暂停2小时", hours=2.0)
        except Exception as _ce:
            print(f"⚠️ 暂停交易写入失败: {_ce}")
        if HAS_FEISHU:
            try:
                _feishu_send("🔴 **DSL告警: 止损监控数据源全空**\n\n"
                             "盘中实时价格获取失败(麦蕊与降级源均不可用)。\n\n"
                             "**处置**: 已自动暂停交易2小时。请人工检查数据源后手动解除:\n"
                             "`data/circuit_breaker.json` 的 `trading_paused` 置 false")
            except Exception:
                pass
        return

    print(f"✅ 已获取{len(price_dict)}只股票实时价")

    # ═══════════════ 3. 止损检查 + 自动执行 ═══════════════
    result = trader.check_stop_losses(price_dict)

    triggered = result.get('triggered', 0)
    executed = result.get('executed', [])
    errors = result.get('errors', [])
    portfolio_pct = result.get('portfolio_pnl_pct', 0)

    # ──────────── v4.6.9i(审计F1-4): 熔断器当日回撤回写(盘中多个检查点) ────────────
    try:
        from core.circuit_breaker import CircuitBreaker
        _cb = CircuitBreaker()
        _ledger = trader.load_ledger()
        _cash = float(_ledger.get('current_cash', 0))
        _pos_val = sum(float(p.get('quantity', 0)) * float(p.get('current_price', 0))
                       for p in _ledger.get('positions', []))
        _equity = _cash + _pos_val
        if _equity > 0:
            _dd = _cb.update_equity_drawdown(_equity)
            print(f"  📉 熔断回撤: 当日 {_dd:+.2%}")
    except Exception as _ce2:
        print(f"⚠️ 熔断回撤更新失败: {_ce2}")

    if triggered > 0:
        print(f"\n🔴 止损触发: {triggered}笔")
        for e in executed:
            code = e.get('stock_code', '?')
            pnl = e.get('pnl_pct', 0)
            reason = e.get('reason', 'unknown')
            print(f"   🔴 {code}: 盈亏{pnl:.1f}%, 原因={reason}")
    else:
        print("✅ 止损: 全部持仓在止损线内")

    # v4.6.x P2: 止损后刷新持仓，避免已清仓标的被止盈循环空卖
    if executed:
        refreshed = trader.get_portfolio_summary()
        positions = refreshed.get('positions', []) if refreshed else positions
        # 从price_dict中移除已清仓标的
        sold_codes = {e.get('stock_code', '') for e in executed}
        price_dict = {k: v for k, v in price_dict.items() if k not in sold_codes}
        print(f"  📋 止损后持仓刷新: {len(positions)}只 (已清除{sold_codes})")

    # ═══════════════ 4. v4.5.14: 盘中止盈检查 ═══════════════
    tp_triggered = 0
    tp_executed = []
    try:
        from core.risk_manager import RiskManager
        rm = RiskManager()
        print(f"\n🟢 止盈检查 (固定/RS动态/移动):")
        for pos in positions:
            code = pos.get('stock_code', '')
            name = pos.get('stock_name', pos.get('name', code))
            avg_price = pos.get('avg_price', pos.get('avg_cost', 0))
            cur_price = price_dict.get(code, 0)
            if not code or avg_price <= 0 or cur_price <= 0:
                continue
            tp = rm.check_single_position_take_profit(
                symbol=code, name=name,
                avg_price=avg_price, current_price=cur_price
            )
            if tp.get('triggered'):
                tp_triggered += 1
                reason = tp.get('reason', 'unknown')
                pnl_pct = tp.get('pct', 0)
                print(f"   🟢 {name}({code}): 盈利{pnl_pct:.1%} | {reason}")
                # 执行止盈卖出
                try:
                    qty = pos.get('shares', pos.get('quantity', 0))
                    if qty > 0:
                        sell_result = trader.execute_trade(
                            market='A', stock_code=code,
                            action='SELL', quantity=qty,
                            price=cur_price, reason=f"止盈: {reason}"
                        )
                        tp_executed.append({
                            'stock_code': code, 'name': name,
                            'pnl_pct': pnl_pct, 'reason': reason,
                            'sell_result': sell_result
                        })
                        print(f"      ✅ 已卖出 {qty}股 @ {cur_price}")
                except Exception as se:
                    import logging; logging.getLogger(__name__).error(f"止盈卖出失败 {name}({code}): {se}", exc_info=True)
                    print(f"      ❌ 卖出失败: {se}")
                    tp_executed.append({
                        'stock_code': code, 'name': name,
                        'pnl_pct': pnl_pct, 'reason': reason,
                        'error': str(se)
                    })
        if tp_triggered == 0:
            print("   ✅ 无持仓触发止盈")
    except Exception as te:
        print(f"  ⚠️ 止盈检查失败: {te}")

    # ═══════════════ 5. 汇总输出 ═══════════════
    print(f"\n📊 监控结果:")
    print(f"   止损: {triggered}触发 / {len(executed)}执行")
    print(f"   止盈: {tp_triggered}触发 / {len(tp_executed)}执行")
    if errors:
        print(f"   错误: {len(errors)}笔")
    print(f"   组合盈亏: {portfolio_pct:.1f}%")

    # ═══════════════ 6. 飞书告警 ═══════════════
    alert_parts = []
    if triggered > 0:
        for e in executed:
            code = e.get('stock_code', '?')
            pnl = e.get('pnl_pct', 0)
            reason = e.get('reason', 'unknown')
            alert_parts.append(f"🔴 **{code}**: {pnl:.1f}% | {reason}")
    if tp_triggered > 0:
        for e in tp_executed:
            code = e.get('stock_code', '?')
            pnl = e.get('pnl_pct', 0)
            reason = e.get('reason', 'unknown')
            alert_parts.append(f"🟢 **{code}**: +{pnl:.1%} | {reason}")

    if alert_parts:
        title = f"🛡️ 仓位监控: 止损{triggered}/止盈{tp_triggered}"
        content = f"**盘中监控 - {datetime.now().strftime('%H:%M')}**\n\n"
        content += f"组合盈亏: {portfolio_pct:.1f}%\n\n"
        content += "\n".join(alert_parts)
        if portfolio_pct <= -5:
            content += f"\n\n⚠️ 组合回撤{portfolio_pct:.1f}% > 5%, 暂停新开仓"
        _send_alert(title, content)

    if triggered == 0 and tp_triggered == 0:
        print("✅ 全部持仓在止损/止盈线内, 无需操作")

    # ═══════════════ 7. JSON镜像同步 ═══════════════
    try:
        trader._sync_to_json()
        print(f"  📊 持仓JSON镜像已同步(含最新价格)")
    except Exception as _e:
        print(f"  ⚠️ JSON镜像同步失败: {_e}")

    result['tp_triggered'] = tp_triggered
    result['tp_executed'] = tp_executed
    return result


if __name__ == '__main__':
    # v4.5.13: ProgressTracker for Dashboard
    try:
        from common.progress_tracker import ProgressTracker
        _tracker = ProgressTracker("stop_loss_monitor", total_steps=2)
        _tracker.step(1, "止损+止盈扫描")
    except Exception:
        _tracker = None

    _result = main()

    if _tracker:
        _triggered = _result.get('triggered', 0) if _result else 0
        _executed = len(_result.get('executed', [])) if _result else 0
        _tp_triggered = _result.get('tp_triggered', 0) if _result else 0
        _tp_executed = len(_result.get('tp_executed', [])) if _result else 0
        _errors = len(_result.get('errors', [])) if _result else 0
        _tracker.complete(
            f"止损:{_triggered}触发/{_executed}执行 | 止盈:{_tp_triggered}触发/{_tp_executed}执行 | 错误:{_errors}"
        )
