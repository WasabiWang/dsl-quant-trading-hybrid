#!/usr/bin/env python3
"""
DSL v4.5.3b 盘中信号监控器 — 轻量异常检测 + P1实时预测刷新
  09:45 开盘15分钟检测 → 过滤跳空假信号 + 刷新异动股预测
  11:00 午前确认 → 检测上午趋势反转 + 刷新异动股预测
  14:00 仅告警不干预 → 避免尾盘追涨杀跌
"""
import os
import sys, os, json, subprocess
from datetime import datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 网络代理配置：绕过VPN/代理工具访问国内金融数据源
os.environ['NO_PROXY'] = 'eastmoney.com,akshare.cn,sina.com.cn,push2.eastmoney.com,push2his.eastmoney.com,api.mairuiapi.com,a.mairuiapi.com,127.0.0.1,localhost,*.eastmoney.com,*.akshare.cn,*.sina.com.cn,*.qq.com,*.163.com,*.ifeng.com,*.hexun.com,*.stockstar.com,*.cnfol.com,*.gtimg.cn,*.sinajs.cn,*.dfcfw.com'
os.environ['no_proxy'] = os.environ['NO_PROXY']

# 阈值
GAP_THRESHOLD = 0.03    # 3%偏离 → 取消信号
WARN_THRESHOLD = 0.015  # 1.5%偏离 → 仅告警
REFRESH_THRESHOLD = 0.02  # P1: 2%异动 → 刷新预测

def fetch_realtime_price(code: str) -> dict:
    """获取盘中实时价格（麦蕊API优先，akshare备用）"""
    import signal
    
    # 主数据源：麦蕊API
    try:
        from config.mairui_api_config import get_stock_real
        real = get_stock_real(code)
        price = float(real.get("current_price", 0) or 0)
        if price and price > 0:
            return {
                'price': price,
                'change_pct': float(real.get("change_percent", 0) or 0),
                'volume': float(real.get("volume", 0) or 0),
            }
    except Exception:
        pass
    
    # 备用源：akshare
    try:
        from common.akshare_utils import safe_stock_zh_a_spot_em
        df = safe_stock_zh_a_spot_em()
        if df is not None:
            df['code_num'] = df['代码'].str.replace('^(sh|sz|bj)', '', regex=True)
            row = df[df['code_num'] == code]
            if len(row) > 0:
                r = row.iloc[0]
                return {
                    'price': float(r['最新价']),
                    'change_pct': float(r['涨跌幅']),
                    'volume': float(r.get('成交量', 0)),
                }
    except Exception as e:
        print(f"  ⚠️ {code}(akshare备用): {e}")
    return None


def load_predictions() -> list:
    """加载最新预测中的高信度信号（v4.5.12: 增加日期校验）"""
    pred_file = os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")
    if not os.path.exists(pred_file):
        return []
    with open(pred_file) as f:
        data = json.load(f)
    # v4.5.12 P1-1: 日期校验 — 非今日预测降权告警
    predict_date = data.get('predict_date', '')
    today = datetime.now().strftime('%Y-%m-%d')
    if predict_date and predict_date != today:
        days_old = 0
        try:
            days_old = (datetime.strptime(today, '%Y-%m-%d') - datetime.strptime(predict_date, '%Y-%m-%d')).days
        except Exception:
            pass
        if days_old > 1:
            print(f"⛔ 预测数据过期{days_old}天(predict_date={predict_date}), 回退为空列表")
            return []
        elif days_old == 1:
            print(f"⚠️ 预测数据为昨天(predict_date={predict_date}), signal_weight×0.7")
            for p in data.get('predictions', []):
                p['signal_weight'] = round(p.get('signal_weight', 1.0) * 0.7, 2)
                p['stale_warning'] = True
    preds = [p for p in data.get('predictions', [])
             if p.get('confidence_level') == 'high' and p.get('signal') != 'hold']
    if predict_date != today:
        print(f"  📊 加载{len(preds)}条高信度信号(日期{predict_date}≠今天, 已降权)")
    return preds


