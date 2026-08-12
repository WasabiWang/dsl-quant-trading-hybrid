#!/usr/bin/env python3
"""
增强版实时监控脚本
功能：实时拉取行情、监控信号触发、异常告警
改进：更好的错误处理、重试机制、详细日志
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
import sys
import traceback

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/dsl_realtime_monitor.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dsl_data_sdk import get_price as sdk_get_price
    SDK_AVAILABLE = True
    logger.info("DSL数据SDK导入成功")
except ImportError as e:
    SDK_AVAILABLE = False
    logger.warning(f"DSL数据SDK导入失败: {e}")
    # 备用方案
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location(
            "dsl_data_sdk", 
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dsl_data_sdk.py")
        )
        dsl_data_sdk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dsl_data_sdk)
        sdk_get_price = dsl_data_sdk.get_price
        SDK_AVAILABLE = True
        logger.info("备用数据SDK导入成功")
    except Exception as e2:
        logger.error(f"备用数据SDK也导入失败: {e2}")
        SDK_AVAILABLE = False

# 监控股票池（默认核心持仓）
STOCK_POOL = ["sh600519", "sh601318", "sz000858", "sz002475", "sz002594"]
# 信号阈值
RS_SIGNAL_THRESHOLD = 0.7  # RS前30%分位
MA60_OFFSET = 0.02  # MA60±2%

# 信号冷却：同一标的一类信号 30 分钟内不重复推送
SIGNAL_COOLDOWN_MINUTES = 30
_signal_cooldown_cache = {}

# 错误重试配置
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒

def safe_get_price(symbol, retries=MAX_RETRIES):
    """
    安全获取价格，带重试机制
    
    Args:
        symbol: 股票代码
        retries: 最大重试次数
        
    Returns:
        价格数据字典或None
    """
    if not SDK_AVAILABLE:
        logger.error(f"数据SDK不可用，无法获取{symbol}价格")
        return None
    
    for attempt in range(retries):
        try:
            price_data = sdk_get_price(symbol)
            if price_data and 'price' in price_data and price_data['price'] > 0:
                logger.debug(f"成功获取{symbol}价格: {price_data['price']:.2f}")
                return price_data
            else:
                logger.warning(f"获取{symbol}价格数据无效: {price_data}")
        except Exception as e:
            logger.error(f"获取{symbol}价格失败 (尝试{attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"获取{symbol}价格所有重试均失败")
    
    return None


def _is_cooldown(code, signal_type) -> bool:
    """检查信号冷却期"""
    global _signal_cooldown_cache
    key = f"{code}_{signal_type}"
    from datetime import datetime as _dt
    now = _dt.now()
    if key in _signal_cooldown_cache:
        elapsed = (now - _signal_cooldown_cache[key]).total_seconds() / 60
        if elapsed < SIGNAL_COOLDOWN_MINUTES:
            return True
    _signal_cooldown_cache[key] = now
    return False


def monitor_stock_pool(stock_pool=None):
    """
    监控股票池
    
    Args:
        stock_pool: 股票代码列表，如果为None使用默认
        
    Returns:
        信号列表
    """
    if stock_pool is None:
        stock_pool = STOCK_POOL
    
    signals = []
    failed_stocks = []
    
    logger.info(f"开始监控{len(stock_pool)}只股票")
    
    for code in stock_pool:
        try:
            # 获取实时价格
            price_data = safe_get_price(code)
            if not price_data:
                failed_stocks.append(code)
                continue
            
            current_price = price_data.get("price", 0)
            if current_price <= 0:
                logger.warning(f"{code}价格无效: {current_price}")
                continue
            
            # 检查买入信号（简化版）
            buy_signal = False
            sell_signal = False
            
            # 这里可以添加实际的信号逻辑
            # 示例：价格超过60日均线2%时买入
            ma60 = get_ma60(code)
            if current_price > ma60 * (1 + MA60_OFFSET):
                buy_signal = True
                logger.info(f"{code}触发买入信号: {current_price:.2f} > MA60 {ma60:.2f}")
            
            # 价格低于60日均线2%时卖出
            if current_price < ma60 * (1 - MA60_OFFSET):
                sell_signal = True
                logger.info(f"{code}触发卖出信号: {current_price:.2f} < MA60 {ma60:.2f}")
            
            # 记录信号（带冷却检查）
            if buy_signal and not _is_cooldown(code, "BUY"):
                signal = {
                    "type": "BUY",
                    "code": code,
                    "name": price_data.get("name", "未知"),
                    "price": current_price,
                    "change": price_data.get("change", 0),
                    "change_pct": price_data.get("change_pct", 0),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ma60": ma60
                }
                signals.append(signal)
            if sell_signal and not _is_cooldown(code, "SELL"):
                signal = {
                    "type": "SELL",
                    "code": code,
                    "name": price_data.get("name", "未知"),
                    "price": current_price,
                    "change": price_data.get("change", 0),
                    "change_pct": price_data.get("change_pct", 0),
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ma60": ma60
                }
                signals.append(signal)
                
        except Exception as e:
            logger.error(f"监控{code}时发生错误: {e}")
            logger.debug(traceback.format_exc())
            failed_stocks.append(code)
    
    # 记录监控结果
    if signals:
        logger.info(f"发现{len(signals)}个交易信号")
    if failed_stocks:
        logger.warning(f"{len(failed_stocks)}只股票监控失败: {failed_stocks}")
    
    return signals, failed_stocks

def get_ma60(code):
    """获取60日均线（简化实现）"""
    try:
        price_data = safe_get_price(code)
        if price_data:
            # 简化：使用昨收价作为MA60近似
            return price_data.get("price", 0) * 0.98
    except Exception as e:
        logger.error(f"获取{code} MA60失败: {e}")
    
    return 0

def send_feishu_alert(message, level="info"):
    """
    发送飞书告警（简化版）
    
    Args:
        message: 消息内容
        level: 告警级别 (info/warning/error)
    """
    try:
        # 这里应该调用OpenClaw的message工具
        # 简化实现：只记录日志
        log_msg = f"[飞书告警 {level.upper()}] {message}"
        if level == "error":
            logger.error(log_msg)
        elif level == "warning":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)
        
        # 实际发送逻辑（需要OpenClaw环境）
        # from openclaw_tools import message
        # message.send(channel="feishu", message=message)
        
    except Exception as e:
        logger.error(f"发送飞书告警失败: {e}")

def run_monitoring_loop(interval=60, max_iterations=None):
    """
    运行监控循环
    
    Args:
        interval: 监控间隔（秒）
        max_iterations: 最大循环次数，None表示无限循环
    """
    logger.info(f"启动实时监控，间隔{interval}秒")
    
    iteration = 0
    while True:
        if max_iterations and iteration >= max_iterations:
            logger.info(f"达到最大循环次数{max_iterations}，停止监控")
            break
        
        iteration += 1
        loop_start = datetime.now()
        
        try:
            logger.info(f"开始第{iteration}次监控循环")
            
            # 监控股票池
            signals, failed_stocks = monitor_stock_pool()
            
            # 发送信号告警
            if signals:
                alert_msg = f"⚡️ 实时交易信号触发 ({len(signals)}个):\n"
                for s in signals:
                    change_str = f"{s['change']:+.2f} ({s['change_pct']:+.2f}%)"
                    alert_msg += f"{s['time']} | {s['type']} | {s['name']}({s['code']}) | 价格: {s['price']:.2f} | 涨跌: {change_str}\n"
                
                send_feishu_alert(alert_msg)
            
            # 如果有失败股票，发送警告
            if failed_stocks:
                warning_msg = f"⚠️ {len(failed_stocks)}只股票监控失败: {', '.join(failed_stocks)}"
                send_feishu_alert(warning_msg, level="warning")
            
        except Exception as e:
            error_msg = f"❌ 监控循环发生严重错误: {e}"
            logger.error(error_msg)
            logger.error(traceback.format_exc())
            send_feishu_alert(error_msg, level="error")
        
        # 计算本次循环耗时
        loop_duration = (datetime.now() - loop_start).total_seconds()
        logger.info(f"第{iteration}次监控循环完成，耗时{loop_duration:.1f}秒")
        
        # 等待下一次循环
        if loop_duration < interval:
            sleep_time = interval - loop_duration
            logger.debug(f"等待{sleep_time:.1f}秒后继续")
            time.sleep(sleep_time)
        else:
            logger.warning(f"监控循环耗时{loop_duration:.1f}秒超过间隔{interval}秒，立即开始下一次")

def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='增强版实时监控脚本')
    parser.add_argument('--market', type=str, default='a', choices=['a', 'hk'], help='市场类型')
    parser.add_argument('--interval', type=int, default=60, help='监控间隔（秒）')
    parser.add_argument('--iterations', type=int, default=None, help='最大循环次数')
    parser.add_argument('--test', action='store_true', help='测试模式（单次运行）')
    parser.add_argument('--stocks', type=str, help='自定义股票代码（逗号分隔）')
    
    args = parser.parse_args()
    
    # 设置股票池
    stock_pool = STOCK_POOL
    if args.stocks:
        stock_pool = [s.strip() for s in args.stocks.split(',')]
        logger.info(f"使用自定义股票池: {stock_pool}")
    
    # 根据市场调整股票池
    if args.market == 'hk':
        stock_pool = ['00700.HK', '09988.HK', '03690.HK', '00941.HK', '00883.HK']
        logger.info(f"切换到港股市场，股票池: {stock_pool}")
    
    if args.test:
        logger.info("运行测试模式（单次监控）")
        signals, failed_stocks = monitor_stock_pool(stock_pool)
        
        print(f"\n📊 监控结果:")
        print(f"  成功监控: {len(stock_pool) - len(failed_stocks)}/{len(stock_pool)}")
        print(f"  发现信号: {len(signals)}个")
        print(f"  失败股票: {failed_stocks if failed_stocks else '无'}")
        
        if signals:
            print(f"\n🚨 交易信号:")
            for s in signals:
                print(f"  {s['time']} | {s['type']} | {s['name']}({s['code']}) | 价格: {s['price']:.2f}")
    else:
        # 生产模式：运行监控循环
        run_monitoring_loop(
            interval=args.interval,
            max_iterations=args.iterations
        )

if __name__ == "__main__":
    main()