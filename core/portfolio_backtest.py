#!/usr/bin/env python3
"""
portfolio_backtest.py - 多策略组合回测引擎
多策略并行回测+净值组合+相关性矩阵+蒙特卡洛权重优化

作者：DeepSeek (custom-api-deepseek-com/deepseek-chat)
日期：2026-04-19
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

class PortfolioBacktest:
    """多策略组合回测引擎"""
    
    def __init__(self):
        self.strategies = {}
        self.results = {}
        self.correlation_matrix = None
        
    def add_strategy(self, strategy_id: str, returns_series: pd.Series):
        """
        添加策略收益序列
        
        Args:
            strategy_id: 策略ID
            returns_series: 收益序列
        """
        self.strategies[strategy_id] = returns_series
        print(f"✅ 添加策略: {strategy_id}, 数据点: {len(returns_series)}")
    
    def calculate_correlation_matrix(self) -> pd.DataFrame:
        """
        计算策略相关性矩阵
        
        Returns:
            相关性矩阵DataFrame
        """
        if len(self.strategies) < 2:
            print("⚠️ 策略数量不足，无法计算相关性矩阵")
            return pd.DataFrame()
        
        # 对齐所有策略的时间序列
        aligned_data = pd.DataFrame()
        for strategy_id, returns in self.strategies.items():
            aligned_data[strategy_id] = returns
        
        # 删除NaN值
        aligned_data = aligned_data.dropna()
        
        # 计算相关性矩阵
        self.correlation_matrix = aligned_data.corr()
        
        print("📊 相关性矩阵计算完成:")
        print(self.correlation_matrix.round(3))
        
        return self.correlation_matrix
    
    def monte_carlo_weight_optimization(self, num_simulations: int = 10000) -> Dict:
        """
        蒙特卡洛权重优化
        
        Args:
            num_simulations: 模拟次数
            
        Returns:
            优化结果
        """
        if len(self.strategies) < 2:
            print("⚠️ 策略数量不足，无法进行权重优化")
            return {}
        
        # 对齐数据
        aligned_data = pd.DataFrame()
        for strategy_id, returns in self.strategies.items():
            aligned_data[strategy_id] = returns
        
        aligned_data = aligned_data.dropna()
        
        if aligned_data.empty:
            print("❌ 数据对齐后为空")
            return {}
        
        n_strategies = len(self.strategies)
        strategy_ids = list(self.strategies.keys())
        
        # 存储模拟结果
        simulations = []
        
        print(f"🎲 开始蒙特卡洛权重优化 ({num_simulations}次模拟)...")
        
        for i in range(num_simulations):
            # 生成随机权重（和为1）
            weights = np.random.random(n_strategies)
            weights = weights / weights.sum()
            
            # 计算组合收益
            portfolio_returns = (aligned_data * weights).sum(axis=1)
            
            # 计算绩效指标
            total_return = portfolio_returns.sum()
            annual_return = total_return * 252 / len(portfolio_returns) if len(portfolio_returns) > 0 else 0
            volatility = portfolio_returns.std() * np.sqrt(252)
            sharpe_ratio = annual_return / volatility if volatility > 0 else 0
            # 索提诺比率(仅惩罚下行波动)
            downside = portfolio_returns[portfolio_returns < 0]
            downside_std = downside.std() * np.sqrt(252) if len(downside) > 0 else 0
            sortino_ratio = annual_return / downside_std if downside_std > 0 else 0
            
            # 计算最大回撤
            cumulative = (1 + portfolio_returns).cumprod()
            running_max = cumulative.expanding().max()
            drawdown = (cumulative - running_max) / running_max
            max_drawdown = drawdown.min()
            
            simulations.append({
                'weights': weights,
                'total_return': total_return,
                'annual_return': annual_return,
                'volatility': volatility,
                'sharpe_ratio': sharpe_ratio,
                'sortino_ratio': round(sortino_ratio, 2),
                'max_drawdown': max_drawdown
            })
        
        # 找到最优组合（最高夏普比率）
        simulations_df = pd.DataFrame(simulations)
        best_idx = simulations_df['sharpe_ratio'].idxmax()
        best_simulation = simulations_df.iloc[best_idx]
        
        # 准备结果
        result = {
            'optimal_weights': dict(zip(strategy_ids, best_simulation['weights'])),
            'performance': {
                'total_return': float(best_simulation['total_return']),
                'annual_return': float(best_simulation['annual_return']),
                'volatility': float(best_simulation['volatility']),
                'sharpe_ratio': float(best_simulation['sharpe_ratio']),
                'max_drawdown': float(best_simulation['max_drawdown'])
            },
            'simulation_stats': {
                'num_simulations': num_simulations,
                'avg_sharpe': float(simulations_df['sharpe_ratio'].mean()),
                'max_sharpe': float(simulations_df['sharpe_ratio'].max()),
                'min_sharpe': float(simulations_df['sharpe_ratio'].min())
            }
        }
        
        print(f"✅ 权重优化完成，最优夏普比率: {best_simulation['sharpe_ratio']:.3f}")
        for strategy_id, weight in result['optimal_weights'].items():
            print(f"  {strategy_id}: {weight:.2%}")
        
        return result
    
    def run_portfolio_backtest(self, weights: Dict[str, float] = None) -> Dict:
        """
        运行组合回测
        
        Args:
            weights: 策略权重字典
            
        Returns:
            组合回测结果
        """
        if len(self.strategies) < 2:
            print("⚠️ 策略数量不足，无法进行组合回测")
            return {}
        
        # 对齐数据
        aligned_data = pd.DataFrame()
        for strategy_id, returns in self.strategies.items():
            aligned_data[strategy_id] = returns
        
        aligned_data = aligned_data.dropna()
        
        if aligned_data.empty:
            print("❌ 数据对齐后为空")
            return {}
        
        strategy_ids = list(self.strategies.keys())
        
        # 如果没有提供权重，使用等权重
        if weights is None:
            weights = {sid: 1/len(strategy_ids) for sid in strategy_ids}
        
        # 确保所有权重和为1
        weight_sum = sum(weights.values())
        if abs(weight_sum - 1.0) > 0.001:
            weights = {k: v/weight_sum for k, v in weights.items()}
        
        # 创建权重数组
        weight_array = np.array([weights.get(sid, 0) for sid in strategy_ids])
        
        # 计算组合收益
        portfolio_returns = (aligned_data * weight_array).sum(axis=1)
        
        # 计算绩效指标
        result = self._calculate_performance_metrics(portfolio_returns, strategy_ids, weights)
        
        # 存储结果
        self.results['portfolio'] = result
        
        return result
    
    def _calculate_performance_metrics(self, returns: pd.Series, strategy_ids: List[str], 
                                     weights: Dict[str, float]) -> Dict:
        """计算绩效指标"""
        if len(returns) == 0:
            return {}
        
        # 基础指标
        total_return = returns.sum()
        annual_return = total_return * 252 / len(returns)
        volatility = returns.std() * np.sqrt(252)
        sharpe_ratio = annual_return / volatility if volatility > 0 else 0
        
        # 最大回撤
        cumulative = (1 + returns).cumprod()
        running_max = cumulative.expanding().max()
        drawdown = (cumulative - running_max) / running_max
        max_drawdown = drawdown.min()
        
        # 胜率
        win_rate = (returns > 0).mean()
        
        # 盈亏比
        winning_returns = returns[returns > 0]
        losing_returns = returns[returns < 0]
        profit_loss_ratio = abs(winning_returns.mean() / losing_returns.mean()) if len(losing_returns) > 0 else 0
        
        # 计算各策略贡献
        strategy_contributions = {}
        for sid in strategy_ids:
            if sid in self.strategies:
                strategy_returns = self.strategies[sid].reindex(returns.index).fillna(0)
                contribution = (strategy_returns * weights.get(sid, 0)).sum()
                strategy_contributions[sid] = {
                    'weight': weights.get(sid, 0),
                    'contribution': float(contribution),
                    'contribution_pct': float(contribution / total_return) if total_return != 0 else 0
                }
        
        result = {
            'returns_series': returns,
            'performance': {
                'total_return': float(total_return),
                'annual_return': float(annual_return),
                'volatility': float(volatility),
                'sharpe_ratio': float(sharpe_ratio),
                'max_drawdown': float(max_drawdown),
                'win_rate': float(win_rate),
                'profit_loss_ratio': float(profit_loss_ratio),
                'num_trades': len(returns)
            },
            'weights': weights,
            'strategy_contributions': strategy_contributions,
            'timestamp': datetime.now().isoformat()
        }
        
        return result
    
    def generate_portfolio_report(self) -> str:
        """生成组合报告"""
        if 'portfolio' not in self.results:
            return "无组合回测结果"
        
        result = self.results['portfolio']
        perf = result['performance']
        
        report = "# 多策略组合回测报告\n\n"
        report += f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        report += "## 📊 组合绩效\n"
        report += f"- **总收益率**: {perf['total_return']:.2%}\n"
        report += f"- **年化收益率**: {perf['annual_return']:.2%}\n"
        report += f"- **年化波动率**: {perf['volatility']:.2%}\n"
        report += f"- **夏普比率**: {perf['sharpe_ratio']:.3f}\n"
        report += f"- **最大回撤**: {perf['max_drawdown']:.2%}\n"
        report += f"- **胜率**: {perf['win_rate']:.2%}\n"
        report += f"- **盈亏比**: {perf['profit_loss_ratio']:.2f}\n"
        report += f"- **交易次数**: {perf['num_trades']}\n\n"
        
        report += "## ⚖️ 策略权重\n"
        for strategy_id, weight in result['weights'].items():
            contribution = result['strategy_contributions'].get(strategy_id, {})
            contrib_pct = contribution.get('contribution_pct', 0)
            report += f"- **{strategy_id}**: {weight:.2%} (贡献: {contrib_pct:.2%})\n"
        
        # 相关性矩阵
        if self.correlation_matrix is not None and not self.correlation_matrix.empty:
            report += "\n## 🔗 策略相关性\n"
            report += "| 策略 | " + " | ".join(self.correlation_matrix.columns) + " |\n"
            report += "|------|" + "|".join(["---"] * (len(self.correlation_matrix.columns) + 1)) + "|\n"
            
            for i, row in self.correlation_matrix.iterrows():
                report += f"| {i} | " + " | ".join([f"{val:.3f}" for val in row.values]) + " |\n"
        
        report += "\n## 📈 绩效分析\n"
        if perf['sharpe_ratio'] > 1.0:
            report += "✅ **优秀**: 夏普比率 > 1.0，风险调整后收益良好\n"
        elif perf['sharpe_ratio'] > 0.5:
            report += "⚠️ **一般**: 夏普比率 0.5-1.0，有一定风险调整收益\n"
        else:
            report += "❌ **较差**: 夏普比率 < 0.5，风险调整后收益不理想\n"
        
        if perf['max_drawdown'] > -0.20:
            report += "✅ **回撤控制良好**: 最大回撤 < 20%\n"
        elif perf['max_drawdown'] > -0.30:
            report += "⚠️ **回撤控制一般**: 最大回撤 20-30%\n"
        else:
            report += "❌ **回撤控制较差**: 最大回撤 > 30%\n"
        
        return report

def test_portfolio_backtest():
    """测试组合回测引擎"""
    print("=" * 60)
    print("🧪 多策略组合回测引擎测试")
    print("=" * 60)
    
    # 创建测试数据
    np.random.seed(42)
    dates = pd.date_range('2025-01-01', periods=100, freq='D')
    
    # 创建3个测试策略
    strategy_a = pd.Series(np.random.randn(100) * 0.01, index=dates)
    strategy_b = pd.Series(np.random.randn(100) * 0.015, index=dates)
    strategy_c = pd.Series(np.random.randn(100) * 0.02, index=dates)
    
    # 创建组合回测引擎
    portfolio = PortfolioBacktest()
    
    # 添加策略
    portfolio.add_strategy("MA交叉策略", strategy_a)
    portfolio.add_strategy("RSI超卖策略", strategy_b)
    portfolio.add_strategy("北向资金策略", strategy_c)
    
    # 计算相关性矩阵
    correlation_matrix = portfolio.calculate_correlation_matrix()
    print(f"✅ 相关性矩阵形状: {correlation_matrix.shape}")
    
    # 蒙特卡洛权重优化
    optimization_result = portfolio.monte_carlo_weight_optimization(num_simulations=1000)
    if optimization_result:
        print(f"✅ 最优夏普比率: {optimization_result['performance']['sharpe_ratio']:.3f}")
    
    # 运行组合回测（使用等权重）
    portfolio_result = portfolio.run_portfolio_backtest()
    if portfolio_result:
        perf = portfolio_result['performance']
        print(f"✅ 组合回测结果:")
        print(f"  总收益率: {perf['total_return']:.2%}")
        print(f"  夏普比率: {perf['sharpe_ratio']:.3f}")
        print(f"  最大回撤: {perf['max_drawdown']:.2%}")
    
    # 生成报告
    report = portfolio.generate_portfolio_report()
    print(f"\n📊 报告生成成功")
    
    print("\n" + "=" * 60)
    print("✅ 组合回测引擎测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_portfolio_backtest()