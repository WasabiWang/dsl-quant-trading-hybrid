#!/usr/bin/env python3
"""
A股实时监控脚本
功能：实时拉取行情、监控信号触发、异常告警
"""
import os
import json
import time
import requests
from datetime import datetime, timedelta
import sys

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dsl_data_sdk import get_price as sdk_get_price
except ImportError:
    # 备用方案：直接导入
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "dsl_data_sdk", 
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsl_data_sdk.py")
    )
    dsl_data_sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dsl_data_sdk)
    sdk_get_price = dsl_data_sdk.get_price

# 飞书通知通过 common.feishu_utils 发送
try:
    from common.feishu_utils import send_markdown as _feishu_send_markdown
    HAS_FEISHU = True
except ImportError:
    HAS_FEISHU = False

def send_feishu_alert(msg, title="⚡️ A股实时监控告警"):
    """发送飞书告警，优先App API，回退打印"""
    if HAS_FEISHU:
        try:
            _feishu_send_markdown(title=title, content=msg)
            print(f"  📤 飞书告警已发送: {title}")
            return True
        except Exception as e:
            print(f"  ⚠️ 飞书告警发送失败: {e}")
    else:
        print(f"  📢 [飞书未配置] {title}: {msg}")
    return False

# 监控股票池（默认核心持仓）
STOCK_POOL = ["sh600519", "sh601318", "sz000858", "sz002475", "sz002594"]
# 信号阈值
RS_SIGNAL_THRESHOLD = 0.7  # RS前30%分位
MA60_OFFSET = 0.02  # MA60±2%
# 信号冷却机制
# ⛔ 重要：冷却缓存使用文件持久化（不是内存），因为crontab每次启动新进程
# 文件路径：/tmp/dsl_signal_cooldown.json
SIGNAL_COOLDOWN_MINUTES = 60  # 同一标的一类信号 60 分钟内不重复推送
COOLDOWN_FILE = "/tmp/dsl_signal_cooldown.json"
# 蓝筹股白名单（无ML模型，盘整期RA信号易误判）
# 这些股票即使MA60条件触发，也不推送交易信号
# 原因：大盘蓝筹流动性深、波动窄，MA60±2%条件在日常震荡中频繁满足
_COOLDOWN_BLUECHIPS = {"sh601318", "sh601398", "sh601288", "sh601939", "sh601988",
                       "sh600036", "sh601166", "sh600000", "sh600016",
                       "sh600519", "sh600887", "sh600276", "sh601899", "sh600900"}

def get_realtime_price(code):
    """统一获取实时行情，使用 DSL 数据 SDK，并补全 close_prev 字段"""
    try:
        data = sdk_get_price(code)
        if not data:
            return None
        # 计算前收盘价（close_prev）: price - change（若 change 可得）
        price = data.get('price')
        change = data.get('change')
        if price is not None and change is not None:
            data['close_prev'] = price - change
        else:
            data['close_prev'] = price
        return data
    except Exception as e:
        print(f"SDK 获取{code}行情失败: {str(e)}")
        return None

def calculate_rs(code, current_price):
    """计算RS相对强弱（真实60天历史分位）"""
    try:
        # 读取本地存储的60天历史RS数据
        history_file = f"../data/rs_history/{code}.json"
        if os.path.exists(history_file):
            with open(history_file, "r") as f:
                history_rs = json.load(f)
        else:
            history_rs = []
        # 计算当前RS：个股近20日涨幅 / 沪深300近20日涨幅
        hs300_price = get_realtime_price("sh000300")["price"]
        hs300_20d_prev = get_hs300_20d_prev()
        stock_20d_return = (current_price - get_stock_20d_prev(code)) / get_stock_20d_prev(code)
        hs300_20d_return = (hs300_price - hs300_20d_prev) / hs300_20d_prev
        current_rs = 1 / (1 + 2.71828 ** (-(stock_20d_return - hs300_20d_return) * 10))
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

def _load_cooldown_cache() -> dict:
    """从文件加载冷却缓存（兼容跨进程）"""
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_cooldown_cache(cache: dict):
    """保存冷却缓存到文件"""
    try:
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(cache, f)
    except Exception:
        pass


