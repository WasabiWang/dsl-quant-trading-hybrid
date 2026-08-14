#!/usr/bin/env python3
"""
risk_manager.py — DSL v4.5.12 组合风控模块

借鉴 Lean/VectorBT 的组合层风控，A股化适配：
1. 硬止损(按板块差异化涨跌停规则)
2. 止盈(固定止盈 + 移动止盈 + RS动态止盈)
3. 组合仓位约束(行业+概念+现金流)
4. 组合级别风险监控

调用方:
  • execute_planned_trades.py: 执行前止损/止盈检查
  • morning_decision.py: plan_trades阶段应用仓位约束
  • feedback_controller.py: 绩效归因
"""
import os, sys, json, yaml, time
from datetime import datetime
from typing import Dict, List, Tuple, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from common.config import config
from common.logger import get_logger
from config.constants import COMMISSION_RATE, LIMIT_RATES, get_market_type

logger = get_logger("risk_manager")

# 统一涨跌停规则 — 从 constants.LIMIT_RATES 读取（唯一真相源）
# 不再在此处独立定义 LIMIT_RULES

# 默认止损线
DEFAULT_STOP_LOSS = -0.08
DEFAULT_PORTFOLIO_STOP = -0.05
DEFAULT_TOTAL_STOP = -0.20

# 默认止盈线 v4.5.12
DEFAULT_TAKE_PROFIT = 0.15           # 固定止盈 15%
DEFAULT_TRAILING_PROFIT_TRIGGER = 0.10   # 移动止盈触发：盈利10% (v4.5.14: 5%→10%, 避免提前截断)
DEFAULT_TRAILING_PROFIT_DRAWDOWN = 0.05  # 移动止盈最低档回撤：5%

# v4.6.x: 分级移动止盈回撤阈值 — 盈利越大容忍度越高
TRAILING_DRAWDOWN_TIERS = [
    (1.00, 0.15),  # 盈利≥100% → 回撤容忍15%（超级牛股让利润奔跑）
    (0.60, 0.12),  # 盈利≥60%  → 回撤容忍12%（已确认牛股）
    (0.30, 0.08),  # 盈利≥30%  → 回撤容忍8%（趋势形成中）
    (0.10, 0.05),  # 盈利≥10%  → 回撤容忍5%（起步阶段快锁）
]

# RS动态止盈 v4.5.12
RS_TAKE_PROFIT_TIERS = [
    (0.75, 0.20),  # RS>0.75 → 止盈20%
    (0.50, 0.15),  # RS>0.50 → 止盈15%
    (0.25, 0.10),  # RS>0.25 → 止盈10%
    (0.00, 0.05),  # RS<0.25 → 止盈5%
]


def get_limit_pct(symbol: str) -> float:
    """获取涨跌停幅度，从 constants.LIMIT_RATES 读取"""
    return LIMIT_RATES.get(get_market_type(symbol), 0.10)


def get_stop_loss_pct(symbol: str) -> float:
    """获取板块差异化止损线 — v4.6.x: 统一委托 paper_trader.get_stop_loss_pct（ATR+固定）"""
    from scripts.paper_trader import get_stop_loss_pct as pt_get_sl
    return pt_get_sl(symbol)


def get_adaptive_params() -> dict:
    """读取adaptive_params.yaml中的风控配置"""
    try:
        path = os.path.join(PROJECT_ROOT, "config", "adaptive_params.yaml")
        with open(path) as f:
            return yaml.safe_load(f).get("trading", {}).get("risk_management", {})
    except Exception:
        return {}


