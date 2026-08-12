import json
import sys
import os
from datetime import datetime

# 接入 stock-core 模块
sys.path.append(os.path.join(os.path.dirname(__file__), '../../alpha-quant-pro'))
try:
    from helpers.fetch_theme_data import fetch_all as fetch_a_flow
    from helpers.fetch_theme_data_hk import fetch_all as fetch_hk_flow
except:
    fetch_a_flow = None
    fetch_hk_flow = None

class AgentResult:
    def __init__(self, name, score, signal, reason):
        self.name = name
        self.score = score  # -1 to 1
        self.signal = signal # "BUY", "SELL", "HOLD"
        self.reason = reason

class AgentSwarm:
    def __init__(self):
        self.agents = []
        self.results = []

    def register_agent(self, agent_func):
        self.agents.append(agent_func)

    def execute_all(self, market_data, news_data):
        print(f"🤖 Agent Swarm 启动 | {datetime.now().strftime('%H:%M:%S')}")
        self.results = []
        for agent in self.agents:
            try:
                res = agent(market_data, news_data)
                self.results.append(res)
                status = "✅" if res.score > 0.5 else "⚠️" if res.score > 0 else "❌"
                print(f"  {status} [{res.name}]: {res.signal} (Score: {res.score:.2f}) | {res.reason}")
            except Exception as e:
                print(f"  ❌ [{agent.__name__}] Error: {e}")

        return self.make_final_decision()

    def make_final_decision(self):
        if not self.results:
            return {"signal": "ERROR", "position": 0, "reason": "No agents executed"}

        # 1. 核心安全校验 (一票否决制)
        flow_agent = next((r for r in self.results if r.name == "FlowAgent"), None)
        macro_agent = next((r for r in self.results if r.name == "MacroAgent"), None)

        # 如果资金大幅流出或宏观极差,直接熔断
        if (flow_agent and flow_agent.score < -0.1) or (macro_agent and macro_agent.score < -0.5):
            return {"signal": "HOLD", "confidence": 0, "position_pct": 0, "reason": "触及风控熔断红线 (资金/宏观警示)"}

        # 2. 综合评分 (加权平均)
        total_score = sum(r.score for r in self.results) / len(self.results)

        # 3. 决策逻辑
        final_signal = "HOLD"
        if total_score > 0.7:
            final_signal = "STRONG_BUY"
        elif total_score > 0.4:
            final_signal = "BUY"
        elif total_score < -0.4:
            final_signal = "SELL"

        # 4. 动态仓位计算
        risk_agent = next((r for r in self.results if r.name == "RiskAgent"), None)
        position_pct = risk_agent.score if risk_agent else 0.1

        return {
            "signal": final_signal,
            "confidence": total_score,
            "position_pct": position_pct,
            "details": [r.__dict__ for r in self.results]
        }

# --- 示例 Agent 实现 ---

def macro_agent(data, news):
    # v3.1.2 升级:港股离岸流动性监控
    score = 0.0
    market = data.get('market', 'A')
    reasons = []

    # 1. 基础宏观面
    if "油价" in news or "Oil" in news:
        score += 0.3
        reasons.append("油价波动影响")

    # 2. A 股政策加权
    if market == 'A':
        policy_keywords = ["国务院", "工信部", "发改委", "行动计划"]
        policy_strength = sum(1 for k in policy_keywords if k in news)
        if policy_strength > 0:
            score += 0.2 * policy_strength
            reasons.append(f"政策面利好 (+{0.2*policy_strength})")
        else:
            reasons.append("暂无重大政策驱动")

    # 3. 港股流动性监控 (USD/HKD Liquidity Check)
    if market == 'HK':
        dxy_trend = data.get('dxy_trend', 'NEUTRAL') # 美元指数趋势
        if dxy_trend == 'UP':
            score -= 0.4 # 美元走强,港股流动性承压
            reasons.append("美元走强压制流动性")
        else:
            reasons.append("离岸流动性中性")

    reason_str = "; ".join(reasons) if reasons else "宏观环境平稳"
    return AgentResult("MacroAgent", score, "HOLD", f"{reason_str} | 最终评分: {score:.2f}")

