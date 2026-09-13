#!/usr/bin/env python3
"""v4.7.5 P0: 三层状态契约测试 (historical_summary / current_summary / risk_gate)

锁定语义 (Task 1 TDD):
  - 当前模型族无成熟行 → insufficient_data (而非 degraded)
  - 旧模型族负 IC 不串池标记当前族 degraded
  - stale 优先于负向质量判定
  - coverage 异常 → data_issue 最优先

纯函数 (Task 2-4):
  - normalize_model_family / mature_cutoff / trading_session_lag
  - build_status_payload / build_legacy_conservative_gate / resolve_rank_ic_new_position_cap
  - hac_mean_test / shadow_quality_status (影子, 不驱动 risk_gate)
"""
import os, sys, math

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import numpy as np

import rank_ic_monitor as rim


THRESHOLDS = dict(rim.DEFAULT_THRESHOLDS)


def _row(day, ic, version="v4.7.4", coverage=1.0):
    return {
        "date": day,
        "model_version": version,
        "model_family": rim.normalize_model_family(version),
        "rank_ic": ic,
        "coverage": coverage,
        "n": 24,
    }


# ── Task 1: 状态语义 ────────────────────────────────────────────────────────

def test_current_model_without_mature_rows_is_insufficient_not_degraded():
    result = rim.build_status_payload(
        series=[_row("2026-08-18", -0.08, "v4.6.9i")],
        daily_records=[{"date": "2026-09-07", "version": "v4.7.4"}],
        current_version="v4.7.4",
        mature_cutoff_date="2026-08-31",
        trading_days=[],
        thresholds=THRESHOLDS,
    )
    assert result["current_summary"]["evaluation_status"] == "insufficient_data"
    assert result["current_summary"]["actionable"] is False
    assert result["historical_summary"]["recent_mean"] == -0.08


def test_old_negative_ic_does_not_label_current_family_degraded():
    old = [_row(f"2026-08-{d:02d}", -0.20, "v4.6.9i") for d in range(1, 11)]
    current = [_row(f"2026-09-{d:02d}", 0.10, "v4.7.4") for d in range(1, 11)]
    result = rim.build_status_payload(old + current, [], "v4.7.4", "2026-09-10",
                                      trading_days=[], thresholds=THRESHOLDS)
    assert result["current_summary"]["recent_mean"] == 0.10
    assert result["current_summary"]["model_family"] == "v4.7"
    assert result["current_summary"]["evaluation_status"] == "healthy"


def test_stale_precedes_negative_quality_status():
    result = rim.classify_current_status(
        recent_mean=-0.20, n_days=20, lag_trading_days=5, coverage=1.0,
        thresholds=THRESHOLDS,
    )
    assert result["evaluation_status"] == "stale"
    assert result["actionable"] is False


def test_insufficient_below_min_status_days():
    result = rim.classify_current_status(
        recent_mean=-0.20, n_days=5, lag_trading_days=0, coverage=1.0,
        thresholds=THRESHOLDS,
    )
    assert result["evaluation_status"] == "insufficient_data"
    assert result["actionable"] is False


def test_data_issue_precedes_everything():
    result = rim.classify_current_status(
        recent_mean=0.10, n_days=20, lag_trading_days=0, coverage=0.4,
        thresholds=THRESHOLDS,
    )
    assert result["evaluation_status"] == "data_issue"
    assert result["actionable"] is False


# ── Task 2: 模型族 / 成熟度纯函数 ──────────────────────────────────────────

def test_normalize_model_family_groups_patch_versions():
    assert rim.normalize_model_family("v4.7.4") == "v4.7"
    assert rim.normalize_model_family("v4.7.3.1") == "v4.7"
    assert rim.normalize_model_family("v4.6.9i") == "v4.6"
    assert rim.normalize_model_family("") == "unknown"
    assert rim.normalize_model_family(None) == "unknown"


def test_mature_cutoff_is_deterministic_no_wall_clock():
    days = ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14",
            "2026-08-17", "2026-08-18"]
    # horizon=5 → 倒数第6个交易日 (08-11) 及之前均已兑现
    assert rim.mature_cutoff(days, horizon=5, as_of_date="2026-08-18") == "2026-08-11"
    assert rim.mature_cutoff(days, horizon=5, as_of_date="2026-08-10") is None


def test_trading_session_lag_counts_sessions_not_days():
    days = ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]
    assert rim.trading_session_lag("2026-08-18", "2026-08-21", days) == 3
    assert rim.trading_session_lag("2026-08-21", "2026-08-21", days) == 0


