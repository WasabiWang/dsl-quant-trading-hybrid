"""
异常熔断机制 v4.5.9
核心功能：监控异常情况，自动暂停交易，避免系统性风险
熔断规则：
1. 单日账户回撤≥5% → 暂停所有交易24小时 (黑天鹅联动收紧至3%)
2. 连续3次交易失败 → 暂停所有交易1小时
3. 单票持仓超过30% → 禁止买入该标的
4. 单日交易次数超过10次 → 暂停交易2小时
5. 单日总亏损≥2% → 强制平仓所有持仓，暂停交易24小时 (黑天鹅联动收紧至1%)
"""
import json
import os
import time
import fcntl
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# 黑天鹅联动阈值 (在对应方法中动态读取)
BLACK_SWAN_STATUS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "black_swan_status.json"
)
ADAPTIVE_PARAMS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "adaptive_params.yaml"
)
class CircuitBreaker:
    def __init__(self, data_path: str = "../data/circuit_breaker.json"):
        # v4.5.3c fix: 绝对路径，兼容任意工作目录
        PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_path = "../data/circuit_breaker.json"
        if data_path and data_path != default_path:
            self.data_path = data_path if os.path.isabs(data_path) else os.path.join(PROJECT_ROOT, data_path)
        else:
            self.data_path = os.path.join(PROJECT_ROOT, "data", "circuit_breaker.json")
        self._init_data()
    
    def _acquire_lock(self, fd):
        """P1-FIX: 获取文件锁"""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except (IOError, OSError):
            pass  # 非关键路径，锁失败不阻塞

    def _release_lock(self, fd):
        """P1-FIX: 释放文件锁"""
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass

    def _init_data(self):
        """初始化熔断状态数据"""
        if not os.path.exists(os.path.dirname(self.data_path)):
            os.makedirs(os.path.dirname(self.data_path))
        if not os.path.exists(self.data_path):
            init_data = {
                "trading_paused": False,
                "pause_until": 0,  # 暂停到的时间戳
                "pause_reason": "",
                "today_drawdown": 0.0,  # 今日最大回撤
                "today_total_loss": 0.0,  # 今日累计亏损(元)
                "today_starting_capital": 0.0,  # 今日起始资金
                "consecutive_failed_trades": 0,  # 连续失败交易次数
                "today_trade_count": 0,  # 今日交易次数
                "last_trade_date": "",  # 上次交易日期
                "trigger_history": [],  # 熔断触发历史
                "limit_trigger_history": []
            }
            self._save_data(init_data)

    def _get_black_swan_severity(self) -> int:
        """
        读取黑天鹅状态, 返回severity等级 (0-10)
        用于动态收紧熔断阈值
        """
        try:
            # 从 black_swan_status.json 读取
            if os.path.exists(BLACK_SWAN_STATUS_PATH):
                with open(BLACK_SWAN_STATUS_PATH) as f:
                    bs_data = json.load(f)
                # lppl_to_dsl 写入的结构
                lppl_info = bs_data.get('lppl', {})
                urgency = lppl_info.get('urgency', 'LOW')
                risk_score = lppl_info.get('risk_score', 0)

                urgency_map = {'CRITICAL': 8, 'HIGH': 6, 'ELEVATED': 4, 'LOW': 0}
                from_urgency = urgency_map.get(urgency, 0)

                # 也尝试从 event_history.json 读取severity
                event_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "confidence_data", "event_history.json"
                )
                if os.path.exists(event_path):
                    with open(event_path) as f:
                        evt = json.load(f)
                    raw_events = evt.get("events", [])
                    max_sev = 0
                    for e in raw_events:
                        sev = e.get("severity", 0)
                        if sev > max_sev:
                            max_sev = sev
                    return max(max_sev, from_urgency)

                return from_urgency
        except Exception:
            return 0
    
    def _load_data(self) -> dict:
        """加载熔断状态 (P1-FIX: 添加文件锁)"""
        with open(self.data_path, "r", encoding="utf-8") as f:
            self._acquire_lock(f)
            data = json.load(f)
            self._release_lock(f)
        # 跨天重置每日数据
        today = datetime.now().strftime("%Y-%m-%d")
        if data["last_trade_date"] != today:
            data["today_drawdown"] = 0.0
            data["today_total_loss"] = 0.0
            data["today_trade_count"] = 0
            data["consecutive_failed_trades"] = 0
            data["last_trade_date"] = today
            self._save_data(data)
        return data
    
    def _save_data(self, data: dict):
        """保存熔断状态 (P1-FIX: 添加文件锁+原子写入)"""
        try:
            tmp_path = self.data_path + ".tmp"
            with open(tmp_path, 'w', encoding="utf-8") as f:
                self._acquire_lock(f)
                json.dump(data, f, ensure_ascii=False, indent=2)
                self._release_lock(f)
            os.replace(tmp_path, self.data_path)  # atomic rename
        except Exception as e:
            logger.error(f"保存熔断器状态失败: {e}")
    
    def _check_pause_status(self) -> bool:
        """检查是否处于暂停状态"""
        data = self._load_data()
        if data["trading_paused"] and time.time() < data["pause_until"]:
            return True
        # 暂停时间到了自动恢复
        if data["trading_paused"] and time.time() >= data["pause_until"]:
            data["trading_paused"] = False
            data["pause_reason"] = ""
            data["pause_until"] = 0
            self._save_data(data)
        return False
    
    def is_trading_allowed(self) -> tuple[bool, str]:
        """检查是否允许交易
        返回：(是否允许, 原因/提示信息)
        """
        # v4.5.12 P1-8: A股午休时段禁止交易
        try:
            from config.constants import is_lunch_break
            if is_lunch_break():
                return False, "A股午休时段(11:30-13:00)，交易已暂停"
        except ImportError:
            pass

        if self._check_pause_status():
            data = self._load_data()
            resume_time = datetime.fromtimestamp(data["pause_until"]).strftime("%Y-%m-%d %H:%M:%S")
            return False, f"交易已暂停，恢复时间：{resume_time}，原因：{data['pause_reason']}"

        # P1-FIX: 持仓限制检查需在调用时通过check_position_limit(code, buy_amount, total_capital)单独执行
        # is_trading_allowed()作为通用门控，不持有交易上下文参数
        return True, "交易允许"
    
    def pause(self, reason: str, hours: float = 2.0) -> bool:
        """v4.6.9i(审计F1-3): 手动/自动暂停交易(如止损监控数据源全空时)。
        写 trading_paused=True + pause_until, is_trading_allowed() 自动拦截。"""
        try:
            data = self._load_data()
            data["trading_paused"] = True
            data["pause_until"] = time.time() + hours * 3600
            data["pause_reason"] = reason[:200]
            self._save_data(data)
            return True
        except Exception as e:
            print(f"⚠️ 暂停交易写入失败: {e}")
            return False

    def update_equity_drawdown(self, equity: float) -> float:
        """v4.6.9i(审计F1-4): 从当前总权益计算当日回撤并回写熔断器(生产调用点)。
        首次调用设置 today_starting_capital; 返回当日回撤百分比(负数)。"""
        data = self._load_data()
        start = data.get("today_starting_capital", 0)
        if not start or start <= 0:
            start = equity
            data["today_starting_capital"] = equity
            self._save_data(data)
        dd = (equity - start) / start if start > 0 else 0.0
        self.update_drawdown(dd)
        return dd

    def update_drawdown(self, current_drawdown: float):
        """更新今日最大回撤，触发熔断阈值自动暂停"""
        data = self._load_data()
        # v4.6.9i(审计F1-4): 修复方向比较 — 回撤为负数, 原 `>` 与初始值0.0比较导致永不更新
        if current_drawdown < data["today_drawdown"]:
            data["today_drawdown"] = current_drawdown

        # 黑天鹅联动: severity≥6 时回撤阈值从5%收紧到3%
        severity = self._get_black_swan_severity()
        dd_threshold = -0.03 if severity >= 6 else -0.05

        # 单日回撤超阈值熔断，暂停24小时 (P0fix: drawdown为负数，<= -0.05触发)
        if current_drawdown <= dd_threshold:
            pause_hours = 24
            data["trading_paused"] = True
            data["pause_until"] = time.time() + pause_hours * 3600
            threshold_pct = abs(dd_threshold) * 100
            data["pause_reason"] = f"单日回撤{current_drawdown*100:.2f}%≥{threshold_pct:.0f}%，触发熔断"
            if severity >= 6:
                data["pause_reason"] += f"[黑天鹅联动severity={severity}]"
            data["trigger_history"].append({
                "time": datetime.now().isoformat(),
                "reason": data["pause_reason"],
                "pause_hours": pause_hours
            })
            self._save_data(data)
            print(f"⚠️ 熔断触发：{data['pause_reason']}，暂停交易{pause_hours}小时")
    
    def record_trade_result(self, success: bool):
        """记录交易结果，连续失败触发熔断"""
        data = self._load_data()
        if success:
            data["consecutive_failed_trades"] = 0
        else:
            data["consecutive_failed_trades"] += 1
            # 连续3次交易失败，暂停1小时
            if data["consecutive_failed_trades"] >= 3:
                pause_hours = 1
                data["trading_paused"] = True
                data["pause_until"] = time.time() + pause_hours * 3600
                data["pause_reason"] = f"连续{data['consecutive_failed_trades']}次交易失败，触发熔断"
                data["trigger_history"].append({
                    "time": datetime.now().isoformat(),
                    "reason": data["pause_reason"],
                    "pause_hours": pause_hours
                })
                print(f"⚠️ 熔断触发：{data['pause_reason']}，暂停交易{pause_hours}小时")
        self._save_data(data)
    
    def update_trade_count(self):
        """更新今日交易次数，超过阈值触发熔断"""
        data = self._load_data()
        data["today_trade_count"] += 1
        # 单日交易次数超过10次，暂停2小时
        if data["today_trade_count"] >= 10:
            pause_hours = 2
            data["trading_paused"] = True
            data["pause_until"] = time.time() + pause_hours * 3600
            data["pause_reason"] = f"单日交易次数{data['today_trade_count']}次≥10次，触发熔断"
            data["trigger_history"].append({
                "time": datetime.now().isoformat(),
                "reason": data["pause_reason"],
                "pause_hours": pause_hours
            })
            print(f"⚠️ 熔断触发：{data['pause_reason']}，暂停交易{pause_hours}小时")
        self._save_data(data)
    
    def check_position_limit(self, code: str, buy_amount: float, total_capital: float) -> tuple[bool, str]:
        """检查单票持仓限制，单票持仓不能超过总资金30%"""
        position_value = buy_amount
        if position_value / total_capital > 0.3:
            return False, f"单票{code}持仓比例{position_value/total_capital*100:.2f}%≥30%，禁止买入"
        return True, "持仓比例符合要求"
    
    def set_today_starting_capital(self, capital: float):
        """设置今日起始资金（每日开盘前调用）"""
        data = self._load_data()
        data["today_starting_capital"] = capital
        data["today_total_loss"] = 0.0
        self._save_data(data)

    def update_daily_loss(self, loss_amount: float):
        """更新今日累计亏损（每笔交易后调用，传入负值）
        P1新增：单日总亏损≥2%触发熔断 (黑天鹅联动收紧至1%)
        """
        data = self._load_data()
        data["today_total_loss"] += abs(loss_amount)

        # 黑天鹅联动: severity≥6 时日亏损阈值从2%收紧到1%
        severity = self._get_black_swan_severity()
        loss_threshold = 0.01 if severity >= 6 else 0.02

        starting_capital = data.get("today_starting_capital", 0.0)
        if starting_capital > 0:
            loss_pct = data["today_total_loss"] / starting_capital
            if loss_pct >= loss_threshold:  # 日亏损≥阈值
                pause_hours = 24
                data["trading_paused"] = True
                data["pause_until"] = time.time() + pause_hours * 3600
                threshold_pct = loss_threshold * 100
                data["pause_reason"] = (
                    f"单日累计亏损{loss_pct*100:.2f}%≥{threshold_pct:.0f}%"
                    f"(亏损{data['today_total_loss']:,.2f}元)，触发熔断"
                )
                if severity >= 6:
                    data["pause_reason"] += f"[黑天鹅联动severity={severity}]"
                data["trigger_history"].append({
                    "time": datetime.now().isoformat(),
                    "reason": data["pause_reason"],
                    "pause_hours": pause_hours
                })
                self._save_data(data)
                print(f"🚨 熔断触发：{data['pause_reason']}，暂停交易{pause_hours}小时")
                return
        self._save_data(data)

    def check_limit_up_down(self, code: str, current_price: float, prev_close: float, action: str) -> tuple[bool, str]:
        """检查涨跌停限制（v4.5.12: 板块差异化）

        action: 'buy' 或 'sell'
        返回：(是否允许, 原因)
        """
        if prev_close <= 0:
            return True, "昨收价无效，跳过涨跌停检查"

        # 板块差异化涨跌停幅度 — 统一从 constants.LIMIT_RATES 读取
        try:
            from core.risk_manager import get_market_type
            from config.constants import LIMIT_RATES
            market_type = get_market_type(code)
        except ImportError:
            market_type = "A"
        limit_rate = LIMIT_RATES.get(market_type, 0.10)
        limit_up = round(prev_close * (1 + limit_rate), 2)
        limit_down = round(prev_close * (1 - limit_rate), 2)
        
        if action == 'buy' and current_price >= limit_up:
            self._log_limit_trigger(code, '涨停', current_price, limit_up)
            return False, f"{code}已涨停({current_price}≥{limit_up})，禁止买入"
        
        if action == 'sell' and current_price <= limit_down:
            self._log_limit_trigger(code, '跌停', current_price, limit_down)
            return False, f"{code}已跌停({current_price}≤{limit_down})，禁止卖出"
        
        return True, "未触发涨跌停限制"
    
    def _log_limit_trigger(self, code: str, limit_type: str, price: float, limit_price: float):
        """记录涨跌停触发事件"""
        data = self._load_data()
        if 'limit_trigger_history' not in data:
            data['limit_trigger_history'] = []
        data['limit_trigger_history'].append({
            "time": datetime.now().isoformat(),
            "code": code,
            "type": limit_type,
            "price": price,
            "limit_price": limit_price
        })
        if len(data['limit_trigger_history']) > 100:
            data['limit_trigger_history'] = data['limit_trigger_history'][-100:]
        self._save_data(data)
        print(f"🚫 涨跌停熔断：{code} {limit_type} 当前价{price} 限制价{limit_price}")
    
    def manual_resume(self):
        """手动恢复交易"""
        data = self._load_data()
        data["trading_paused"] = False
        data["pause_reason"] = ""
        data["pause_until"] = 0
        data["consecutive_failed_trades"] = 0
        self._save_data(data)
        print("✅ 熔断已手动解除，交易恢复正常")
    
    def get_status(self) -> dict:
        """获取当前熔断状态"""
        data = self._load_data()
        starting = data.get("today_starting_capital", 0.0)
        loss_pct = (data["today_total_loss"] / starting * 100) if starting > 0 else 0
        status = {
            "trading_paused": data["trading_paused"],
            "pause_until": datetime.fromtimestamp(data["pause_until"]).strftime("%Y-%m-%d %H:%M:%S") if data["pause_until"] > 0 else "",
            "pause_reason": data["pause_reason"],
            "today_drawdown": f"{data['today_drawdown']*100:.2f}%",
            "today_total_loss": f"{data['today_total_loss']:,.2f}",
            "today_loss_pct": f"{loss_pct:.2f}%",
            "consecutive_failed_trades": data["consecutive_failed_trades"],
            "today_trade_count": data["today_trade_count"]
        }
        return status

# 全局单例
circuit_breaker = CircuitBreaker()