def flow_agent(data, news):
    # v3.0.8 升级:引入 a-share-sector-picking 的"板块生命周期判定"
    flow = data.get('flow', 0)
    sector_stage = data.get('sector_stage', 'fermentation') # 启动, 发酵, 高潮, 退潮
    crowding_index = data.get('crowding_index', 0.5) # 0-1, 越高越拥挤

    # 冷却系数计算 (Sector Cooling Coefficient)
    cooling_coef = 1.0
    if sector_stage == 'high_climax':
        cooling_coef = 0.5 # 高潮期减半
        stage_reason = "板块高潮期,风险加剧"
    elif sector_stage == 'recession':
        cooling_coef = 0.0  # 退潮期熔断
        stage_reason = "板块退潮,资金撤离"
    elif sector_stage == 'start':
        stage_reason = "板块启动初期,机会显现"
    else:
        stage_reason = f"板块处于{sector_stage}阶段"

    # 拥挤度惩罚
    crowding_reason = ""
    if crowding_index > 0.8:
        cooling_coef *= 0.5
        crowding_reason = "交易拥挤度过高,降权处理"

    # 基础评分
    base_score = min(flow / 100, 1.0) * cooling_coef

    signal = "BUY" if base_score > 0.5 else "HOLD"
    reason = f"{stage_reason}; 资金净流入{flow}亿"
    if crowding_reason: reason += f"; {crowding_reason}"
    reason += f" | 冷却系数{cooling_coef} | 评分:{base_score:.2f}"

    return AgentResult("FlowAgent", base_score, signal, reason)

def profiling_agent(data, news):
    # v3.1.1 升级:港股通/港股主力画像适配
    market = data.get('market', 'A')
    score = 0.0
    profile_type = "Unknown"

    if market == 'HK':
        # 港股逻辑:看南向资金 + 外资投行变动
        southbound_net = data.get('southbound_net', 0) # 南向资金净流入 (百万)
        broker_concentration = data.get('broker_concentration', 0) # 头部券商持仓集中度

        if southbound_net > 50:
            score += 0.3
            profile_type = "Southbound Driven"
        if broker_concentration > 0.6: # 筹码集中在大行手中
            score += 0.2
            profile_type = "Institutional Lock-up"

        # 港股流动性红线 (防老千股)
        if data.get('mkt_cap', 100) < 50 and data.get('avg_vol', 10) < 1:
            score = -1.0
            profile_type = "ILLEGAL_ZOMBIE" # 警惕老千股/仙股

    else:
        # A 股逻辑:龙虎榜
        inst_buy = data.get('lh_institutional_buy', 0)
        lhasa_ratio = data.get('lhasa_seat_ratio', 0.0)

        if inst_buy > 50:
            score += 0.4
            profile_type = "Institutional Driven"
        elif inst_buy < -20:
            score -= 0.3
            profile_type = "Institutional Exit"

        if lhasa_ratio > 0.3:
            score -= 0.2
            profile_type = "Retail Speculation"
        elif 0.1 < lhasa_ratio < 0.25 and inst_buy > 20:
            score += 0.2
            profile_type = "Strong Resonance"

    return AgentResult("ProfilingAgent", score, "HOLD", f"主力画像: {profile_type} (Market: {market})")