class RiskManager:
    """组合级别风控管理器"""

    __all__methods__ = [
        "_check_rs_strong",
        "_check_tight_stop",
        "_check_trend_intact",
        "_get_rs_take_profit",
        "_get_trailing_drawdown_threshold",
        "_load_position_peaks",
        "_save_position_peaks",
        "check_portfolio_stop",
        "check_prediction_stop_consistency",
        "check_sector_concentration",
        "check_single_position_stop",
        "check_single_position_take_profit",
        "pre_execution_check",
    ]

    def __init__(self):
        self.adaptive = get_adaptive_params()
        self.enabled = self.adaptive.get("enabled", True)
        self.stop_loss_config = self.adaptive.get("stop_loss", {})
        self.take_profit_config = self.adaptive.get("take_profit", {})
        self.position_limits = self.adaptive.get("position_limits", {})
        self._peak_equity = None  # v4.5.12: 跟踪历史峰值用于回撤计算
        self._position_peaks = {}  # v4.5.12: {symbol: peak_price} 用于移动止盈
        self._bs_cache = None       # P2-FIX: 黑天鹅事件缓存
        self._bs_cache_time = 0     # 缓存时间戳
        self._bs_cache_ttl = 300    # 缓存有效期(秒)
        self._load_position_peaks()  # P2-FIX: 启动时加载持久化峰值

    def _load_bs_severity(self) -> int:
        """v4.6.9i(审计F1-1): 读取最新黑天鹅severity — 优先 memory/black-swan 最新 analysis JSON
        (21:00复盘每日写入, severity_overview.current_overall_severity),
        fallback event_history.json(曾冻结2个月, 审计P4-WARN)。"""
        try:
            _cands = []
            for _dir in (os.path.join(PROJECT_ROOT, '..', 'memory', 'black-swan'),
                         os.path.expanduser('~/.agents/skills/black-swan-monitor/data')):
                if os.path.isdir(_dir):
                    for _f in os.listdir(_dir):
                        if _f.startswith('analysis-') and _f.endswith('.json'):
                            _cands.append(os.path.join(_dir, _f))
            if _cands:
                _cands.sort(reverse=True)
                with open(_cands[0], 'r', encoding='utf-8') as _f:
                    _data = json.load(_f)
                _so = _data.get('severity_overview', {}) or {}
                _sev = _so.get('current_overall_severity')
                if _sev is not None:
                    return int(_sev)
        except Exception:
            pass
        # fallback: event_history.json 旧结构
        try:
            with open(os.path.join(PROJECT_ROOT, 'confidence_data', 'event_history.json')) as f:
                events_data = json.load(f)
            bs_severity = 0
            for evt in events_data.get('events', []):
                for se in (evt.get('data', {}) or {}).get('events', []):
                    bs_severity = max(bs_severity, se.get('severity', 0))
            return bs_severity
        except Exception:
            return 0

    # ═══════════════ 止损检查 ═══════════════

    def check_single_position_stop(
        self, symbol: str, name: str, avg_price: float, current_price: float
    ) -> dict:
        """检查单票是否触发止损

        Returns:
            {"triggered": bool, "pct": float, "reason": str}
        """
        if avg_price <= 0 or current_price <= 0:
            return {"triggered": False, "pct": 0, "reason": "价格无效"}

        pct = (current_price - avg_price) / avg_price
        stop_loss = get_stop_loss_pct(symbol)

        # 黑天鹅severity≥7时收紧止损
        if self.stop_loss_config.get("black_swan_tighten", True):
            try:
                now = time.time()
                if self._bs_cache is None or (now - self._bs_cache_time) > self._bs_cache_ttl:
                    self._bs_cache = self._load_bs_severity()
                    self._bs_cache_time = now
                bs_severity = self._bs_cache
                if bs_severity >= 7:
                    stop_loss = max(stop_loss, -0.05)
                    logger.info(f"🦢 黑天鹅severity={bs_severity} → 止损收紧到{stop_loss:.1%}")
            except Exception:
                pass

        triggered = pct <= stop_loss
        if triggered:
            limit_pct = get_limit_pct(symbol)
            logger.warning(
                f"🔴 止损触发: {name}({symbol}) "
                f"盈亏={pct:.2%} ≤ 止损线={stop_loss:.1%} "
                f"(涨跌停={limit_pct:.0%})"
            )

        return {
            "triggered": triggered,
            "pct": round(pct, 4),
            "stop_loss_line": round(stop_loss, 4),
            "reason": f"盈亏{pct:.2%} ≤ {stop_loss:.1%}" if triggered else "正常",
        }

    def check_portfolio_stop(
        self, initial_capital: float, current_equity: float, peak_equity: float = None
    ) -> dict:
        """检查组合是否触发日回撤/总回撤止损 v4.5.12: peak_equity回撤"""
        if initial_capital <= 0:
            return {"triggered": False, "reason": "初始资金无效"}

        # v4.5.12: 使用峰值计算回撤（如果未提供峰值则从init追踪）
        if peak_equity is None:
            peak_equity = getattr(self, '_peak_equity', None)
        if peak_equity is None or current_equity > peak_equity:
            peak_equity = current_equity
        self._peak_equity = peak_equity

        # 从峰值计算回撤
        drawdown_from_peak = (current_equity - peak_equity) / peak_equity if peak_equity > 0 else 0
        total_stop = self.stop_loss_config.get(
            "portfolio_total", DEFAULT_TOTAL_STOP
        )
        daily_stop = self.stop_loss_config.get(
            "portfolio_daily", DEFAULT_PORTFOLIO_STOP
        )

        if drawdown_from_peak <= total_stop:
            return {
                "triggered": True,
                "level": "total",
                "pct": drawdown_from_peak,
                "stop_line": total_stop,
                "reason": f"组合总回撤{drawdown_from_peak:.2%} ≤ {total_stop:.1%} (峰值回撤)",
            }
        elif drawdown_from_peak <= daily_stop:
            return {
                "triggered": True,
                "level": "daily",
                "pct": drawdown_from_peak,
                "stop_line": daily_stop,
                "reason": f"组合日回撤{drawdown_from_peak:.2%} ≤ {daily_stop:.1%} (峰值回撤)",
            }

        return {"triggered": False, "reason": "正常"}

    # ═══════════════ 止损预测一致性检查 v4.5.12 P2-13 ═══════════════

    def check_prediction_stop_consistency(
        self, trade: dict
    ) -> dict:
        """验证买入信号的止损线与预测期望是否一致

        计算: EV = confidence × predicted_return + (1 - confidence) × stop_loss_pct
        如果 EV < 0, 则该笔交易期望值为负, 做多无统计学优势。

        Args:
            trade: 交易信号, 需包含 code/symbol, confidence, predicted_return

        Returns:
            {"consistent": bool, "ev": float, ...}
        """
        code = trade.get("code", trade.get("symbol", ""))
        confidence = float(trade.get("confidence", 0))
        predicted_return = float(trade.get("predicted_return", 0))

        if confidence <= 0 or predicted_return == 0:
            return {"consistent": True, "ev": 0,
                    "reason": "无预测数据, 跳过一致性检查"}

        stop_loss = get_stop_loss_pct(code)
        ev = confidence * predicted_return + (1 - confidence) * stop_loss

        if ev < 0:
            return {
                "consistent": False,
                "ev": round(ev, 4),
                "stop_loss": stop_loss,
                "confidence": confidence,
                "predicted_return": predicted_return,
                "reason": (
                    f"期望值{ev:.2%}<0: 置信度{confidence:.0%}×预测{predicted_return:.1%}"
                    f"+ 失败概率{(1-confidence):.0%}×止损{stop_loss:.1%}"
                ),
            }
        return {
            "consistent": True,
            "ev": round(ev, 4),
            "stop_loss": stop_loss,
            "confidence": confidence,
            "predicted_return": predicted_return,
            "reason": f"期望值{ev:.2%}≥0, 通过",
        }

    # ═══════════════ 止盈检查 v4.5.12 ═══════════════

    def _get_rs_take_profit(self, symbol: str) -> float:
        """获取RS动态止盈比例

        从 adaptive_params 读取RS分位数据，按分位返回止盈比例。
        如无RS数据，回退到固定止盈。
        """
        rs_override = self.take_profit_config.get("rs_enabled", True)
        if not rs_override:
            return self.take_profit_config.get("fixed_pct", DEFAULT_TAKE_PROFIT)
        try:
            rs_path = os.path.join(PROJECT_ROOT, "cache", "pre_market",
                                   f"{datetime.now().strftime('%Y%m%d')}_stocks.json")
            if os.path.exists(rs_path):
                with open(rs_path) as f:
                    stocks = json.load(f)
                for s in stocks:
                    if s.get("symbol", "") == symbol or s.get("code", "") == symbol:
                        rs = float(s.get("rs_quantile", s.get("rs_score", 0.5)))
                        for floor, tp in RS_TAKE_PROFIT_TIERS:
                            if rs >= floor:
                                return tp
        except Exception:
            pass
        return self.take_profit_config.get("fixed_pct", DEFAULT_TAKE_PROFIT)

    def check_single_position_take_profit(
        self, symbol: str, name: str, avg_price: float, current_price: float,
        peak_price: float = None,
    ) -> dict:
        """检查单票是否触发止盈 (v4.5.14 修复层级冲突)

        三层止盈 (无冲突版):
        1. RS弱股紧止盈: rs_tp < fixed_tp 时, 盈利≥rs_tp即卖出 (5%/10%)
        2. 有效目标止盈: 盈利 ≥ effective_target = max(fixed_tp, rs_tp)
           - RS强(分位>0.75) → effective=20%, 放行到更高
           - RS普通 → effective=15% (固定止盈)
        3. 移动止盈: 盈利≥trail_trigger(10%)且未达effective_target, 从峰值回落≥drawdown

        v4.5.14 修复:
        - 冲突1: RS强(20%)被固定(15%)截断 → 改为 effective_target=max(fixed, rs)
        - 冲突2: 移动止盈5%触发太低, 7-14%利润被提前截断 → 提高到10%

        Returns:
            {"triggered": bool, "pct": float, "reason": str, "action": str}
        """
        if avg_price <= 0 or current_price <= 0:
            return {"triggered": False, "pct": 0, "reason": "价格无效", "action": ""}

        pct = (current_price - avg_price) / avg_price

        # v4.6.x P3: 紧止损 — 在pct<=0判断前检查，覆盖3%-8%亏损区间
        tight_result = self._check_tight_stop(symbol, name, pct, current_price, peak_price)
        if tight_result:
            return tight_result

        if pct <= 0:
            return {"triggered": False, "pct": round(pct, 4), "reason": "未盈利", "action": ""}

        # 更新持仓峰值
        pos_key = symbol
        if pos_key not in self._position_peaks or current_price > self._position_peaks[pos_key]:
            self._position_peaks[pos_key] = current_price
            self._save_position_peaks()  # P2-FIX: 持久化移动止损峰值
        if peak_price is None:
            peak_price = self._position_peaks.get(pos_key, current_price)

        # 计算有效止盈目标: RS强时用RS止盈(20%), RS弱时用固定止盈(15%)
        fixed_tp = self.take_profit_config.get("fixed_pct", DEFAULT_TAKE_PROFIT)
        rs_tp = self._get_rs_take_profit(symbol)
        effective_target = max(fixed_tp, rs_tp)

        # v4.6.x P1: ATR波动率自适应止盈目标
        # 高波动股(ATR>3%): 放宽止盈(×1.3), 低波动股(ATR<1%): 收紧止盈(×0.8)
        try:
            from scripts.paper_trader import _get_atr_pct
            atr_pct = _get_atr_pct(symbol)
            if atr_pct and atr_pct > 0:
                if atr_pct > 0.03:
                    wave_factor = 1.30
                elif atr_pct < 0.01:
                    wave_factor = 0.80
                else:
                    wave_factor = 1.0 + (atr_pct - 0.01) * 15  # 线性插值: 1%→0.8, 2%→1.0, 3%→1.3
                    wave_factor = max(0.70, min(1.50, wave_factor))
                effective_target = effective_target * wave_factor
                rs_tp = rs_tp * wave_factor
                logger.debug(f"ATR自适应: {symbol} atr={atr_pct:.2%} wave={wave_factor:.2f} tp_target={effective_target:.1%}")
        except Exception:
            pass

        # 1. RS弱股紧止盈 (rs_tp < fixed_tp → RS分位<0.5 → 止盈5%/10%)
        if rs_tp < fixed_tp and pct >= rs_tp:
            logger.info(f"🟢 RS弱股紧止盈: {name}({symbol}) 盈利={pct:.2%} ≥ RS止盈={rs_tp:.1%} (RS弱, 比固定{fixed_tp:.1%}更紧)")
            return {"triggered": True, "pct": round(pct, 4),
                    "reason": f"RS弱股紧止盈 盈利{pct:.2%}≥RS止盈{rs_tp:.1%}", "action": "SELL"}

        # 2. v4.6.x: 达到有效目标 → 三重确认决定卖 or 切换移动跟踪
        # 不再无条件清仓：RS强势+趋势完好的牛股切换为移动止盈模式继续持有
        if pct >= effective_target:
            rs_strong = self._check_rs_strong(symbol)
            trend_intact = self._check_trend_intact(symbol, current_price)

            if rs_strong and trend_intact:
                # 牛股：不卖，切换为移动止盈模式（按分级回撤阈值从峰值跟踪）
                trail_threshold = self._get_trailing_drawdown_threshold(pct)
                drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price and peak_price > current_price else 0
                if drawdown_from_peak >= trail_threshold:
                    logger.info(
                        f"🔴 牛股移动止盈: {name}({symbol}) "
                        f"盈利={pct:.2%}≥{effective_target:.1%} "
                        f"峰值回撤{drawdown_from_peak:.1%}≥{trail_threshold:.1%} → 止盈"
                    )
                    return {"triggered": True, "pct": round(pct, 4),
                            "reason": f"牛股移动止盈 盈利{pct:.2%} 峰值回撤{drawdown_from_peak:.1%}≥{trail_threshold:.0%}",
                            "action": "SELL"}
                else:
                    logger.info(
                        f"⭐ 牛股暂持: {name}({symbol}) "
                        f"盈利={pct:.2%}≥{effective_target:.1%} RS强势+趋势完好 → 继续持有"
                    )
            else:
                # RS走弱或趋势反转 → 执行止盈
                signals = []
                if not rs_strong: signals.append("RS走弱")
                if not trend_intact: signals.append("破MA20")
                logger.info(
                    f"🟢 目标止盈: {name}({symbol}) "
                    f"盈利={pct:.2%}≥{effective_target:.1%} | {', '.join(signals)}"
                )
                return {"triggered": True, "pct": round(pct, 4),
                        "reason": f"目标止盈 盈利{pct:.2%}≥{effective_target:.1%} {'+'.join(signals)}",
                        "action": "SELL"}

        # 3. v4.6.x 融合版移动止盈 — 盈利在10%到目标之间，分级回撤 × RS确认 × MA趋势
        trail_trigger = self.take_profit_config.get("trailing_trigger", DEFAULT_TRAILING_PROFIT_TRIGGER)
        if pct >= trail_trigger and pct < effective_target:
            peak_pct = (peak_price - avg_price) / avg_price
            drawdown_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0
            trail_threshold = self._get_trailing_drawdown_threshold(pct)

            if drawdown_from_peak >= trail_threshold:
                # 三重确认：RS强度 + MA趋势（任一走弱才执行止盈）
                rs_strong = self._check_rs_strong(symbol)
                trend_intact = self._check_trend_intact(symbol, current_price)

                if rs_strong and trend_intact:
                    logger.info(
                        f"⏸️ 移动止盈暂缓: {name}({symbol}) "
                        f"峰值={peak_pct:.2%} 当前={pct:.2%} 回落={drawdown_from_peak:.1%}≥{trail_threshold:.1%} "
                        f"但RS强势+趋势完好 → 继续持有"
                    )
                    # 暂缓：记录但不触发
                else:
                    signals = []
                    if not rs_strong: signals.append("RS走弱")
                    if not trend_intact: signals.append("趋势反转(破MA20)")
                    logger.info(
                        f"🔴 移动止盈: {name}({symbol}) "
                        f"峰值={peak_pct:.2%} 当前={pct:.2%} 回落={drawdown_from_peak:.1%}≥{trail_threshold:.1%} "
                        f"| {', '.join(signals)}"
                    )
                    return {"triggered": True, "pct": round(pct, 4),
                            "reason": f"移动止盈 峰值{peak_pct:.2%}→{pct:.2%} 回落{drawdown_from_peak:.1%} {'+'.join(signals)}",
                            "action": "SELL"}

        return {"triggered": False, "pct": round(pct, 4), "reason": "正常", "action": ""}

    def _get_trailing_drawdown_threshold(self, profit_pct: float) -> float:
        """v4.6.x: 按盈利分级返回回撤容忍阈值"""
        for floor, dd in TRAILING_DRAWDOWN_TIERS:
            if profit_pct >= floor:
                return dd
        return DEFAULT_TRAILING_PROFIT_DRAWDOWN

    def _check_rs_strong(self, symbol: str) -> bool:
        """v4.6.x: RS分位>0.6 → 相对市场仍强势，倾向持有"""
        try:
            rs_path = os.path.join(PROJECT_ROOT, "cache", "pre_market",
                                   f"{datetime.now().strftime('%Y%m%d')}_stocks.json")
            if os.path.exists(rs_path):
                with open(rs_path) as f:
                    stocks = json.load(f)
                for s in stocks:
                    if s.get("symbol", "") == symbol or s.get("code", "") == symbol:
                        rs = float(s.get("rs_quantile", s.get("rs_score", 0.5)))
                        return rs > 0.60
        except Exception:
            pass
        return False  # 无数据时保守：视为走弱

    def _check_trend_intact(self, symbol: str, current_price: float) -> bool:
        """v4.6.x: 价格>MA20 → 趋势完好，回撤是正常调整而非反转"""
        try:
            from scripts.paper_trader import _get_ma_prices
            ma20, ma50, _ = _get_ma_prices(symbol)
            if ma20 and current_price > ma20:
                return True
            # MA50作为备用：如果MA20不可用但MA50可用
            if ma50 and current_price > ma50:
                return True
        except Exception:
            pass
        return False  # 无数据时保守：视为趋势反转

    def _check_tight_stop(self, symbol: str, name: str, pct: float,
                           current_price: float, peak_price: float = None) -> Optional[dict]:
        """v4.6.x P3: 紧止损 — 持仓亏损3%-8%区间保护

        触发条件 (满足任一):
        1. RS分位<0.3 (弱势股持续走弱)
        2. 距持仓峰值回撤≥5% (持续下跌趋势)

        不触发: 亏损<3% (噪音) 或 已触发正式止损(≥8%由check_stop_losses处理)
        """
        TIGHT_STOP_THRESHOLD = -0.03   # 亏损≥3%
        TIGHT_STOP_PEAK_DRAWDOWN = 0.05  # 距峰值回撤≥5%

        # 不在紧止损区间 (3%-8%亏损)
        if pct > TIGHT_STOP_THRESHOLD:
            return None

        triggers = []

        # 条件1: RS分位低 (弱势股)
        try:
            rs_tp = self._get_rs_take_profit(symbol)
            # rs_tp=5% → RS<0.25 (最弱), rs_tp=10% → RS 0.25-0.50
            if rs_tp <= 0.10:
                triggers.append(f"RS弱势(止盈={rs_tp:.0%})")
        except Exception:
            pass

        # 条件2: 距持仓峰值回撤≥5%
        if peak_price and peak_price > current_price:
            peak_drawdown = (peak_price - current_price) / peak_price
            if peak_drawdown >= TIGHT_STOP_PEAK_DRAWDOWN:
                triggers.append(f"峰值回撤{peak_drawdown:.1%}≥{TIGHT_STOP_PEAK_DRAWDOWN:.0%}")

        if triggers:
            reason = f"紧止损({', '.join(triggers)}) 亏损{pct:.2%}"
            logger.info(f"🔶 紧止损: {name}({symbol}) {reason}")
            return {"triggered": True, "pct": round(pct, 4), "reason": reason, "action": "SELL"}

        return None

    # ═══════════════ 仓位约束 ═══════════════

    def check_sector_concentration(
        self, planned_buys: List[dict], current_positions: List[dict],
        stock_pool: List[dict]
    ) -> dict:
        """检查行业/概念集中度（基于市值而非计数）

        计算方式:
        - 集中度 = 某行业持仓总市值 / 组合总持仓市值
        - 组合总持仓市值 = 所有当前持仓市值 + 所有计划买入金额
        - 避免小仓位与重仓位权重相等的计数偏差

        Returns:
            {
                "violations": [{"code": "...", "sector": "...", "reason": "..."}],
                "warnings": [...],
                "ok": bool
            }
        """
        violations = []
        warnings = []
        max_sector = self.position_limits.get("max_single_sector_pct", 0.25)
        max_concept = self.position_limits.get("max_single_concept_pct", 0.20)

        if max_sector <= 0 and max_concept <= 0:
            return {"violations": [], "warnings": [], "ok": True}

        # 构建代码→行业/概念映射
        pool_map = {}
        for s in stock_pool:
            code = s.get("symbol", "")
            concepts = s.get("concept", "")
            pools = {}
            if isinstance(concepts, str):
                for c in concepts.split(","):
                    if "申万行业" in c:
                        pools["sector"] = c.replace("A股-申万行业-", "").strip()
                    if "热门概念" in c:
                        pools.setdefault("concepts", []).append(
                            c.replace("A股-热门概念-", "").strip()
                        )
            pools["name"] = s.get("name", "")
            pools["tier"] = s.get("tier", "")
            pool_map[code] = pools

        # 计算组合总持仓市值 + 各行业/概念市值
        sector_values = {}    # {sector: total_value}
        concept_values = {}   # {concept: total_value}
        total_portfolio_value = 0.0

        def _get_value(pos: dict) -> float:
            """获取单个持仓的市值"""
            qty = int(pos.get("quantity", pos.get("shares", 0)))
            price = float(pos.get("current_price", pos.get("price", 0)))
            return qty * price

        # 遍历当前持仓
        for pos in current_positions:
            pos_val = _get_value(pos)
            if pos_val <= 0:
                continue
            total_portfolio_value += pos_val
            code = pos.get("code", pos.get("symbol", ""))
            info = pool_map.get(code, {})
            sec = info.get("sector", "")
            if sec:
                sector_values[sec] = sector_values.get(sec, 0) + pos_val
            for c in info.get("concepts", []):
                concept_values[c] = concept_values.get(c, 0) + pos_val

        # 遍历计划买入
        for buy in planned_buys:
            buy_val = _get_value(buy)
            if buy_val <= 0:
                continue
            total_portfolio_value += buy_val
            code = buy.get("code", buy.get("symbol", ""))
            info = pool_map.get(code, {})
            sec = info.get("sector", "")
            if sec:
                sec_val = sector_values.get(sec, 0) + buy_val
            else:
                sec_val = 0
            if sec and total_portfolio_value > 0:
                concentration = sec_val / total_portfolio_value
                if concentration > max_sector:
                    violations.append({
                        "code": code,
                        "name": info.get("name", code),
                        "sector": sec,
                        "concentration": round(concentration, 3),
                        "limit": max_sector,
                        "reason": f"{sec}行业市值占比{concentration:.0%}>{max_sector:.0%}",
                    })
                sector_values[sec] = sec_val

            for c in info.get("concepts", []):
                conc_val = concept_values.get(c, 0) + buy_val
                if total_portfolio_value > 0:
                    concentration = conc_val / total_portfolio_value
                    if concentration > max_concept:
                        warnings.append({
                            "code": code,
                            "name": info.get("name", code),
                            "concept": c,
                            "concentration": round(concentration, 3),
                            "limit": max_concept,
                            "reason": f"概念'{c}'市值占比{concentration:.0%}>{max_concept:.0%}",
                        })
                concept_values[c] = conc_val

        ok = len(violations) == 0

        if violations:
            logger.warning(f"⚠️ 行业集中度违规: {len(violations)}个")
        if warnings:
            logger.info(f"ℹ️ 概念集中度警告: {len(warnings)}个")

        return {
            "violations": violations,
            "warnings": warnings,
            "ok": ok,
        }

    # ═══════════════ 综合检查 ═══════════════

    def pre_execution_check(
        self,
        planned_trades: List[dict],
        current_positions: List[dict],
        initial_capital: float,
        current_equity: float,
        stock_pool: List[dict],
    ) -> dict:
        """执行前综合风控检查

        Returns:
            {
                "approved": bool,
                "blocked_trades": [],
                "stop_losses": [],
                "concentration_violations": [],
                "portfolio_status": str
            }
        """
        result = {
            "approved": True,
            "blocked_trades": [],
            "stop_losses": [],
            "take_profits": [],
            "concentration_violations": [],
            "portfolio_status": "🟢 正常",
        }

        if not self.enabled:
            return result

        # 1. 组合级别止损
        pf_stop = self.check_portfolio_stop(initial_capital, current_equity)
        if pf_stop.get("triggered"):
            result["approved"] = False
            result["portfolio_status"] = f"🔴 组合止损: {pf_stop['reason']}"
            logger.critical(result["portfolio_status"])
            # v4.5.12 P2-13: 双通道告警
            try:
                from common.alert_utils import send_alert
                send_alert("组合止损触发", pf_stop['reason'], level="critical")
            except Exception:
                pass
            return result

        # 2. 检查当前持仓：止损 + 止盈
        buy_trades = [t for t in planned_trades if t.get("action", "") in ("BUY", "buy")]
        for trade in buy_trades:
            code = trade.get("code", trade.get("symbol", ""))
            avg_price = trade.get("avg_cost", trade.get("price", 0))
            current_price = trade.get("current_price", avg_price)

            sl = self.check_single_position_stop(
                code, trade.get("name", code), avg_price, current_price
            )
            if sl["triggered"]:
                result["stop_losses"].append(sl)

        # 遍历当前持仓
        for pos in current_positions:
            pcode = pos.get("code", pos.get("symbol", ""))
            pname = pos.get("name", pcode)
            avg_cost = pos.get("avg_price", pos.get("avg_cost", 0))
            cur_price = pos.get("current_price", 0)
            peak = pos.get("highest_price", None)

            # 止损
            sl = self.check_single_position_stop(pcode, pname, avg_cost, cur_price)
            if sl["triggered"]:
                result["stop_losses"].append(sl)

            # v4.5.12: 止盈
            tp = self.check_single_position_take_profit(
                pcode, pname, avg_cost, cur_price, peak
            )
            if tp["triggered"]:
                result["take_profits"].append(tp)

        # 2b. 止损预测一致性校验 (v4.5.12 P2-13)
        for trade in buy_trades:
            ev_check = self.check_prediction_stop_consistency(trade)
            if not ev_check.get("consistent", True):
                code = trade.get("code", trade.get("symbol", ""))
                logger.warning(f"  ⚠️ 止损预测不一致: {code} {ev_check['reason']}")
                result["blocked_trades"].append({
                    "code": code,
                    "name": trade.get("name", code),
                    "reason": ev_check["reason"],
                })

        # 3. 行业/概念集中度
        sector_check = self.check_sector_concentration(
            buy_trades, current_positions, stock_pool
        )
        if not sector_check["ok"]:
            result["approved"] = False
            result["concentration_violations"] = sector_check["violations"]
            for v in sector_check["violations"]:
                result["blocked_trades"].append({
                    "code": v["code"],
                    "name": v.get("name", ""),
                    "reason": v["reason"],
                })

        if result["stop_losses"]:
            result["portfolio_status"] = "🟡 止损警告"
        if result["take_profits"]:
            result["portfolio_status"] = "🟢 止盈触发"
        if result["blocked_trades"]:
            result["portfolio_status"] = "🔴 集中度超标"

        # v4.6.x: 持仓关联风险检测
        try:
            corr_check = self.check_correlation_risk(current_positions)
            if corr_check.get("high_correlation"):
                result["correlation_risk"] = corr_check
                avg = corr_check.get("avg_correlation", 0)
                clusters = corr_check.get("clusters", [])
                cluster_desc = "; ".join(
                    [f"{c['stocks']}(r={c['avg_corr']})" for c in clusters[:3]]
                )
                logger.warning(f"📊 持仓关联风险: 平均r={avg:.2f}, 集群: {cluster_desc}")
                result["portfolio_status"] = "🟡 关联风险"
        except Exception as e:
            logger.warning(f"相关性检测异常: {e}")

        return result