def refresh_intraday_predictions(price_cache: dict) -> int:
    """P1: 对异动>2%的股票重新拉K线+实时推理，刷新daily_predict.json"""
    import numpy as np
    refreshed = 0
    pred_file = os.path.join(PROJECT_ROOT, "cache", "daily_predict.json")
    if not os.path.exists(pred_file):
        return 0
    with open(pred_file) as f:
        data = json.load(f)

    predictions = data.get('predictions', [])
    # 筛选异动>2%且非hold的股票
    mover_codes = []
    for code, rt in price_cache.items():
        if abs(rt.get('change_pct', 0)) >= REFRESH_THRESHOLD * 100:
            mover_codes.append(code)

    if not mover_codes:
        return 0

    print(f"  📡 P1实时刷新: {len(mover_codes)}只异动>2%股票重新推理...")
    try:
        from core.pool_predictor import get_predictor
        from dsl_data_sdk_original import get_kline, normalize_symbol
        predictor = get_predictor()

        for code in mover_codes[:10]:  # 最多10只，控制延迟
            try:
                normal = normalize_symbol(code)
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
                kline = get_kline(normal, start, end)
                if not kline or len(kline) < 60:
                    continue
                import pandas as pd
                df = pd.DataFrame(kline)
                for col in ["close", "volume", "high", "low", "open"]:
                    if col in df.columns:
                        df[col] = df[col].astype(float)

                pred = predictor.predict(code, df)
                if pred.get('signal') == 'hold':
                    continue
                # 更新predicted_return和confidence
                for p in predictions:
                    if p.get('symbol') == code:
                        p['predicted_return'] = pred.get('predicted_return', 0)
                        p['score'] = pred.get('score', 5)
                        p['confidence'] = pred.get('confidence', 0.5)
                        p['intraday_refreshed'] = True
                        p['intraday_change'] = price_cache.get(code, {}).get('change_pct', 0)
                        refreshed += 1
                        name = p.get('name', code)
                        new_ret = pred.get('predicted_return', 0)
                        print(f"    ✅ {code} {name}: 预测刷新→{new_ret:+.3%}")
                        break
            except Exception as e:
                print(f"    ⚠️ {code} 刷新失败: {e}")

        if refreshed > 0:
            data['predictions'] = predictions
            data['intraday_refreshed_at'] = datetime.now().isoformat()
            data['intraday_refreshed_count'] = refreshed
            with open(pred_file, 'w') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"  ⚠️ P1实时刷新异常: {e}")

    return refreshed


def check_signal_health(pred: dict, realtime: dict, session: str) -> dict:
    """检查信号是否还健康"""
    action = pred['signal'].lower()  # v4.5.14: 规范化大小写，避免来源格式变化导致匹配失败
    change = realtime['change_pct']
    issues = []

    if action == 'buy' and change > GAP_THRESHOLD * 100:
        issues.append(f"BUY信号但已高开{change:+.1f}%，追高风险")
    elif action == 'buy' and change < -GAP_THRESHOLD * 100:
        issues.append(f"BUY信号但已跌{change:+.1f}%")
    elif action == 'sell' and change > GAP_THRESHOLD * 100:
        issues.append(f"SELL信号但已涨{change:+.1f}%")
    elif action == 'buy' and change < -WARN_THRESHOLD * 100:
        issues.append(f"⚠️ BUY信号但微跌{change:+.1f}%")
    elif action == 'sell' and change > WARN_THRESHOLD * 100:
        issues.append(f"⚠️ SELL信号但微涨{change:+.1f}%")

    if issues:
        return {'status': 'cancel' if abs(change) > GAP_THRESHOLD * 100 else 'warn',
                'issues': issues, 'change': change}
    return {'status': 'ok', 'change': change}


