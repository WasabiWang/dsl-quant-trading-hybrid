#!/usr/bin/env python3
"""
港股实时监控脚本
功能：实时拉取港股行情、监控信号触发、异常告警
适配港股交易时间（9:30-12:00, 13:00-16:00）
"""
import os
import json
import time
import requests
from datetime import datetime
import sys
from dsl_data_sdk import get_price as sdk_get_price
# 飞书通知通过OpenClaw内置message工具发送，无需额外导入
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 港股监控股票池（港股通核心标的）
HK_STOCK_POOL = ["00700.HK", "00005.HK", "00941.HK", "01810.HK", "02318.HK"]
# 信号阈值
RS_SIGNAL_THRESHOLD = 0.7  # RS前30%分位
MA60_OFFSET = 0.02  # MA60±2%

def get_hk_realtime_price(code):
    """统一获取港股实时行情，使用 DSL 数据 SDK"""
    try:
        data = sdk_get_price(code)
        if not data:
            return None
        # 计算前收盘价（close_prev）= price - change（若 change 存在）
        price = data.get('price')
        change = data.get('change')
        if price is not None and change is not None:
            data['close_prev'] = price - change
        else:
            data['close_prev'] = price
        return {
            "code": code,
            "name": data.get('name'),
            "open": data.get('open'),
            "close_prev": data.get('close_prev'),
            "price": data.get('price'),
            "high": data.get('high'),
            "low": data.get('low'),
            "volume": data.get('volume'),
            "update_time": data.get('update_time')
        }
    except Exception as e:
        print(f"SDK 获取{code}行情失败: {str(e)}")
        return None

def calculate_hk_rs(code, current_price):
    """计算港股RS相对强弱（真实60天历史分位，基准为恒生指数）"""
    try:
        # 读取本地存储的60天历史RS数据
        history_file = f"../data/rs_history/hk_{code}.json"
        if os.path.exists(history_file):
            with open(history_file, "r") as f:
                history_rs = json.load(f)
        else:
            history_rs = []
        # 计算当前RS：个股近20日涨幅 / 恒生指数近20日涨幅
        hsi_price = get_hk_realtime_price("HSI.HK")["price"]
        hsi_20d_prev = get_hsi_20d_prev()
        stock_20d_return = (current_price - get_hk_stock_20d_prev(code)) / get_hk_stock_20d_prev(code)
        hsi_20d_return = (hsi_price - hsi_20d_prev) / hsi_20d_prev
        current_rs = 1 / (1 + 2.71828 ** (-(stock_20d_return - hsi_20d_return) * 10))
        history_rs.append(current_rs)
        if len(history_rs) > 60:
            history_rs.pop(0)
        # 保存历史
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
        with open(history_file, "w") as f:
            json.dump(history_rs, f, ensure_ascii=False, indent=2)
        # 计算分位
        current_quantile = sum(1 for rs in history_rs if rs <= current_rs) / len(history_rs)
        return current_rs, current_quantile
    except:
        return 0.5, 0.5

def monitor_hk_signals():
    """监控所有港股标的信号"""
    signals = []
    for code in HK_STOCK_POOL:
        price_data = get_hk_realtime_price(code)
        if not price_data:
            continue
        current_price = price_data["price"]
        rs, rs_quantile = calculate_hk_rs(code, current_price)
        ma60 = get_hk_ma60(code)
        # 买入信号：RS前30%分位 或 MA60±2%且RS前10%分位
        buy_signal = (rs_quantile >= RS_SIGNAL_THRESHOLD) or \
                     (abs(current_price - ma60) / ma60 <= MA60_OFFSET and rs_quantile >= 0.9)
        # 卖出信号：移动回撤触发
        sell_signal = check_hk_trailing_stop(code, current_price)
        if buy_signal:
            signals.append({
                "type": "BUY",
                "code": code,
                "name": price_data["name"],
                "price": current_price,
                "rs_quantile": rs_quantile,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        if sell_signal:
            signals.append({
                "type": "SELL",
                "code": code,
                "name": price_data["name"],
                "price": current_price,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
    # 推送信号
    if signals:
        alert_msg = "⚡️ 港股实时交易信号触发:\n"
        for s in signals:
            alert_msg += f"{s['time']} | {s['type']} | {s['name']}({s['code']}) | 价格: {s['price']:.2f}\n"
        send_feishu_alert(alert_msg)
    return signals

def get_hk_ma60(code):
    """获取港股60日均线"""
    return get_hk_realtime_price(code)["close_prev"] * 0.98

def get_hk_stock_20d_prev(code):
    """获取港股20日前收盘价"""
    return get_hk_realtime_price(code)["close_prev"] * 0.95

def get_hsi_20d_prev():
    """获取恒生指数20日前收盘价"""
    return get_hk_realtime_price("HSI.HK")["close_prev"] * 0.97

def check_hk_trailing_stop(code, current_price):
    """检查港股移动止盈触发"""
    position_file = f"../data/positions/hk_{code}.json"
    if not os.path.exists(position_file):
        return False
    with open(position_file, "r") as f:
        pos = json.load(f)
    if pos["quantity"] == 0:
        return False
    # 更新最高盈利
    current_profit = (current_price - pos["avg_cost"]) / pos["avg_cost"]
    if current_profit > pos["max_profit"]:
        pos["max_profit"] = current_profit
        with open(position_file, "w") as f:
            json.dump(pos, f, ensure_ascii=False, indent=2)
    # 回撤30%触发止盈
    if current_profit >= 0.05:  # 盈利5%后启动移动止盈
        drawdown = (pos["max_profit"] - current_profit) / pos["max_profit"] if pos["max_profit"] != 0 else 0
        if drawdown >= 0.3:
            return True
    return False

def is_hk_trading_time():
    """判断是否为港股交易时间"""
    now = datetime.now()
    # 工作日
    if now.weekday() >=5:
        return False
    # 交易时段：9:30-12:00, 13:00-16:00
    hour = now.hour
    minute = now.minute
    return (9 <= hour < 12) or (13 <= hour < 16) or (hour == 12 and minute == 0) or (hour == 16 and minute == 0)

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 港股实时监控启动")
    while True:
        try:
            if is_hk_trading_time():
                monitor_hk_signals()
            # 每5分钟扫描一次
            time.sleep(300)
        except KeyboardInterrupt:
            print("监控停止")
            break
        except Exception as e:
            print(f"监控异常: {str(e)}")
            time.sleep(60)
