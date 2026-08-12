#!/usr/bin/env python3
"""
多因子风险评分引擎 v1.1
========================
从单一keyword scoring升级为7维度量化评分

维度:
  1. LPPL 泡沫强度       (0-1) 来自 lppl_model
  2. 波动率期限结构       (0-1) VIX / VIX3M / VIX term structure
  3. 信用利差压力         (0-1) Shibor 3M 同比变化率
  4. 流动性环境           (0-1) Shibor ON + 回购利率 + M2增速
  5. 跨资产相关性         (0-1) 中美10Y国债利差 vs 沪深300
  6. 资金流向             (0-1) 北向资金净流入方向
  7. 宏观情绪             (0-1) 综合新闻情绪+黑天鹅事件
"""
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import json
import requests
from pathlib import Path
import time

# Cache for data fetches (short TTL so each run gets fresh data)
_DATA_CACHE = {}
_DATA_CACHE_TIME = 0
_DATA_CACHE_TTL = 60  # 60秒缓存


def _fetch_with_cache(key: str, fetch_fn, ttl: int = 60):
    """带内存缓存的通用数据获取"""
    global _DATA_CACHE, _DATA_CACHE_TIME
    now = time.time()
    if key in _DATA_CACHE and (now - _DATA_CACHE_TIME) < ttl:
        return _DATA_CACHE[key]
    try:
        val = fetch_fn()
        _DATA_CACHE[key] = val
        _DATA_CACHE_TIME = now
        return val
    except Exception:
        return _DATA_CACHE.get(key, None)  # fallback to stale


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class RiskScore:
    """多因子风险评分"""
    timestamp: str
    overall_score: float          # 0-100 综合风险分数
    risk_level: str               # LOW / ELEVATED / HIGH / CRITICAL
    factors: Dict[str, float]     # 各因子分数
    weights: Dict[str, float]     # 各因子权重
    details: Dict[str, str]       # 各因子详情
    lppl_signals: List[Dict] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


# ============================================================================
# 数据获取层
# ============================================================================

def _get_shibor() -> Optional[dict]:
    """从 akshare 获取 Shibor 数据"""
    try:
        import akshare as ak
        df = ak.macro_china_shibor_all()
        if df is None or len(df) < 2:
            return None
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        return {
            'on': float(latest.get('O/N-定价', 1.5) or 1.5),
            'on_change': float(latest.get('O/N-涨跌幅', 0) or 0),
            'w1': float(latest.get('1W-定价', 1.6) or 1.6),
            'm3': float(latest.get('3M-定价', 1.8) or 1.8),
            'm3_prev': float(prev.get('3M-定价', 1.8) or 1.8),
            'm1': float(latest.get('1M-定价', 1.7) or 1.7),
        }
    except Exception:
        return None


def _get_bond_yield() -> Optional[dict]:
    """从中美国债收益率获取信用数据"""
    try:
        import akshare as ak
        df = ak.bond_zh_us_rate()
        if df is None or len(df) < 2:
            return None
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        cn10y = float(latest.get('中国国债收益率10年', 1.7) or 1.7)
        cn2y = float(latest.get('中国国债收益率2年', 1.2) or 1.2)
        us10y = float(latest.get('美国国债收益率10年', 4.5) or 4.5)
        us2y = float(latest.get('美国国债收益率2年', 4.0) or 4.0)
        cn10y_prev = float(prev.get('中国国债收益率10年', 1.7) or 1.7)
        us10y_prev = float(prev.get('美国国债收益率10年', 4.5) or 4.5)
        return {
            'cn_10y': cn10y,
            'cn_2y': cn2y,
            'cn_spread': cn10y - cn2y,      # 中债期限利差
            'us_10y': us10y,
            'us_2y': us2y,
            'us_spread': us10y - us2y,       # 美债期限利差
            'cn_10y_change': cn10y - cn10y_prev,
            'us_10y_change': us10y - us10y_prev,
            'cn_us_spread': cn10y - us10y,   # 中美利差
        }
    except Exception:
        return None