def write_override(cancelled: list):
    """写入信号撤销记录"""
    override_file = os.path.join(PROJECT_ROOT, "cache", "signal_override.json")
    overrides = []
    if os.path.exists(override_file):
        with open(override_file) as f:
            overrides = json.load(f)

    existing = {o['symbol']: i for i, o in enumerate(overrides)}
    for c in cancelled:
        entry = {
            'symbol': c['symbol'], 'name': c['name'],
            'action': 'cancel', 'reason': c['reason'],
            'time': datetime.now().isoformat(),
            'original_signal': c['original_signal'],
        }
        if c['symbol'] in existing:
            overrides[existing[c['symbol']]] = entry
        else:
            overrides.append(entry)

    with open(override_file, 'w') as f:
        json.dump(overrides, f, ensure_ascii=False, indent=2)


def run(session: str = "auto"):
    """执行一次盘中信号监控"""
    now = datetime.now()
    hhmm = now.strftime('%H:%M') if session == 'auto' else session
    is_afternoon = session in ('14:00',) or (session == 'auto' and now.hour >= 14)
    
    # 进度追踪 (映射session名到标准task_id)
    session_map = {"09:45": "intraday_monitor_0945", "11:00": "intraday_monitor_1100", "14:00": "intraday_monitor_1400"}
    task_name = session_map.get(session if session != 'auto' else hhmm, f"intraday_monitor_{hhmm.replace(':','')}")
    from common.progress_tracker import ProgressTracker
    tracker = ProgressTracker(task_name, total_steps=3)
    tracker.step(1, "加载预测信号")

    print(f"{'='*60}")
    print(f"🔍 DSL 盘中信号监控 [{hhmm}]")
    print(f"{'='*60}")

    high_conf = load_predictions()
    if not high_conf:
        print("ℹ️ 无高信度信号")
        tracker.complete("无高信度信号", session=session)
        return

    # 批量获取实时行情（麦蕊API优先，akshare备用）
    tracker.step(2, "获取实时行情")
    print(f"📡 获取实时行情...")
    price_cache = {}
    
    # 主数据源：麦蕊多股行情API
    try:
        from config.mairui_api_config import get_multi_stock_real
        stock_codes = [p['symbol'] for p in high_conf]
        # 麦蕊每次最多20只，分批查询
        chunk_size = 20
        for i in range(0, len(stock_codes), chunk_size):
            chunk = stock_codes[i:i+chunk_size]
            try:
                batch = get_multi_stock_real(chunk)
                if isinstance(batch, list):
                    for b in batch:
                        code = str(b.get('dm', ''))[-6:]
                        if code:
                            price_cache[code] = {
                                'price': float(b.get('p', 0) or 0),
                                'change_pct': float(b.get('pc', 0) or 0),
                                'volume': float(b.get('v', 0) or 0),
                            }
            except Exception as chunk_e:
                print(f"  ⚠️ 麦蕊分批查询(第{i//chunk_size+1}批)失败: {chunk_e}")
                # 降级：逐只查询（比akshare快，且不依赖pyecharts）
                try:
                    from config.mairui_api_config import get_stock_real
                    for sc in chunk:
                        try:
                            real = get_stock_real(sc)
                            price = float(real.get('current_price', 0) or 0)
                            if price > 0:
                                price_cache[sc] = {
                                    'price': price,
                                    'change_pct': float(real.get('change_percent', 0) or 0),
                                    'volume': float(real.get('volume', 0) or 0),
                                }
                        except Exception as se:
                            print(f"    ⚠️ {sc} 单只查询也失败: {se}")
                except Exception:
                    pass
    except Exception as e:
        print(f"  ⚠️ 麦蕊API不可用: {e}")
    
    # 备用源：akshare（仅当麦蕊未覆盖全部标的时才调用）
    missing = [p['symbol'] for p in high_conf if p['symbol'] not in price_cache]
    if missing:
        print(f"  📡 麦蕊未覆盖{len(missing)}只, 调用akshare备用源...")
        try:
            from common.akshare_utils import safe_stock_zh_a_spot_em
            df = safe_stock_zh_a_spot_em()
            if df is not None:
                df['code_num'] = df['代码'].str.replace('^(sh|sz|bj)', '', regex=True)
                for _, r in df.iterrows():
                    code = str(r['code_num'])
                    if code in missing:
                        price_cache[code] = {
                            'price': float(r['最新价']),
                            'change_pct': float(r['涨跌幅']),
                            'volume': float(r.get('成交量', 0)),
                        }
        except Exception as e:
            print(f"  ⚠️ akshare备用源失败: {e}")
    else:
        print(f"  ✅ 麦蕊已覆盖全部{len(high_conf)}只, 跳过akshare备用源")
    
    if not price_cache:
        print("⏸️ 行情数据为空，跳过监控")
        return

    if not price_cache:
        print("⏸️ 行情数据为空，跳过监控")
        tracker.complete("行情数据为空", session=session)
        return

    # P1: 对异动>2%的股票实时刷新预测（09:45和11:00执行，14:00跳过）
    if not is_afternoon and price_cache:
        refreshed = refresh_intraday_predictions(price_cache)
        if refreshed > 0:
            # 重新加载预测（已包含刷新后的数据）
            high_conf = load_predictions()
            print(f"  📡 已刷新{refreshed}只股票的预测")

    tracker.step(3, f"检查{len(high_conf)}个信号", signal_count=len(high_conf))
    print(f"📋 检查 {len(high_conf)} 个高信度信号...")
    cancelled = []
    warned = []

    for pred in high_conf:
        code = pred['symbol']
        name = pred['name']
        rt = price_cache.get(code)
        if not rt:
            print(f"  ⚠️ {code} {name}: 行情获取失败")
            continue

        result = check_signal_health(pred, rt, session)
        if result['status'] == 'cancel':
            if is_afternoon:
                print(f"  🟡 {code} {name}: {pred['signal']} 已偏离{result['change']:+.1f}% (14:00不干预)")
                continue
            print(f"  ❌ {code} {name}: 取消{pred['signal']}信号 ({'; '.join(result['issues'])})")
            cancelled.append({
                'symbol': code, 'name': name,
                'original_signal': pred['signal'],
                'reason': result['issues'][0],
            })
        elif result['status'] == 'warn':
            print(f"  ⚠️ {code} {name}: {pred['signal']} ({'; '.join(result['issues'])})")
            warned.append({'symbol': code, 'name': name, 'reason': result['issues'][0]})
        else:
            print(f"  ✅ {code} {name}: {pred['signal']} 正常 ({result['change']:+.1f}%)")

    # 写入撤销
    if cancelled and not is_afternoon:
        write_override(cancelled)
        print(f"\n🚫 已撤销 {len(cancelled)} 个信号")
        # 发送飞书告警
        try:
            from common.feishu_utils import send_markdown
            cancel_list = '\n'.join(f"- {c['name']}({c['symbol']}): 原{c['original_signal']}, {c['reason']}" for c in cancelled)
            send_markdown(f"🚫 盘中信号撤销 [{hhmm}]\n\n{cancel_list}")
        except Exception:
            pass

    print(f"\n📊 结果: {len(high_conf)}高信度 → ✅{len(high_conf)-len(cancelled)-len(warned)}正常 | ⚠️{len(warned)}告警 | ❌{len(cancelled)}撤销")
    tracker.complete(f"信号监控完成 [{hhmm}]", session=session, high_signals=len(high_conf), cancelled=len(cancelled), warned=len(warned))
    return {'cancelled': cancelled, 'warned': warned}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', default='auto', help='09:45/11:00/14:00')
    args = parser.parse_args()
    run(args.session)
