#!/usr/bin/env python3
"""L0 单元测试: scripts/feature_drift_monitor.py (v4.7.3 P1)

覆盖:
  - psi_single: 同分布≈0 / 水平平移显著 / 常数特征=0 / 小样本nan
  - rank_disp_all: 截面结构重排 → 大位移; 整体水平平移 → ≈0位移 (双通道核心语义)
  - judge_results: 噪声本底校准判级 + 良性量纲膨胀标记
"""
import os, sys
import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

import feature_drift_monitor as fdm


def _make_panel(n_symbols=12, n_days=30, feat_vals=None):
    """构造 面板: symbol × trade_date × features。feat_vals: {feat: (base_fn, recent_fn)}"""
    rows = []
    for s in range(n_symbols):
        for d in range(n_days):
            row = {"symbol": f"s{s:02d}", "trade_date": pd.Timestamp("2026-01-01") + pd.Timedelta(days=d)}
            for f, (fn_b, fn_r) in (feat_vals or {}).items():
                row[f] = fn_b(s, d) if d < n_days // 2 else fn_r(s, d)
            rows.append(row)
    return pd.DataFrame(rows)


def test_psi_identical_zero():
    a = np.random.RandomState(42).normal(0, 1, 1000)
    assert fdm.psi_single(a, a.copy()) < 1e-6


def test_psi_level_shift_large():
    a = np.random.RandomState(42).normal(0, 1, 1000)
    b = np.random.RandomState(43).normal(3, 1, 1000)  # 整体平移3σ
    assert fdm.psi_single(a, b) > 0.5


def test_psi_constant_zero():
    a = np.ones(500)
    assert fdm.psi_single(a, a) == 0.0


def test_psi_small_nan():
    a = np.random.RandomState(0).normal(0, 1, 15)
    assert np.isnan(fdm.psi_single(a, a))


def test_rank_disp_level_shift_near_zero():
    """整体水平平移 (牛市量能膨胀) → rank_disp ≈ 0 (双通道核心)。"""
    panel = _make_panel(n_symbols=12, n_days=40, feat_vals={
        "f1": (lambda s, d: float(s), lambda s, d: float(s) + 100.0),  # 平移
    })
    base = panel[panel["trade_date"] < pd.Timestamp("2026-01-21")]
    recent = panel[panel["trade_date"] >= pd.Timestamp("2026-01-21")]
    disp = fdm.rank_disp_all(base, recent, ["f1"])
    assert abs(disp["f1"]) < 0.02


def test_rank_disp_structure_shift_large():
    """截面重排 (风格切换: 前半段s小的f1高, 后半段s大的f1高) → 大位移。
    完全反转的位移均值理论上限=0.5, 故断言 > 0.4。"""
    panel = _make_panel(n_symbols=12, n_days=40, feat_vals={
        "f1": (lambda s, d: 12.0 - float(s), lambda s, d: float(s)),  # 反转
    })
    base = panel[panel["trade_date"] < pd.Timestamp("2026-01-21")]
    recent = panel[panel["trade_date"] >= pd.Timestamp("2026-01-21")]
    disp = fdm.rank_disp_all(base, recent, ["f1"])
    assert disp["f1"] > 0.4


def test_judge_results_noise_floor_calibrated():
    """本底0.15 → medium阈值=max(0.12,1.8*0.15)=0.27, severe=max(0.20,2.5*0.15)=0.375"""
    disp = {"f1": 0.30, "f2": 0.40, "f3": 0.10}
    floor = {"f1": 0.15, "f2": 0.15, "f3": 0.15}
    rng = np.random.RandomState(0)
    base = pd.DataFrame({"f1": rng.normal(0, 1, 300), "f2": rng.normal(0, 1, 300),
                         "f3": rng.normal(0, 1, 300)})
    recent = base + 0.01
    results = fdm.judge_results(disp, floor, ["f1", "f2", "f3"], base, recent)
    levels = {r["feature"]: r["level"] for r in results}
    assert levels["f1"] == "medium"   # 0.30 >= 0.27
    assert levels["f2"] == "severe"   # 0.40 >= 0.375
    assert levels["f3"] == "stable"   # 0.10 < 0.27


def test_judge_results_fallback_fixed():
    """本底不可估计 → 退化固定阈值 0.10/0.25"""
    disp = {"f1": 0.30, "f2": 0.05}
    floor = {}
    rng = np.random.RandomState(1)
    base = pd.DataFrame({"f1": rng.normal(0, 1, 300), "f2": rng.normal(0, 1, 300)})
    recent = base + 0.01
    results = fdm.judge_results(disp, floor, ["f1", "f2"], base, recent)
    levels = {r["feature"]: r["level"] for r in results}
    assert levels["f1"] == "severe"
    assert levels["f2"] == "stable"


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
