#!/usr/bin/env python3
"""
模拟交易主脚本

演示如何使用Paper Trading Engine进行策略验证
"""

import sys
import os
import logging
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from simulation.paper_trading import PaperTradingEngine
from agents.analysts.technical_analyst import TechnicalAnalyst
from agents.analysts.sentiment_analyst import SentimentAnalyst
from agents.analysts.macro_analyst import MacroAnalyst
from agents.researchers.bullish_researcher import BullishResearcher
from agents.researchers.bearish_researcher import BearishResearcher
from agents.orchestrator.decision_fusion import DecisionFusion
from agents.risk_manager.risk_agent import RiskManager
from agents.trader.trader_agent import TraderAgent

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def run_paper_trading_demo():
    """运行模拟交易演示"""
    
    print("=" * 60)
    print("🚀 DSL Quant Trading Hybrid - 模拟交易系统")
    print("=" * 60)
    
    # 1. 初始化模拟交易引擎
    engine = PaperTradingEngine(initial_capital=100000.0)
    
    # 2. 初始化所有Agent
    print("\n📊 初始化Agent团队...")
    tech = TechnicalAnalyst()
    sent = SentimentAnalyst()
    macro = MacroAnalyst()
    bull = BullishResearcher()
    bear = BearishResearcher()
    fusion = DecisionFusion()
    risk = RiskManager()
    trader = TraderAgent()
    
    # 3. 测试股票
    test_stocks = [
        {"symbol": "600519.SH", "name": "贵州茅台", "price": 1800.0},
        {"symbol": "0700.HK", "name": "腾讯控股", "price": 350.0},
        {"symbol": "AAPL", "name": "Apple", "price": 175.0}
    ]
    
    # 4. 执行模拟交易
    print("\n" + "=" * 60)
    print("🔄 开始模拟交易分析...")
    print("=" * 60)
    
    for stock in test_stocks:
        symbol = stock["symbol"]
        name = stock["name"]
        price = stock["price"]
        
        print(f"\n📈 分析 {name} ({symbol}) @ ¥{price}")
        print("-" * 40)
        
        try:
            # 执行分析
            tech_result = tech.analyze(symbol, {})
            sent_result = sent.analyze(symbol, {})
            macro_result = macro.analyze(symbol, {})
            
            # 研究者辩论
            bull_result = bull.analyze(symbol, {
                "technical": tech_result,
                "sentiment": sent_result,
                "macro": macro_result
            })
            
            bear_result = bear.analyze(symbol, {
                "technical": tech_result,
                "sentiment": sent_result,
                "macro": macro_result
            })
            
            # 决策融合
            fusion_result = fusion.fuse({
                "technical": tech_result,
                "sentiment": sent_result,
                "macro": macro_result,
                "bullish": bull_result,
                "bearish": bear_result
            })
            
            # 风险评估
            risk_result = risk.analyze(symbol, {
                "price": price,
                "final_score": fusion_result["final_score"],
                "confidence": fusion_result["confidence"],
                "component_scores": fusion_result["component_scores"]
            })
            
            # 生成交易指令
            order = trader.analyze(symbol, {
                "fusion": fusion_result,
                "risk": risk_result
            })
            
            # 打印分析结果
            print(f"  技术面: {tech_result.get('recommendation', 'N/A')}")
            print(f"  情绪面: {sent_result.get('sentiment_score', 0):.2f}")
            print(f"  宏观面: {macro_result.get('macro_score', 0):.2f}")
            print(f"  综合评分: {fusion_result['final_score']:.2f}")
            print(f"  最终建议: {order['action']}")
            print(f"  置信度: {order.get('position_size', 0)*100:.1f}%")
            
            # 执行模拟交易
            if order["action"] != "HOLD" and order.get("position_size", 0) > 0:
                quantity = max(100, int(100000 * order["position_size"] / price / 100) * 100)
                result = engine.execute_order(
                    symbol=symbol,
                    action=order["action"],
                    quantity=quantity,
                    price=price
                )
                
                if result["success"]:
                    print(f"  ✅ 交易执行: {order['action']} {quantity}股 @ ¥{price}")
                else:
                    print(f"  ❌ 交易失败: {result.get('reason', '未知错误')}")
            else:
                print(f"  ⏸️   HOLD - 不执行交易")
        
        except Exception as e:
            logger.error(f"分析 {symbol} 失败: {e}")
            print(f"  ⚠️  分析失败: {e}")
    
    # 5. 打印总结
    print("\n" + "=" * 60)
    print("📊 模拟交易总结")
    print("=" * 60)
    
    summary = engine.get_positions_summary()
    print(f"\n💰 初始资金: ¥{engine.initial_capital:,.2f}")
    print(f"💵 剩余现金: ¥{summary['cash']:,.2f}")
    print(f"📈 持仓市值: ¥{summary['total_market_value']:,.2f}")
    print(f"💼 总资产: ¥{summary['total_assets']:,.2f}")
    print(f"📊 盈亏: ¥{summary['profit_loss']:,.2f} ({summary['return_rate']:.2f}%)")
    print(f"📝 交易次数: {engine.stats['total_trades']}")
    print(f"💸 总佣金: ¥{engine.stats['total_commission']:.2f}")
    
    # 持仓详情
    if engine.positions:
        print(f"\n📦 持仓详情:")
        for symbol, pos in engine.positions.items():
            print(f"  {symbol}: {pos['quantity']}股 @ ¥{pos['avg_price']:.2f}")
    
    # 交易历史
    history = engine.get_trade_history()
    if history:
        print(f"\n📜 最近交易:")
        for trade in history[-5:]:
            print(f"  {trade['timestamp']}: {trade['action']} {trade['quantity']} {trade['symbol']} @ ¥{trade['price']}")
    
    print("\n" + "=" * 60)
    print("✅ 模拟交易演示完成！")
    print("=" * 60)


if __name__ == "__main__":
    run_paper_trading_demo()
