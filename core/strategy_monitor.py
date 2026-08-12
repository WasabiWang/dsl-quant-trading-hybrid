#!/usr/bin/env python3
"""
风控模块 - Strategy Monitor
用于监控交易策略风险，包括仓位管理、止损止盈、异常检测等

【GLM-5】
"""
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

class StrategyMonitor:
    """策略监控器"""
    
    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        
        # 默认风控参数
        self.max_position_pct = self.config.get('max_position_pct', 0.3)  # 单只股票最大仓位30%
        self.max_total_position = self.config.get('max_total_position', 0.8)  # 总仓位上限80%
        self.stop_loss_pct = self.config.get('stop_loss_pct', -0.08)  # 止损线 -8% (与risk_manager统一)
        self.take_profit_pct = self.config.get('take_profit_pct', 0.15)  # 止盈线 15%
        self.trailing_stop_pct = self.config.get('trailing_stop_pct', 0.05)  # 移动止盈 5%
        
        # 波动率参数
        self.volatility_lookback = self.config.get('volatility_lookback', 20)  # 波动率回看天数
        
    def calculate_volatility(self, prices: pd.Series) -> float:
        """计算波动率（年化）"""
        if len(prices) < 2:
            return 0.0
        returns = prices.pct_change().dropna()
        if len(returns) == 0:
            return 0.0
        volatility = returns.std() * np.sqrt(252)  # 年化
        return volatility
    
    def calculate_position_size(self, symbol: str, account_value: float, 
                                current_price: float, volatility: float) -> int:
        """
        根据波动率计算仓位大小
        波动越小，仓位越重
        """
        # 波动率越高，仓位越低
        if volatility <= 0:
            volatility = 0.2  # 默认20%波动率
        
        # 目标：波动率倒数 * 基础仓位
        target_pct = min(self.max_position_pct, 0.3 / volatility)
        target_pct = max(target_pct, 0.05)  # 最小5%
        
        # 计算股数（A股100股整数倍）
        position_value = account_value * target_pct
        shares = int(position_value / current_price / 100) * 100
        
        return shares
    
    def check_stop_loss(self, entry_price: float, current_price: float) -> Tuple[bool, float]:
        """检查是否触发止损"""
        pnl_pct = (current_price - entry_price) / entry_price
        
        if pnl_pct <= self.stop_loss_pct:
            return True, pnl_pct
        return False, pnl_pct
    
    def check_take_profit(self, entry_price: float, current_price: float, 
                          peak_price: float = None) -> Tuple[bool, str]:
        """
        检查是否触发止盈
        支持固定止盈 + 移动止盈
        """
        pnl_pct = (current_price - entry_price) / entry_price
        
        # 固定止盈
        if pnl_pct >= self.take_profit_pct:
            return True, "固定止盈"
        
        # 移动止盈
        if peak_price is not None:
            trailing_trigger = (peak_price - current_price) / peak_price
            if trailing_trigger >= self.trailing_stop_pct and pnl_pct > 0:
                return True, "移动止盈"
        
        return False, ""
    
    def check_risk_limits(self, positions: Dict[str, Dict], account_value: float) -> Dict:
        """检查整体风险限制"""
        total_value = sum(p['value'] for p in positions.values())
        total_pct = total_value / account_value if account_value > 0 else 0
        
        warnings = []
        
        if total_pct > self.max_total_position:
            warnings.append(f"总仓位超过上限: {total_pct:.1%} > {self.max_total_position:.1%}")
        
        for symbol, pos in positions.items():
            if pos['pct'] > self.max_position_pct:
                warnings.append(f"单只股票仓位超标: {symbol} {pos['pct']:.1%} > {self.max_position_pct:.1%}")
        
        return {
            'total_pct': total_pct,
            'warnings': warnings,
            'safe': len(warnings) == 0
        }
    
    def detect_anomaly(self, prices: pd.Series, window: int = 5) -> List[Dict]:
        """检测价格异常波动"""
        if len(prices) < window:
            return []
        
        anomalies = []
        
        # 计算滚动统计
        ma = prices.rolling(window).mean()
        std = prices.rolling(window).std()
        
        # 检测异常跌幅
        recent = prices.iloc[-window:]
        max_drop = (recent.max() - recent.min()) / recent.max()
        
        if max_drop > 0.10:  # 10%以上波动
            anomalies.append({
                'type': 'high_volatility',
                'magnitude': max_drop,
                'message': f'近期波动较大: {max_drop:.1%}'
            })
        
        return anomalies
    
    def generate_risk_report(self, symbol: str, entry_price: float, 
                            current_price: float, account_value: float) -> Dict:
        """生成风险报告"""
        pnl_pct = (current_price - entry_price) / entry_price
        
        # 检查各项风控
        stop_loss_triggered, current_pnl = self.check_stop_loss(entry_price, current_price)
        take_profit_triggered, tp_reason = self.check_take_profit(entry_price, current_price)
        
        # 计算当前持仓
        position_value = current_price * 100  # 假设1手
        position_pct = position_value / account_value if account_value > 0 else 0
        
        report = {
            'symbol': symbol,
            'entry_price': entry_price,
            'current_price': current_price,
            'pnl_pct': pnl_pct,
            'position_pct': position_pct,
            'stop_loss_triggered': stop_loss_triggered,
            'take_profit_triggered': take_profit_triggered,
            'take_profit_reason': tp_reason,
            'action': 'HOLD',
            'message': '继续持有'
        }
        
        # 生成建议
        if stop_loss_triggered:
            report['action'] = 'SELL'
            report['message'] = f'触发止损 {current_pnl:.1%}'
        elif take_profit_triggered:
            report['action'] = 'SELL'
            report['message'] = f'触发止盈: {tp_reason}'
        elif pnl_pct < self.stop_loss_pct * 0.5:
            report['action'] = 'WARN'
            report['message'] = f'接近止损线 {current_pnl:.1%}'
        
        return report