def _get_money_supply() -> Optional[float]:
    """获取 M2 同比增速"""
    try:
        import akshare as ak
        df = ak.macro_china_supply_of_money()
        if df is None or len(df) < 2:
            return None
        # 获取最新有数据的M2同比
        for i in range(min(5, len(df))):
            row = df.iloc[-1 - i]
            val = row.get('货币和准货币（广义货币M2）同比增长')
            if val and not np.isnan(float(val)):
                return float(val)
        return None
    except Exception:
        return None


def _get_north_flow() -> Optional[float]:
    """获取北向资金净买入方向 (30日均值)"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_hist_em()
        if df is None or len(df) < 30:
            return None
        # 取最近30条净买入金额 (注意: 最新日期的值可能是NaN, 跳过)
        flows = df['当日成交净买额'].dropna().tail(30).astype(float)
        if len(flows) < 5:
            return None
        return flows.mean()
    except Exception:
        return None


def _get_vix_from_sina() -> float:
    """从新浪获取VIX"""
    try:
        headers = {'Referer': 'https://finance.sina.com.cn'}
        r = requests.get('https://hq.sinajs.cn/list=gb_vix', headers=headers, timeout=10)
        parts = r.text.split('"')[1].split(',')
        vix_val = float(parts[1]) if len(parts) > 1 and parts[1] else 16.0
        if vix_val < 1:
            return 16.0
        return vix_val
    except Exception:
        return 16.0


# ============================================================================
# 因子1: LPPL 泡沫强度 (来自 lppl_model 输出)
# ============================================================================

def compute_lppl_factor(lppl_results: Dict[str, any]) -> Tuple[float, str]:
    weights = {
        '上证指数': 0.25,
        '创业板指': 0.20,
        '科创50': 0.20,
        'S&P500': 0.15,
        'NVDA': 0.10,
        '半导体': 0.10,
    }

    weighted_score = 0.0
    total_weight = 0.0
    details_parts = []

    for name, diag in lppl_results.items():
        w = weights.get(name, 0.05)
        strength = diag.get('bubble_strength', 0)
        crash_prob = diag.get('crash_probability', 0)
        factor_risk = strength * max(crash_prob, 0.3)
        weighted_score += factor_risk * w
        total_weight += w
        tc = diag.get('critical_date', 'N/A')
        details_parts.append(f"{name}: strength={strength:.0%} crash={crash_prob:.0%} tc={tc}")

    if total_weight > 0:
        weighted_score /= total_weight

    detail = " | ".join(details_parts)
    return min(1.0, weighted_score), detail


# ============================================================================
# 因子2: 波动率期限结构
# ============================================================================

def compute_vol_factor(viX: Optional[float] = None) -> Tuple[float, str]:
    if viX is None:
        vix_val = _get_vix_from_sina()
    else:
        vix_val = viX

    if vix_val > 35:
        score, detail = 1.0, f"VIX={vix_val:.1f} 极度恐慌"
    elif vix_val > 25:
        score, detail = 0.8, f"VIX={vix_val:.1f} 高度紧张"
    elif vix_val > 20:
        score, detail = 0.5, f"VIX={vix_val:.1f} 中度波动"
    elif vix_val < 12:
        score, detail = 0.4, f"VIX={vix_val:.1f} 极度自满(尾部风险积蓄)"
    elif vix_val < 15:
        score, detail = 0.2, f"VIX={vix_val:.1f} 自满区"
    else:
        score, detail = 0.3, f"VIX={vix_val:.1f} 正常"

    return score, detail


# ============================================================================
# 因子3: 信用利差压力 (接入实时Shibor 3M + 信用利差)
# ============================================================================

def compute_credit_factor() -> Tuple[float, str]:
    """
    信用利差风险评估
    使用 Shibor 3M 同比变化 + 中美国债利差变化
    """
    shibor = _get_shibor()
    if shibor and shibor['m3'] > 0:
        m3 = shibor['m3']
        m3_prev = shibor['m3_prev']
        # 同比变化率 (若prev=0则跳过)
        if m3_prev > 0:
            change_pct = (m3 - m3_prev) / m3_prev * 100
        else:
            change_pct = 0.0

        # 历史分位映射: 近5年Shibor 3M在1.4-3.5之间波动
        # 当前值1.4 → score≈0.1, 当前值2.5 → score≈0.5, 当前值3.5+ → score≈1.0
        level_score = min(1.0, max(0.05, (m3 - 1.0) / 3.0))
        # 变化率贡献: 月环比>20%视为快速收紧
        change_score = min(1.0, max(0.0, change_pct / 20.0))
        score = 0.7 * level_score + 0.3 * change_score
        detail = f"Shibor 3M={m3:.2f}% 环比{change_pct:+.1f}%"
    else:
        # Fallback: 用中美利差
        bond = _get_bond_yield()
        if bond:
            # 中美利差为负且扩大=资本外流压力
            cn_us = bond['cn_us_spread']
            if cn_us < -1.5:
                score = 0.7
                detail = f"中美利差{cn_us:+.2f}%, 资本外流压力大"
            elif cn_us < -0.5:
                score = 0.5
                detail = f"中美利差{cn_us:+.2f}%, 利差倒挂"
            else:
                score = 0.3
                detail = f"中美利差{cn_us:+.2f}%, 正常"
        else:
            score = 0.30
            detail = "信用数据暂不可用(默认中性)"

    return score, detail


# ============================================================================
# 因子4: 流动性环境 (接入Shibor ON + 回购利率 + M2)
# ============================================================================

def compute_liquidity_factor() -> Tuple[float, str]:
    """
    流动性风险评估
    指标: Shibor隔夜 + FR007回购 + M2增速
    """
    parts = []

    # Shibor ON
    shibor = _get_shibor()
    if shibor:
        on_rate = shibor['on']
        # ON rate < 1.2 → 宽松, > 1.8 → 收紧
        on_score = min(1.0, max(0.0, (on_rate - 1.0) / 1.5))
        parts.append(f"ON={on_rate:.3f}%")
    else:
        on_score = 0.35

    # M2 同比增速
    m2 = _get_money_supply()
    if m2 and m2 > 0:
        # M2 < 7% → 收紧, M2 > 10% → 宽松
        # 风险: M2过低(流动性紧缺)或M2过高(通胀风险)
        if m2 < 7:
            m2_score = 0.7  # 流动性偏紧
        elif m2 > 12:
            m2_score = 0.5  # 流动性过度
        elif m2 > 10:
            m2_score = 0.25
        elif m2 > 8:
            m2_score = 0.35
        else:
            m2_score = 0.50  # 7-8% 中性偏紧
        parts.append(f"M2={m2:.1f}%")
    else:
        m2_score = 0.30

    score = 0.6 * on_score + 0.4 * m2_score
    detail = "流动性: " + ", ".join(parts) if parts else "流动性数据待接入(默认中性)"
    return score, detail


# ============================================================================
# 因子5: 跨资产相关性突变 (中债期限利差反转 = 风险信号)
# ============================================================================

def compute_correlation_factor() -> Tuple[float, str]:
    """
    跨资产相关性风险评估
    使用中债期限利差(10Y-2Y)的绝对水平和变化趋势
    利差倒挂(负)或快速收窄 = 系统性风险信号
    """
    bond = _get_bond_yield()
    if bond:
        cn_spread = bond['cn_spread']
        us_spread = bond['us_spread']
        # 中债期限利差<0 → 倒挂(危机信号)
        if cn_spread < 0:
            score = 0.8
            detail = f"中债10Y-2Y利差={cn_spread:.2f}%, 倒挂(危机信号)"
        elif cn_spread < 0.3:
            score = 0.6
            detail = f"中债10Y-2Y利差={cn_spread:.2f}%, 平坦(衰退担忧)"
        elif cn_spread > 1.5:
            score = 0.5
            detail = f"中债10Y-2Y利差={cn_spread:.2f}%, 陡峭(通胀/收紧预期)"
        else:
            score = 0.25 + abs(cn_spread - 0.7) * 0.3  # 0.7附近最健康
            detail = f"中债10Y-2Y利差={cn_spread:.2f}%, 正常"
        # 美债倒挂叠加 → 更高风险
        if us_spread < 0:
            score = min(1.0, score + 0.2)
            detail += f" 美债利差{us_spread:.2f}%亦倒挂"
    else:
        score = 0.25
        detail = "收益率曲线数据暂不可用(默认偏低)"

    return score, detail


# ============================================================================
# 因子6: 资金流向 (北向资金净流入)
# ============================================================================

def compute_flow_factor() -> Tuple[float, str]:
    """
    资金流向风险评估
    使用北向资金30日均值
    """
    flow = _get_north_flow()
    if flow is not None:
        # flow 单位: 亿元, 正值=净流入
        # 巨幅流入(>100亿) → 过热风险; 巨幅流出(<-50亿) → 撤退风险
        if flow > 200:
            score = 0.7  # 过热
            detail = f"北向30日均净流入{flow:.0f}亿, 过热"
        elif flow > 100:
            score = 0.5
            detail = f"北向30日均净流入{flow:.0f}亿, 偏高"
        elif flow > 0:
            score = 0.25
            detail = f"北向30日均净流入{flow:.0f}亿, 正常"
        elif flow > -30:
            score = 0.50
            detail = f"北向30日均净流出{abs(flow):.0f}亿, 轻度流出"
        else:
            score = 0.80
            detail = f"北向30日均净流出{abs(flow):.0f}亿, 大幅流出"
    else:
        score = 0.30
        detail = "北向资金数据暂不可用(默认中性)"

    return score, detail


# ============================================================================
# 因子7: 宏观情绪
# ============================================================================

def compute_sentiment_factor() -> Tuple[float, str]:
    try:
        bs_path = Path.home() / '.openclaw/workspace/dsl-quant-trading-hybrid/data/black_swan_status.json'
        with open(bs_path) as f:
            bs_data = json.load(f)

        # 读取lppl sub-dict (lppl_to_dsl写入的结构)
        lppl_info = bs_data.get('lppl', {})
        urgency = lppl_info.get('urgency', 'LOW')
        risk_score = lppl_info.get('risk_score', 0)

        # 读取active_events
        active_events = lppl_info.get('active_events', [])
        event_count = len(active_events)

        # 综合评分: urgency + risk_score + event_count
        base = 0.25
        urgency_map = {'CRITICAL': 0.4, 'HIGH': 0.25, 'ELEVATED': 0.15, 'LOW': 0}
        base += urgency_map.get(urgency, 0)
        base += min(0.3, risk_score / 300)  # risk_score 最高~100 → +0.33
        base += min(0.1, event_count * 0.05)

        score = min(1.0, base)
        detail = f"urgency={urgency}, risk={risk_score:.0f}, events={event_count}"
    except Exception:
        score = 0.25
        detail = "事件监控待接入"

    return score, detail


# ============================================================================
# 主评分引擎
# ============================================================================

def compute_risk_score(lppl_results: Optional[Dict] = None,
                       viX: Optional[float] = None) -> RiskScore:
    weights = {
        'lppl_bubble': 0.25,       # LPPL泡沫信号
        'volatility': 0.15,         # 波动率结构
        'credit': 0.15,             # 信用压力 (提升权重: 现在有实时数据)
        'liquidity': 0.15,          # 流动性 (提升权重: 现在有实时数据)
        'correlation': 0.10,        # 跨资产相关性
        'flow': 0.10,               # 资金流向
        'sentiment': 0.10,          # 宏观情绪 (降低: LPPL已覆盖大部分)
    }

    # 读取准确率反馈: 从black_swan_status.json获取accuracy_rate
    # 用于校准LPPL泡沫强度: 低准确率 → LPPL因子打折
    accuracy_discount = 1.0
    try:
        bs_path = Path.home() / '.openclaw/workspace/dsl-quant-trading-hybrid/data/black_swan_status.json'
        if bs_path.exists():
            with open(bs_path) as f:
                bs_data = json.load(f)
            acc = bs_data.get('accuracy_rate', 0.5)
            # accuracy_rate 是 auto_verify 写入的0-100的数值
            if isinstance(acc, (int, float)) and acc > 0:
                # 归一化到0.5-1.0区间: acc=50%→discount=0.75, acc=100%→discount=1.0
                acc_val = acc / 100.0
                accuracy_discount = 0.5 + 0.5 * min(1.0, max(0.0, acc_val))
    except Exception:
        pass

    factors = {}
    details = {}

    if lppl_results:
        lppl_score, lppl_detail = compute_lppl_factor(lppl_results)
    else:
        lppl_score, lppl_detail = 0.5, "LPPL数据未传入"
    discounted = lppl_score * accuracy_discount
    discount_note = f" (accuracy折扣{accuracy_discount:.0%})" if accuracy_discount < 1.0 else ""
    factors['lppl_bubble'] = discounted
    details['lppl_bubble'] = lppl_detail + discount_note

    vol_score, vol_detail = compute_vol_factor(viX)
    factors['volatility'] = vol_score
    details['volatility'] = vol_detail

    credit_score, credit_detail = compute_credit_factor()
    factors['credit'] = credit_score
    details['credit'] = credit_detail

    liq_score, liq_detail = compute_liquidity_factor()
    factors['liquidity'] = liq_score
    details['liquidity'] = liq_detail

    corr_score, corr_detail = compute_correlation_factor()
    factors['correlation'] = corr_score
    details['correlation'] = corr_detail

    flow_score, flow_detail = compute_flow_factor()
    factors['flow'] = flow_score
    details['flow'] = flow_detail

    sent_score, sent_detail = compute_sentiment_factor()
    factors['sentiment'] = sent_score
    details['sentiment'] = sent_detail

    overall = sum(factors[k] * weights[k] for k in weights)

    if overall >= 0.75:
        level = "CRITICAL"
    elif overall >= 0.55:
        level = "HIGH"
    elif overall >= 0.35:
        level = "ELEVATED"
    else:
        level = "LOW"

    recommendations = []
    if factors['lppl_bubble'] > 0.6:
        recommendations.append("LPPL检测到泡沫成熟期信号，建议立即减仓至50%以下")
    if factors['volatility'] > 0.6:
        recommendations.append("波动率处于高位，建议增加对冲仓位")
    if factors['credit'] > 0.6:
        recommendations.append(f"信用利差收紧(Shibor 3M上升)，回避高杠杆标的")
    if factors['liquidity'] > 0.6:
        recommendations.append("市场流动性偏紧，建议降低杠杆")
    if factors['flow'] > 0.6:
        recommendations.append("北向资金持续流出，市场承压")
    if level == "CRITICAL":
        recommendations.append("综合风险CRITICAL级别，建议全面降低仓位+增加VIX对冲")
    elif level == "HIGH":
        recommendations.append("HIGH风险，建议仓位≤40%+设置严格止损")

    return RiskScore(
        timestamp=datetime.now().isoformat(),
        overall_score=round(overall * 100, 1),
        risk_level=level,
        factors=factors,
        weights=weights,
        details=details,
        recommendations=recommendations
    )


if __name__ == "__main__":
    print("=" * 55)
    print("  Multi-Factor Risk Score Engine v1.1")
    print("=" * 55)

    mock_lppl = {
        '上证指数': {'bubble_strength': 0.0, 'crash_probability': 0.0, 'critical_date': 'N/A'},
        '创业板指': {'bubble_strength': 0.0, 'crash_probability': 0.0, 'critical_date': 'N/A'},
        '科创50': {'bubble_strength': 0.0, 'crash_probability': 0.0, 'critical_date': 'N/A'},
        'S&P500': {'bubble_strength': 0.0, 'crash_probability': 0.0, 'critical_date': 'N/A'},
    }

    score = compute_risk_score(mock_lppl, viX=16.5)

    print(f"\n  综合风险分数: {score.overall_score:.1f}/100")
    print(f"  风险等级: {score.risk_level}")
    print(f"\n  各因子分数:")
    for name, val in score.factors.items():
        bar = '#' * int(val * 20)
        print(f"    {name:14s} [{bar:20s}] {val:.2f}  ({score.details[name]})")
    print(f"\n  建议:")
    for rec in score.recommendations:
        print(f"    {rec}")
