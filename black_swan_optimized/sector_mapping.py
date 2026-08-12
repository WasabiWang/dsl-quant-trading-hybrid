#!/usr/bin/env python3
"""
行业机会映射引擎 v1.0
=====================
从"事件→风险"扩展到"事件→风险+机会+板块轮动"

三层映射:
  L1: 宏观事件 → 大类资产方向
  L2: 大类资产 → 行业板块轮动
  L3: 行业板块 → 具体标的池
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from datetime import datetime
import json

# ============================================================================
# L1: 宏观事件 → 大类资产方向
# ============================================================================

MACRO_TO_ASSET = {
    # AI泡沫破裂场景
    "ai_bubble_burst": {
        "label": "AI泡沫破裂",
        "triggers": ["LPPL泡沫强度>0.8", "科技股领跌", "NVDA单日跌>7%"],
        "asset_impact": {
            "US_Tech": {"direction": "down", "severity": "high", "note": "直接冲击"},
            "A_Tech": {"direction": "down", "severity": "high", "note": "情绪传染+北向撤出"},
            "US_Treasury": {"direction": "up", "severity": "medium", "note": "避险买盘"},
            "Gold": {"direction": "up", "severity": "medium", "note": "避险资产"},
            "VIX": {"direction": "up", "severity": "high", "note": "恐慌飙升"},
            "USD": {"direction": "up", "severity": "medium", "note": "资金回流美元"},
            "EM_Stocks": {"direction": "down", "severity": "medium", "note": "风险偏好下降"},
        }
    },

    # 美联储降息场景
    "fed_rate_cut": {
        "label": "美联储降息周期",
        "triggers": ["FOMC降息", "通胀回落至目标", "就业市场疲软"],
        "asset_impact": {
            "US_Tech": {"direction": "up", "severity": "medium", "note": "利率敏感，估值修复"},
            "A_Tech": {"direction": "up", "severity": "medium", "note": "流动性外溢"},
            "US_Treasury": {"direction": "up", "severity": "medium", "note": "收益率下行"},
            "Gold": {"direction": "up", "severity": "high", "note": "美元走弱+实际利率下行"},
            "EM_Stocks": {"direction": "up", "severity": "high", "note": "资金回流新兴市场"},
            "USD": {"direction": "down", "severity": "high", "note": "利差收窄"},
        }
    },

    # 中东冲突升级
    "iran_war_escalation": {
        "label": "中东冲突升级",
        "triggers": ["霍尔木兹封锁", "美伊军事对抗", "油价>120"],
        "asset_impact": {
            "Oil": {"direction": "up", "severity": "high", "note": "供应中断"},
            "Gold": {"direction": "up", "severity": "high", "note": "避险+通胀"},
            "US_Tech": {"direction": "down", "severity": "medium", "note": "成本压力"},
            "Defense": {"direction": "up", "severity": "high", "note": "军费增加"},
            "Shipping": {"direction": "up", "severity": "high", "note": "运费飙升"},
            "Airlines": {"direction": "down", "severity": "high", "note": "燃油成本"},
        }
    },

    # 中美关税升级
    "tariff_escalation": {
        "label": "中美关税升级",
        "triggers": ["145%关税维持", "新制裁", "技术脱钩加速"],
        "asset_impact": {
            "A_Export": {"direction": "down", "severity": "high", "note": "直接冲击出口链"},
            "A_Semicon": {"direction": "up", "severity": "medium", "note": "国产替代加速"},
            "A_Defense": {"direction": "up", "severity": "medium", "note": "自主可控"},
            "CNY": {"direction": "down", "severity": "medium", "note": "贬值压力"},
            "US_Import": {"direction": "down", "severity": "medium", "note": "进口成本上升"},
        }
    },
}


# ============================================================================
# L2: 大类资产 → A股行业板块轮动
# ============================================================================

ASSET_TO_SECTOR = {
    "US_Tech_down": {
        "trigger": "美股科技暴跌",
        "A_impact": [
            {"sector": "半导体", "direction": "down", "lag": "即时", "reason": "全球科技风险偏好联动"},
            {"sector": "AI算力", "direction": "down", "lag": "即时", "reason": "NVDA链直接映射"},
            {"sector": "消费电子", "direction": "down", "lag": "1-2天", "reason": "需求预期下调"},
            {"sector": "国产替代", "direction": "up", "lag": "3-5天", "reason": "脱钩逻辑强化(滞后反应)"},
            {"sector": "黄金", "direction": "up", "lag": "即时", "reason": "避险资金流入"},
        ]
    },
    "Gold_up": {
        "trigger": "金价上涨",
        "A_impact": [
            {"sector": "黄金股", "direction": "up", "lag": "即时", "reason": "直接映射"},
            {"sector": "有色金属", "direction": "up", "lag": "1-2天", "reason": "商品联动"},
            {"sector": "银行", "direction": "down", "lag": "3-5天", "reason": "避险→风险偏好降"},
        ]
    },
    "Oil_up": {
        "trigger": "油价上涨",
        "A_impact": [
            {"sector": "石油石化", "direction": "up", "lag": "即时", "reason": "直接受益"},
            {"sector": "煤炭", "direction": "up", "lag": "1-2天", "reason": "能源替代"},
            {"sector": "航空", "direction": "down", "lag": "即时", "reason": "成本上升"},
            {"sector": "新能源汽车", "direction": "up", "lag": "1-2天", "reason": "油车使用成本上升→电车替代"},
        ]
    },
    "CNY_down": {
        "trigger": "人民币贬值",
        "A_impact": [
            {"sector": "出口纺织", "direction": "up", "lag": "1-2天", "reason": "贬值受益"},
            {"sector": "家电出口", "direction": "up", "lag": "1-2天", "reason": "价格竞争力"},
            {"sector": "航空", "direction": "down", "lag": "即时", "reason": "美元债务压力"},
            {"sector": "北向重仓", "direction": "down", "lag": "即时", "reason": "北向资金流出"},
        ]
    },
    "EM_Stocks_down": {
        "trigger": "新兴市场下跌",
        "A_impact": [
            {"sector": "北向重仓", "direction": "down", "lag": "即时", "reason": "全球资金撤离EM"},
            {"sector": "港股通", "direction": "down", "lag": "即时", "reason": "联动下跌"},
            {"sector": "高股息/红利", "direction": "up", "lag": "1-2天", "reason": "防御性轮动"},
        ]
    },
    "Bond_yields_down": {
        "trigger": "利率下行",
        "A_impact": [
            {"sector": "成长股/创业板", "direction": "up", "lag": "1-2天", "reason": "估值分母下降"},
            {"sector": "房地产", "direction": "up", "lag": "3-5天", "reason": "融资成本降"},
            {"sector": "高股息", "direction": "down", "lag": "3-5天", "reason": "相对吸引力下降"},
        ]
    },
}


# ============================================================================
# L3: 行业板块 → 具体标的池 (DSL股票池内)
# ============================================================================

SECTOR_TO_POOL = {
    "半导体": ["688981", "688012", "002371", "603501"],  # 中芯/中微/北方华创/韦尔
    "AI算力": ["300308", "300502", "002463"],            # 中际/新易盛/沪电
    "黄金股": ["601899", "600489", "600988"],             # 紫金/中金/赤峰
    "石油石化": ["601857", "600028", "600938"],           # 中石油/中石化/中海油
    "国产替代": ["688981", "688256", "688012"],           # 中芯/寒武纪/中微
    "高股息": ["601398", "601939", "600036", "601088"],   # 工行/建行/招行/神华
    "消费电子": ["002475", "300433", "601138"],           # 立讯/蓝思/工业富联
    "新能源汽车": ["300750", "002594", "601012"],         # 宁德/比亚迪/隆基
    "北向重仓": ["600519", "000858", "300750", "601318"], # 茅台/五粮液/宁德/平安
    "出口纺织": ["000301", "000726", "002042"],           # 东方盛虹/鲁泰/华孚
    "航空": ["601111", "600029", "600115"],               # 国航/南航/东航
}


# ============================================================================
# 机会映射引擎
# ============================================================================

@dataclass
class SectorOpportunity:
    """行业机会"""
    sector: str
    direction: str          # up / down
    confidence: float       # 0-1
    trigger: str            # 触发事件
    lag: str                # 滞后时间
    reason: str
    stocks: List[str] = field(default_factory=list)
    action: str = ""        # BUY / SELL / HOLD / HEDGE


def map_opportunities(active_events: List[str],
                      lppl_signals: Optional[Dict] = None) -> List[SectorOpportunity]:
    """
    根据当前活跃的宏观事件 + LPPL信号 → 输出行业机会列表

    Args:
        active_events: 活跃的宏观事件标签列表
        lppl_signals: LPPL检测结果

    Returns:
        行业机会列表
    """
    opportunities = []

    # L1: 事件 → 资产影响
    asset_impacts = {}
    for event in active_events:
        if event in MACRO_TO_ASSET:
            config = MACRO_TO_ASSET[event]
            for asset, impact in config['asset_impact'].items():
                key = f"{asset}_{impact['direction']}"
                if key not in asset_impacts:
                    asset_impacts[key] = {
                        'severity': impact['severity'],
                        'reasons': [impact.get('note', '')],
                        'events': [event]
                    }
                else:
                    asset_impacts[key]['severity'] = max(
                        asset_impacts[key]['severity'], impact['severity']
                    )
                    asset_impacts[key]['events'].append(event)
                    asset_impacts[key]['reasons'].append(impact.get('note', ''))

    # LPPL信号注入
    if lppl_signals:
        for name, diag in lppl_signals.items():
            strength = diag.get('bubble_strength', 0)
            crash_prob = diag.get('crash_probability', 0)
            if strength > 0.8 and crash_prob > 0.7:
                # 泡沫成熟 → 注入防御性轮动信号
                asset_impacts['A_Tech_down'] = {
                    'severity': 'high',
                    'reasons': [f'{name} LPPL泡沫强度{strength:.0%}', '系统性风险临界'],
                    'events': ['lppl_bubble_alert']
                }
                asset_impacts['Gold_up'] = {
                    'severity': 'medium',
                    'reasons': ['泡沫破裂→避险需求'],
                    'events': ['lppl_bubble_alert']
                }

    # L2: 资产影响 → 行业板块
    for asset_key, impact_info in asset_impacts.items():
        if asset_key in ASSET_TO_SECTOR:
            sector_config = ASSET_TO_SECTOR[asset_key]
            severity = impact_info['severity']

            for sector_impact in sector_config['A_impact']:
                sector = sector_impact['sector']
                direction = sector_impact['direction']
                lag = sector_impact['lag']
                reason = sector_impact['reason']

                # 置信度: high=0.8, medium=0.6, low=0.4
                base_confidence = {'high': 0.80, 'medium': 0.60, 'low': 0.40}.get(severity, 0.5)

                # 多事件共振 → 提高置信度
                if len(impact_info['events']) >= 2:
                    base_confidence += 0.1

                # 降级: 如果有冲突信号
                opp_direction = 'down' if direction == 'up' else 'up'
                opp_key = f"{asset_key.replace('_down','').replace('_up','')}_{opp_direction}"
                if opp_key in asset_impacts:
                    base_confidence *= 0.6  # 冲突降权

                confidence = min(1.0, base_confidence)

                # 获取标的池
                stocks = SECTOR_TO_POOL.get(sector, [])

                # 行动建议
                if direction == 'up' and confidence > 0.6:
                    action = "BUY"
                elif direction == 'down' and confidence > 0.6:
                    action = "SELL"
                elif direction == 'up':
                    action = "WATCH_BUY"
                elif direction == 'down':
                    action = "WATCH_SELL"
                else:
                    action = "HOLD"

                opportunities.append(SectorOpportunity(
                    sector=sector,
                    direction=direction,
                    confidence=confidence,
                    trigger=sector_config['trigger'],
                    lag=lag,
                    reason=reason,
                    stocks=stocks,
                    action=action
                ))

    # 排序: 置信度高的在前
    opportunities.sort(key=lambda x: x.confidence, reverse=True)

    return opportunities


def format_opportunity_report(opportunities: List[SectorOpportunity]) -> str:
    """格式化为可读报告"""
    lines = [
        "=" * 65,
        "  🎯 行业机会映射报告",
        "=" * 65,
        f"  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"  机会数量: {len(opportunities)}",
        "",
    ]

    # 分组
    buy_ops = [o for o in opportunities if 'BUY' in o.action]
    sell_ops = [o for o in opportunities if 'SELL' in o.action]
    watch_ops = [o for o in opportunities if 'WATCH' in o.action]

    if buy_ops:
        lines.append("  🟢 买入方向:")
        for o in buy_ops:
            emoji = '🟢' if o.confidence > 0.7 else '🟡'
            lines.append(f"    {emoji} {o.sector:8s} (置信度{o.confidence:.0%})")
            lines.append(f"       触发: {o.trigger} | 滞后: {o.lag}")
            lines.append(f"       逻辑: {o.reason}")
            if o.stocks:
                lines.append(f"       标的: {', '.join(o.stocks[:4])}")
            lines.append("")

    if sell_ops:
        lines.append("  🔴 卖出/回避方向:")
        for o in sell_ops:
            lines.append(f"    🔴 {o.sector:8s} (置信度{o.confidence:.0%})")
            lines.append(f"       触发: {o.trigger} | 滞后: {o.lag}")
            lines.append(f"       逻辑: {o.reason}")
            if o.stocks:
                lines.append(f"       标的: {', '.join(o.stocks[:4])}")
            lines.append("")

    if watch_ops:
        lines.append("  👀 观察方向:")
        for o in watch_ops[:5]:
            dir_emoji = '📈' if o.direction == 'up' else '📉'
            lines.append(f"    {dir_emoji} {o.sector:8s} ({o.lag}) → {o.action}")
        lines.append("")

    return '\n'.join(lines)


if __name__ == "__main__":
    # 测试: 当前最可能的宏观事件组合
    # 基于LPPL检测结果: A股+美股同时检测到泡沫
    current_events = ['ai_bubble_burst', 'tariff_escalation']

    mock_lppl = {
        '上证指数': {'bubble_strength': 1.0, 'crash_probability': 0.95},
        '创业板指': {'bubble_strength': 0.87, 'crash_probability': 0.86},
        'S&P500': {'bubble_strength': 1.0, 'crash_probability': 0.75},
    }

    opps = map_opportunities(current_events, mock_lppl)
    print(format_opportunity_report(opps))