# ── Task 3: gate 拆分 ──────────────────────────────────────────────────────

def test_legacy_conservative_gate_keeps_one_new_position_when_degraded():
    historical = {"quality_status": "degraded"}
    current = {"evaluation_status": "insufficient_data"}
    gate = rim.build_legacy_conservative_gate(historical, current)
    assert gate["policy"] == "legacy_conservative"
    assert gate["max_new_positions"] == 1
    assert gate["basis"] == "historical_summary"


def test_legacy_conservative_gate_critical_is_zero():
    historical = {"quality_status": "critical"}
    current = {"evaluation_status": "healthy"}
    gate = rim.build_legacy_conservative_gate(historical, current)
    assert gate["max_new_positions"] == 0


def test_resolve_rank_ic_new_position_cap():
    gate = {"policy": "legacy_conservative", "max_new_positions": 1}
    assert rim.resolve_rank_ic_new_position_cap(gate, available=5) == 1
    assert rim.resolve_rank_ic_new_position_cap({"max_new_positions": None}, 5) == 5
    assert rim.resolve_rank_ic_new_position_cap({"max_new_positions": 0}, 5) == 0


# ── Task 4: HAC 显著性影子 ─────────────────────────────────────────────────

def test_hac_returns_none_below_min_n():
    assert rim.hac_mean_test([0.1, 0.2, 0.3]) is None


def test_hac_persistent_negative_ci_high_below_zero():
    vals = [-0.05, -0.06, -0.04, -0.07, -0.05, -0.06, -0.04, -0.07, -0.05, -0.06,
            -0.04, -0.07, -0.05, -0.06, -0.04]
    hac = rim.hac_mean_test(vals)
    assert hac is not None
    assert hac["mean"] < 0
    assert hac["ci95_high"] < 0
    assert hac["p_value_negative"] < 0.05
    assert rim.shadow_quality_status(hac, THRESHOLDS) == "degraded"


def test_shadow_watch_when_mean_negative_p_ge_005():
    hac = {"mean": -0.01, "p_value_negative": 0.20, "ci95_high": 0.03}
    assert rim.shadow_quality_status(hac, THRESHOLDS) == "watch"


def test_shadow_degraded_when_mean_negative_p_lt_005():
    hac = {"mean": -0.02, "p_value_negative": 0.01, "ci95_high": -0.005}
    assert rim.shadow_quality_status(hac, THRESHOLDS) == "degraded"


def test_shadow_critical_when_below_critical_mean():
    hac = {"mean": -0.15, "p_value_negative": 0.001, "ci95_high": -0.12}
    assert rim.shadow_quality_status(hac, THRESHOLDS) == "critical"


# ── P1 修复回归 ────────────────────────────────────────────────────────────

def test_unverifiable_maturity_is_not_healthy():
    """P1: 成熟截止日不可计算(滞后为 None)时不得判定 healthy/actionable。"""
    result = rim.classify_current_status(
        recent_mean=0.10, n_days=20, lag_trading_days=None, coverage=1.0,
        thresholds=THRESHOLDS,
    )
    assert result["evaluation_status"] == "insufficient_data"
    assert result["actionable"] is False


def test_immature_rows_excluded_from_current_family():
    """P1: 未成熟截面(兑现窗口未过)不得计入当前族成熟日, 其 coverage=0 也不得污染覆盖率。"""
    series = [
        {"date": "2026-09-01", "model_family": "v4.7", "rank_ic": 0.10,
         "coverage": 1.0, "n": 24, "mature": True},
        {"date": "2026-09-11", "model_family": "v4.7", "rank_ic": None,
         "coverage": 0.0, "n": 0, "mature": False},
    ]
    result = rim.build_status_payload(series, [], "v4.7.4", "2026-09-01",
                                      trading_days=[], thresholds=THRESHOLDS)
    assert result["current_summary"]["n_mature_days"] == 1
    assert result["current_summary"]["evaluation_status"] == "insufficient_data"


def test_duplicate_dates_do_not_inflate_mature_days():
    """P1: 成熟日按唯一交易日期计数, 重复日期不得虚增。"""
    row = {"model_family": "v4.7", "rank_ic": 0.10, "coverage": 1.0, "n": 24}
    series = [dict(row, date="2026-09-01"), dict(row, date="2026-09-01"),
              dict(row, date="2026-09-02")]
    result = rim.build_status_payload(series, [], "v4.7.4", "2026-09-02",
                                      trading_days=[], thresholds=THRESHOLDS)
    assert result["current_summary"]["n_mature_days"] == 2


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