def _is_cooldown(code, signal_type) -> bool:
    """检查冷却期（文件持久化，跨进程）"""
    key = f"{code}_{signal_type}"
    now = datetime.now()
    cache = _load_cooldown_cache()
    if key in cache:
        try:
            ts = datetime.fromisoformat(cache[key])
            elapsed = (now - ts).total_seconds() / 60
            if elapsed < SIGNAL_COOLDOWN_MINUTES:
                return True
        except Exception:
            pass
    cache[key] = now.isoformat()
    _save_cooldown_cache(cache)
    return False


def monitor_signals():
    """监控所有标的信号（带冷却去重）"""
    signals = []
    now = datetime.now()
    for code in STOCK_POOL:
        price_data = get_realtime_price(code)
        if not price_data:
            continue
        current_price = price_data["price"]
        rs, rs_quantile = calculate_rs(code, current_price)
        ma60 = get_ma60(code)
        # 买入信号：RS前30%分位 或 MA60±2%且RS前10%分位
        buy_triggered = (rs_quantile >= RS_SIGNAL_THRESHOLD) or \
                        (abs(current_price - ma60) / ma60 <= MA60_OFFSET and rs_quantile >= 0.9)
        # 卖出信号：移动回撤触发
        sell_triggered = check_trailing_stop(code, current_price)

        # 蓝筹股信号抑制：无ML模型的蓝筹股即使MA60条件触发也被拦截
        # 理由是大盘蓝筹日常波动即可触发MA60±2%，误报率极高
        if code in _COOLDOWN_BLUECHIPS:
            if buy_triggered:
                logger.info(f"蓝筹股{code}条件触发但被拦截（无ML模型，盘整期MA60信号不可信）")
            buy_triggered = False
            sell_triggered = False

        if buy_triggered and not _is_cooldown(code, "BUY"):
            signals.append({
                "type": "BUY",
                "code": code,
                "name": price_data["name"],
                "price": current_price,
                "rs_quantile": rs_quantile,
                "time": now.strftime("%Y-%m-%d %H:%M:%S")
            })
        if sell_triggered and not _is_cooldown(code, "SELL"):
            signals.append({
                "type": "SELL",
                "code": code,
                "name": price_data["name"],
                "price": current_price,
                "time": now.strftime("%Y-%m-%d %H:%M:%S")
            })
    # 推送信号
    if signals:
        alert_msg = "⚡️ A股实时交易信号触发:\n"
        for s in signals:
            alert_msg += f"{s['time']} | {s['type']} | {s['name']}({s['code']}) | 价格: {s['price']:.2f} | 冷却{SIGNAL_COOLDOWN_MINUTES}分钟\n"
        send_feishu_alert(alert_msg)
    return signals

def get_ma60(code):
    """获取60日均线"""
    # 实际对接行情接口获取MA60，这里简化实现
    return get_realtime_price(code)["close_prev"] * 0.98

def get_stock_20d_prev(code):
    """获取20日前收盘价"""
    return get_realtime_price(code)["close_prev"] * 0.95

def get_hs300_20d_prev():
    """获取沪深300 20日前收盘价"""
    return get_realtime_price("sh000300")["close_prev"] * 0.97

def check_trailing_stop(code, current_price):
    """检查移动止盈触发"""
    position_file = f"../data/positions/{code}.json"
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

def _is_trading_time() -> bool:
    """判断当前是否在A股交易时段（工作日9:15-11:35, 13:00-15:05）"""
    from datetime import datetime as _dt
    import calendar as _cal
    
    # 检查中国的节假日（需要 chinese_calendar 库）
    try:
        import chinese_calendar
        now_date = _dt.now().date()
        if chinese_calendar.is_holiday(now_date) or not chinese_calendar.is_workday(now_date):
            return False
    except ImportError:
        # 没有节假日库用周末检查兜底
        now = _dt.now()
        if now.weekday() >= 5:  # 周六日
            return False
    
    now = _dt.now()
    t = now.hour * 60 + now.minute
    # 上午盘: 9:15 - 11:35  下午盘: 13:00 - 15:05
    return (555 <= t <= 695) or (780 <= t <= 905)


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] A股实时监控启动 (launchd常驻)")
    while True:
        try:
            if _is_trading_time():
                monitor_signals()
                time.sleep(300)  # 交易时段每5分钟扫描
            else:
                # 非交易时段休眠5分钟后再检查
                time.sleep(300)
        except KeyboardInterrupt:
            print("监控停止")
            break
        except Exception as e:
            print(f"监控异常: {str(e)}")
            time.sleep(60)