class CircuitBreaker:
    """熔断器 - 连续亏损时暂停交易"""
    
    def __init__(self, max_consecutive_losses: int = 3, cooldown_minutes: int = 60):
        self.max_consecutive_losses = max_consecutive_losses
        self.cooldown_minutes = cooldown_minutes
        self.consecutive_losses = 0
        self.last_loss_time = None
        self.triggered = False
    
    def record_trade(self, pnl_pct: float):
        """记录交易结果"""
        if pnl_pct < 0:
            self.consecutive_losses += 1
            self.last_loss_time = datetime.now()
        else:
            self.consecutive_losses = 0
        
        self.triggered = self.consecutive_losses >= self.max_consecutive_losses
    
    def can_trade(self) -> Tuple[bool, str]:
        """检查是否可以交易"""
        if self.triggered:
            # 检查冷却时间
            if self.last_loss_time:
                elapsed = (datetime.now() - self.last_loss_time).total_seconds() / 60
                if elapsed >= self.cooldown_minutes:
                    self.triggered = False
                    self.consecutive_losses = 0
                    return True, "冷却期结束，恢复交易"
                else:
                    remaining = self.cooldown_minutes - elapsed
                    return False, f"熔断中，{remaining:.0f}分钟后恢复"
            
            return False, "熔断触发，等待冷却"
        
        return True, "正常"
    
    def reset(self):
        """重置熔断器"""
        self.consecutive_losses = 0
        self.last_loss_time = None
        self.triggered = False


def test_strategy_monitor():
    """测试风控模块"""
    print("🛡️ 测试风控模块...")
    
    # 创建监控器
    config = {
        'max_position_pct': 0.3,
        'max_total_position': 0.8,
        'stop_loss_pct': -0.08,
        'take_profit_pct': 0.15,
        'trailing_stop_pct': 0.05
    }
    
    monitor = StrategyMonitor(config)
    
    # 测试数据
    test_prices = pd.Series([100, 102, 101, 103, 98, 95, 97, 99, 101, 100])
    
    # 测试波动率计算
    vol = monitor.calculate_volatility(test_prices)
    print(f"  ✅ 波动率计算: {vol:.2%}")
    
    # 测试仓位计算
    shares = monitor.calculate_position_size("600760", 100000, 50.0, vol)
    print(f"  ✅ 仓位计算: {shares} 股")
    
    # 测试止损
    triggered, pnl = monitor.check_stop_loss(100, 92)
    print(f"  ✅ 止损检查: 触发={triggered}, PnL={pnl:.2%}")
    
    # 测试止盈
    triggered, reason = monitor.check_take_profit(100, 118, peak_price=120)
    print(f"  ✅ 止盈检查: 触发={triggered}, 原因={reason}")
    
    # 测试熔断器
    breaker = CircuitBreaker(max_consecutive_losses=3)
    breaker.record_trade(-0.02)
    breaker.record_trade(-0.03)
    can_trade, msg = breaker.can_trade()
    print(f"  ✅ 熔断器: {can_trade}, {msg}")
    
    print("\n🛡️ 风控模块测试完成!")
    return True


if __name__ == "__main__":
    test_strategy_monitor()