# ═══════════════ 持仓峰值持久化 ═══════════════

    def _save_position_peaks(self):
        """P2-FIX: 持久化移动止损峰值"""
        try:
            path = os.path.join(PROJECT_ROOT, "confidence_data", "position_peaks.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(self._position_peaks, f)
        except Exception as e:
            logger.warning(f"保存position_peaks失败: {e}")

    def _load_position_peaks(self):
        """P2-FIX: 加载移动止损峰值"""
        try:
            path = os.path.join(PROJECT_ROOT, "confidence_data", "position_peaks.json")
            if os.path.exists(path):
                with open(path, 'r') as f:
                    self._position_peaks = json.load(f)
        except Exception:
            self._position_peaks = {}

    # ═══════════════ 持仓关联风险 v4.6.x ═══════════════

    def check_correlation_risk(self, positions: List[dict], lookback_days: int = 60) -> dict:
        """
        检查持仓间的相关性风险

        用麦蕊历史K线计算持仓股票pairwise日收益率相关系数。
        如果平均相关性>0.7, 标记为高关联集群。

        Args:
            positions: [{stock_code, stock_name, ...}]
            lookback_days: 回溯天数

        Returns:
            {
                "high_correlation": bool,
                "avg_correlation": float,
                "clusters": [{stocks, avg_corr, n}],  # 高关联集群
                "pairwise": {code: {code2: corr}}      # 全部pairwise(简略)
            }
        """
        if len(positions) < 2:
            return {"high_correlation": False, "avg_correlation": 0, "clusters": [], "pairwise": {}}

        codes = [p.get('stock_code', p.get('symbol', '')) for p in positions
                 if p.get('stock_code', p.get('symbol', ''))]
        if len(codes) < 2:
            return {"high_correlation": False, "avg_correlation": 0, "clusters": [], "pairwise": {}}

        # 获取历史K线 (麦蕊优先, 新浪fallback)
        price_data = {}  # {code: [close_prices]}
        for code in codes:
            try:
                from config.mairui_api_config import get_kline_history as _get_kline
                end = datetime.now().strftime("%Y-%m-%d")
                start = (datetime.now() - timedelta(days=lookback_days * 2)).strftime("%Y-%m-%d")
                df = _get_kline(code, start_date=start, end_date=end)
                if df is not None and hasattr(df, 'iloc') and len(df) > 10:
                    price_data[code] = df['close'].tolist()
                else:
                    # 麦蕊失败, 尝试新浪
                    from black_swan_optimized.data_adapter import get_sina_kline
                    sina_code = f"sh{code}" if not code.startswith(('sh','sz')) else code
                    if not code.startswith(('sh','sz')):
                        # 判断交易所: 6开头=sh, 0/3开头=sz
                        sina_code = f"sh{code}" if code.startswith('6') else f"sz{code}"
                    prices, _ = get_sina_kline(sina_code, start, end)
                    if prices and len(prices) > 10:
                        price_data[code] = prices
            except Exception:
                continue

        if len(price_data) < 2:
            return {"high_correlation": False, "avg_correlation": 0,
                    "clusters": [], "pairwise": {}, "note": "数据不足以计算相关性"}

        # 对齐日期 (取最短长度)
        min_len = min(len(v) for v in price_data.values())
        if min_len < 5:
            return {"high_correlation": False, "avg_correlation": 0,
                    "clusters": [], "pairwise": {}, "note": f"数据点不足({min_len})"}

        aligned = {k: v[-min_len:] for k, v in price_data.items()}

        # 计算日收益率
        returns = {}
        for k, prices in aligned.items():
            r = [(prices[i] - prices[i - 1]) / prices[i - 1] for i in range(1, len(prices))]
            returns[k] = r

        # 计算pairwise皮尔逊相关系数
        import statistics
        pairwise = {}
        all_corrs = []
        codes_list = list(returns.keys())
        for i in range(len(codes_list)):
            c1 = codes_list[i]
            pairwise[c1] = {}
            for j in range(i + 1, len(codes_list)):
                c2 = codes_list[j]
                r1, r2 = returns[c1], returns[c2]
                if len(r1) != len(r2) or len(r1) < 5:
                    continue
                try:
                    mean1, mean2 = statistics.mean(r1), statistics.mean(r2)
                    std1 = statistics.stdev(r1)
                    std2 = statistics.stdev(r2)
                    if std1 == 0 or std2 == 0:
                        continue
                    cov = sum((r1[k] - mean1) * (r2[k] - mean2) for k in range(len(r1)))
                    corr = cov / ((len(r1) - 1) * std1 * std2)
                    corr = max(-1, min(1, corr))
                    pairwise[c1][c2] = round(corr, 3)
                    pairwise[c2] = pairwise.get(c2, {})
                    pairwise[c2][c1] = round(corr, 3)
                    all_corrs.append(corr)
                except Exception:
                    continue

        if not all_corrs:
            return {"high_correlation": False, "avg_correlation": 0, "clusters": [], "pairwise": pairwise}

        avg_corr = sum(all_corrs) / len(all_corrs)

        # 识别高关联集群 (>0.7)
        clusters = []
        visited = set()
        for c1 in codes_list:
            if c1 in visited:
                continue
            high_corr_neighbors = [c2 for c2 in pairwise.get(c1, {})
                                   if pairwise[c1].get(c2, 0) > 0.7]
            if high_corr_neighbors:
                cluster_stocks = [c1] + high_corr_neighbors
                visited.update(cluster_stocks)
                # 计算该集群平均相关性
                cluster_corrs = [pairwise[c1].get(c2, 0) for c2 in high_corr_neighbors]
                clusters.append({
                    "stocks": cluster_stocks,
                    "avg_corr": round(sum(cluster_corrs) / len(cluster_corrs), 3),
                    "n": len(cluster_stocks),
                })

        high_corr = avg_corr > 0.7 or len(clusters) > 0

        return {
            "high_correlation": high_corr,
            "avg_correlation": round(avg_corr, 3),
            "clusters": clusters,
            "pairwise": pairwise,
        }

# ═══════════════ 绩效归因 ═══════════════

def performance_attribution(
    trades: List[dict],
    benchmark_returns: Optional[List[float]] = None,
    sector_returns: Optional[Dict[str, float]] = None,
    open_positions: Optional[List[dict]] = None,
) -> dict:
    """绩效归因分析

    分解: 选股α + 行业权重β + 择时γ + 交易成本

    v4.5.9b: 融合已实现+未实现盈亏。open_positions格式:
        [{"symbol": str, "quantity": int, "avg_cost": float, "current_price": float}]

    Returns:
        {"stock_selection": float, "sector_allocation": float,
         "timing": float, "cost": float, "total": float}
    """
    if not trades and not open_positions:
        return {"stock_selection": 0, "sector_allocation": 0, "timing": 0, "cost": 0, "total": 0}

    benchmark_returns = benchmark_returns or []
    sector_returns = sector_returns or {}

    # === 已实现盈亏 (SELL交易) ===
    closed = [t for t in trades if t.get("action", "") in ("SELL", "sell") and t.get("pnl") is not None and t.get("pnl", 0) != 0]
    realized_pnl = sum(t.get("pnl", 0) for t in closed)
    realized_winning = sum(t.get("pnl", 0) for t in closed if t.get("pnl", 0) > 0)
    realized_losing = abs(sum(t.get("pnl", 0) for t in closed if t.get("pnl", 0) < 0))
    realized_wins = len([t for t in closed if t.get("pnl", 0) > 0])
    realized_losses = len([t for t in closed if t.get("pnl", 0) < 0])

    # === 未实现盈亏 (当前持仓浮盈/浮亏) ===
    unrealized_pnl = 0.0
    unrealized_winning = 0.0
    unrealized_losing = 0.0
    unrealized_wins = 0
    unrealized_losses = 0
    if open_positions:
        for pos in open_positions:
            avg_cost = float(pos.get("avg_cost", 0))
            cur_price = float(pos.get("current_price", 0))
            qty = int(pos.get("quantity", 0))
            if avg_cost > 0 and cur_price > 0 and qty > 0:
                upnl = (cur_price - avg_cost) * qty
                unrealized_pnl += upnl
                if upnl > 0:
                    unrealized_winning += upnl
                    unrealized_wins += 1
                elif upnl < 0:
                    unrealized_losing += abs(upnl)
                    unrealized_losses += 1

    # === 融合计算 ===
    total_pnl = realized_pnl + unrealized_pnl
    total_winning = realized_winning + unrealized_winning
    total_losing = realized_losing + unrealized_losing
    total_wins = realized_wins + unrealized_wins
    total_losses = realized_losses + unrealized_losses

    if not closed and not open_positions:
        return {"stock_selection": 0, "sector_allocation": 0, "timing": 0, "cost": 0, "total": 0}

    # 简单归因:
    # - 择时: 通过比较持有期vs最优持有期
    # - 选股: 盈利交易的α(超额收益)
    # - 成本: 估算交易成本
    if closed:
        avg_hold = sum(t.get("hold", 0) for t in closed) / len(closed)
    else:
        avg_hold = 10  # 未实现持仓暂估10天
    ideal_hold = 20  # 假设最优持有期20天

    timing_pnl = total_winning * (1 - min(avg_hold, ideal_hold) / max(avg_hold, ideal_hold, 1))
    stock_selection = total_winning - timing_pnl
    # P2-FIX: 用实际交易金额计算成本，而非硬编码10万
    trading_cost = sum(
        abs(t.get("amount", t.get("price", 0) * t.get("quantity", 100000))) * COMMISSION_RATE
        for t in trades if t.get("action", "") in ("BUY", "buy", "SELL", "sell")
    )

    total_trades = total_wins + total_losses
    win_rate = round(total_wins / total_trades, 4) if total_trades > 0 else 0
    profit_factor = round(total_winning / total_losing, 4) if total_losing > 0 else float('inf')

    return {
        "stock_selection": round(stock_selection / max(abs(total_pnl), 1), 4),
        "sector_allocation": round(0.2 * total_winning / max(abs(total_pnl), 1), 4),
        "timing": round(timing_pnl / max(abs(total_pnl), 1), 4),
        "cost": round(-trading_cost / max(abs(total_pnl), 1), 4),
        "total": round(total_pnl, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_hold_days": round(avg_hold, 1),
        "benchmark_return": round(sum(benchmark_returns), 4) if benchmark_returns else 0,
        "sector_context_count": len(sector_returns),
        "realized_wins": realized_wins,
        "unrealized_wins": unrealized_wins,
        "total_positions": len(open_positions) if open_positions else 0,
    }