def tech_agent(data, news):
    # v3.1.3 升级：增加趋势健康度与30分钟确认逻辑
    rs_val = data.get('rs', 0)
    price_vs_ma60 = data.get('price_vs_ma60', 1.0) # 当前价 / 60日均线
    confirm_30min = data.get('confirm_30min', False) # 30分钟突破确认
    
    score = rs_val * 1.5 
    reasons = []
    
    # 趋势健康度过滤 (Trend Health Filter)
    if price_vs_ma60 < 1.0:
        score -= 0.5 # 价格在60日线下方，大幅降权
        reasons.append("价格在60日线下方(-0.5)")
    else:
        reasons.append("价格站稳60日线上方")

    # 30分钟确认机制 (Timing Optimization)
    if not confirm_30min:
        score -= 0.3 # 未经过30分钟确认，降低冲动交易
        reasons.append("30分钟级别未确认(-0.3)")
    else:
        reasons.append("30分钟级别已确认突破")

    # 机构/游资画像加权
    lh_institutional_buy = data.get('lh_institutional_buy', 0)
    if lh_institutional_buy > 50: 
        score += 0.3
        reasons.append("机构大额买入(+0.3)")
    
    final_score = max(0, min(1.0, score))
    reason_str = "; ".join(reasons)
    return AgentResult("TechAgent", final_score, "BUY" if final_score > 0.6 else "HOLD", f"{reason_str} | 最终评分:{final_score:.2f}")

def risk_agent(data, news):
    # v3.1.3 升级:优化 ATR 止损倍数与确认逻辑
    volatility = data.get('vol', 0.5)
    market = data.get('market', 'A')
    base_score = max(0.1, 1.0 - volatility)

    # ATR 动态止损优化:倍数从 2.0 提升至 2.5
    atr_mult = 2.5
    stop_loss_dist = atr_mult * data.get('vol', 0.02)
    
    reasons = []
    if volatility > 0.6:
        reasons.append(f"波动率过高({volatility}), 建议轻仓")
    else:
        reasons.append(f"波动率稳定({volatility}), 建议适度仓位")
    
    reasons.append(f"建议仓位: {base_score*100:.1f}%")
    reasons.append(f"ATR止损位: {stop_loss_dist*100:.1f}%")
    
    return AgentResult("RiskAgent", base_score, "HOLD", "; ".join(reasons))

def sentiment_agent(data, news):
    # 1. 基础关键词情感分析
    positive_words = ["涨", "高开", "鼓励", "加强", "突破", "复苏", "利好"]
    negative_words = ["跌", "紧张", "危机", "制裁", "衰退", "利空"]

    # 2. 政策语义增强 (宏观白名单)
    policy_booster_words = ["国家队", "降准", "降息", "托底", "稳增长", "中央经济工作会议"]

    score = 0.0
    pos_found = [w for w in positive_words if w in news]
    neg_found = [w for w in negative_words if w in news]
    pol_found = [w for w in policy_booster_words if w in news]
    
    for word in pos_found: score += 0.1
    for word in neg_found: score -= 0.1
    for word in pol_found: score += 0.3

    score = max(-1.0, min(1.0, score))
    
    reasons = []
    if pos_found: reasons.append(f"正向: {','.join(pos_found)}")
    if neg_found: reasons.append(f"负向: {','.join(neg_found)}")
    if pol_found: reasons.append(f"政策: {','.join(pol_found)}")
    if not reasons: reasons.append("情绪面中性")
    
    signal = "BUY" if score > 0.2 else "SELL" if score < -0.2 else "HOLD"
    return AgentResult("SentimentAgent", score, signal, f"{'; '.join(reasons)} | 评分: {score:.2f}")

if __name__ == "__main__":
    swarm = AgentSwarm()
    swarm.register_agent(macro_agent)
    swarm.register_agent(flow_agent)
    swarm.register_agent(tech_agent)
    swarm.register_agent(risk_agent)

    # 模拟数据输入
    mock_data = {'flow': 15.2, 'rs': 0.45, 'vol': 0.3}
    mock_news = "油价暴涨,中東局势紧张"

    decision = swarm.execute_all(mock_data, mock_news)
    print(f"\n🎯 最终决策: {decision['signal']} | 置信度: {decision['confidence']:.2f} | 建议仓位: {decision['position_pct']*100:.1f}%")
