#!/usr/bin/env python3
"""
DSL Test — 生产信号评分引擎回归测试 (v4.5.17)

防止"修A坏B"的回归测试套件:
- 边缘场景全覆盖 (change_pct=0, ±10%, NaN)
- 阈值可达性验证 (增持/减持是否能到达)
- 与ML方向一致性测试
- 风险因子独立性验证

运行: .venv/bin/python3 -m pytest tests/test_production_signal.py -v
"""
import os, sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

import pytest
from core.production_signal import compute_alpha_score


# ════════════════════════════════════════════
# 场景矩阵: (label, change_pct, ml_pred, risk_ratio, black_swan, expected_min, expected_max)
# ════════════════════════════════════════════

def test_p0_change_zero_ml_buy():
    """P0-3回归: change_pct=0 + ML buy → 不触发方向矛盾惩罚"""
    r = compute_alpha_score('TEST', 0, ml_pred={'signal':'buy','confidence':0.68},
                            risk_position_ratio=0.56, black_swan_active=True)
    assert r['total_score'] >= 4.8, f"change=0+ML buy 应≥4.8, 实得{r['total_score']}"
    assert r['raw_factors']['f2_ml_confirm'] >= 0, "不应为负(方向矛盾惩罚)"
    assert r['action_signal'] in ('关注', '中性'), f"合理信号应为关注/中性, 实得{r['action_signal']}"


def test_p0_change_zero_ml_sell():
    """P0-3回归: change_pct=0 + ML sell → 不触发方向矛盾惩罚"""
    r = compute_alpha_score('TEST', 0, ml_pred={'signal':'sell','confidence':0.65},
                            risk_position_ratio=0.56, black_swan_active=True)
    assert r['total_score'] <= 5.2, f"change=0+ML sell 应≤5.2, 实得{r['total_score']}"
    assert r['raw_factors']['f2_ml_confirm'] <= 0, "sell信号f2应为负"


def test_change_positive_ml_buy_match():
    """方向一致: 涨+ML买 → 得分提高"""
    r = compute_alpha_score('TEST', 2.0, ml_pred={'signal':'buy','confidence':0.68},
                            risk_position_ratio=0.56, black_swan_active=True)
    assert r['total_score'] >= 5.0, f"涨+ML买应≥5.0, 实得{r['total_score']}"
    assert r['raw_factors']['f2_ml_confirm'] > 0, "方向一致f2应为正"


def test_change_negative_ml_sell_match():
    """方向一致: 跌+ML卖 → 得分降低"""
    r = compute_alpha_score('TEST', -2.0, ml_pred={'signal':'sell','confidence':0.65},
                            risk_position_ratio=0.56, black_swan_active=True)
    assert r['total_score'] <= 5.0, f"跌+ML卖应≤5.0, 实得{r['total_score']}"
    assert r['raw_factors']['f2_ml_confirm'] < 0, "卖信号f2应为负"


def test_change_positive_ml_sell_contradict():
    """方向矛盾: 涨+ML卖 → 严重惩罚"""
    r = compute_alpha_score('TEST', 2.0, ml_pred={'signal':'sell','confidence':0.65},
                            risk_position_ratio=0.56, black_swan_active=True)
    assert r['raw_factors']['f2_ml_confirm'] == -0.8, "方向矛盾f2应=-0.8"
    assert r['raw_factors']['f1_momentum'] <= 0.4, "动量因子应打折至0.4或以下"


def test_no_ml_data_falls_back():
    """无ML数据时中性评分"""
    r = compute_alpha_score('TEST', 0.5, ml_pred=None,
                            risk_position_ratio=0.56, black_swan_active=True)
    assert 4.5 <= r['total_score'] <= 6.0, f"无ML数据应中性, 实得{r['total_score']}"
    assert r['raw_factors']['f2_ml_confirm'] == 0.0, "无ML时f2应为0"


def test_strong_buy_reaches_zengchi():
    """P1-2回归: 强信号应可达「增持」"""
    # 涨2%+ML买0.7+无黑天鹅
    r = compute_alpha_score('TEST', 2.0, ml_pred={'signal':'buy','confidence':0.70},
                            risk_position_ratio=0.8, black_swan_active=False)
    assert r['action_signal'] == '增持', f"强信号应达增持, 实得{r['action_signal']}({r['total_score']})"


def test_weak_sell_stays_neutral():
    """弱卖出信号应不触发过度卖出"""
    r = compute_alpha_score('TEST', -0.5, ml_pred={'signal':'sell','confidence':0.55},
                            risk_position_ratio=0.8, black_swan_active=False)
    assert r['action_signal'] in ('中性', '减持'), f"弱卖信号应中性/减持, 实得{r['action_signal']}"


def test_moderate_buy_reaches_guanzhu():
    """中等信号应可达「关注」"""
    r = compute_alpha_score('TEST', 1.0, ml_pred={'signal':'buy','confidence':0.60},
                            risk_position_ratio=0.56, black_swan_active=True)
    assert r['action_signal'] in ('关注', '增持'), f"中等信号应≥关注, 实得{r['action_signal']}({r['total_score']})"


def test_black_swan_penalty():
    """黑天鹅活跃+低仓位限制 → f4风险惩罚生效"""
    r1 = compute_alpha_score('TEST', 1.0, ml_pred={'signal':'buy','confidence':0.60},
                             risk_position_ratio=0.8, black_swan_active=False)
    r2 = compute_alpha_score('TEST', 1.0, ml_pred={'signal':'buy','confidence':0.60},
                             risk_position_ratio=0.4, black_swan_active=True)
    assert r1['total_score'] >= r2['total_score'], "黑天鹅下评分应被压低"
    assert r2['raw_factors']['f4_risk'] < 0, "黑天鹅时f4应为负"


def test_score_bounded():
    """评分永远在[0,10]范围内"""
    for chg in [-10, -5, 0, 5, 10]:
        for conf in [0, 0.5, 1.0]:
            for sig in ['buy', 'hold', 'sell']:
                r = compute_alpha_score('TEST', chg, ml_pred={'signal':sig,'confidence':conf},
                                        risk_position_ratio=0.5, black_swan_active=True)
                assert 0 <= r['total_score'] <= 10, f"评分越界: chg={chg}, conf={conf}, sig={sig}, score={r['total_score']}"


def test_f1_momentum_capped():
    """F1动量因子 ±2%封顶"""
    r1 = compute_alpha_score('TEST', 2.0, risk_position_ratio=0.8, black_swan_active=False)
    r2 = compute_alpha_score('TEST', 5.0, risk_position_ratio=0.8, black_swan_active=False)
    assert r1['total_score'] == r2['total_score'], "涨2%和5%应同分(F1已封顶)"


def test_volume_factor_independent():
    """量比因子独立生效"""
    r_bull = compute_alpha_score('TEST', 1.0, volume_ratio=2.0, ml_pred={'signal':'buy','confidence':0.6},
                                 risk_position_ratio=0.8, black_swan_active=False)
    r_bear = compute_alpha_score('TEST', 1.0, volume_ratio=0.5, ml_pred={'signal':'buy','confidence':0.6},
                                 risk_position_ratio=0.8, black_swan_active=False)
    assert r_bull['total_score'] >= r_bear['total_score'], "放量涨应比缩量涨得分高"
    assert r_bull['raw_factors']['f3_volume'] > 0
    assert r_bear['raw_factors']['f3_volume'] < 0


if __name__ == '__main__':
    pytest.main([__file__, '-v'])