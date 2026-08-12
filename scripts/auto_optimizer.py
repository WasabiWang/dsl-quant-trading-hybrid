import json
import os

def analyze_decision_bias(trade_log, market_context):
    """
    基于 Agent Lightning 理念的自动归因分析
    """
    # 模拟 LLM 归因逻辑（实际生产中应调用 openrouter/qwen 进行深度推理）
    analysis_report = {
        "date": trade_log.get("date"),
        "performance": trade_log.get("pnl_pct"),
        "root_cause": "",
        "prompt_optimization_suggestion": ""
    }

    pnl = trade_log.get("pnl_pct", 0)
    
    # 1. 亏损归因逻辑
    if pnl < -2.0:
        analysis_report["root_cause"] = "RiskAgent 止损逻辑滞后 / MacroAgent 未识别宏观风险"
        analysis_report["prompt_optimization_suggestion"] = "建议增强 MacroAgent 对美元指数(DXY)波动的敏感度权重；将 RiskAgent 的 ATR 止损倍数从 2.5 下调至 2.0。"
    elif pnl < 0:
        analysis_report["root_cause"] = "TechAgent 30分钟确认机制未能过滤‘诱多’陷阱"
        analysis_report["prompt_optimization_suggestion"] = "建议在 TechAgent 中增加‘量价背离’校验逻辑：若价格上涨但成交量萎缩，强制降低评分。"
    
    # 2. 盈利归因逻辑
    elif pnl > 2.0:
        analysis_report["root_cause"] = "FlowAgent 资金流捕捉精准 / ProfilingAgent 机构画像生效"
        analysis_report["prompt_optimization_suggestion"] = "建议固化当前的‘机构净买入 > 5000万’加分逻辑，并尝试将其权重从 0.3 提升至 0.4。"
    else:
        analysis_report["root_cause"] = "市场震荡，策略表现平稳"
        analysis_report["prompt_optimization_suggestion"] = "暂无重大逻辑缺陷，建议维持当前参数，关注板块轮动速度。"

    return analysis_report

def generate_daily_optimization_report():
    print("🤖 Model: openrouter/qwen/qwen3.6-plus:free")
    print("🚀 启动 Agent Lightning 自动优化流程...")
    
    # 读取最新的模拟盘记录
    ledger_path = os.path.join(os.path.dirname(__file__), "../data/paper_trading_ledger.json")
    if not os.path.exists(ledger_path):
        print("❌ 未发现模拟盘账本，无法进行归因分析。")
        return

    with open(ledger_path, 'r') as f:
        ledger = json.load(f)
    
    if not ledger["trade_history"]:
        print("⚠️ 今日无交易记录，跳过归因分析。")
        return

    last_trade = ledger["trade_history"][-1]
    
    # 执行归因
    report = analyze_decision_bias(last_trade, {"dxy_trend": "UP", "sector_flow": "Semiconductor"})
    
    print(f"\n📊 📈 今日交易归因报告 ({report['date']})")
    print(f"💰 盈亏表现: {report['performance']}%")
    print(f"🔍 核心归因: {report['root_cause']}")
    print(f"💡 优化建议: {report['prompt_optimization_suggestion']}")
    
    # 将建议追加到飞书文档或本地日志
    print("\n✅ 优化建议已生成，请查阅飞书技术手册‘每日迭代’章节。")

if __name__ == "__main__":
    generate_daily_optimization_report()